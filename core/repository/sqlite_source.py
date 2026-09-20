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
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from ..domain.profit import (Operation, Posting, SettlementSnapshot,
                             quantize_cny, rate_is_valid, sum_amounts,
                             to_decimal)
from .base import (DailyAmounts, DataCutoff, OverdueInfo, PeriodAmounts,
                   PeriodOrderRow, PeriodSkuRow, SkuDetail,
                   UnattributedAmounts, UnattributedRecord)
from .sku_attribution import attribute_order_lines

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


def _parse_day(value) -> Optional[date]:
    """把库里的 `YYYY-MM-DD` 日期串解析成 date；解析不了就返回 None。

    只认日期的前 10 位：`settlement_date` 是 DATE 列（库里存 `2026-09-10`），
    但历史数据里也可能混进带时间的串，取前 10 位比直接 `fromisoformat` 稳。
    """
    if not value:
        return None
    s = str(value).strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


# ── data_cutoff 的候选时间戳（分两类，见 SqliteSource.data_cutoff）──
#
# 财务表：它们的新鲜度 = 数据的新鲜度。
_FINANCE_TIMESTAMP_SQL = {
    'settlement_snapshots.updated_at':
        'SELECT max(updated_at) FROM settlement_snapshots',
    'posting_profit_facts.updated_at':
        'SELECT max(updated_at) FROM posting_profit_facts',
    'finance_transactions.updated_at':
        'SELECT max(updated_at) FROM finance_transactions',
}

# 运营表：只记录，不参与判断。
# 之前这里写的是 `sync_runs.finished_at` —— 该列**不存在**（实际列名是
# recorded_at），`_scalar` 吞掉 OperationalError 返回 None，于是这个候选
# 一直静默失效。改成真实列名，并且只当诊断信息。
_OBSERVED_TIMESTAMP_SQL = {
    'overdue_fast_scans.updated_at':
        'SELECT max(updated_at) FROM overdue_fast_scans',
    'sync_runs.recorded_at':
        'SELECT max(recorded_at) FROM sync_runs',
}

# 窗口右端 = 最后一个**已结算**的日期。只有 state='locked' 的快照算已结算，
# 与 orders_for_period 的过滤条件逐字一致 —— 两处口径必须同步改。
_WINDOW_END_SQL = ("SELECT max(settlement_date) FROM settlement_snapshots "
                   "WHERE state = 'locked'")

# ── 成本来源策略（ADR-0009）─────────────────────────────────────
#
#: 库优先：店铺库里有采购成本就用库里的；为空时才用成本库兜底。
#: 当前生产库已锁定订单的成本非空率 100%，所以这个策略下数字与引入成本库之前一致。
COST_POLICY_BOOK_FIRST = 'book_first'
#: 成本库为权威：订单成本一律按「成本库单价 × 数量」合成（阶段 D 迁移完成后启用）。
COST_POLICY_BOOK_AUTHORITATIVE = 'book_authoritative'


class SqliteSource:
    """只读读取现有 SQLite 店铺库。"""

    def __init__(self, path: str = DEFAULT_STORE, verify_readonly: bool = True,
                 alias: Optional[str] = None,
                 unit_costs: Optional[dict] = None,
                 cost_policy: str = COST_POLICY_BOOK_FIRST):
        if not os.path.isfile(path):
            raise FileNotFoundError('找不到店铺库: %s' % path)
        uri = 'file:%s?mode=ro' % path.replace('\\', '/')
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        self.path = path
        #: 店铺别名。core 内部不依赖它，Web 层用它做「别名 → 库路径」的核对。
        self.alias = alias or os.path.splitext(os.path.basename(path))[0]
        #: 成本库注入的单价（货号 → 单件 CNY）。
        #: ⚠️ 名字刻意不叫 `_unit_costs` —— 这个类里已经有一个**同名方法**
        #: `_unit_costs()`（逐 SKU 下钻用它从订单级成本反推单价），
        #: 属性会把它盖掉，报 `'dict' object is not callable`（踩过）。
        self.cost_book_prices = dict(unit_costs or {})
        self._cost_policy = (cost_policy or COST_POLICY_BOOK_FIRST).strip()
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
        """库的时间边界：财务表的写入时间 + 最后一个已结算日。

        候选时间戳**分两组**，这是本方法唯一需要小心的地方：

        * `_FINANCE_TIMESTAMP_SQL` —— 财务表。它们的新鲜度就是数据的新鲜度，
          可以决定「数据截止」。
        * `_OBSERVED_TIMESTAMP_SQL` —— 运营表。只记录进 `candidates` 供排查，
          **不参与**任何判断。`overdue_fast_scans.updated_at` 曾经参与过，
          于是 2026-09-19 的一次逾期扫描把窗口右端从 09-10 推到 09-19，
          `days=7` 的窗口里一天财务数据都没有 —— 详见 `DataCutoff` 的注释。

        `window_end` 另走一条路：**锁定快照里最大的 settlement_date**。
        这是数据自己给出的边界，比任何写入时间都可靠 —— 写入时间会被
        重跑同步整体刷新到今天，而窗口查询的过滤条件恰恰是
        `s.settlement_date BETWEEN ? AND ?`，右端越过最后一个结算日就必然是空窗。
        """
        raw = {name: self._scalar(sql)
               for name, sql in {**_FINANCE_TIMESTAMP_SQL,
                                 **_OBSERVED_TIMESTAMP_SQL}.items()}
        parsed = {k: _parse_dt(v) for k, v in raw.items()}
        window_end = _parse_day(self._scalar(_WINDOW_END_SQL))
        finance = {k: v for k, v in parsed.items()
                   if k in _FINANCE_TIMESTAMP_SQL and v is not None}
        if finance:
            source = max(finance, key=lambda k: finance[k])
            return DataCutoff(finance[source], source, raw, window_end)
        # 一张财务表都读不到（库结构对不上 / 是空库）：退回运营表只是为了
        # 「别显示一个空截止时间」，但 source 带 `observed:` 前缀，
        # 不把运营时间伪装成财务时间。
        observed = {k: v for k, v in parsed.items() if v is not None}
        if observed:
            source = max(observed, key=lambda k: observed[k])
            return DataCutoff(observed[source], 'observed:' + source, raw, window_end)
        return DataCutoff(None, 'none', raw, window_end)

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
        items = self._items_for_postings(list(by_pn))
        out = []
        for pn, r in by_pn.items():
            out.append(self._period_order_row(pn, r, operations.get(pn, ()),
                                              items.get(pn, ())))
        return tuple(out)

    def _has_table(self, name: str) -> bool:
        """库里有没有这张表。用于兼容「旧库没有 posting_items」的场景。"""
        try:
            r = self._conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name = ?",
                (name,)).fetchone()
        except sqlite3.OperationalError:
            return False
        return bool(r and r[0])

    def _fallback_cost(self, items: Sequence[tuple], stored: Optional[Decimal]) -> Optional[Decimal]:
        """库里没有成本时，按成本库的单价合成（ADR-0009）。

        `items` 是 `_items_for_postings` 的行：`(货号, SKU, 商品名, 数量)`。

        三条规则，任一条不满足就**如实返回缺失**（不猜、不补 0）：

        1. 成本库里确实有这个货号的单价；
        2. **只对单货号订单**合成 —— 多货号订单的成本无法从单价唯一还原，沿用 ADR-0006 的
           「不摊分」原则，保持缺失并由完整性规则报 `missing_purchase_cost`；
        3. 策略允许：`book_first` 只在库里没有成本时兜底；`book_authoritative` 一律合成。
        """
        if not self.cost_book_prices or not items:
            return stored
        if self._cost_policy == COST_POLICY_BOOK_FIRST and stored is not None:
            return stored
        offers = {row[0] for row in items if row and row[0]}
        if len(offers) != 1:
            return stored
        offer = offers.pop()
        unit = self.cost_book_prices.get(offer)
        if unit is None:
            return stored
        qty = sum(int(row[3] or 0) for row in items if row and row[0] == offer)
        return quantize_cny(unit * Decimal(qty))

    def _period_order_row(self, pn: str, r, operations, items: Sequence[tuple] = ()) -> PeriodOrderRow:
        """把一行窗口查询结果组装成 `PeriodOrderRow`（逐单口径的原始输入）。

        抽出来是为了让「订单口径」的两条路径（看板与逐 SKU 下钻的对账）
        共用同一段组装代码 —— 两处各写一遍迟早会漂移。
        """
        stored_snapshot_cost = to_decimal(r['purchase_cost_cny'])
        stored_posting_cost = to_decimal(r['posting_purchase_cost_cny'])
        return PeriodOrderRow(
            posting_number=pn,
            settlement_date=r['settlement_date'],
            snapshot=SettlementSnapshot(
                posting_number=pn,
                settlement_date=r['settlement_date'],
                state='locked',
                direct_net_rub=to_decimal(r['direct_net_rub']),
                settled_sales_rub=to_decimal(r['settled_sales_rub']),
                exchange_rate_rub_per_cny=to_decimal(r['exchange_rate_rub_per_cny']),
                purchase_cost_cny=self._fallback_cost(items, stored_snapshot_cost),
                operation_ids=self._json_ids(r['operation_ids_json']),
                unknown_reason=r['unknown_reason'],
            ),
            posting=self._posting_from_row(pn, r,
                                           self._fallback_cost(items, stored_posting_cost)),
            expected_operation_ids=self._json_ids(r['linked_operation_ids_json']),
            operations=tuple(operations),
            reference_actual_profit_cny=to_decimal(r['actual_profit_cny']),
            reference_estimated_profit_cny=to_decimal(r['estimated_profit_cny']),
        )

    # ── 逐 SKU 下钻（ADR-0006）────────────────────────────────────
    #
    # ⚠️ 本方法**没有任何利润口径**：它只做「按数据自带的分组键取数与归属」。
    # 收入/成本落不到某个货号时，一律进未归属桶并写明原因，绝不分摊、绝不补 0。
    def sku_detail_for_period(self, store_alias: str, start_date: str,
                              end_date: str) -> SkuDetail:
        """窗口内逐 SKU（按货号）的取数与归属结果。

        窗口口径与 `orders_for_period` 逐字相同（锁定快照的 settlement_date），
        否则「逐 SKU 合计」与「订单口径合计」会对不上。
        """
        posting_rows = self._conn.execute("""
            SELECT s.posting_number, s.settlement_date, s.direct_net_rub,
                   s.settled_sales_rub, s.exchange_rate_rub_per_cny,
                   s.purchase_cost_cny, s.unknown_reason, s.operation_ids_json,
                   f.actual_profit_cny, f.linked_operation_ids_json,
                   p.status, p.revenue_cny,
                   p.purchase_cost_cny AS posting_purchase_cost_cny,
                   p.logistics_cost_cny, p.estimated_platform_fee_cny,
                   p.estimated_profit_cny, p.raw_json
              FROM settlement_snapshots s
              JOIN posting_profit_facts f ON f.posting_number = s.posting_number
              LEFT JOIN postings p ON p.posting_number = s.posting_number
             WHERE s.state = 'locked' AND s.settlement_date BETWEEN ? AND ?
             ORDER BY s.settlement_date DESC, s.posting_number DESC
        """, (start_date, end_date)).fetchall()
        if not posting_rows:
            return SkuDetail()

        pns = [r['posting_number'] for r in posting_rows]
        operations = self._operations_for_postings(pns, start_date, end_date)
        items_by_pn = self._items_for_postings(pns)
        unit_costs = self._unit_costs()

        rows: list = []
        unattributed: list = []
        actual_unattributed: list = []
        for r in posting_rows:
            pn = r['posting_number']
            items = items_by_pn.get(pn, ())
            prices = self._unit_prices(r['raw_json'])
            # 按行收入用**权威的明细行数量**乘单价（而不是 raw_json 里自带的
            # quantity）：数量只有一个来源，两个来源对不上时让收入归属失败，
            # 而不是各用一半、悄悄凑出一个和。
            line_revenues = {
                str(offer): prices[str(offer)] * Decimal(int(qty or 0))
                for (offer, _sku, _name, qty) in items
                if str(offer) in prices
            }
            posting = self._posting_from_row(pn, r)
            order_row = self._period_order_row(pn, r, operations.get(pn, ()))
            line_rows, records = attribute_order_lines(
                posting_number=pn,
                settlement_date=r['settlement_date'],
                items=items,
                revenue_cny=None if posting is None else posting.revenue_cny,
                purchase_cost_cny=None if posting is None else posting.purchase_cost_cny,
                unit_cost_by_offer=unit_costs,
                line_revenues=line_revenues,
                logistics_cost_cny=None if posting is None else posting.logistics_cost_cny,
                platform_fee_cny=(
                    None if posting is None else posting.estimated_platform_fee_cny),
                snapshot=order_row.snapshot,
                operations=operations.get(pn, ()),
                expected_operation_ids=self._json_ids(r['linked_operation_ids_json']),
            )
            rows.extend(line_rows)
            unattributed.extend(records)
            if not line_rows or not line_rows[0].actual_attributable:
                # 实际利润整单都归不出去（多货号订单，或连商品明细行都没有）——
                # 交给调用方按订单口径走 evaluate_actual，再计入「未归属」，
                # 绝不分摊到各 SKU，也绝不静默丢掉。
                actual_unattributed.append(order_row)

        return SkuDetail(
            rows=tuple(rows),
            unattributed=UnattributedAmounts(
                posting_count=len({rec.posting_number for rec in unattributed
                                   if rec.posting_number}),
                records=tuple(unattributed),
            ),
            actual_unattributed=tuple(actual_unattributed),
            order_count=len(posting_rows),
        )

    def _items_for_postings(self, posting_numbers: Sequence[str]) -> dict:
        """一次取齐一批订单的商品明细行（货号 / SKU / 商品名 / 数量）。

        这批明细就是**逐 SKU 的分组键**：一个订单只有一个货号时整单归属到它，
        多个货号时按数据自带的按行金额落位。逐单 N+1 查询在 2000 单上不可接受，
        所以分批一次取齐（SQLite 的绑定上限是 999）。
        """
        out: dict = {}
        pns = list(posting_numbers)
        for i in range(0, len(pns), 900):
            chunk = pns[i:i + 900]
            for r in self._conn.execute(
                    'SELECT posting_number, offer_id, sku, product_name, quantity '
                    'FROM posting_items WHERE posting_number IN (%s) '
                    'ORDER BY posting_number, offer_id' % ','.join('?' * len(chunk)),
                    tuple(chunk)):
                out.setdefault(r['posting_number'], []).append(
                    (r['offer_id'], r['sku'], r['product_name'], r['quantity']))
        return {pn: tuple(v) for pn, v in out.items()}

    @staticmethod
    def _unit_prices(raw_json) -> dict:
        """`postings.raw_json.products[]` → {货号: 单价}。

        只在**多货号订单**需要按行拆分收入时用到。库里的 `price` 有两种形态
        （实测同一张表里都有）：字符串 `'40.0000'`，或
        `{'amount': '32', 'currency': 'CNY'}`。两种都认；认不出就不放进映射，
        归属逻辑会因此把整单收入判成 `revenue_not_attributable`
        —— 而不是拿 0 顶替、也不是猜一个比例。
        """
        if not raw_json:
            return {}
        try:
            raw = json.loads(raw_json)
        except ValueError:
            return {}
        out: dict = {}
        for product in (raw.get('products') or []):
            offer = product.get('offer_id')
            if not offer:
                continue
            price = product.get('price')
            if isinstance(price, dict):
                amount = to_decimal(price.get('amount'))
            else:
                amount = to_decimal(price)
            if amount is None:
                continue
            out[str(offer)] = amount
        return out

    def _unit_costs(self) -> dict:
        """货号 → 单件采购成本（CNY）。两个来源，按优先级合并。

        1. **`sku_cost_cache`（sku → 单件成本）—— 权威来源。**
           这正是本系统存在的理由：OZON 的两份导出里唯独没有采购成本，
           必须由我方按 SKU 主动录入。本机这张表当前是 **0 行**，
           所以下面还有一个反推的兜底。
        2. **反推**：单货号订单的整单成本 ÷ 数量。
           单货号订单的成本整单属于那一个货号（实测 11002 单里 10953 单如此），
           所以这个商就是该货号的单件成本，不需要任何分摊。
           同一货号出现**两个不同值**时一律不采用 —— 冲突即不可信，
           宁可界面上多一个「缺成本」，也不拿一个可能错的数当真。

        ⚠️ 第 2 条是**临时方案**（见 docs/adr/0006 的「未解决」一节）：
        它把「现有系统已经算好的逐单成本」反推成成本库，属于拿结果当输入。
        接上真的成本库（`sku_cost_cache` 有数据）后，第 2 条自动退居其次。
        """
        by_sku: dict = {}
        try:
            for r in self._conn.execute('SELECT sku, unit_cost_cny FROM sku_cost_cache'):
                value = to_decimal(r['unit_cost_cny'])
                if r['sku'] is not None and value is not None:
                    by_sku[str(r['sku'])] = value
        except sqlite3.OperationalError:
            pass  # 库结构对不上时按「没有成本库」处理，但下面照样会有反推

        out: dict = {}
        if by_sku:
            for r in self._conn.execute('SELECT DISTINCT offer_id, sku FROM posting_items'):
                if not r['offer_id']:
                    continue
                key = None if r['sku'] is None else str(r['sku'])
                value = by_sku.get(key)
                if value is not None:
                    out[str(r['offer_id'])] = value

        derived: dict = {}
        conflict = set()
        for r in self._conn.execute("""
            SELECT i.offer_id AS offer_id, i.quantity AS quantity,
                   p.purchase_cost_cny AS cost
              FROM posting_items i
              JOIN postings p ON p.posting_number = i.posting_number
             WHERE p.purchase_cost_cny IS NOT NULL
               AND i.posting_number IN (
                   SELECT posting_number FROM posting_items
                    GROUP BY posting_number HAVING count(*) = 1)
        """):
            cost = to_decimal(r['cost'])
            quantity = int(r['quantity'] or 0)
            if cost is None or quantity <= 0 or not r['offer_id']:
                continue
            unit = cost / Decimal(quantity)
            prev = derived.get(str(r['offer_id']))
            if prev is None:
                derived[str(r['offer_id'])] = unit
            elif prev != unit:
                conflict.add(str(r['offer_id']))
        for offer_id in conflict:
            derived.pop(offer_id, None)
        for offer_id, unit in derived.items():
            out.setdefault(offer_id, unit)
        return out

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
    def _posting_from_row(pn: str, r,
                          purchase_cost_cny: Optional[Decimal] = None) -> Optional[Posting]:
        if r['revenue_cny'] is None and r['status'] is None:
            return None  # LEFT JOIN 没命中：这张订单在 postings 里不存在
        return Posting(
            posting_number=pn,
            status=r['status'],
            revenue_cny=to_decimal(r['revenue_cny']),
            purchase_cost_cny=(to_decimal(r['posting_purchase_cost_cny'])
                               if purchase_cost_cny is None else purchase_cost_cny),
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
