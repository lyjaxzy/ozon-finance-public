# -*- coding: utf-8 -*-
"""夹具数据源：从冻结的黄金样本 JSON 读数据。

用途：CI / 无生产库的机器上也能跑完整回归。夹具本身入库（289 KB），
生产库不入库。这保证了「重构不改变结果」这件事可以被任何人复现。
"""
import io
import json
import os
from typing import Optional, Sequence

from ..domain.profit import Operation, Posting, SettlementSnapshot, to_decimal

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
