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

from core.domain.profit import (Posting, SkuLineInput, completion_rate,
                                completion_rate_from_counts, evaluate_actual,
                                evaluate_estimated, evaluate_sku_profit,
                                quantize_cny, quantize_rate, sum_amounts)
from core.repository.base import DataCutoff, ProfitDataSource
from core.repository.sqlite_source import CN_TZ

from . import config

#: 利润构成的四项。顺序即前端展示顺序，照抄前端约定。
COMPOSITION_KEYS = (
    ('direct_net', '直接净额'),
    ('purchase', '采购成本'),
    ('platform', '平台费用'),
    ('logistics', '物流费用'),
)

#: 逐 SKU 下钻的排序键白名单。只允许排这几个，避免把任意列名拼进查询/排序。
SKU_SORT_KEYS = (
    'estimated_profit_cny', 'actual_profit_cny', 'purchase_cost_cny',
    'revenue_cny', 'quantity', 'order_count', 'offer_id',
)
SKU_DEFAULT_SORT = 'estimated_profit_cny'

#: 缺失原因的**中文标签**。枚举取值来自 core/domain/profit.py，
#: 这里只做展示映射 —— 判定逻辑一行都不在这里。
SKU_REASON_LABELS = {
    'no_settled_sale': '未结算销售',
    'no_direct_settlement': '无直接净额',
    'invalid_exchange_rate': '汇率越界',
    'missing_exchange_rate': '缺结算汇率',
    'missing_purchase_cost': '缺采购成本',
    'missing_revenue': '缺收入',
    'missing_operation': '流水取不全',
    'platform_subsidy_one_ruble_promotion': '平台补贴 1 卢布促销',
    'multi_item_posting': '多货号订单，该费用无按货号的分组键',
    'revenue_not_attributable': '按行收入与订单收入对不上，不摊分',
    'cost_not_attributable': '按行成本与订单成本对不上，不摊分',
    'missing_posting_item': '订单没有商品明细行，无法归属到货号',
}


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


def _period_end(cutoff: DataCutoff, today: Optional[date]) -> str:
    """窗口右端。

    三级优先，从「数据自己说的」到「机器当天」：

    1. `window_end` —— 最后一个已结算日。这是唯一能保证**窗口最后一天有数**
       的边界，因为窗口查询的过滤条件就是结算日落在区间内。
    2. `cutoff.date()` —— 数据源给不出结算日时，退回库里最后一个写入时间。
       夹具 / 测试替身走这条，与拆分之前逐字相同。
    3. 系统当天 —— 库里连时间戳都没有时的最后兜底。

    为什么不用系统当天当默认：店铺库是定期同步的，按当天取窗口会让最后
    几天永远为空，看板会「看着正常但没有数」。
    """
    if cutoff is not None and cutoff.window_end is not None:
        return cutoff.window_end.isoformat()
    if cutoff is not None and cutoff.cutoff is not None:
        return cutoff.cutoff.date().isoformat()
    return (today or date.today()).isoformat()


def build_dashboard(source: ProfitDataSource, store_alias: str, days: int,
                    today: Optional[date] = None,
                    order_limit: Optional[int] = None,
                    order_offset: int = 0,
                    cutoff: Optional[DataCutoff] = None,
                    window_end: Optional[date] = None) -> dict:
    """组装一个店铺看板的完整响应体。

    三个可选参数都是给「多店合计」与「服务端分页」用的，默认值让单店行为
    与加它们之前逐字相同：

    * `cutoff` —— 已经取过的 `DataCutoff`。多店合计时要先按各店的
      `window_end` 算出共同窗口，再逐个店组装；不传就现取（单店路径）。
    * `window_end` —— **强制窗口右端**。多店合计时必须让所有店用同一个区间，
      否则「合计」是几个不同期间相加（ADR-0008 §二）。单店不传，走
      `_period_end(cutoff)` 的既有逻辑。
    * `order_offset` —— orders 数组的起始下标（服务端分页）。
      注意合计/趋势/构成**永远**覆盖整个窗口，不受它影响；
      这一条在 README 与界面上都写明了，否则「明细加起来 ≠ 合计」会被当成 bug。
    """
    if cutoff is None:
        cutoff = source.data_cutoff(store_alias)
    end_date = (window_end.isoformat() if window_end is not None
                else _period_end(cutoff, today))
    start_date = (date.fromisoformat(end_date)
                  - timedelta(days=days - 1)).isoformat()

    limit = config.MAX_ORDERS_IN_RESPONSE if order_limit is None else order_limit
    rows = source.orders_for_period(store_alias, start_date, end_date)
    # 汇总一律用整个窗口，orders 数组可以分页；分页只影响明细条数，不影响合计
    offset = max(0, int(order_offset or 0))
    if limit is None or limit < 0:
        detail_rows = rows[offset:]
    else:
        detail_rows = rows[offset:offset + limit]

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


def build_multi_store_summary(entries: Sequence[tuple], days: int,
                              today: Optional[date] = None) -> dict:
    """多店合计（ADR-0008）。

    `entries` 是 `[(alias, display_name, source), ...]`，**数据源由调用方
    （路由层）打开并负责关闭** —— 这里不碰 Runtime，也不做授权判断。

    为什么合计必须在这里做、而不是前端把几个单店响应加起来：
      1. 前端加总就是第二套口径（本项目的金额一律是字符串，就是为了禁止浮点加法）；
      2. 完成率**不能**用各店完成率的平均 —— 那会按店等权，
         两家店单量差 10 倍时明显失真。必须 Σ完整单数 / Σ总单数。
      3. 跨店相加前必须先统一窗口，见下面的 `common_end`。

    窗口：`common_end = min(各店 window_end)`。若某店给不出 window_end
    （一单都没结算），就用其余店算，并在 warnings 里点名 —— 不假装它有数据。
    """
    per_store = []
    window_ends = {}
    for alias, display_name, source in entries:
        cutoff = source.data_cutoff(alias)
        window_ends[alias] = cutoff.window_end
        per_store.append({
            'alias': alias,
            'display_name': display_name,
            'source': source,
            'cutoff': cutoff,
        })

    knowable = [w for w in window_ends.values() if w is not None]
    common_end = min(knowable) if knowable else None

    totals_sum = {'actual_profit_cny': Decimal('0'),
                  'estimated_profit_cny': Decimal('0'),
                  'complete_order_count': 0, 'total_order_count': 0}
    has_actual = False
    has_estimated = False
    overdue_values = []
    overdue_missing = []
    trend_merged = {}
    composition_merged = {key: [] for key, _ in COMPOSITION_KEYS}
    breakdown = []

    for item in per_store:
        alias = item['alias']
        payload = build_dashboard(
            item['source'], alias, days, today=today,
            order_limit=0, cutoff=item['cutoff'], window_end=common_end)
        store_totals = payload['totals']

        # 金额：字符串 → Decimal 相加，全程不经过浮点
        if store_totals['actual_profit_cny'] is not None:
            has_actual = True
            totals_sum['actual_profit_cny'] += _dec(store_totals['actual_profit_cny'])
        if store_totals['estimated_profit_cny'] is not None:
            has_estimated = True
            totals_sum['estimated_profit_cny'] += _dec(store_totals['estimated_profit_cny'])
        totals_sum['complete_order_count'] += store_totals['complete_order_count']
        totals_sum['total_order_count'] += store_totals['total_order_count']

        overdue = store_totals['overdue_count']
        if overdue is None:
            overdue_missing.append(alias)
        else:
            overdue_values.append(int(overdue))

        for point in payload['trend']:
            bucket = trend_merged.setdefault(point['date'], {'a': [], 'e': []})
            if point['actual_profit_cny'] is not None:
                bucket['a'].append(_dec(point['actual_profit_cny']))
            if point['estimated_profit_cny'] is not None:
                bucket['e'].append(_dec(point['estimated_profit_cny']))

        for cell in payload['composition']:
            composition_merged[cell['key']].append(_dec(cell['value_cny']))

        breakdown.append({
            'alias': alias,
            'display_name': item['display_name'],
            'data_cutoff': payload['data_cutoff'],
            'window_end': (None if window_ends[alias] is None
                           else window_ends[alias].isoformat()),
            'period': payload['period'],
            #: 逐店合计是**在同一共同窗口下**算出来的，所以各店之和 == 顶层合计。
            #: 这条等式由 api/tests/test_multi_store.py 把守。
            'totals': store_totals,
        })

    end_date = common_end.isoformat() if common_end is not None else None
    if end_date is None and per_store:
        # 所有店都给不出已结算日：退回各店数据截止里最晚的那天（只是别下发空窗口）
        latest = [it['cutoff'].cutoff for it in per_store if it['cutoff'].cutoff]
        end_date = (max(latest).date().isoformat() if latest
                    else (today or date.today()).isoformat())
    start_date = (date.fromisoformat(end_date)
                  - timedelta(days=days - 1)).isoformat()

    cutoffs = [it['cutoff'].cutoff for it in per_store if it['cutoff'].cutoff]
    overdue_count = sum(overdue_values) if not overdue_missing else None

    totals = {
        'actual_profit_cny': money(totals_sum['actual_profit_cny']) if has_actual else None,
        'estimated_profit_cny': (money(totals_sum['estimated_profit_cny'])
                                 if has_estimated else None),
        # 完成率：Σ完整单数 / Σ总单数，走 core 里唯一定义的那一处除法
        'completion_rate': format(completion_rate_from_counts(
            totals_sum['complete_order_count'],
            totals_sum['total_order_count']), 'f'),
        'complete_order_count': totals_sum['complete_order_count'],
        'total_order_count': totals_sum['total_order_count'],
        'overdue_count': overdue_count,
    }

    trend = [{
        'date': day,
        'actual_profit_cny': (money(sum_amounts(trend_merged[day]['a']))
                              if trend_merged[day]['a'] else None),
        'estimated_profit_cny': (money(sum_amounts(trend_merged[day]['e']))
                                 if trend_merged[day]['e'] else None),
    } for day in sorted(trend_merged)]

    composition = [{
        'key': key, 'label': label,
        # 与单店一致：一个数都没有时下发 null（不写 0.00）
        'value_cny': (money(sum_amounts(composition_merged[key]))
                      if composition_merged[key] else None),
    } for key, label in COMPOSITION_KEYS]

    return {
        'store_aliases': [it['alias'] for it in per_store],
        'stores': breakdown,
        # 展示用的数据截止 = 各店最晚的一次写入；口径用的窗口右端 = 各店最早的那个
        'data_cutoff': format_cutoff(max(cutoffs) if cutoffs else None),
        'window_end': end_date,
        'period': {'start': start_date, 'end': end_date, 'days': days},
        'totals': totals,
        'trend': trend,
        'composition': composition,
        'warnings': _aggregate_warnings(breakdown, window_ends, overdue_missing,
                                        end_date),
    }


def _aggregate_warnings(breakdown: Sequence[dict], window_ends: dict,
                        overdue_missing: Sequence[str], end_date: str) -> list:
    """把「合计里少了什么」说清楚。合计最容易骗人的地方就是它不说话。"""
    out = []
    for item in breakdown:
        alias = item['alias']
        own = window_ends.get(alias)
        if own is None:
            out.append('%s 没有任何已锁定结算，它给不出窗口右端，也没有数据进合计。'
                       % alias)
            continue
        if own.isoformat() != end_date:
            out.append(
                '%s 的最后结算日是 %s，晚于共同窗口右端 %s，'
                '因此该店 %s ~ %s 的数据**不在本次合计里**。'
                '要看到它们，请单独打开该店看板。'
                % (alias, own.isoformat(), end_date,
                   (own + timedelta(days=1)).isoformat(), own.isoformat()))
    for alias in overdue_missing:
        out.append('%s 给不出逾期单数（数据源不支持或没有扫描数据），'
                   '合计里的逾期数因此为 null，而不是把它当成 0。' % alias)
    return out


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


# ══════════════════════════════════════════════════════════════════
# 逐 SKU 利润下钻（ADR-0006）
# ══════════════════════════════════════════════════════════════════
#
# 与 build_dashboard 的关系：
#   * **不改** build_dashboard 的响应形状（前端依赖它）；
#   * 窗口的算法**逐字复用**（data_cutoff + days），否则下钻明细的合计
#     会与指标卡对不上，而那种「差一点」最容易被当成 bug；
#   * 利润一律来自 core/domain/profit.py：逐 SKU 走 evaluate_sku_profit，
#     订单口径走 evaluate_actual / evaluate_estimated。这里只做 Decimal→字符串。
#
# 本文件仍然**没有任何利润公式**。

def _sku_line(row) -> SkuLineInput:
    """仓储层的取数行 → 领域层的输入。只做字段搬运，不做计算。"""
    return SkuLineInput(
        offer_id=row.offer_id,
        posting_number=row.posting_number,
        sku=row.sku,
        product_name=row.product_name,
        quantity=row.quantity,
        revenue_cny=row.revenue_cny,
        purchase_cost_cny=row.purchase_cost_cny,
        logistics_cost_cny=row.logistics_cost_cny,
        platform_fee_cny=row.platform_fee_cny,
        snapshot=row.snapshot,
        operations=row.operations,
        expected_operation_ids=row.expected_operation_ids,
        actual_attributable=row.actual_attributable,
        actual_unknown_reason=row.actual_unknown_reason,
        revenue_unknown_reason=row.revenue_unknown_reason,
        cost_unknown_reason=row.cost_unknown_reason,
        logistics_unknown_reason=row.logistics_unknown_reason,
        platform_fee_unknown_reason=row.platform_fee_unknown_reason,
    )


def _reason_label(reason) -> Optional[str]:
    if reason is None:
        return None
    return SKU_REASON_LABELS.get(reason, reason)


def _unattributed_summary(records, posting_count: int) -> dict:
    """把「归不出去的金额」按字段汇总，并留下原因分布。

    这个区块是 honest-by-construction 的落点：**逐 SKU 合计 + 未归属 = 订单口径合计**。
    少了它，归不出去的钱就只能被悄悄摊掉或被丢掉 —— 两者都是本项目禁止的。
    """
    fields = {}
    for name in ('revenue_cny', 'purchase_cost_cny', 'logistics_cny', 'platform_fee_cny'):
        values = [rec.amount for rec in records
                  if rec.field == name and rec.amount is not None]
        fields[name] = sum_amounts(values) if values else None

    grouped: dict = {}
    for rec in records:
        entry = grouped.setdefault((rec.reason, rec.field),
                                   {'reason': rec.reason, 'field': rec.field,
                                    'amounts': [], 'postings': set()})
        if rec.amount is not None:
            entry['amounts'].append(rec.amount)
        if rec.posting_number:
            entry['postings'].add(rec.posting_number)
    reasons = []
    for (reason, field), entry in sorted(grouped.items()):
        reasons.append({
            'reason': reason,
            'label': SKU_REASON_LABELS.get(reason, reason),
            'field': field,
            'posting_count': len(entry['postings']),
            'amount_cny': money(sum_amounts(entry['amounts']))
            if entry['amounts'] else None,
        })

    has_records = bool(records)
    estimate = evaluate_estimated(Posting(
        posting_number='<unattributed>',
        status=None,
        revenue_cny=fields['revenue_cny'],
        purchase_cost_cny=fields['purchase_cost_cny'],
        logistics_cost_cny=fields['logistics_cny'],
        estimated_platform_fee_cny=fields['platform_fee_cny'],
    ))
    return {
        'posting_count': posting_count,
        'revenue_cny': money(fields['revenue_cny']),
        'purchase_cost_cny': money(fields['purchase_cost_cny']),
        'logistics_cny': money(fields['logistics_cny']),
        'platform_fee_cny': money(fields['platform_fee_cny']),
        # §7.2 的权威实现直接复用：未归属的四项拼成一个合成订单喂进去，
        # 于是「缺失项按 0」这套约定与订单口径逐字一致，不必在这里再写一遍减法。
        'estimated_profit_cny': (money(estimate.estimated_profit_cny)
                                 if has_records else None),
        'reasons': reasons,
    }


def _order_level_totals(source: ProfitDataSource, store_alias: str,
                        start_date: str, end_date: str) -> dict:
    """**订单口径**的独立取数与计算 —— 逐 SKU 对账的右半边。

    刻意走 `orders_for_period` + 领域层，也就是看板自己那条路径：
    这样对账比的是「两条独立路径有没有漂移」，而不是「同一段代码等于它自己」。
    """
    rows = source.orders_for_period(store_alias, start_date, end_date)
    if not rows:
        return {'order_count': 0, 'revenue_cny': None, 'purchase_cost_cny': None,
                'logistics_cny': None, 'platform_fee_cny': None,
                'estimated_profit_cny': None, 'actual_profit_cny': None}
    actuals = [evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
               for r in rows]
    postings = [r.posting for r in rows if r.posting is not None]
    actual_ok = [a.actual_profit_cny for a in actuals if a.complete]
    estimated_ok = [evaluate_estimated(p).estimated_profit_cny for p in postings]
    return {
        'order_count': len(rows),
        'revenue_cny': money(_total(p.revenue_cny for p in postings)),
        'purchase_cost_cny': money(_total(p.purchase_cost_cny for p in postings)),
        'logistics_cny': money(_total(p.logistics_cost_cny for p in postings)),
        'platform_fee_cny': money(_total(p.estimated_platform_fee_cny for p in postings)),
        'estimated_profit_cny': money(sum_amounts(estimated_ok)) if postings else None,
        'actual_profit_cny': money(sum_amounts(actual_ok)) if actual_ok else None,
    }


def build_sku_detail(source: ProfitDataSource, store_alias: str, days: int,
                     limit: Optional[int] = None, offset: int = 0,
                     sort: str = SKU_DEFAULT_SORT, desc: bool = True,
                     query: Optional[str] = None,
                     today: Optional[date] = None) -> dict:
    """组装逐 SKU 利润下钻的响应体。

    排序 / 搜索 / 分页**只影响下发条数**，不影响任何合计数：
    合计一律在全窗口的完整集合上算，否则「翻页看到的和」与「指标卡」会差。
    """
    cutoff = source.data_cutoff(store_alias)
    end_date = _period_end(cutoff, today)
    start_date = (date.fromisoformat(end_date)
                  - timedelta(days=days - 1)).isoformat()

    detail = source.sku_detail_for_period(store_alias, start_date, end_date)
    profits = list(evaluate_sku_profit([_sku_line(r) for r in detail.rows]))

    # ── 合计（全窗口，不受搜索/分页影响）──
    rows = detail.rows
    sku_revenue = _total(r.revenue_cny for r in rows)
    sku_cost = _total(r.purchase_cost_cny for r in rows)
    sku_logistics = _total(r.logistics_cost_cny for r in rows)
    sku_fee = _total(r.platform_fee_cny for r in rows)
    sku_estimate = sum_amounts(p.estimated_profit_reconciled_cny for p in profits) \
        if rows else None
    sku_actual = (sum_amounts(p.actual_profit_reconciled_cny for p in profits)
                  if any(p.actual_reconciled_count for p in profits) else None)
    complete_est = [p.estimated_profit_cny for p in profits
                    if p.estimated_profit_cny is not None]
    complete_act = [p.actual_profit_cny for p in profits
                    if p.actual_profit_cny is not None]

    unattributed = _unattributed_summary(detail.unattributed.records,
                                         detail.unattributed.posting_count)
    un_actual_ok = [evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
                    for r in detail.actual_unattributed]
    un_actual = [a.actual_profit_cny for a in un_actual_ok if a.complete]

    # ── 搜索 / 排序 / 分页 ──
    needle = (query or '').strip().lower()
    if needle:
        matched = [p for p in profits if _sku_matches(p, needle)]
    else:
        matched = list(profits)
    matched.sort(key=lambda p: p.offer_id)
    missing = [p for p in matched if getattr(p, sort) is None]
    present = [p for p in matched if getattr(p, sort) is not None]
    present.sort(key=lambda p: getattr(p, sort), reverse=bool(desc))
    # 缺失值永远排在最后：排「利润最高」时不该把「算不出来」顶到第一位
    ordered = present + missing
    page = ordered[offset:offset + limit] if limit is not None else ordered[offset:]

    # ── 订单口径（独立路径）与对账 ──
    order_level = _order_level_totals(source, store_alias, start_date, end_date)
    sku_level = {
        'order_count': detail.order_count,
        'revenue_cny': money(sku_revenue),
        'purchase_cost_cny': money(sku_cost),
        'logistics_cny': money(sku_logistics),
        'platform_fee_cny': money(sku_fee),
        'estimated_profit_cny': money(sku_estimate),
        'actual_profit_cny': money(sku_actual),
    }
    matches = {}
    for key in ('revenue_cny', 'purchase_cost_cny', 'logistics_cny',
                'platform_fee_cny', 'estimated_profit_cny', 'actual_profit_cny'):
        un_side = unattributed.get(key)
        if key == 'actual_profit_cny':
            un_side = money(sum_amounts(un_actual)) if un_actual else None
        left = sum_amounts([_dec(sku_level[key]), _dec(un_side)])
        right = _dec(order_level[key])
        matches[key] = bool(left == right)
    matches['order_count'] = detail.order_count == order_level['order_count']

    return {
        'store_alias': store_alias,
        'data_cutoff': format_cutoff(cutoff.cutoff),
        'period': {'start': start_date, 'end': end_date, 'days': days},
        'query': {'sort': sort, 'desc': bool(desc), 'q': query or None,
                  'limit': limit, 'offset': offset,
                  'sort_keys': list(SKU_SORT_KEYS)},
        'totals': {
            'sku_count': len(profits),
            'matched_sku_count': len(ordered),
            'returned_count': len(page),
            'truncated': len(ordered) > offset + len(page),
            'order_count': detail.order_count,
            'missing_cost_sku_count': sum(1 for p in profits
                                          if p.purchase_cost_cny is None),
            'incomplete_sku_count': sum(1 for p in profits if not p.complete),
            'estimated_incomplete_sku_count': sum(1 for p in profits
                                                  if not p.estimated_complete),
            'actual_incomplete_sku_count': sum(1 for p in profits
                                               if not p.actual_complete),
            'quantity': sum(int(r.quantity or 0) for r in rows),
            'revenue_cny': money(sku_revenue),
            'purchase_cost_cny': money(sku_cost),
            'logistics_cny': money(sku_logistics),
            'platform_fee_cny': money(sku_fee),
            'estimated_profit_cny': money(sku_estimate),
            'actual_profit_cny': money(sku_actual),
            'estimated_profit_complete_only_cny': (
                money(sum_amounts(complete_est)) if complete_est else None),
            'actual_profit_complete_only_cny': (
                money(sum_amounts(complete_act)) if complete_act else None),
        },
        'unattributed': dict(unattributed,
                             actual_profit_cny=(money(sum_amounts(un_actual))
                                                if un_actual else None),
                             actual_posting_count=len(detail.actual_unattributed)),
        'reconciliation': {
            'sku_level': sku_level,
            'order_level': order_level,
            'matches': matches,
            'note': ('逐 SKU 合计 + 未归属 = 订单口径合计。'
                     '订单口径走看板同一条路径（orders_for_period + 领域层）。'),
        },
        'rows': [_sku_row(p) for p in page],
    }


def _dec(value) -> Decimal:
    """把下发的字符串金额还原成 Decimal；None/空串按 0（求和时才用）。"""
    if value is None or value == '':
        return Decimal('0')
    return Decimal(str(value))


def _total(values) -> Optional[Decimal]:
    """有值才求和；**一个值都没有时返回 None**。

    这条与 `money()` 的注释是同一件事：缺数据和零是两回事。
    逐 SKU 下钻里「平台佣金一个数都没有」必须下发 null，
    写成 `0.00` 会让人以为「这个窗口的平台佣金是零」。
    """
    present = [v for v in values if v is not None]
    return sum_amounts(present) if present else None


def _sku_matches(profit, needle: str) -> bool:
    for value in (profit.offer_id, profit.sku, profit.product_name):
        if value and needle in str(value).lower():
            return True
    return False


def _sku_row(profit) -> dict:
    """一个 SKU 的下发行。金额一律字符串（与现有接口一致）。"""
    return {
        'offer_id': profit.offer_id,
        'sku': profit.sku,
        'product_name': profit.product_name,
        'quantity': int(profit.quantity),
        'order_count': int(profit.order_count),
        'revenue_cny': money(profit.revenue_cny),
        'purchase_cost_cny': money(profit.purchase_cost_cny),
        'logistics_cny': money(profit.logistics_cny),
        'platform_fee_cny': money(profit.platform_fee_cny),
        'estimated_profit_cny': money(profit.estimated_profit_cny),
        'actual_profit_cny': money(profit.actual_profit_cny),
        'estimated_complete': bool(profit.estimated_complete),
        'actual_complete': bool(profit.actual_complete),
        'complete': bool(profit.complete),
        'unknown_reason': profit.unknown_reason,
        'unknown_reason_label': _reason_label(profit.unknown_reason),
        'missing_fields': list(profit.missing_fields),
        'incomplete_order_count': int(profit.incomplete_order_count),
    }
