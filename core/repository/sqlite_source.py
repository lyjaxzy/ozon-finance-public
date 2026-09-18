# -*- coding: utf-8 -*-
"""SQLite 数据源：只读打开现有店铺库。

「DB 可替换」的现状实现。将来换 PostgreSQL 时，只需再写一个满足
repository/base.py 的类，领域层与测试都不需要改。

安全：一律以 mode=ro 打开，绝无写入路径。

本文件里**没有任何利润口径**。看板专用的聚合方法（orders_for_period /
amounts_for_period / daily_amounts）只做取数与求和，判定「哪些订单算完整」
一律以库里的 actual_complete 为准 —— 该列的语义与 core/domain/profit.py 的
完整性规则一致，已由 core/tests/test_profit_golden.py 逐单核对。
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, Sequence

from ..domain.profit import (Operation, Posting, SettlementSnapshot,
                             quantize_cny, rate_is_valid, sum_amounts,
                             to_decimal)
from .base import (DailyAmounts, DataCutoff, OverdueInfo, PeriodAmounts,
                   PeriodOrderRow)

DEFAULT_STORE = r'<DATA_ROOT>\data\stores\store_alpha.db'

#: 数据截止时间统一按国内时间展示（库里存的是 UTC）
CN_TZ = timezone(timedelta(hours=8))

#: 逐单取权威操作集**明细行**的 SQL。用 json_each 展开 linked_operation_ids_json，
#: 一次查询取齐一批订单的操作行，不必逐单 N+1 往返
#: （实测：14 天窗口 ≈ 2000 单，批量 1 次 ≈ 0.4 秒）。
#:
#: 刻意**不在 SQL 里 SUM**：库中 amount_rub 是 TEXT，SQLite 的 SUM 会把它转成 REAL，
#: 于是 0.1+0.2 这类二进制浮点误差就会进到金额里。这里只取原始文本，
#: 求和交给 core.domain.profit 用 Decimal 做 —— 金额一律 Decimal 是 PRD §14.2 的硬约束。
_NET_CTE = """
WITH targets AS (
    SELECT s.posting_number AS pn, f.linked_operation_ids_json AS ids_json
      FROM settlement_snapshots s
      JOIN posting_profit_facts f ON f.posting_number = s.posting_number
     WHERE s.state = 'locked' AND s.settlement_date BETWEEN ? AND ?
), uniq AS (
    SELECT DISTINCT t.pn AS pn, j.value AS op_id
      FROM targets t, json_each(t.ids_json) j
)
SELECT u.pn AS pn, u.op_id AS op_id,
       t.amount_rub AS amount_rub, t.operation_category AS operation_category,
       t.occurred_at AS occurred_at
  FROM uniq u
  LEFT JOIN finance_transactions t ON t.operation_id = u.op_id
 ORDER BY u.pn, u.op_id
"""

#: 单订单版本的同一查询（核对/调试用）
_NET_CTE_ONE = """
WITH uniq AS (
    SELECT DISTINCT j.value AS op_id
      FROM posting_profit_facts f, json_each(f.linked_operation_ids_json) j
     WHERE f.posting_number = ?
)
SELECT u.op_id AS op_id, t.amount_rub AS amount_rub,
       t.operation_category AS operation_category, t.occurred_at AS occurred_at
  FROM uniq u
  LEFT JOIN finance_transactions t ON t.operation_id = u.op_id
 ORDER BY u.op_id
"""


def _q2(value):
    """把 SQL 聚合出来的金额收敛到分。

    库里的金额是 TEXT，`SUM(CAST(x AS REAL))` 会带出二进制浮点噪声
    （实测 46082.29 会变成 46082.2900000000005），直接下发会让前端的
    字符串比较与「是否相等」判断出错。这里统一用领域层的 quantize_cny 收口，
    而不是各处自己 round —— 舍入方式只有 core 里那一个定义。
    """
    return None if value is None else quantize_cny(value)


def _parse_dt(value) -> Optional[datetime]:
    """把库里的 ISO 时间串解析成带时区的 datetime；解析不了就返回 None。

    刻意不用 `str.replace('Z','+00:00')` 之外的花招：库里既有
    `2026-09-10T16:46:47.314183+00:00`（UTC），也有 `2026-09-10 16:46:47`（无时区），
    后者按国内时间理解 —— 本系统只服务国内店铺库。
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return dt


class SqliteSource:
    """只读读取现有 SQLite 店铺库。"""

    def __init__(self, path: str = DEFAULT_STORE, verify_readonly: bool = True,
                 alias: Optional[str] = None):
        if not os.path.isfile(path):
            raise FileNotFoundError('找不到店铺库: %s' % path)
        uri = 'file:%s?mode=ro' % path.replace('\\', '/')
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        self.path = path
        #: 店铺别名。core 内部不依赖它，Web 层用它做「别名 → 库路径」的核对。
        self.alias = alias or os.path.splitext(os.path.basename(path))[0]
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

    # ── 只读聚合（看板用）────────────────────────────────────────
    def _scalar(self, sql, args=()):
        """执行单值查询；库里结构对不上时返回 None 而不是把请求打崩。"""
        try:
            r = self._conn.execute(sql, args).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if r is None else r[0]

    def _amounts(self, start_date: str, end_date: str) -> dict:
        """窗口内合计的原始数字（不含口径判定）。

        关于 estimated_profit_cny：这里**刻意不加 `estimated_complete=1` 条件**，
        只要求 `postings.estimated_profit_cny` 非空。原因：`estimated_complete`
        是事实表上的**全库**完整标记，它会在一个已锁定结算的窗口里把绝大多数订单
        挡掉（实测 14 天窗口 1978 单里只剩 44%），于是廉价路径与
        `evaluate_estimated` 的结果差一倍 —— 两个数都「看着对」，但对不上。
        §7.2 的完整性判据（revenue 与采购成本是否可得）才是权威，
        而 estimated_profit_cny 非空恰好等价于该判据成立。
        """
        r = self._conn.execute("""
            SELECT count(*) AS total_order_count,
                   sum(CASE WHEN f.actual_complete = 1 THEN 1 ELSE 0 END) AS complete_order_count,
                   sum(CASE WHEN f.actual_complete = 1
                            THEN CAST(f.actual_profit_cny AS REAL) END) AS actual_profit_cny,
                   sum(CAST(p.estimated_profit_cny AS REAL)) AS estimated_profit_cny,
                   sum(CASE WHEN s.exchange_rate_rub_per_cny IS NOT NULL
                             AND CAST(s.exchange_rate_rub_per_cny AS REAL) > 0
                            THEN f.linked_operation_count END) AS linked_operation_orders,
                   sum(CASE WHEN f.actual_complete = 1
                            THEN CAST(f.purchase_cost_cny AS REAL) END) AS purchase_cost_cny,
                   sum(CAST(p.estimated_platform_fee_cny AS REAL)) AS platform_fee_cny,
                   sum(CAST(p.logistics_cost_cny AS REAL)) AS logistics_cost_cny
              FROM settlement_snapshots s
              JOIN posting_profit_facts f ON f.posting_number = s.posting_number
              LEFT JOIN postings p ON p.posting_number = s.posting_number
             WHERE s.state = 'locked' AND s.settlement_date BETWEEN ? AND ?
        """, (start_date, end_date)).fetchone()
        return dict(r) if r is not None else {}

    def direct_net_totals(self, store_alias: str, start_date: str,
                          end_date: str) -> dict:
        """窗口内直接净额的逐单取数：{posting_number: (operations, 汇率)}。

        「直接净额」要按每单汇率折成 CNY 才能相加，所以必须逐单取回，
        不能在 SQL 里 SUM(net_rub)（各单汇率不同，先加后除会算错）。
        汇总本身交给调用方用 Decimal 做 —— core/domain/profit.py 的
        sum_amounts 是唯一的求和定义。
        """
        rows = self._conn.execute("""
            SELECT s.posting_number AS pn, s.exchange_rate_rub_per_cny AS rate
              FROM settlement_snapshots s
              JOIN posting_profit_facts f ON f.posting_number = s.posting_number
             WHERE s.state = 'locked' AND s.settlement_date BETWEEN ? AND ?
        """, (start_date, end_date)).fetchall()
        operations = self._operations_for_postings([r['pn'] for r in rows],
                                                   start_date, end_date)
        return {r['pn']: (operations.get(r['pn'], ()), to_decimal(r['rate']))
                for r in rows}

    def data_cutoff(self, store_alias: str) -> DataCutoff:
        cands = {
            'settlement_snapshots.updated_at':
                self._scalar('SELECT max(updated_at) FROM settlement_snapshots'),
            'posting_profit_facts.updated_at':
                self._scalar('SELECT max(updated_at) FROM posting_profit_facts'),
            'overdue_fast_scans.updated_at':
                self._scalar('SELECT max(updated_at) FROM overdue_fast_scans'),
            'sync_runs.finished_at':
                self._scalar('SELECT max(finished_at) FROM sync_runs'),
        }
        parsed = {k: _parse_dt(v) for k, v in cands.items()}
        usable = {k: v for k, v in parsed.items() if v is not None}
        if not usable:
            return DataCutoff(None, 'none', cands)
        source = max(usable, key=lambda k: usable[k])
        return DataCutoff(usable[source], source, cands)

    def orders_for_period(self, store_alias: str, start_date: str, end_date: str,
                          limit: Optional[int] = None, offset: int = 0) -> Sequence[PeriodOrderRow]:
        """`limit=None` 表示取整个窗口（看板汇总用），此时不做数据库分页。

        注意：汇总取整个窗口在 14 天 ≈ 2000 单的量级上是一次 0.4 秒的量级，
        可以接受；若将来窗口拉长到一年，需要改成先做日聚合再按天细化。
        """
        sql = """
            SELECT s.posting_number, s.settlement_date, s.direct_net_rub, s.settled_sales_rub,
                   s.exchange_rate_rub_per_cny, s.purchase_cost_cny, s.unknown_reason,
                   s.operation_ids_json,
                   f.actual_complete, f.actual_unknown_reason, f.actual_profit_cny,
                   f.linked_operation_ids_json, f.estimated_profit_cny AS fact_estimated_profit_cny,
                   p.status, p.revenue_cny, p.purchase_cost_cny AS posting_purchase_cost_cny,
                   p.logistics_cost_cny, p.estimated_platform_fee_cny, p.estimated_profit_cny
              FROM settlement_snapshots s
              JOIN posting_profit_facts f ON f.posting_number = s.posting_number
              LEFT JOIN postings p ON p.posting_number = s.posting_number
             WHERE s.state = 'locked' AND s.settlement_date BETWEEN ? AND ?
             ORDER BY s.settlement_date DESC, s.posting_number DESC
        """
        args = [start_date, end_date]
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            args += [int(limit), int(offset)]
        rows = self._conn.execute(sql, tuple(args)).fetchall()
        if not rows:
            return ()
        by_pn = {r['posting_number']: r for r in rows}
        operations = self._operations_for_postings(list(by_pn), start_date, end_date)
        out = []
        for pn, r in by_pn.items():
            out.append(PeriodOrderRow(
                posting_number=pn,
                settlement_date=r['settlement_date'],
                snapshot=SettlementSnapshot(
                    posting_number=pn,
                    settlement_date=r['settlement_date'],
                    state='locked',
                    direct_net_rub=to_decimal(r['direct_net_rub']),
                    settled_sales_rub=to_decimal(r['settled_sales_rub']),
                    exchange_rate_rub_per_cny=to_decimal(r['exchange_rate_rub_per_cny']),
                    purchase_cost_cny=to_decimal(r['purchase_cost_cny']),
                    operation_ids=self._json_ids(r['operation_ids_json']),
                    unknown_reason=r['unknown_reason'],
                ),
                posting=self._posting_from_row(pn, r),
                expected_operation_ids=self._json_ids(r['linked_operation_ids_json']),
                operations=operations.get(pn, ()),
                reference_actual_profit_cny=to_decimal(r['actual_profit_cny']),
                reference_estimated_profit_cny=to_decimal(r['estimated_profit_cny']),
            ))
        return tuple(out)

    def amounts_for_period(self, store_alias: str, start_date: str,
                           end_date: str) -> PeriodAmounts:
        """窗口合计的**廉价路径**：一次 SQL 扫描，不逐单取操作明细。

        金额走 CAST(... AS REAL) 再转回 Decimal，因此与逐单 Decimal 精算
        可能有 1 分以内的尾差；它对账/初筛够用，看板的权威数字走
        `orders_for_period` + core.domain.profit（见 core/repository/base.py 的说明）。
        """
        a = self._amounts(start_date, end_date)
        actual_profit_cny = _q2(to_decimal(a.get('actual_profit_cny')))
        estimated_profit_cny = _q2(to_decimal(a.get('estimated_profit_cny')))
        direct = self.direct_net_totals(store_alias, start_date, end_date)
        direct_cny = _q2(sum_amounts(
            (sum_amounts(op.amount_rub for op in ops) / rate
             for ops, rate in direct.values()
             if rate is not None and rate_is_valid(rate)))) if direct else None
        return PeriodAmounts(
            total_order_count=int(a.get('total_order_count') or 0),
            complete_order_count=int(a.get('complete_order_count') or 0),
            actual_profit_cny=actual_profit_cny,
            estimated_profit_cny=estimated_profit_cny,
            direct_net_cny=direct_cny,
            purchase_cost_cny=_q2(to_decimal(a.get('purchase_cost_cny'))),
            platform_fee_cny=_q2(to_decimal(a.get('platform_fee_cny'))),
            logistics_cost_cny=_q2(to_decimal(a.get('logistics_cost_cny'))),
        )

    def daily_amounts(self, store_alias: str, start_date: str,
                      end_date: str) -> Sequence[DailyAmounts]:
        """按天汇总（廉价路径，SQL 聚合，未取操作明细）。

        只返回**有数据的日期**；没有订单的日期不会补 0 行 ——
        「没有数据」和「利润为 0」是两件事，补 0 会让两者不可分辨。

        estimated_profit_cny 同样不加 `estimated_complete=1` 条件，理由见 `_amounts`；
        否则按天加起来会对不上总计（实测差一倍）。
        """
        rows = self._conn.execute("""
            SELECT s.settlement_date AS d,
                   count(*) AS order_count,
                   sum(CASE WHEN f.actual_complete = 1 THEN 1 ELSE 0 END) AS complete_order_count,
                   sum(CASE WHEN f.actual_complete = 1
                            THEN CAST(f.actual_profit_cny AS REAL) END) AS actual_profit_cny,
                   sum(CAST(p.estimated_profit_cny AS REAL)) AS estimated_profit_cny
              FROM settlement_snapshots s
              JOIN posting_profit_facts f ON f.posting_number = s.posting_number
              LEFT JOIN postings p ON p.posting_number = s.posting_number
             WHERE s.state = 'locked' AND s.settlement_date BETWEEN ? AND ?
             GROUP BY d
             ORDER BY d
        """, (start_date, end_date)).fetchall()
        return tuple(
            DailyAmounts(
                date=r['d'],
                actual_profit_cny=_q2(to_decimal(r['actual_profit_cny'])),
                estimated_profit_cny=_q2(to_decimal(r['estimated_profit_cny'])),
                order_count=int(r['order_count'] or 0),
                complete_order_count=int(r['complete_order_count'] or 0),
            )
            for r in rows)

    def overdue_count(self, store_alias: str) -> OverdueInfo:
        """逾期单数取自 overdue_fast_current（当前生效的那次扫描）。

        刻意不按看板窗口过滤：这张表存的是**当前**在途逾期集合，
        不是历史每日快照，按窗口过滤只会把结果打成 0，属于伪装成成功的错误。
        """
        try:
            row = self._conn.execute("""
                SELECT c.scan_id, c.as_of, c.row_count,
                       s.overdue_order_count, s.active_order_count, s.status
                  FROM overdue_fast_current c
                  LEFT JOIN overdue_fast_scans s ON s.scan_id = c.scan_id
                 WHERE c.store_alias = ?
            """, (store_alias,)).fetchone()
        except sqlite3.OperationalError:
            return OverdueInfo(store_alias, None, 'overdue_fast_current',
                               available=False, note='库中没有逾期扫描表')
        if row is None:
            return OverdueInfo(store_alias, None, 'overdue_fast_current',
                               available=False, note='本店铺没有生效的逾期扫描记录')
        count = row['overdue_order_count']
        if count is None:
            # 扫描汇总列缺失时，退回到明细表现场数一遍（必须带上 is_overdue=1）
            count = self._scalar(
                'SELECT count(*) FROM overdue_fast_staging WHERE scan_id=? AND is_overdue=1',
                (row['scan_id'],))
        return OverdueInfo(
            store_alias, None if count is None else int(count),
            'overdue_fast_scans.overdue_order_count', row['as_of'],
            note=('扫描状态 %s，在途订单 %s 单' % (row['status'], row['active_order_count'])),
        )

    # ── 聚合用到的内部工具 ──
    @staticmethod
    def _json_ids(raw) -> tuple:
        if not raw:
            return ()
        try:
            return tuple(str(x) for x in json.loads(raw))
        except ValueError:
            return ()

    @staticmethod
    def _posting_from_row(pn: str, r) -> Optional[Posting]:
        if r['revenue_cny'] is None and r['status'] is None:
            return None  # LEFT JOIN 没命中：这张订单在 postings 里不存在
        return Posting(
            posting_number=pn,
            status=r['status'],
            revenue_cny=to_decimal(r['revenue_cny']),
            purchase_cost_cny=to_decimal(r['posting_purchase_cost_cny']),
            logistics_cost_cny=to_decimal(r['logistics_cost_cny']),
            estimated_platform_fee_cny=to_decimal(r['estimated_platform_fee_cny']),
        )

    def _operations_for_postings(self, posting_numbers: Sequence[str],
                                 start_date: str, end_date: str) -> dict:
        """一次取齐一批订单的权威操作集**流水明细**。

        按 operation_id 展开 json 数组再 LEFT JOIN，因此**能取到挂在基单号上的手续费**
        （如 FAKE-11D63D3B 的 ACQUIRING 记在 FAKE-015C8A22）——
        这正是 core/README.md 里 35.1% vs 100% 那条结论的落点。

        LEFT JOIN 刻意保留取不到流水的 op_id（amount_rub 为 None）：
        evaluate_actual 要靠「操作条数 == 期望条数」发现取数不全，
        若在这里就把缺行过滤掉，MISSING_OPERATION 这个保护就失效了。
        """
        wanted = set(posting_numbers)
        out = {}
        try:
            rows = self._conn.execute(_NET_CTE, (start_date, end_date)).fetchall()
        except sqlite3.OperationalError:
            return out
        for r in rows:
            pn = r['pn']
            if pn not in wanted:
                continue
            out.setdefault(pn, []).append(Operation(
                operation_id=r['op_id'],
                amount_rub=to_decimal(r['amount_rub']),
                operation_category=r['operation_category'],
                occurred_at=r['occurred_at'],
            ))
        return {pn: tuple(ops) for pn, ops in out.items()}

    def operations_for_linked_ids(self, posting_number: str) -> Sequence[Operation]:
        """按**事实表的权威操作集**取该订单的流水明细。

        与 `operations_for(pn, linked_operation_ids(pn))` 等价，
        区别是走一条预编译的 CTE，省掉一次往返。逐单核对/调试时用它。
        """
        rows = self._conn.execute(_NET_CTE_ONE, (posting_number,)).fetchall()
        return tuple(
            Operation(operation_id=r['op_id'],
                      amount_rub=to_decimal(r['amount_rub']),
                      operation_category=r['operation_category'],
                      occurred_at=r['occurred_at'])
            for r in rows)
