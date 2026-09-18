# -*- coding: utf-8 -*-
"""夹具数据源：从冻结的黄金样本 JSON 读数据。

用途：CI / 无生产库的机器上也能跑完整回归。夹具本身入库（289 KB），
生产库不入库。这保证了「重构不改变结果」这件事可以被任何人复现。
"""
import io
import json
import os
from typing import Optional, Sequence

from ..domain.profit import (Operation, Posting, SettlementSnapshot,
                             evaluate_estimated, sum_amounts, to_decimal)
from .base import (DailyAmounts, DataCutoff, OverdueInfo, PeriodAmounts,
                   PeriodOrderRow)

DEFAULT_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tests', 'fixtures', 'golden_sample.json')


class FixtureSource:
    """读冻结夹具。只读，无副作用。"""

    def __init__(self, path: str = DEFAULT_FIXTURE):
        if not os.path.isfile(path):
            raise FileNotFoundError(
                '找不到黄金样本夹具: %s\n请先运行 core/tools/freeze_golden.py 生成。' % path)
        with io.open(path, encoding='utf-8') as fh:
            self._data = json.load(fh)
        self.path = path
        self._by_pn = {o['posting_number']: o for o in self._data['orders']}

    # ── 元信息 ──
    @property
    def meta(self) -> dict:
        return self._data['meta']

    # ── 输入 ──
    def list_posting_numbers(self) -> Sequence[str]:
        return list(self._by_pn.keys())

    def settlement_snapshot(self, posting_number: str) -> Optional[SettlementSnapshot]:
        o = self._by_pn.get(posting_number)
        if not o or not o.get('settlement_snapshot'):
            return None
        s = o['settlement_snapshot']
        return SettlementSnapshot(
            posting_number=posting_number,
            settlement_date=s.get('settlement_date'),
            state=s.get('state'),
            direct_net_rub=to_decimal(s.get('direct_net_rub')),
            settled_sales_rub=to_decimal(s.get('settled_sales_rub')),
            exchange_rate_rub_per_cny=to_decimal(s.get('exchange_rate_rub_per_cny')),
            purchase_cost_cny=to_decimal(s.get('purchase_cost_cny')),
            operation_ids=tuple(s.get('operation_ids') or ()),
            unknown_reason=s.get('unknown_reason'),
        )

    def operations(self, posting_number: str) -> Sequence[Operation]:
        """返回该订单冻结的全部操作（快照集合 ∪ 事实表集合）。"""
        return self.operations_for(posting_number, None)

    def operations_for(self, posting_number: str,
                       ids: Optional[Sequence[str]]) -> Sequence[Operation]:
        o = self._by_pn.get(posting_number)
        if not o:
            return ()
        rows = o.get('operations', [])
        if ids is not None:
            keep = set(ids)
            rows = [r for r in rows if r['operation_id'] in keep]
        return tuple(
            Operation(operation_id=r['operation_id'],
                      amount_rub=to_decimal(r.get('amount_rub')),
                      operation_category=r.get('operation_category'),
                      occurred_at=r.get('occurred_at'))
            for r in rows)

    def linked_operation_ids(self, posting_number: str) -> Sequence[str]:
        o = self._by_pn.get(posting_number)
        if not o:
            return ()
        return tuple((o.get('fact') or {}).get('linked_operation_ids') or ())

    def posting(self, posting_number: str) -> Optional[Posting]:
        o = self._by_pn.get(posting_number)
        if not o or not o.get('posting'):
            return None
        p = o['posting']
        return Posting(
            posting_number=posting_number,
            status=p.get('status'),
            revenue_cny=to_decimal(p.get('revenue_cny')),
            purchase_cost_cny=to_decimal(p.get('purchase_cost_cny')),
            logistics_cost_cny=to_decimal(p.get('logistics_cost_cny')),
            estimated_platform_fee_cny=to_decimal(p.get('estimated_platform_fee_cny')),
        )

    # ── 参照 ──
    def reference_actual(self, posting_number: str) -> dict:
        o = self._by_pn.get(posting_number)
        return (o or {}).get('fact') or {}

    def reference_estimated(self, posting_number: str) -> Optional[str]:
        o = self._by_pn.get(posting_number)
        if not o:
            return None
        return (o.get('posting') or {}).get('estimated_profit_cny')

    def reference_completion(self) -> dict:
        return self._data.get('completion_rate') or {}

    # ── 只读聚合（看板用）────────────────────────────────────────
    #
    # 夹具能实现的部分都在下面。夹具里**没有**直接净额所需的东西吗？有 ——
    # `fact.linked_operation_ids` 与 `operations[]` 都在，所以 §7.1 可以照常算。
    # 夹具里**没有**的是逾期扫描数据和逐单写入时间戳，那些明确抛 NotImplementedError。
    def data_cutoff(self, store_alias: str) -> DataCutoff:
        """夹具没有全库写入时间，只能退回最后一条锁定快照的 locked_at。"""
        stamps = []
        for o in self._data['orders']:
            s = o.get('settlement_snapshot') or {}
            dt = _parse_dt(s.get('locked_at'))
            if dt is not None:
                stamps.append(dt)
        if not stamps:
            return DataCutoff(None, 'none', {})
        best = max(stamps)
        return DataCutoff(best, 'settlement_snapshot.locked_at',
                          {'settlement_snapshot.locked_at': best.isoformat()})

    def orders_for_period(self, store_alias: str, start_date: str, end_date: str,
                          limit=None, offset: int = 0) -> Sequence[PeriodOrderRow]:
        rows = [o for o in self._data['orders']
                if _in_window(o, start_date, end_date)]
        rows.sort(key=lambda o: (o.get('settlement_snapshot') or {}).get('settlement_date') or '',
                  reverse=True)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:int(limit)]
        out = []
        for o in rows:
            pn = o['posting_number']
            out.append(PeriodOrderRow(
                posting_number=pn,
                settlement_date=(o.get('settlement_snapshot') or {}).get('settlement_date'),
                snapshot=self.settlement_snapshot(pn),
                posting=self.posting(pn),
                expected_operation_ids=self.linked_operation_ids(pn),
                operations=self.operations(pn),
                reference_actual_profit_cny=to_decimal(
                    (o.get('fact') or {}).get('actual_profit_cny')),
                reference_estimated_profit_cny=to_decimal(
                    (o.get('posting') or {}).get('estimated_profit_cny')),
            ))
        return tuple(out)

    def amounts_for_period(self, store_alias: str, start_date: str,
                           end_date: str) -> PeriodAmounts:
        """夹具版本：逐单算完再汇总（夹具只有 147 单，不需要廉价路径）。

        为了让 `amounts_for_period` 与 `daily_amounts` 自洽，
        这里的 actual_profit 取夹具冻结的 fact 值（与 §7.1 精算一致，
        已由 test_profit_golden 逐单核对）。
        """
        rows = self.orders_for_period(store_alias, start_date, end_date)
        actual = []
        estimated = []
        complete = 0
        for r in rows:
            if r.reference_actual_profit_cny is not None:
                actual.append(r.reference_actual_profit_cny)
                complete += 1
            if r.posting is not None:
                estimated.append(evaluate_estimated(r.posting).estimated_profit_cny)
        return PeriodAmounts(
            total_order_count=len(rows),
            complete_order_count=complete,
            actual_profit_cny=sum_amounts(actual) if actual else None,
            estimated_profit_cny=sum_amounts(estimated) if estimated else None,
            direct_net_cny=None,
            purchase_cost_cny=sum_amounts(
                [r.snapshot.purchase_cost_cny for r in rows if r.snapshot]) if rows else None,
            platform_fee_cny=sum_amounts(
                [r.posting.estimated_platform_fee_cny for r in rows if r.posting]) if rows else None,
            logistics_cost_cny=sum_amounts(
                [r.posting.logistics_cost_cny for r in rows if r.posting]) if rows else None,
        )

    def daily_amounts(self, store_alias: str, start_date: str,
                      end_date: str) -> Sequence[DailyAmounts]:
        bucket = {}
        for r in self.orders_for_period(store_alias, start_date, end_date):
            d = r.settlement_date or ''
            b = bucket.setdefault(d, {'n': 0, 'c': 0, 'a': [], 'e': []})
            b['n'] += 1
            if r.reference_actual_profit_cny is not None:
                b['c'] += 1
                b['a'].append(r.reference_actual_profit_cny)
            if r.posting is not None:
                b['e'].append(evaluate_estimated(r.posting).estimated_profit_cny)
        return tuple(
            DailyAmounts(date=d,
                         actual_profit_cny=sum_amounts(v['a']) if v['a'] else None,
                         estimated_profit_cny=sum_amounts(v['e']) if v['e'] else None,
                         order_count=v['n'], complete_order_count=v['c'])
            for d, v in sorted(bucket.items()))

    def overdue_count(self, store_alias: str) -> OverdueInfo:
        """夹具里没有逾期扫描数据 —— 明确抛错，不返回 0 冒充。

        逾期数据在店铺库的 `overdue_fast_current` / `overdue_fast_scans` 表里，
        `core/tools/freeze_golden.py` 冻结夹具时只抽了订单、快照、流水、发货单，
        没有把那次扫描的逾期集合一起冻结（它是「当前态」，不是历史事实）。
        要让夹具也支持逾期数，需要先扩展夹具 schema_version。
        """
        raise NotImplementedError(
            'FixtureSource 不提供逾期单数：夹具未冻结 overdue_fast_* 扫描数据。'
            '看板的逾期口径请使用 SqliteSource（生产店铺库）。')


def _parse_dt(value):
    """复用 sqlite_source 的时间解析，避免两处实现漂移。"""
    from .sqlite_source import _parse_dt as _impl
    return _impl(value)


def _in_window(order: dict, start_date: str, end_date: str) -> bool:
    snap = order.get('settlement_snapshot') or {}
    d = snap.get('settlement_date')
    if not d:
        return False
    return start_date <= str(d)[:10] <= end_date
