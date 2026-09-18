# -*- coding: utf-8 -*-
"""SQLite 数据源：只读打开现有店铺库。

「DB 可替换」的现状实现。将来换 PostgreSQL 时，只需再写一个满足
repository/base.py 的类，领域层与测试都不需要改。

安全：一律以 mode=ro 打开，绝无写入路径。
"""
import json
import os
import sqlite3
from typing import Optional, Sequence

from ..domain.profit import Operation, Posting, SettlementSnapshot, to_decimal

DEFAULT_STORE = r'<DATA_ROOT>\data\stores\store_alpha.db'


class SqliteSource:
    """只读读取现有 SQLite 店铺库。"""

    def __init__(self, path: str = DEFAULT_STORE, verify_readonly: bool = True):
        if not os.path.isfile(path):
            raise FileNotFoundError('找不到店铺库: %s' % path)
        uri = 'file:%s?mode=ro' % path.replace('\\', '/')
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        self.path = path
        if verify_readonly:
            # 兜底自检：确认这个连接真的写不进去
            try:
                self._conn.execute('CREATE TABLE __write_probe(x)')
                raise RuntimeError('安全断言失败：连接居然可写！')
            except sqlite3.OperationalError:
                pass  # 预期路径：readonly database

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 输入 ──
    def list_posting_numbers(self) -> Sequence[str]:
        return [r[0] for r in self._conn.execute(
            'SELECT posting_number FROM posting_profit_facts ORDER BY posting_number')]

    def settlement_snapshot(self, posting_number: str) -> Optional[SettlementSnapshot]:
        r = self._conn.execute("""
            SELECT posting_number, settlement_date, state, direct_net_rub, settled_sales_rub,
                   exchange_rate_rub_per_cny, purchase_cost_cny, unknown_reason, operation_ids_json
            FROM settlement_snapshots
            WHERE posting_number=? AND state='locked'
            ORDER BY settlement_date DESC LIMIT 1
        """, (posting_number,)).fetchone()
        if r is None:
            return None
        ids = ()
        if r['operation_ids_json']:
            try:
                ids = tuple(str(x) for x in json.loads(r['operation_ids_json']))
            except ValueError:
                ids = ()
        return SettlementSnapshot(
            posting_number=r['posting_number'],
            settlement_date=r['settlement_date'],
            state=r['state'],
            direct_net_rub=to_decimal(r['direct_net_rub']),
            settled_sales_rub=to_decimal(r['settled_sales_rub']),
            exchange_rate_rub_per_cny=to_decimal(r['exchange_rate_rub_per_cny']),
            purchase_cost_cny=to_decimal(r['purchase_cost_cny']),
            operation_ids=ids,
            unknown_reason=r['unknown_reason'],
        )

    def operations(self, posting_number: str) -> Sequence[Operation]:
        ids = list(self.snapshot_operation_ids(posting_number))
        for extra in self.linked_operation_ids(posting_number):
            if extra not in ids:
                ids.append(extra)
        return self.operations_for(posting_number, ids)

    def snapshot_operation_ids(self, posting_number: str) -> Sequence[str]:
        snap = self.settlement_snapshot(posting_number)
        return () if snap is None else snap.operation_ids

    def operations_for(self, posting_number: str,
                       ids) -> Sequence[Operation]:
        if not ids:
            return ()
        ids = [str(i) for i in ids]
        placeholders = ','.join('?' * len(ids))
        rows = self._conn.execute(
            'SELECT operation_id, amount_rub, operation_category, occurred_at '
            'FROM finance_transactions WHERE operation_id IN (%s)' % placeholders,
            tuple(ids)).fetchall()
        return tuple(
            Operation(operation_id=r['operation_id'],
                      amount_rub=to_decimal(r['amount_rub']),
                      operation_category=r['operation_category'],
                      occurred_at=r['occurred_at'])
            for r in rows)

    def linked_operation_ids(self, posting_number: str) -> Sequence[str]:
        r = self._conn.execute(
            'SELECT linked_operation_ids_json FROM posting_profit_facts WHERE posting_number=?',
            (posting_number,)).fetchone()
        if r is None or not r['linked_operation_ids_json']:
            return ()
        try:
            return tuple(str(x) for x in json.loads(r['linked_operation_ids_json']))
        except ValueError:
            return ()

    def posting(self, posting_number: str) -> Optional[Posting]:
        r = self._conn.execute("""
            SELECT posting_number, status, revenue_cny, purchase_cost_cny, logistics_cost_cny,
                   estimated_platform_fee_cny
            FROM postings WHERE posting_number=?
        """, (posting_number,)).fetchone()
        if r is None:
            return None
        return Posting(
            posting_number=r['posting_number'],
            status=r['status'],
            revenue_cny=to_decimal(r['revenue_cny']),
            purchase_cost_cny=to_decimal(r['purchase_cost_cny']),
            logistics_cost_cny=to_decimal(r['logistics_cost_cny']),
            estimated_platform_fee_cny=to_decimal(r['estimated_platform_fee_cny']),
        )

    # ── 参照 ──
    def reference_actual(self, posting_number: str) -> dict:
        r = self._conn.execute("""
            SELECT actual_profit_cny, direct_net_rub, settled_sales_rub,
                   exchange_rate_rub_per_cny, purchase_cost_cny, actual_complete,
                   actual_unknown_reason
            FROM posting_profit_facts WHERE posting_number=?
        """, (posting_number,)).fetchone()
        if r is None:
            return {}
        return {
            'actual_profit_cny': r['actual_profit_cny'],
            'direct_net_rub': r['direct_net_rub'],
            'settled_sales_rub': r['settled_sales_rub'],
            'exchange_rate_rub_per_cny': r['exchange_rate_rub_per_cny'],
            'purchase_cost_cny': r['purchase_cost_cny'],
            'actual_complete': r['actual_complete'],
            'actual_unknown_reason': r['actual_unknown_reason'],
        }

    def reference_estimated(self, posting_number: str) -> Optional[str]:
        r = self._conn.execute(
            'SELECT estimated_profit_cny FROM postings WHERE posting_number=?',
            (posting_number,)).fetchone()
        return None if r is None else r['estimated_profit_cny']

    def reference_completion(self) -> dict:
        r = self._conn.execute("""
            SELECT count(*) AS total,
                   sum(CASE WHEN actual_complete=1 THEN 1 ELSE 0 END) AS actual_complete,
                   sum(CASE WHEN estimated_complete=1 THEN 1 ELSE 0 END) AS estimated_complete
            FROM posting_profit_facts
        """).fetchone()
        return {'total': r['total'], 'actual_complete': r['actual_complete'],
                'estimated_complete': r['estimated_complete']}
