# -*- coding: utf-8 -*-
"""看板载荷组装 —— 「参数校验 → 取数 → 调领域层 → 组装 JSON」的最后两步。

**本文件不允许出现任何利润公式。** 实际利润、预估利润、完成率一律由
`core/domain/profit.py` 算完再取用；这里只做 Decimal → 字符串的下发格式化。

为什么不复用仓储层的 `amounts_for_period` / `daily_amounts`？
看板的 `totals`、`trend`、`orders` 三者必须**出自同一批数**，否则用户会看到
「明细分加起来 ≠ 合计」而无法解释。所以三者都从 `orders_for_period` 派生；
那两个廉价聚合方法保留给对账/告警，并有一条测试证明它们与这里一致。
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from core.domain.profit import (completion_rate, evaluate_actual,
                                evaluate_estimated, quantize_cny, quantize_rate,
                                sum_amounts)
from core.repository.base import ProfitDataSource
from core.repository.sqlite_source import CN_TZ

from . import config

#: 利润构成的四项。顺序即前端展示顺序，照抄前端约定。
COMPOSITION_KEYS = (
    ('direct_net', '直接净额'),
    ('purchase', '采购成本'),
    ('platform', '平台费用'),
    ('logistics', '物流费用'),
)


def money(value: Optional[Decimal]) -> Optional[str]:
    """金额一律以字符串下发，且**固定 2 位小数**，避免 JS 浮点误差。

    `format(Decimal('220.5'), 'f')` 会得到 `'220.5'` —— 位数不一致会让前端
    的对齐与「字符串相等」判断都出问题，所以统一走领域层的 quantize_cny 收敛到分。
    舍入方式（ROUND_HALF_UP）只有 core/domain/profit.py 那一个定义，这里不自己 round。

    `None` 原样下发为 null —— **不写 '0.00'**。缺失和零是两件事：
    写 0 会让前端把「这单采购成本没录」显示成「采购成本为零」。
    """
    return None if value is None else format(quantize_cny(value), 'f')


def rate(value: Optional[Decimal]) -> Optional[str]:
    """汇率按 4 位小数下发（与 PRD §7.5 的 RATE_QUANTIZE 一致）。

    刻意与金额分开：汇率用 2 位小数会把 10.0682 压成 10.07，
    前端拿这个值反算金额就会对不上。
    """
    return None if value is None else format(quantize_rate(value), 'f')


def format_cutoff(moment: Optional[datetime]) -> Optional[str]:
    """数据截止时间统一转成**北京时间带 +08:00 偏移**的 ISO 串。

    库里存的是 UTC（`2026-09-10T17:16:21+00:00`），直接下发会让用户看到
    一个比自己晚 8 小时的「截止时间」。这里只做时区换算，不改时刻。
    """
    if moment is None:
        return None
    if moment.utcoffset() is None:
        # 无时区信息的时间按国内时间理解（与 SqliteSource._parse_dt 一致）
        return moment.replace(tzinfo=CN_TZ).isoformat()
    return moment.astimezone(CN_TZ).isoformat()


def _period_end(cutoff: Optional[datetime], today: Optional[date]) -> str:
    """窗口右端。

    优先用**库里的数据截止日**而不是系统当天：店铺库是定期同步的，
    如果按当天取窗口，最后几天永远为空，看板会「看着正常但没有数」。
    库里没有任何时间戳时才退回系统当天。
    """
    if cutoff is not None:
        return cutoff.date().isoformat()
    return (today or date.today()).isoformat()


def build_dashboard(source: ProfitDataSource, store_alias: str, days: int,
                    today: Optional[date] = None,
                    order_limit: Optional[int] = None) -> dict:
    """组装一个店铺看板的完整响应体。"""
    cutoff = source.data_cutoff(store_alias)
    end_date = _period_end(cutoff.cutoff, today)
    start_date = (date.fromisoformat(end_date)
                  - timedelta(days=days - 1)).isoformat()

    limit = config.MAX_ORDERS_IN_RESPONSE if order_limit is None else order_limit
    rows = source.orders_for_period(store_alias, start_date, end_date)
    # 汇总一律用整个窗口，orders 数组可以截断；截断只影响明细条数，不影响合计
    detail_rows = rows if limit is None or limit < 0 else rows[:limit]

    # ── 逐单走领域层 ──
    actuals = []           # evaluate_actual 的结果，顺序与 rows 一致
    order_profits = []     # 给 orders 数组用
    estimated_values = []  # §7.2 的逐单结果，用于合计
    for row in rows:
        actual = evaluate_actual(row.snapshot, row.operations,
                                 row.expected_operation_ids)
        actuals.append(actual)
        # §7.2 的权威算法：有发货单就用 evaluate_estimated，
        # 没有发货单时退回事实表里存的 estimated_profit_cny（并在 README 声明）
        estimated = None
        if row.posting is not None:
            estimated = evaluate_estimated(row.posting).estimated_profit_cny
        elif row.reference_estimated_profit_cny is not None:
            estimated = row.reference_estimated_profit_cny
        estimated_values.append(estimated)
        order_profits.append(actual)

    total_count = len(rows)
    complete_count = sum(1 for a in actuals if a.complete)

    # ── 合计：实际利润只累加 complete 的订单，缺失一律跳过（不补 0）──
    actual_ok = [a.actual_profit_cny for a in actuals
                 if a.complete and a.actual_profit_cny is not None]
    estimated_ok = [v for v in estimated_values if v is not None]

    totals = {
        'actual_profit_cny': money(sum_amounts(actual_ok)) if actual_ok else None,
        'estimated_profit_cny': money(sum_amounts(estimated_ok)) if estimated_ok else None,
        'completion_rate': format(completion_rate(actuals), 'f'),
        'complete_order_count': complete_count,
        'total_order_count': total_count,
        'overdue_count': _overdue(source, store_alias),
    }

    # ── 趋势：只输出有数据的日期，不补零日期行 ──
    trend = _trend(rows, order_profits, estimated_values)

    # ── 利润构成：四项口径各自独立取合计，不加总成利润 ──
    composition = _composition(rows, actuals)

    # ── 明细 ──
    orders = []
    for row, actual in zip(detail_rows, order_profits):
        orders.append({
            'posting_number': row.posting_number,
            'settlement_date': row.settlement_date,
            'direct_net_rub': money(actual.direct_net_rub),
            'exchange_rate_rub_per_cny': rate(actual.exchange_rate),
            'purchase_cost_cny': money(actual.purchase_cost_cny),
            'actual_profit_cny': money(actual.actual_profit_cny),
            'complete': actual.complete,
            'unknown_reason': actual.unknown_reason,
        })

    return {
        'store_alias': store_alias,
        'data_cutoff': format_cutoff(cutoff.cutoff),
        'period': {'start': start_date, 'end': end_date, 'days': days},
        'totals': totals,
        'trend': trend,
        'composition': composition,
        'orders': orders,
        # 诊断信息刻意**不放进响应**（前端契约是定死的）。
        # unknown_reason 的分布通过日志暴露，见 app.py 的 access log。
    }


def _overdue(source: ProfitDataSource, store_alias: str) -> Optional[int]:
    """逾期单数。数据源不支持时返回 None —— 不返回 0 冒充「没有逾期」。"""
    try:
        info = source.overdue_count(store_alias)
    except NotImplementedError:
        return None
    if not info.available:
        return None
    return info.overdue_count


def _trend(rows: Sequence, actuals: Sequence, estimated_values: Sequence) -> list:
    """按天汇总。日期升序，**只含有数据的日期**。"""
    buckets = {}
    for row, actual, estimated in zip(rows, actuals, estimated_values):
        day = row.settlement_date or ''
        b = buckets.setdefault(day, {'a': [], 'e': []})
        if actual.complete and actual.actual_profit_cny is not None:
            b['a'].append(actual.actual_profit_cny)
        if estimated is not None:
            b['e'].append(estimated)
    out = []
    for day in sorted(buckets):
        b = buckets[day]
        out.append({
            'date': day,
            'actual_profit_cny': money(sum_amounts(b['a'])) if b['a'] else None,
            'estimated_profit_cny': money(sum_amounts(b['e'])) if b['e'] else None,
        })
    return out


def _composition(rows: Sequence, actuals: Sequence) -> list:
    """利润构成四项。

    四项是**并列口径**，不是相加关系：
        实际利润 = 直接净额 / 每单汇率 − 采购成本（§7.1）
        平台费用、物流费用是 §7.2 预估利润的扣减项。
    前端只做展示，不做勾稽 —— 所以这里不做「合计 = 利润」之类的校验。

    来源与口径：
      * 直接净额 = Σ（权威操作集金额 ÷ 该单汇率），走 Decimal 精算，**不乘平均汇率**
      * 采购成本 = Σ 各单采购成本（窗口内全部订单，含未完成单；未完成单的采购
        成本仍然真实存在，不能因为利润算不出就把成本也丢掉）
      * 平台费用 = Σ postings.estimated_platform_fee_cny。该列当前全库为空，
        于是合计为 None，这里按展示需要折成 0.00 并在 README 声明 ——
        它不参与任何利润口径，折 0 不会污染利润数字。
      * 物流费用 = Σ postings.logistics_cost_cny
    """
    direct = []
    purchase = []
    logistics = []
    platform = []
    for row, actual in zip(rows, actuals):
        if actual.direct_net_rub is not None and actual.exchange_rate is not None:
            # 直接净额折 CNY 必须用该单自己的汇率。用区间平均汇率去折算，
            # 在汇率波动时会把金额算歪，而且算歪的部分无法解释。
            direct.append(actual.direct_net_rub / actual.exchange_rate)
        purchase.append(actual.purchase_cost_cny)
        if row.posting is not None:
            logistics.append(row.posting.logistics_cost_cny)
            platform.append(row.posting.estimated_platform_fee_cny)
    values = {
        'direct_net': sum_amounts(direct) if direct else None,
        'purchase': sum_amounts(purchase) if purchase else None,
        'platform': sum_amounts(platform) if platform else None,
        'logistics': sum_amounts(logistics) if logistics else None,
    }
    if values['platform'] is None:
        values['platform'] = Decimal('0.00')
    return [{'key': key, 'label': label, 'value_cny': money(values[key])}
            for key, label in COMPOSITION_KEYS]
