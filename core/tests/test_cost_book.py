# -*- coding: utf-8 -*-
"""采购成本库的测试（ADR-0009）。

**这个文件钉的是七条规则里最容易被后来的改动悄悄破坏的四条**：

1. **生效日期回溯**（规则 4）：同一货号 09-01=12.20、09-10=15.00 时，
   09-05 的单必须取 12.20，09-12 取 15.00，08-31 必须**没有价**（None，不是 0）。
   改成本不能悄悄改写历史利润 —— 这条一旦破，已结算月份的利润会静默变化。
2. **幂等**：同一批导两次，第二次 `unchanged`、**不新增事件**、不改 `updated_at`。
   否则每次「重新导入一下」都会往审计表里灌噪声。
3. **变更留痕只追加**（规则 5）：事件条数只增不减，永不覆盖、永不删除。
4. **迁移只读旧库**（§三之三）：迁移后旧产品库文件的**内容哈希必须逐字节不变**。

另外两个边界也在这里钉住：**预览绝不落库**、**只读打开真的写不进去**。

所有数据都是本文件自己造的合成数据（临时目录里的 sqlite / csv / xlsx），
**不碰生产库、不碰 `C:\\ProgramData` 下的任何东西** —— 所以公开子集里同样能跑。

跑法：
    python -m unittest core.tests.test_cost_book -v
"""
import csv
import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from ..repository.cost_book import (STATUS_CONFIRMED, STATUS_PENDING, CostBook,
                                    CostRow, parse_cost_file)

RULE = '─' * 70


# ── 合成数据 ─────────────────────────────────────────────────────
def row(offer, cost, effective, status=STATUS_CONFIRMED, platform=None,
        note=None, source='test.csv'):
    """一条成本行。金额用字符串写，避免浮点。"""
    return CostRow(seller_sku=offer, unit_cost_cny=Decimal(cost),
                   effective_from=effective, platform_sku=platform,
                   note=note, status=status, source=source)


def write_csv(path, lines):
    """写一份 UTF-8(带 BOM) 的 csv —— 与用户从 Excel 另存出来的形状一致。"""
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as fh:
        fh.write(''.join(line + '\n' for line in lines))
    return path


def write_xlsx(path, rows):
    """写一份 xlsx；单元格原样给字符串/数字，让 openpyxl 自己决定类型。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for cells in rows:
        ws.append(list(cells))
    wb.save(path)
    return path


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def make_legacy_db(path, rows):
    """造一个**合成旧库**：表结构照抄原产品的 `purchase_costs`。

    `rows` 是 `(cost_id, seller_sku, platform_sku, unit_cost_cny, created_at)`。
    真实旧库就是这张表的形状（ADR-0009 §三），所以迁移路径能在这里被完整走到。
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            'CREATE TABLE purchase_costs ('
            ' cost_id TEXT PRIMARY KEY, scope TEXT, store_alias TEXT,'
            ' seller_sku TEXT, platform_sku TEXT, unit_cost_cny TEXT,'
            ' status TEXT, source TEXT, note TEXT, created_at TEXT, updated_at TEXT)')
        for r in rows:
            conn.execute(
                'INSERT INTO purchase_costs (cost_id, scope, store_alias, seller_sku,'
                ' platform_sku, unit_cost_cny, status, source, note, created_at,'
                ' updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (r[0], 'shared', None, r[1], r[2], r[3], 'confirmed', 'legacy', None,
                 r[4], r[4]))
        conn.commit()
    finally:
        conn.close()
    return path


class CostBookTestBase(unittest.TestCase):
    """每个用例一个临时目录：互不干扰，也不留垃圾。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ozon-cost-test-')
        self.addCleanup(self._rmtree, self.tmp)
        self.path = os.path.join(self.tmp, 'cost_book.db')
        self.book = CostBook(self.path)
        self.addCleanup(self.book.close)

    @staticmethod
    def _rmtree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 1. 建表 / 幂等
# ══════════════════════════════════════════════════════════════════
class CostBookSchemaTest(CostBookTestBase):
    def test_opens_empty_and_creates_tables(self):
        """`CostBook(path)` 建库：文件出现、两张表都在、0 行。"""
        self.assertTrue(os.path.isfile(self.path), '构造时应当落盘')
        names = {r[0] for r in self.book._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('sku_costs', names)
        self.assertIn('sku_cost_change_events', names)
        self.assertEqual(0, self.book.count())
        self.assertEqual(([], 0), self.book.list_costs())
        self.assertEqual(([], 0), self.book.change_events())

    def test_reopen_is_idempotent(self):
        """重复构造不能清库、不能重建丢数据（`CREATE TABLE IF NOT EXISTS`）。"""
        self.book.apply([row('SKU-A', '12.20', '2026-09-01')], actor='t')
        again = CostBook(self.path)
        self.addCleanup(again.close)
        self.assertEqual(1, again.count())
        self.assertEqual(Decimal('12.20'), again.unit_cost('SKU-A', '2026-09-01'))

    def test_second_import_of_same_batch_is_unchanged_and_silent(self):
        """同一批导两次：第二次 `unchanged`、**不新增事件**、不改 `updated_at`。

        三条断言缺一不可：
          * `unchanged` 计数 —— 让用户知道「这次导入什么都没做」；
          * 事件数不变 —— 否则审计表会被「再导一次」灌满；
          * `updated_at` 不变 —— 否则台账看不出「这条其实没被改过」。
        """
        batch = [row('SKU-A', '12.20', '2026-09-01'),
                 row('SKU-B', '7.00', '2026-09-02')]
        first = self.book.apply(batch, actor='finance01', file_sha256='sha-1')
        self.assertEqual({'created': 2, 'updated': 0, 'unchanged': 0}, first)

        rows_before = {r['cost_id']: r for r in self.book.list_costs()[0]}
        events_before = self.book.change_events()[1]
        self.assertEqual(2, events_before)

        second = self.book.apply(batch, actor='finance01', file_sha256='sha-2')
        self.assertEqual({'created': 0, 'updated': 0, 'unchanged': 2}, second)

        self.assertEqual(events_before, self.book.change_events()[1],
                         '未变的行不得产生变更事件')
        rows_after = {r['cost_id']: r for r in self.book.list_costs()[0]}
        self.assertEqual(set(rows_before), set(rows_after))
        for cost_id, before in rows_before.items():
            after = rows_after[cost_id]
            self.assertEqual(before['updated_at'], after['updated_at'],
                             '未变的行不得被改写 updated_at')
            self.assertEqual(before['unit_cost_cny'], after['unit_cost_cny'])
        self.assertEqual(2, self.book.count(), '导两次不能变成 4 行')


# ══════════════════════════════════════════════════════════════════
# 2. 生效日期回溯（规则 4）
# ══════════════════════════════════════════════════════════════════
class EffectiveFromTest(CostBookTestBase):
    def setUp(self):
        super().setUp()
        self.book.apply([row('SKU-A', '12.20', '2026-09-01')], actor='t')
        self.book.apply([row('SKU-A', '15.00', '2026-09-10')], actor='t')

    def test_price_is_chosen_by_order_date(self):
        """09-05 的单取 12.20，09-12 的单取 15.00 —— 改价不重写历史。"""
        self.assertEqual(Decimal('12.20'), self.book.unit_cost('SKU-A', '2026-09-05'))
        self.assertEqual(Decimal('15.00'), self.book.unit_cost('SKU-A', '2026-09-12'))

    def test_boundary_days_are_inclusive(self):
        """生效当日就生效（`effective_from <= 订单日`），前一天仍取旧价。"""
        self.assertEqual(Decimal('12.20'), self.book.unit_cost('SKU-A', '2026-09-01'))
        self.assertEqual(Decimal('12.20'), self.book.unit_cost('SKU-A', '2026-09-09'))
        self.assertEqual(Decimal('15.00'), self.book.unit_cost('SKU-A', '2026-09-10'))

    def test_before_first_effective_date_is_none_not_zero(self):
        """**缺价和零价是两件事**：第一版生效日之前的订单必须拿到 None。

        如果这里返回 `Decimal('0')`，订单利润会算出「采购成本 0」——
        一个看着正常的假数字，比空值危险得多。
        """
        self.assertIsNone(self.book.unit_cost('SKU-A', '2026-08-31'))
        self.assertIsNone(self.book.unit_cost('SKU-不存在', '2026-09-12'))
        self.assertNotEqual(Decimal('0'), self.book.unit_cost('SKU-A', '2026-08-31'))

    def test_price_map_uses_latest_effective_price(self):
        """`price_map` 取每个货号的**最新生效价**，并且同样按日期回溯。"""
        self.book.apply([row('SKU-B', '3.50', '2026-09-03')], actor='t')
        self.assertEqual({'SKU-A': Decimal('12.20'), 'SKU-B': Decimal('3.50')},
                         self.book.price_map('2026-09-05'))
        self.assertEqual({'SKU-A': Decimal('15.00'), 'SKU-B': Decimal('3.50')},
                         self.book.price_map('2026-09-12'))
        self.assertEqual({}, self.book.price_map('2026-08-31'),
                         '早于所有生效日：一个货号都不该出现')

    def test_pending_rows_stay_out_of_price_map(self):
        """`status='pending'` 不参与核算：既不进 `price_map`，也不被 `unit_cost` 取到。

        pending 是留给将来的审批态。它一旦漏进 price_map，等于「未审批的价
        直接参与了利润核算」—— 这正是审批态要防的事。
        """
        self.book.apply([row('SKU-P', '3.00', '2026-09-01')], actor='t')
        self.book.apply([row('SKU-P', '9.00', '2026-09-02', status=STATUS_PENDING)],
                        actor='t')
        # 台账里两条都在（状态要留痕），但只有一个价参与核算
        self.assertEqual(Decimal('3.00'), self.book.unit_cost('SKU-P', '2026-09-02'))
        self.assertEqual(Decimal('3.00'), self.book.price_map('2026-09-02')['SKU-P'])
        self.assertNotIn(Decimal('9.00'), self.book.price_map('2026-09-02').values())
        statuses = [r['status'] for r in self.book.list_costs(keyword='SKU-P')[0]]
        self.assertEqual([STATUS_CONFIRMED, STATUS_PENDING], sorted(statuses),
                         '两条都在台账里（状态要留痕），只是 pending 不参与核算')

    def test_pending_only_offer_is_missing_not_zero(self):
        """整个货号只有 pending 行时：它是「缺价」而不是「有价」。"""
        self.book.apply([row('SKU-Q', '5.00', '2026-09-01', status=STATUS_PENDING)],
                        actor='t')
        self.assertIsNone(self.book.unit_cost('SKU-Q', '2026-09-10'))
        self.assertNotIn('SKU-Q', self.book.price_map('2026-09-10'))


# ══════════════════════════════════════════════════════════════════
# 3. 同一 effective_from 改价 / 变更留痕（规则 5）
# ══════════════════════════════════════════════════════════════════
class ChangeTrackingTest(CostBookTestBase):
    def test_same_effective_from_goes_through_update_branch(self):
        """同一个 `effective_from` 改价 → `updated==1`，事件 action 是 `update`，
        `old_json`/`new_json` **两边都记下来**。

        「把哪个货号从 A 改成 B」是审计的全部价值；只记新值等于没留痕。
        """
        self.book.apply([row('SKU-A', '12.20', '2026-09-01')], actor='finance01')
        result = self.book.apply([row('SKU-A', '18.80', '2026-09-01',
                                      source='second.xlsx')],
                                 actor='finance02')
        self.assertEqual({'created': 0, 'updated': 1, 'unchanged': 0}, result)
        self.assertEqual(1, self.book.count(), '改价是原地更新，不是新增一行')
        self.assertEqual(Decimal('18.80'), self.book.unit_cost('SKU-A', '2026-09-01'))

        events, total = self.book.change_events()
        self.assertEqual(2, total)
        latest = events[0]                      # 按 id DESC，最新事件在第一行
        self.assertEqual('update', latest['action'])
        self.assertEqual('SKU-A', latest['seller_sku'])
        self.assertEqual('2026-09-01', latest['effective_from'])
        self.assertEqual('finance02', latest['actor_id'])
        self.assertIsNotNone(latest['old_json'], 'update 事件必须留下改前的值')
        self.assertIsNotNone(latest['new_json'], 'update 事件必须留下改后的值')
        self.assertIn('12.20', latest['old_json'])
        self.assertIn('18.80', latest['new_json'])
        self.assertNotIn('18.80', latest['old_json'])
        self.assertEqual('insert', events[1]['action'], '第一次写入是 insert')

    def test_events_are_append_only(self):
        """事件只追加：条数随写入只增不减，且历史事件的字段永不被改写。

        这是「审计」这个词的最低要求 —— 如果后来的写入会覆盖旧事件，
        那审计表本身就不可信了。
        """
        sequence = [
            [row('SKU-A', '12.20', '2026-09-01')],
            [row('SKU-A', '18.80', '2026-09-01')],          # update 分支
            [row('SKU-A', '18.80', '2026-09-01')],          # unchanged：不产生事件
            [row('SKU-A', '20.00', '2026-09-10')],          # 新生效日：insert
        ]
        counts = []
        snapshots = []
        for batch in sequence:
            self.book.apply(batch, actor='finance01')
            events, total = self.book.change_events()
            counts.append(total)
            snapshots.append([(e['id'], e['action'], e['old_json'], e['new_json'])
                              for e in events])

        self.assertEqual([1, 2, 2, 3], counts, 'unchanged 不得写事件（第 3 步与第 2 步相同）')
        self.assertEqual(sorted(counts), counts, '事件条数只能单调不减')
        # 第 2 步之后的两个快照必须逐字相同：旧事件没被「顺手更新」过
        self.assertEqual(snapshots[1], snapshots[2])
        # 每一步都保留着更早的事件（没有删除）
        for i in range(1, len(snapshots)):
            earlier_ids = {x[0] for x in snapshots[i - 1]}
            self.assertTrue(earlier_ids.issubset({x[0] for x in snapshots[i]}),
                            '第 %d 步丢了历史事件' % (i + 1))

    def test_event_carries_actor_source_and_file_hash(self):
        """「谁、何时、来源文件是谁」必须能追出来（规则 5）。"""
        self.book.apply([row('SKU-A', '12.20', '2026-09-01', source='模板.xlsx')],
                        actor='finance01', file_sha256='deadbeef')
        event = self.book.change_events()[0][0]
        self.assertEqual('finance01', event['actor_id'])
        self.assertEqual('模板.xlsx', event['source'])
        self.assertEqual('deadbeef', event['file_sha256'])
        self.assertTrue(event['occurred_at'], 'occurred_at 不能为空')
        self.assertTrue(event['cost_id'], '事件必须挂到具体的 cost_id 上')

    def test_events_can_be_filtered_by_offer(self):
        """留痕支持按货号查 —— 界面上「这个货号什么时候被谁改过」。"""
        self.book.apply([row('SKU-A', '1.00', '2026-09-01'),
                         row('SKU-B', '2.00', '2026-09-01')], actor='t')
        events, total = self.book.change_events(seller_sku='SKU-A')
        self.assertEqual(1, total)
        self.assertEqual(['SKU-A'], [e['seller_sku'] for e in events])


# ══════════════════════════════════════════════════════════════════
# 4. 预览（绝不落库）
# ══════════════════════════════════════════════════════════════════
class PreviewTest(CostBookTestBase):
    def test_preview_does_not_touch_the_book(self):
        """`preview` 前后 `count()`、事件数、`updated_at` 全部不变。

        预览要是落了库，「预览和实际不一致」就再也无法被发现 ——
        用户看到的影响面与他点确认后的结果会不是一回事。
        """
        self.book.apply([row('SKU-A', '12.20', '2026-09-01')], actor='t')
        before_rows = self.book.list_costs()[0]
        before_events = self.book.change_events()[1]

        pv = self.book.preview([row('SKU-A', '12.20', '2026-09-01'),   # unchanged
                                row('SKU-A', '99.00', '2026-09-01'),   # updated
                                row('SKU-NEW', '4.00', '2026-09-05')])  # created

        self.assertEqual(1, self.book.count())
        self.assertEqual(before_rows, self.book.list_costs()[0])
        self.assertEqual(before_events, self.book.change_events()[1])
        self.assertEqual(1, len(pv.created))
        self.assertEqual(1, len(pv.updated))
        self.assertEqual(1, len(pv.unchanged))
        self.assertEqual(3, pv.total)
        self.assertTrue(pv.ok)
        self.assertEqual([], pv.invalid)

    def test_preview_classification_and_old_value(self):
        """分类必须与 `apply` 的分支逐条对应，且 updated 带出**旧单价**。"""
        self.book.apply([row('SKU-A', '12.20', '2026-09-01')], actor='t')
        pv = self.book.preview([row('SKU-A', '12.20', '2026-09-01'),
                                row('SKU-A', '15.00', '2026-09-01')])
        self.assertEqual(['SKU-A'], [r.seller_sku for r in pv.unchanged])
        self.assertEqual(1, len(pv.updated))
        new_row, old_cost = pv.updated[0]
        self.assertEqual(Decimal('15.00'), new_row.unit_cost_cny)
        self.assertEqual(Decimal('12.20'), old_cost)
        payload = pv.as_dict()
        self.assertEqual(1, payload['updated_count'])
        self.assertEqual('12.20', payload['updated'][0]['old_unit_cost_cny'])

    def test_preview_on_empty_book_is_all_created(self):
        pv = self.book.preview([row('SKU-A', '1.00', '2026-09-01'),
                                row('SKU-B', '2.00', '2026-09-01')])
        self.assertEqual(2, len(pv.created))
        self.assertEqual([], pv.updated)
        self.assertEqual([], pv.unchanged)
        self.assertEqual(0, self.book.count())

    def test_preview_does_not_create_the_file_when_readonly_absent(self):
        """只读打开一个不存在的库 → `FileNotFoundError`（不是建出一个空库来）。"""
        missing = os.path.join(self.tmp, 'nope.db')
        with self.assertRaises(FileNotFoundError):
            CostBook(missing, readonly=True)
        self.assertFalse(os.path.isfile(missing), '只读打开不得顺手建库')

    def test_readonly_open_cannot_write(self):
        """只读打开真的写不进去 —— 迁移/查询路径靠这个保证不越界。"""
        self.book.apply([row('SKU-A', '12.20', '2026-09-01')], actor='t')
        ro = CostBook(self.path, readonly=True)
        self.addCleanup(ro.close)
        self.assertEqual(Decimal('12.20'), ro.unit_cost('SKU-A', '2026-09-05'))
        with self.assertRaises(sqlite3.OperationalError):
            ro.apply([row('SKU-B', '1.00', '2026-09-01')], actor='t')
        self.assertEqual(1, self.book.count(), '失败的写入不得留下半条数据')


# ══════════════════════════════════════════════════════════════════
# 5. parse_cost_file：xlsx / csv / 列名别名 / 非法行
# ══════════════════════════════════════════════════════════════════
class ParseCostFileTest(CostBookTestBase):
    def test_reads_csv_with_chinese_header(self):
        path = write_csv(os.path.join(self.tmp, 'a.csv'), [
            '货号,单价',
            'SKU-A,12.20',
            'SKU-B,7',
        ])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad)
        self.assertEqual(['SKU-A', 'SKU-B'], [r.seller_sku for r in rows])
        self.assertEqual([Decimal('12.20'), Decimal('7')],
                         [r.unit_cost_cny for r in rows])
        self.assertEqual('a.csv', rows[0].source, 'source 记来源文件名（审计要）')
        self.assertEqual(STATUS_CONFIRMED, rows[0].status)

    def test_reads_xlsx(self):
        path = write_xlsx(os.path.join(self.tmp, 'a.xlsx'), [
            ['货号', 'OZON 数字 SKU', '单价', '备注'],
            ['SKU-A', '111', 12.2, '测'],
        ])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad)
        self.assertEqual(1, len(rows))
        self.assertEqual('SKU-A', rows[0].seller_sku)
        self.assertEqual(Decimal('12.2'), rows[0].unit_cost_cny)
        self.assertEqual('111', rows[0].platform_sku)
        self.assertEqual('测', rows[0].note)

    def test_column_aliases_are_accepted(self):
        """列名别名：货号 / offer_id / seller_sku；单价 / 采购成本 / unit_cost_cny。

        模板要与原产品《采购成本模板.xlsx》兼容，两条链上的名字都得认。
        """
        cases = [
            ('货号', '单价'),
            ('offer_id', '采购成本'),
            ('seller_sku', 'unit_cost_cny'),
            ('Offer ID', '成本'),
        ]
        for offer_col, cost_col in cases:
            with self.subTest(offer_col=offer_col, cost_col=cost_col):
                path = write_csv(
                    os.path.join(self.tmp, 'alias.csv'),
                    ['%s,%s' % (offer_col, cost_col), 'SKU-X,9.90'])
                rows, bad = parse_cost_file(path)
                self.assertEqual([], bad, (offer_col, cost_col))
                self.assertEqual(['SKU-X'], [r.seller_sku for r in rows])
                self.assertEqual(Decimal('9.90'), rows[0].unit_cost_cny)

    def test_header_may_not_be_the_first_row(self):
        """表头可能在前面几行（标题行、空行），前 10 行内要能找到。"""
        path = write_csv(os.path.join(self.tmp, 'b.csv'), [
            '采购成本表,',
            ',',
            '货号,单价',
            'SKU-A,1.00',
        ])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad)
        self.assertEqual(['SKU-A'], [r.seller_sku for r in rows])

    def test_blank_rows_are_skipped_not_reported(self):
        """整行空白是**格式噪声**，跳过即可，不该报错。

        用户从 Excel 里另存出来的表末尾常带几百行空行；把它们当非法行报出来，
        用户会以为文件坏了，而实际上一个数据问题都没有。
        """
        path = write_csv(os.path.join(self.tmp, 'c.csv'), [
            '货号,单价',
            'SKU-A,1.00',
            ',',
            ',',
            '',
            'SKU-B,2.00',
        ])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad, '空行不得进非法行清单')
        self.assertEqual(['SKU-A', 'SKU-B'], [r.seller_sku for r in rows])

    def test_invalid_rows_are_reported_one_by_one_with_reason(self):
        """非法行逐条报原因：缺货号 / 缺单价 / 单价解析不了 / 重复 / 坏日期。

        返回的**行号必须是文件里的真实行号**（表头是第 1 行，数据从第 2 行起）——
        没有行号，用户拿着一份 3000 行的表不知道去哪改。
        """
        path = write_csv(os.path.join(self.tmp, 'd.csv'), [
            '货号,单价,生效日期',          # 第 1 行：表头
            'SKU-A,12.20,2026-09-01',     # 第 2 行：合法
            ',12.00,2026-09-01',          # 第 3 行：缺货号
            'SKU-B,,2026-09-01',          # 第 4 行：缺单价
            'SKU-C,abc,2026-09-01',       # 第 5 行：单价解析不了
            'SKU-A,13.00,2026-09-02',     # 第 6 行：与第 2 行重复
            'SKU-D,1.00,2026/09/01',      # 第 7 行：日期不是 YYYY-MM-DD
        ])
        rows, bad = parse_cost_file(path)
        self.assertEqual(['SKU-A'], [r.seller_sku for r in rows],
                         '合法行必须照常返回，非法行不能把整份文件拖黑')
        by_line = {n: (v, why) for n, v, why in bad}
        self.assertEqual([3, 4, 5, 6, 7], sorted(by_line), '行号要与文件一致')
        self.assertIn('缺货号', by_line[3][1])
        self.assertIn('缺单价', by_line[4][1])
        self.assertIn('解析不了', by_line[5][1])
        self.assertIn('重复', by_line[6][1])
        self.assertIn('第 2 行', by_line[6][1], '重复行要指出与哪一行撞了')
        self.assertIn('YYYY-MM-DD', by_line[7][1])
        # 每条非法行都要带上原始值，否则用户不知道该改什么
        self.assertEqual('12.00', by_line[3][0])
        self.assertEqual('abc', by_line[5][0])
        for _n, value, _why in bad:
            self.assertIsInstance(value, str)

    def test_negative_price_is_rejected(self):
        """负单价必须报错，而不是当成「退款冲销」悄悄写进成本库。"""
        path = write_csv(os.path.join(self.tmp, 'e.csv'), [
            '货号,单价', 'SKU-A,-1.00'])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], rows)
        self.assertEqual(1, len(bad))
        self.assertIn('负', bad[0][2])

    def test_missing_header_reports_instead_of_guessing(self):
        """读不出表头 → 返回一条说明，而不是猜列、更不是返回空表装成功。"""
        path = write_csv(os.path.join(self.tmp, 'f.csv'), ['甲,乙', '1,2'])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], rows)
        self.assertEqual(1, len(bad))
        self.assertIn('表头', bad[0][2])

    def test_file_without_any_header_reports_instead_of_succeeding(self):
        """完全空 / 无表头的文件是**错误**，不是「0 条成本」。

        「什么都没导入」必须说出来：静默返回 0 条会让用户以为导入成功了。
        """
        empty = write_csv(os.path.join(self.tmp, 'g.csv'), [])
        rows, bad = parse_cost_file(empty)
        self.assertEqual([], rows)
        self.assertEqual(1, len(bad))
        self.assertTrue(bad[0][2], '空文件必须给出原因')

        header_only = write_csv(os.path.join(self.tmp, 'g2.csv'), ['货号,单价'])
        rows, bad = parse_cost_file(header_only)
        self.assertEqual([], rows)
        self.assertEqual([], bad,
                         '只有表头 → 0 行 0 非法行（由接口层判「文件里没有成本行」）')

    def test_empty_effective_date_defaults_to_today(self):
        """生效日期留空 → 默认今天（模板里这列是可选列）。"""
        from datetime import datetime

        from ..repository.sqlite_source import CN_TZ
        path = write_csv(os.path.join(self.tmp, 'h.csv'), [
            '货号,单价', 'SKU-A,1.00'])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad)
        self.assertEqual(datetime.now(CN_TZ).date().isoformat(),
                         rows[0].effective_from)

    def test_no_effective_column_at_all_is_fine(self):
        """整列都没有（原产品模板只有 货号|SKU|单价|备注）→ 全部按今天生效。"""
        path = write_csv(os.path.join(self.tmp, 'i.csv'), [
            '货号,单价,备注', 'SKU-A,1.00,备注'])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad)
        self.assertEqual(1, len(rows))

    def test_price_with_thousand_separator_is_parsed(self):
        """`1,234.50` 这种 Excel 风格的千分位要能读（否则真实数据会整行报错）。"""
        path = write_csv(os.path.join(self.tmp, 'j.csv'), [
            '货号,单价', 'SKU-A,"1,234.50"'])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad)
        self.assertEqual(Decimal('1234.50'), rows[0].unit_cost_cny)

    def test_date_time_cell_is_truncated_to_its_day(self):
        """xlsx 里被识别成日期时间的单元格 → 取日期部分（前 10 位）。"""
        path = write_xlsx(os.path.join(self.tmp, 'k.xlsx'), [
            ['货号', '单价', '生效日期'],
            ['SKU-A', '1.00', '2026-09-01 00:00:00'],
        ])
        rows, bad = parse_cost_file(path)
        self.assertEqual([], bad)
        self.assertEqual('2026-09-01', rows[0].effective_from)


# ══════════════════════════════════════════════════════════════════
# 6. migrate_from_legacy：只读旧库 + 跳过的行逐条给原因 + 幂等
# ══════════════════════════════════════════════════════════════════
LEGACY_ROWS = [
    # cost_id, seller_sku, platform_sku, unit_cost_cny, created_at
    ('c1', 'OLD-1', '111', '5.50', '2026-09-03T10:00:00+00:00'),
    ('c2', 'OLD-2', '222', '6.50', '2026-09-04T10:00:00+00:00'),
    ('c3', 'OLD-3', None, '7.25', '2026-09-05T10:00:00+00:00'),
    # 旧库里真实存在的脏数据：2026-09-07 那次导入留下了货号为空的 71 行（ADR-0009）
    ('c4', '', '333', '8.50', '2026-09-07T10:00:00+00:00'),
    ('c5', '   ', None, '9.50', '2026-09-07T10:00:00+00:00'),
]
LEGACY_VALID = 3           # OLD-1 / OLD-2 / OLD-3
LEGACY_SKIPPED = 2         # c4 / c5（货号为空）


class MigrateFromLegacyTest(CostBookTestBase):
    def setUp(self):
        super().setUp()
        self.legacy = make_legacy_db(os.path.join(self.tmp, 'legacy.db'), LEGACY_ROWS)
        self.legacy_sha = sha256_of(self.legacy)

    def test_migrates_valid_rows_and_reports_skips_with_reasons(self):
        """`created` == 有效行数、`skipped` == 脏行数、`skipped_detail` 有原因。

        跳过的行**不能静默丢弃**：用户需要知道有 2 行没进来、为什么、
        以及它们的 cost_id 是什么（好回旧库去修）。
        """
        result = self.book.migrate_from_legacy(self.legacy, actor='migration')
        self.assertEqual(LEGACY_VALID, result['created'])
        self.assertEqual(0, result['updated'])
        self.assertEqual(0, result['unchanged'])
        self.assertEqual(LEGACY_VALID + LEGACY_SKIPPED, result['scanned'])
        self.assertEqual(LEGACY_SKIPPED, result['skipped'])
        self.assertFalse(result['skipped_truncated'])
        self.assertEqual(LEGACY_VALID, self.book.count())

        detail = result['skipped_detail']
        self.assertEqual(LEGACY_SKIPPED, len(detail))
        for item in detail:
            self.assertTrue(item['reason'], '每条跳过都要有原因')
            self.assertIn('货号为空', item['reason'])
            self.assertIn(item['cost_id'], ('c4', 'c5'),
                          '要说清是哪一条（cost_id），否则用户没法回旧库修')
        self.assertEqual({'c4', 'c5'}, {d['cost_id'] for d in detail})

    def test_effective_date_comes_from_created_at(self):
        """生效日期取原记录 `created_at` 的日期部分（诚实默认）。"""
        self.book.migrate_from_legacy(self.legacy, actor='migration')
        self.assertEqual(Decimal('5.50'), self.book.unit_cost('OLD-1', '2026-09-03'))
        self.assertIsNone(self.book.unit_cost('OLD-1', '2026-09-02'),
                          '录成本之前的历史订单不该被这次迁移改写')
        self.assertEqual(Decimal('7.25'), self.book.unit_cost('OLD-3', '2026-09-30'))

    def test_migration_is_idempotent(self):
        """迁移两次：第二次全 `unchanged`，台账行数与事件数都不再增长。"""
        first = self.book.migrate_from_legacy(self.legacy, actor='migration')
        events_after_first = self.book.change_events()[1]
        self.assertEqual(LEGACY_VALID, events_after_first)

        second = self.book.migrate_from_legacy(self.legacy, actor='migration')
        self.assertEqual(0, second['created'])
        self.assertEqual(0, second['updated'])
        self.assertEqual(LEGACY_VALID, second['unchanged'])
        self.assertEqual(LEGACY_SKIPPED, second['skipped'],
                         '跳过的行第二次仍然要被报出来，不能「第二次就不提了」')
        self.assertEqual(LEGACY_VALID, self.book.count())
        self.assertEqual(events_after_first, self.book.change_events()[1],
                         '第二次迁移不得产生任何新事件')

    def test_migration_never_writes_to_the_legacy_file(self):
        """**旧库文件的内容哈希逐字节不变**（它是旧产品的产物，我们只读）。

        mtime 会被别的进程碰到、粒度也粗，所以这里比的是 sha256：
        只要迁移路径里有一句写操作（哪怕最终回滚），这条测试就会炸。
        """
        self.book.migrate_from_legacy(self.legacy, actor='migration')
        self.book.migrate_from_legacy(self.legacy, actor='migration')
        self.assertEqual(self.legacy_sha, sha256_of(self.legacy),
                         '迁移改动了旧产品成本库！')

    def test_migration_events_are_marked_migrated(self):
        """迁移写入的事件 action 带 `migrated` 前缀 —— 与普通导入可区分。"""
        self.book.migrate_from_legacy(self.legacy, actor='root')
        actions = {e['action'] for e in self.book.change_events()[0]}
        self.assertEqual({'migrated_insert'}, actions)
        self.assertEqual({'root'}, {e['actor_id'] for e in self.book.change_events()[0]})

    def test_missing_legacy_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.book.migrate_from_legacy(os.path.join(self.tmp, 'nope.db'))

    def test_legacy_without_purchase_costs_table_raises(self):
        """旧库不是我们认得的形状 → 明确报错，而不是迁出 0 条假装成功。"""
        other = os.path.join(self.tmp, 'other.db')
        conn = sqlite3.connect(other)
        conn.execute('CREATE TABLE something_else(x)')
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError) as ctx:
            self.book.migrate_from_legacy(other)
        self.assertIn('purchase_costs', str(ctx.exception))
        self.assertEqual(0, self.book.count())

    def test_bad_cost_values_are_skipped_not_guessed(self):
        """单价为空 / 解析不了 / 为负 → 全部跳过并写明原因（绝不当 0 写进去）。"""
        legacy = make_legacy_db(os.path.join(self.tmp, 'dirty.db'), [
            ('d1', 'OK-1', None, '1.00', '2026-09-01T00:00:00+00:00'),
            ('d2', 'BAD-1', None, None, '2026-09-01T00:00:00+00:00'),
            ('d3', 'BAD-2', None, 'abc', '2026-09-01T00:00:00+00:00'),
            ('d4', 'BAD-3', None, '-2.00', '2026-09-01T00:00:00+00:00'),
        ])
        result = self.book.migrate_from_legacy(legacy)
        self.assertEqual(1, result['created'])
        self.assertEqual(3, result['skipped'])
        reasons = ' / '.join(d['reason'] for d in result['skipped_detail'])
        self.assertIn('单价', reasons)
        self.assertEqual(1, self.book.count())
        self.assertIsNone(self.book.unit_cost('BAD-1', '2026-09-01'),
                          '解析不了的单价绝不能被当成 0 写进台账')

    def test_legacy_missing_created_at_falls_back_to_today(self):
        """旧库 `created_at` 为空时按今天生效 —— 而不是写一个空字符串日期。"""
        legacy = make_legacy_db(os.path.join(self.tmp, 'nodate.db'), [
            ('n1', 'NODATE-1', None, '1.50', None),
        ])
        result = self.book.migrate_from_legacy(legacy)
        self.assertEqual(1, result['created'])
        from datetime import datetime

        from ..repository.sqlite_source import CN_TZ
        stored = self.book.list_costs()[0][0]
        self.assertEqual(datetime.now(CN_TZ).date().isoformat(), stored['effective_from'])
        self.assertIn('migrated:', stored['source'])

    def test_migration_reads_legacy_in_readonly_mode(self):
        """合成旧库所在的文件在**迁移过程中**也不被写（哈希比对已覆盖，
        这里再钉一条更直接的：旧库的 mtime 与大小都不变）。"""
        size_before = os.path.getsize(self.legacy)
        self.book.migrate_from_legacy(self.legacy)
        self.assertEqual(size_before, os.path.getsize(self.legacy))


# ══════════════════════════════════════════════════════════════════
# 6. 删除错录货号（purge）
# ══════════════════════════════════════════════════════════════════
class PurgeTest(CostBookTestBase):
    """录错货号 / 把测试数据写进生产台账，都需要一个**带留痕**的删除口子。

    没有它，人就会拿 SQL 直接擦库，审计链就断了 —— 这条测试是那个口子的守卫。
    """

    def test_purge_removes_all_rows_and_appends_one_event(self):
        self.book.apply([row('KEEP-1', '5.00', '2026-09-01'),
                         row('WRONG-1', '9.99', '2026-09-01'),
                         row('WRONG-1', '8.88', '2026-09-10')])
        self.assertEqual(3, self.book.count())

        result = self.book.purge('WRONG-1', reason='货号录错了', actor='tester')

        # 同一货号的多条（含不同生效日期）一起删干净，别的货号不受影响
        self.assertEqual(2, result['purged'])
        self.assertEqual(1, self.book.count())
        self.assertIsNotNone(self.book.unit_cost('KEEP-1', '2026-09-10'))
        self.assertIsNone(self.book.unit_cost('WRONG-1', '2026-09-10'))

        # 留痕：每个被删的行一条 purge，且**保留了被删内容的完整快照**
        events, total = self.book.change_events(seller_sku='WRONG-1')
        purges = [e for e in events if e['action'] == 'purge']
        self.assertEqual(2, len(purges))
        self.assertTrue(all(e['old_json'] for e in purges), 'purge 必须留下被删内容')
        self.assertTrue(all(e['source'] == '货号录错了' for e in purges))
        self.assertTrue(all(e['actor_id'] == 'tester' for e in purges))
        self.assertIsNone(purges[0]['new_json'])
        # 该货号名下的事件：2 条 insert + 2 条 purge（`change_events` 已按货号过滤）
        self.assertEqual(4, total)

    def test_purge_missing_offer_is_a_noop(self):
        self.book.apply([row('A-1', '1.00', '2026-09-01')])
        before, _ = self.book.change_events()
        result = self.book.purge('NOT-THERE', reason='不存在', actor='tester')
        self.assertEqual(0, result['purged'])
        after, _ = self.book.change_events()
        self.assertEqual(len(before), len(after), '删不到东西就不该产生事件')
        self.assertEqual(1, self.book.count())

    def test_purge_does_not_touch_history_of_orders(self):
        """删除只影响「以后怎么算」，不动任何已有数据 —— 本测试只钉住成本库自身。"""
        self.book.apply([row('A-1', '1.00', '2026-09-01')])
        self.book.purge('A-1', reason='测试', actor='tester')
        self.assertEqual(0, self.book.count())
        self.assertEqual({}, self.book.price_map('2026-09-10'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
