# -*- coding: utf-8 -*-
"""窗口右端（`data_cutoff.window_end`）的回归测试。

**这个文件是为一个真实事故写的**，不是为了覆盖率：

财务同步停在 2026-09-10，而运营的逾期扫描在 2026-09-19 跑过一次。
`data_cutoff` 当时把「所有表里最大的 updated_at」当成数据截止，
于是 `overdue_fast_scans.updated_at = 2026-09-19` 压过结算数据，
窗口右端被推到 09-19：

    GET /api/dashboard/store/store_alpha?days=7
    → period 2026-09-13 ~ 2026-09-19，total_order_count = 0，
      actual_profit_cny = null

看板显示「0 单 / 实际利润为空」。这不是数据缺失，是窗口取错了。
修法是把两件事拆开（见 `core/repository/base.py::DataCutoff`）：

    cutoff      = 财务表的写入时间                → 展示用「数据截止」
    window_end  = 锁定快照里最大的 settlement_date → 窗口右端

用合成库而不是生产库来测：生产库每天都在变，而这条口径必须永远成立。
用例里的日期、表结构都是本文件自己造的，所以公开子集里同样能跑。

运行：
    python -m unittest core.tests.test_sqlite_source_cutoff -v
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from ..repository.sqlite_source import SqliteSource, _parse_day

FINANCE_UPDATED = '2026-09-10T16:46:47.314183+00:00'
# 三张财务表的时间戳刻意各不相同（照抄生产库的形状）：
# 这样「取财务表里最大的那个」才是真的被测到，而不是被并列值蒙对。
SNAPSHOT_UPDATED = '2026-09-10T16:45:03.155056+00:00'
TXN_UPDATED = '2026-09-10T16:44:18.427951+00:00'
OVERDUE_UPDATED = '2026-09-19T03:38:45.214155+00:00'
SYNC_RECORDED = '2026-09-10T16:46:42.557613+00:00'


def _make_store(rows, drop_finance_tables=False, facts_updated=FINANCE_UPDATED):
    """造一个最小店铺库：只带 data_cutoff 会读的那几张表。

    `rows` 是 (posting_number, settlement_date, state) 三元组。
    逾期扫描的时间戳刻意比财务新 9 天 —— 事故现场就是这个形状。
    """
    tmp = tempfile.mkdtemp(prefix='ozon-cutoff-')
    path = os.path.join(tmp, 'store.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE settlement_snapshots('
                 'posting_number TEXT PRIMARY KEY, settlement_date TEXT, '
                 'state TEXT, updated_at TEXT)')
    conn.execute('CREATE TABLE overdue_fast_scans('
                 'scan_id TEXT PRIMARY KEY, updated_at TEXT)')
    conn.execute("INSERT INTO overdue_fast_scans VALUES('scan-1', ?)",
                 (OVERDUE_UPDATED,))
    if not drop_finance_tables:
        conn.execute('CREATE TABLE posting_profit_facts('
                     'posting_number TEXT PRIMARY KEY, updated_at TEXT)')
        conn.execute('CREATE TABLE finance_transactions('
                     'operation_id TEXT PRIMARY KEY, updated_at TEXT)')
        conn.execute('CREATE TABLE sync_runs(id INTEGER PRIMARY KEY, '
                     'source TEXT, status TEXT, recorded_at TEXT)')
        conn.execute('INSERT INTO sync_runs VALUES(1, ?, ?, ?)',
                     ('finance', 'success', SYNC_RECORDED))
    for pn, day, state in rows:
        conn.execute('INSERT INTO settlement_snapshots VALUES(?,?,?,?)',
                     (pn, day, state, SNAPSHOT_UPDATED))
        if not drop_finance_tables:
            conn.execute('INSERT INTO posting_profit_facts VALUES(?,?)',
                         (pn, facts_updated))
            conn.execute('INSERT INTO finance_transactions VALUES(?,?)',
                         ('op-' + pn, TXN_UPDATED))
    conn.commit()
    conn.close()
    return path


class WindowEndTest(unittest.TestCase):
    """窗口右端必须由「已结算日」给出，不能被运营表的时间戳推走。"""

    def setUp(self):
        self._dirs = []

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def source(self, rows, **kw):
        path = _make_store(rows, **kw)
        self._dirs.append(os.path.dirname(path))
        src = SqliteSource(path)
        self.addCleanup(src.close)
        return src

    def test_operational_scan_does_not_push_window_end(self):
        """核心回归：逾期扫描比财务新 9 天，窗口右端仍必须是 09-10。"""
        src = self.source([('A-1', '2026-09-04', 'locked'),
                           ('A-2', '2026-09-10', 'locked')])
        cutoff = src.data_cutoff('store_alpha')
        self.assertEqual(date(2026, 9, 10), cutoff.window_end)
        self.assertEqual('posting_profit_facts.updated_at', cutoff.source)
        # 运营表的时间戳仍然记录着 —— 只是不再参与判断，排查线索不能丢
        self.assertEqual(OVERDUE_UPDATED,
                         cutoff.candidates['overdue_fast_scans.updated_at'])
        self.assertEqual(OVERDUE_UPDATED,
                         max(cutoff.candidates.values()),
                         '逾期扫描确实是所有候选里最新的一个 —— '
                         '所以这条断言不是在验证一个空条件')

    def test_finance_resync_alone_does_not_push_window_end(self):
        """财务表自己的写入时间晚于最后一个结算日时，窗口也不能跟着走。

        这是拆出 `window_end` 的**第二个理由**，也是比逾期扫描更隐蔽的一个：
        重跑一次财务同步会把 `updated_at` 整体刷新，而结算日并不会因此变新。
        此时 `cutoff.date()` 已经越过数据了 —— 若拿它当窗口右端，
        `days=7` 里有 4 天必然是空的。窗口右端只能由数据自己给出。
        """
        src = self.source([('A-%d' % i, '2026-09-%02d' % (4 + i), 'locked')
                           for i in range(7)],
                          facts_updated='2026-09-14T02:00:00+00:00')
        cutoff = src.data_cutoff('store_alpha')
        self.assertEqual(date(2026, 9, 10), cutoff.window_end)
        self.assertEqual(date(2026, 9, 14), cutoff.cutoff.date(),
                         '写入时间确实被刷新到了 09-14')

        def count(lo, hi):
            return src._conn.execute(
                "SELECT count(*) FROM settlement_snapshots "
                "WHERE state='locked' AND settlement_date BETWEEN ? AND ?",
                (lo.isoformat(), hi.isoformat())).fetchone()[0]

        # 用 window_end：7 天窗口，7 天都有数据
        self.assertEqual(7, count(date(2026, 9, 4), cutoff.window_end))
        # 用写入时间：同样叫「7 天窗口」，只有 3 天有数据
        self.assertEqual(3, count(date(2026, 9, 8), cutoff.cutoff.date()))

    def test_days_window_derived_from_window_end_is_not_empty(self):
        """光有边界还不够：按它取窗口必须真的取到数（事故的直接症状）。"""
        src = self.source([('A-%d' % i, '2026-09-%02d' % (4 + i), 'locked')
                           for i in range(7)])
        end = src.data_cutoff('store_alpha').window_end
        self.assertEqual(date(2026, 9, 10), end)
        # 与 api/dashboard.py 的算法逐字一致：左端 = 右端 − (days − 1)
        start = end - timedelta(days=7 - 1)
        self.assertEqual(date(2026, 9, 4), start)

        def count(lo, hi):
            return src._conn.execute(
                "SELECT count(*) FROM settlement_snapshots "
                "WHERE state='locked' AND settlement_date BETWEEN ? AND ?",
                (lo.isoformat(), hi.isoformat())).fetchone()[0]

        self.assertEqual(7, count(start, end), '右端取对了，7 天窗口就该有 7 天数据')
        # 反证：右端若是 09-19（事故时的行为），同一个窗口一天数据都没有
        self.assertEqual(0, count(date(2026, 9, 13), date(2026, 9, 19)))

    def test_pending_settlements_do_not_extend_window(self):
        """未锁定（pending/draft）的快照不算已结算 —— 不能把窗口拉长到未来。

        与 `orders_for_period` 的 `state='locked'` 过滤是同一个口径：
        两处任一处改了，这条测试就会失败。
        """
        src = self.source([('A-1', '2026-09-08', 'locked'),
                           ('A-2', '2026-09-30', 'pending'),
                           ('A-3', '2026-12-31', 'draft')])
        self.assertEqual(date(2026, 9, 8), src.data_cutoff('store_alpha').window_end)

    def test_window_end_is_none_when_nothing_is_locked(self):
        """一单都没结算 → 给不出窗口右端，让调用方决定怎么兜底。"""
        src = self.source([('A-2', '2026-09-30', 'pending')])
        cutoff = src.data_cutoff('store_alpha')
        self.assertIsNone(cutoff.window_end)
        self.assertIsNotNone(cutoff.cutoff, '写入时间仍然要能显示出来')

    def test_missing_finance_tables_fall_back_and_say_so(self):
        """财务表读不到时退回运营时间，但 source 必须带 observed: 前缀。

        不允许把运营时间伪装成财务时间 —— 那种「悄悄降级」正是事故的成因。
        """
        src = self.source([], drop_finance_tables=True)
        cutoff = src.data_cutoff('store_alpha')
        self.assertTrue(cutoff.source.startswith('observed:'),
                        '退回运营表时 source 必须自曝: %r' % cutoff.source)
        self.assertIsNotNone(cutoff.cutoff)
        self.assertIsNone(cutoff.window_end)

    def test_sync_runs_uses_the_real_column_name(self):
        """`sync_runs` 里没有 finished_at 这一列 —— 曾经写成它，静默失效。

        `_scalar` 会吞掉 OperationalError 返回 None，所以列名写错**不会报错**，
        只会让候选少一个。这条断言把列名钉死。
        """
        src = self.source([('A-1', '2026-09-10', 'locked')])
        cands = src.data_cutoff('store_alpha').candidates
        self.assertIn('sync_runs.recorded_at', cands)
        self.assertNotIn('sync_runs.finished_at', cands)
        self.assertEqual(SYNC_RECORDED, cands['sync_runs.recorded_at'])

    def test_parse_day(self):
        self.assertEqual(date(2026, 9, 10), _parse_day('2026-09-10'))
        self.assertEqual(date(2026, 9, 10), _parse_day('2026-09-10 12:00:00'))
        for bad in (None, '', '   ', 'not-a-date', '2026-13-40', '2026-09'):
            self.assertIsNone(_parse_day(bad), bad)


if __name__ == '__main__':
    unittest.main()
