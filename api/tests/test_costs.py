# -*- coding: utf-8 -*-
"""成本管理接口的测试（ADR-0009）。

这个文件回答四个问题，每个都对应一条 ADR 里的承诺：

1. **契约**：`GET /api/costs` / `/api/costs/events` 的形状稳定（金额是字符串）。
2. **权限**：运营（`store_operator`）能看不能改 —— 写路由只给 `root` / `finance`。
   成本是财务口径的输入，运营改成本等于改了别人的利润。
3. **预览不落库 / 确认才落库 / 非法行整批拒绝**：`preview` 前后 `count()` 不变；
   `apply` 之后 `count()` 增加且事件里记着**登录用户名**；文件里只要有一行非法，
   **一行都不写**（不做「部分导入」那种说不清的事）。
4. **写边界**（最重要的一条）：调用导入接口前后，**生产店铺库文件的 sha256 不变**。
   这是 ADR-0009 §五的核心承诺 —— 成本库是我们唯一允许写的库。

**测试必须用临时成本库**：`Runtime.cost_book_path` 指向 `tempfile` 下的路径，
绝不能碰 `<DATA_ROOT>Platform\\cost_book.db`（那是生产台账）。
本文件每个用例一个临时目录，退出即删。

跑法：
    python -m unittest api.tests.test_costs -v
"""
import hashlib
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from api.stores import Store, StoreRegistry
from api.tests.test_api import (FAKE_CUTOFF, MOCK_PASSWORDS, TEST_REGISTRY,
                                ApiTestBase, FakeSource, make_client)
from core.repository.base import DataCutoff, PeriodSkuRow, SkuDetail
from core.repository.cost_book import CostBook
from core.repository.sqlite_source import CN_TZ, DEFAULT_STORE

#: 生产店铺库（**只读**，本文件只对它做哈希比对）
PROD_STORE = DEFAULT_STORE

#: 上传的字段名（ADR-0009 的接口契约：alias / days / file）
FORM = {'alias': 'store_alpha', 'days': '14'}

CSV_HEADER = '货号,单价,生效日期\n'


def csv_bytes(*lines):
    return (CSV_HEADER + ''.join(line + '\n' for line in lines)).encode('utf-8-sig')


def upload(name, payload):
    """构造 multipart 的 files 参数。"""
    return {'file': (name, io.BytesIO(payload), 'application/octet-stream')}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def make_legacy_db(path, rows):
    """合成旧产品成本库（表结构照抄原产品的 `purchase_costs`）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            'CREATE TABLE purchase_costs (cost_id TEXT PRIMARY KEY, seller_sku TEXT,'
            ' platform_sku TEXT, unit_cost_cny TEXT, created_at TEXT)')
        for r in rows:
            conn.execute('INSERT INTO purchase_costs VALUES(?,?,?,?,?)', r)
        conn.commit()
    finally:
        conn.close()
    return path


# ══════════════════════════════════════════════════════════════════
# 假数据源：补上 `sku_detail_for_period`
# ══════════════════════════════════════════════════════════════════
# 两个窗口内的订单，金额手算可核（窗口 = 数据截止日 2026-09-18 往回 14 天）：
#   O1 单货号 OFFER-MISS-A 数量 3，库里整单成本 168.50
#   O2 单货号 OFFER-MISS-A 数量 2，**库里成本为 None**（归不到货号）
#   O3 单货号 OFFER-MISS-B 数量 1，库里整单成本 20.00
DETAIL_ROWS = (
    PeriodSkuRow(posting_number='PN-0001', settlement_date='2026-09-16',
                 offer_id='OFFER-MISS-A', sku='900001', product_name='测试品 A',
                 quantity=3, purchase_cost_cny=Decimal('168.50')),
    PeriodSkuRow(posting_number='PN-0002', settlement_date='2026-09-16',
                 offer_id='OFFER-MISS-A', sku='900001', product_name='测试品 A',
                 quantity=2, purchase_cost_cny=None),
    PeriodSkuRow(posting_number='PN-0003', settlement_date='2026-09-15',
                 offer_id='OFFER-MISS-B', sku='900002', product_name='测试品 B',
                 quantity=1, purchase_cost_cny=Decimal('20.00')),
)

#: 这份数据的设计意图（下面每个断言都锚在这里）：
#:   * `offer_in_window` = 2 个货号；
#:   * 不往成本库里加任何价时，**两个货号都缺价** → `missing_count` = 2；
#:   * `OFFER-MISS-A` 的订单数 = 2（PN-0001 + PN-0002），数量 = 5；
#:     这正是「订单数只能数单号、不能数明细行数」的验算点。
DETAIL_OFFER_IN_WINDOW = 2
DETAIL_MISSING_OFFERS = 2
MISS_A_ORDERS, MISS_A_QTY = 2, 5
MISS_B_ORDERS, MISS_B_QTY = 1, 1


class DetailSource(FakeSource):
    """带逐 SKU 明细的假数据源。

    刻意继承 `FakeSource`（而不是另写一个类）：看板路径的方法原样复用，
    这样「成本接口与看板用的是同一套窗口/截止日」这件事不需要另行构造。

    **`FakeSource` 本身不支持 `sku_detail_for_period`** —— 所以
    `build_missing_costs` / `_impact` 在基础夹具上会直接 `AttributeError`。
    这个子类补上它，缺成本清单与影响面才有东西可测（见报告）。
    """

    def sku_detail_for_period(self, store_alias, start_date, end_date):
        rows = [r for r in DETAIL_ROWS
                if r.settlement_date and start_date <= r.settlement_date <= end_date]
        return SkuDetail(
            rows=tuple(rows),
            order_count=len({r.posting_number for r in rows}))


class NoDetailSource(FakeSource):
    """**不实现** `sku_detail_for_period` 的数据源：接口必须明确报错。

    直接 `AttributeError` 会把 500 抛给用户；这条测试把「谁负责说不」钉住。
    """

    def sku_detail_for_period(self, store_alias, start_date, end_date):
        raise NotImplementedError('测试用：这个数据源没有商品明细，做不了逐 SKU 归属')


class CostApiTestBase(ApiTestBase):
    """每个用例一个临时成本库 + 一份合成旧库。"""

    source_factory = staticmethod(lambda store: DetailSource())

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ozon-cost-api-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book_path = os.path.join(self.tmp, 'cost_book.db')
        self.legacy_path = os.path.join(self.tmp, 'legacy_purchase_costs.db')
        self.client, self._restore = make_client(
            source_factory=self.source_factory, registry=TEST_REGISTRY,
            cost_book_path=self.book_path,
            legacy_cost_book_path=self.legacy_path)
        self.addCleanup(self._restore)

    # ── 断言用的工具 ──
    def count(self):
        """台账行数。库不存在就是 0（**不建库**：读一下不该有副作用）。"""
        if not os.path.isfile(self.book_path):
            return 0
        book = CostBook(self.book_path, readonly=True)
        try:
            return book.count()
        finally:
            book.close()

    def events(self):
        if not os.path.isfile(self.book_path):
            return []
        book = CostBook(self.book_path, readonly=True)
        try:
            return book.change_events(limit=100)[0]
        finally:
            book.close()

    def read_events(self, limit=100):
        return self.events()[:limit]

    def make_legacy(self, rows=None):
        return make_legacy_db(self.legacy_path, rows if rows is not None else [
            ('c1', 'OLD-1', '111', '5.50', '2026-09-03T10:00:00+00:00'),
            ('c2', 'OLD-2', '222', '6.50', '2026-09-04T10:00:00+00:00'),
            ('c3', '', '333', '7.50', '2026-09-07T10:00:00+00:00'),
        ])

    def preview(self, payload, name='costs.csv', form=None, username='finance01'):
        return self.client.post('/api/costs/import/preview',
                                headers=self.auth(username),
                                data=form or FORM, files=upload(name, payload))

    def apply(self, payload, name='costs.csv', form=None, username='finance01'):
        return self.client.post('/api/costs/import/apply',
                                headers=self.auth(username),
                                data=form or FORM, files=upload(name, payload))

    def migrate(self, username='finance01'):
        return self.client.post('/api/costs/migrate-legacy',
                                headers=self.auth(username))


# ══════════════════════════════════════════════════════════════════
# 1. 契约
# ══════════════════════════════════════════════════════════════════
class CostListContractTest(CostApiTestBase):
    def test_empty_book_still_returns_the_contract_shape(self):
        """成本库还不存在时也是 200 + 完整形状（「还没导入过」不是错误）。

        这里刻意**不建库**：读接口不得有副作用，否则「打开一次页面」
        就会在磁盘上留下一个空台账，后面的判断（有没有导入过）全乱。
        """
        resp = self.client.get('/api/costs', headers=self.auth('admin'))
        self.assertEqual(200, resp.status_code, resp.text)
        body = resp.json()
        for key in ('book_path', 'total', 'returned', 'limit', 'offset', 'keyword',
                    'rows', 'change_event_total', 'last_change'):
            self.assertIn(key, body, key)
        self.assertEqual(0, body['total'])
        self.assertEqual([], body['rows'])
        self.assertEqual(0, body['change_event_total'])
        self.assertIsNone(body['last_change'])
        self.assertEqual(self.book_path, body['book_path'])
        self.assertFalse(os.path.isfile(self.book_path),
                         '读接口不得顺手建出成本库')

    def test_after_apply_list_shows_rows_and_event_total(self):
        self.assertEqual(200, self.apply(csv_bytes('OFFER-X,12.20,2026-09-01')).status_code)
        body = self.client.get('/api/costs', headers=self.auth('finance01')).json()
        self.assertEqual(1, body['total'])
        self.assertEqual(1, body['returned'])
        self.assertEqual(1, body['change_event_total'])
        row = body['rows'][0]
        for key in ('cost_id', 'seller_sku', 'platform_sku', 'unit_cost_cny',
                    'effective_from', 'scope', 'store_alias', 'status', 'source',
                    'note', 'created_at', 'updated_at'):
            self.assertIn(key, row, key)
        self.assertEqual('OFFER-X', row['seller_sku'])
        # 金额一律字符串下发（JS 浮点会吃掉尾差）
        self.assertIsInstance(row['unit_cost_cny'], str)
        self.assertEqual(Decimal('12.20'), Decimal(row['unit_cost_cny']))
        self.assertEqual('shared', row['scope'])
        self.assertIsNone(row['store_alias'], '本次不做店铺级覆盖（规则 3）')
        self.assertTrue(row['created_at'], 'created_at 不能为空')
        self.assertTrue(row['updated_at'], 'updated_at 不能为空')
        self.assertIsNotNone(body['last_change'])
        self.assertEqual('insert', body['last_change']['action'])

    def test_keyword_filters_by_offer_and_platform_sku(self):
        self.apply(csv_bytes('OFFER-AAA,1.00,2026-09-01', 'OFFER-BBB,2.00,2026-09-01'))
        page = self.client.get('/api/costs?keyword=AAA',
                               headers=self.auth('admin')).json()
        self.assertEqual(1, page['total'])
        self.assertEqual('AAA', page['keyword'])
        self.assertEqual(['OFFER-AAA'], [r['seller_sku'] for r in page['rows']])

    def test_pagination_reports_limit_and_offset(self):
        self.apply(csv_bytes(*['OFFER-%03d,1.00,2026-09-01' % i for i in range(5)]))
        page = self.client.get('/api/costs?limit=2&offset=1',
                               headers=self.auth('admin')).json()
        self.assertEqual(5, page['total'], 'total 是全库口径，不是本页行数')
        self.assertEqual(2, page['limit'])
        self.assertEqual(1, page['offset'])
        self.assertEqual(2, len(page['rows']))
        self.assertEqual(2, page['returned'])

    def test_limit_is_validated(self):
        self.assertEqual(422, self.client.get(
            '/api/costs?limit=0', headers=self.auth('admin')).status_code)
        self.assertEqual(422, self.client.get(
            '/api/costs?limit=100000', headers=self.auth('admin')).status_code)

    def test_events_endpoint_shape_and_filter(self):
        self.apply(csv_bytes('OFFER-EVT,3.00,2026-09-01'))
        body = self.client.get('/api/costs/events',
                               headers=self.auth('finance01')).json()
        for key in ('total', 'rows', 'limit', 'offset'):
            self.assertIn(key, body, key)
        self.assertEqual(1, body['total'])
        event = body['rows'][0]
        for key in ('id', 'cost_id', 'seller_sku', 'effective_from', 'action',
                    'old_json', 'new_json', 'source', 'file_sha256', 'actor_id',
                    'occurred_at'):
            self.assertIn(key, event, key)
        self.assertEqual('finance01', event['actor_id'])
        self.assertEqual(64, len(event['file_sha256']), '导入要记下文件 sha256')

        one = self.client.get('/api/costs/events?seller_sku=OFFER-EVT',
                              headers=self.auth('admin')).json()
        self.assertEqual(1, one['total'])
        none = self.client.get('/api/costs/events?seller_sku=NOPE',
                               headers=self.auth('admin')).json()
        self.assertEqual(0, none['total'])
        self.assertEqual([], none['rows'])

    def test_events_on_missing_book_is_empty_not_error(self):
        resp = self.client.get('/api/costs/events', headers=self.auth('admin'))
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual({'total': 0, 'rows': [], 'limit': 50, 'offset': 0},
                         resp.json())


# ══════════════════════════════════════════════════════════════════
# 2. 权限：运营能看不能改
# ══════════════════════════════════════════════════════════════════
class CostPermissionTest(CostApiTestBase):
    def test_operator_can_read_the_book(self):
        """运营要看数（哪些货号缺价是他们的活儿），所以读必须给。"""
        self.apply(csv_bytes('OFFER-R,1.00,2026-09-01'))
        resp = self.client.get('/api/costs', headers=self.auth('operator01'))
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(1, resp.json()['total'])

    def test_operator_cannot_apply_import(self):
        """`store_operator` 导入 → 403，且**一个字都没写进台账**。"""
        before = self.count()
        resp = self.apply(csv_bytes('OFFER-W,9.99,2026-09-01'), username='operator01')
        self.assertEqual(403, resp.status_code, resp.text)
        self.assertEqual({'detail', 'book_path'} & set(resp.json()),
                         set(resp.json()))
        self.assertEqual(before, self.count(), '403 之后台账必须原样')
        self.assertEqual([], self.read_events(), '无权写入不得留下任何事件')

    def test_operator_cannot_migrate_legacy(self):
        self.make_legacy()
        before = sha256_of(self.legacy_path)
        resp = self.migrate(username='operator01')
        self.assertEqual(403, resp.status_code, resp.text)
        self.assertEqual(0, self.count())
        self.assertEqual(before, sha256_of(self.legacy_path))

    def test_operator_cannot_preview_import(self):
        """预览也要求写权限：它会读窗口数据、暴露影响面，属成本管理动作。"""
        resp = self.preview(csv_bytes('OFFER-P,1.00,2026-09-01'),
                            username='operator01')
        self.assertEqual(403, resp.status_code, resp.text)

    def test_finance_and_root_can_write(self):
        self.assertEqual(200, self.apply(
            csv_bytes('OFFER-F,1.00,2026-09-01'), username='finance01').status_code)
        self.assertEqual(200, self.apply(
            csv_bytes('OFFER-G,2.00,2026-09-01'), username='admin').status_code)
        self.assertEqual(2, self.count())

    def test_unauthenticated_is_401_on_every_route(self):
        cases = [
            ('get', '/api/costs', None),
            ('get', '/api/costs/events', None),
            ('get', '/api/costs/missing?alias=store_alpha&days=14', None),
            ('post', '/api/costs/import/preview',
             {'data': FORM, 'files': upload('a.csv', csv_bytes('A,1,2026-09-01'))}),
            ('post', '/api/costs/import/apply',
             {'data': FORM, 'files': upload('a.csv', csv_bytes('A,1,2026-09-01'))}),
            ('post', '/api/costs/migrate-legacy', None),
        ]
        for method, url, kwargs in cases:
            with self.subTest(url=url):
                resp = getattr(self.client, method)(url, **(kwargs or {}))
                self.assertEqual(401, resp.status_code, resp.text)
                self.assertEqual({'detail': '请先登录'}, resp.json())
        self.assertEqual(0, self.count(), '未登录的请求不得产生任何写入')

    def test_cost_write_roles_match_the_documented_contract(self):
        """能写的角色就是 `root` / `finance` —— 与路由里的声明同源。"""
        from api.costs import COST_WRITE_ROLES
        self.assertEqual(('root', 'finance'), COST_WRITE_ROLES)


# ══════════════════════════════════════════════════════════════════
# 3. 预览 / 落库 / 整批拒绝
# ══════════════════════════════════════════════════════════════════
class ImportPreviewTest(CostApiTestBase):
    def test_preview_shape_and_impact_are_hand_checkable(self):
        """预览的差异与影响面必须能手算核对（ADR-0009 §四）。"""
        body = self.preview(csv_bytes('OFFER-NEW,30.00,2026-09-01')).json()
        self.assertEqual(self.book_path, body['book_path'])
        self.assertEqual(1, body['parsed_rows'])
        preview = body['preview']
        self.assertEqual(1, preview['total'])
        self.assertEqual(1, preview['created_count'])
        self.assertEqual(0, preview['updated_count'])
        self.assertEqual(0, preview['unchanged_count'])
        self.assertEqual(0, preview['invalid_count'])
        self.assertTrue(preview['ok'])
        self.assertEqual('OFFER-NEW', preview['created'][0]['seller_sku'])
        self.assertEqual('costs.csv', body['file']['name'])
        self.assertEqual(64, len(body['file']['sha256']))

        impact = body['impact']
        self.assertEqual({'start': '2026-09-05', 'end': '2026-09-18', 'days': 14},
                         impact['window'])
        self.assertEqual(0, impact['matched_skus'],
                         'OFFER-NEW 在窗口里没有订单 → 命中 0 个货号')
        self.assertEqual(0, impact['book_first']['affected_orders'])
        self.assertEqual('0.00', impact['book_first']['delta_purchase_cost_cny'])
        self.assertEqual(0, impact['book_authoritative']['affected_orders'])
        self.assertEqual('0.00', impact['book_authoritative']['delta_purchase_cost_cny'])
        self.assertEqual('book_first', impact['current_policy'])

    def test_preview_does_not_touch_the_book(self):
        """**预览绝不落库**：前后 `count()` 与事件数都不变。

        「未变 / 改动 / 新增」三类用**三次独立预览**分别验证：
        同一个货号在一个文件里出现两次会被解析器判成「重复」并整行拒绝
        （那是刻意的防呆 —— 用户显然是想改价，而不是想写两条同日的价），
        所以混在一个文件里根本走不到 update 分支。
        """
        self.apply(csv_bytes('OFFER-A,12.20,2026-09-01'))
        count_before = self.count()
        events_before = self.read_events()

        unchanged = self.preview(csv_bytes('OFFER-A,12.20,2026-09-01')).json()
        updated = self.preview(csv_bytes('OFFER-A,99.00,2026-09-01')).json()
        created = self.preview(csv_bytes('OFFER-B,5.00,2026-09-02')).json()

        self.assertEqual(count_before, self.count(), '预览把数据写进去了！')
        self.assertEqual(events_before, self.read_events(), '预览产生了变更事件！')

        for body in (unchanged, updated, created):
            preview = body['preview']
            # total 只数解析出来的行：total == created + updated + unchanged
            self.assertEqual(preview['created_count'] + preview['updated_count']
                             + preview['unchanged_count'], preview['total'])
            self.assertTrue(preview['ok'])

        self.assertEqual(1, unchanged['preview']['unchanged_count'])
        self.assertEqual(0, unchanged['preview']['updated_count'])
        self.assertEqual(1, updated['preview']['updated_count'])
        self.assertEqual(0, updated['preview']['unchanged_count'])
        # 「改了什么」要带上旧值，否则用户不知道会不会动到已有数字
        self.assertEqual('12.20', updated['preview']['updated'][0]['old_unit_cost_cny'])
        self.assertEqual('99.00', updated['preview']['updated'][0]['unit_cost_cny'])
        self.assertEqual(1, created['preview']['created_count'])
        self.assertEqual(0, created['preview']['updated_count'])

    def test_preview_reports_invalid_rows_but_still_returns(self):
        """预览要能把非法行**列出来**（让用户去改），而不是只报一句「有错」。"""
        body = self.preview(csv_bytes('OFFER-OK,1.00,2026-09-01',
                                      ',5.00,2026-09-01')).json()
        preview = body['preview']
        self.assertEqual(1, preview['invalid_count'])
        self.assertFalse(preview['ok'])
        self.assertEqual(3, preview['invalid'][0]['line'])
        self.assertIn('缺货号', preview['invalid'][0]['reason'])
        self.assertEqual(1, body['parsed_rows'], '解析出来的只有 1 行合法数据')
        self.assertEqual(1, preview['created_count'], '合法行仍然要给出影响面')
        self.assertEqual(0, self.count(), '预览不得落库')

    def test_preview_of_wholly_invalid_file_is_422(self):
        resp = self.preview(csv_bytes(',5.00,2026-09-01'))
        self.assertEqual(422, resp.status_code, resp.text)
        self.assertIn('没有可用的成本行', resp.json()['detail'])
        self.assertEqual(0, self.count())

    def test_preview_impact_counts_orders_not_detail_rows(self):
        """影响面的 `affected_orders` 是**订单数**（按单号去重），不是明细行数。

        回归点（曾实现为 `getattr(row, 'order_count', 0)`，恒为 0）：
          * `OFFER-MISS-A` 出现在 PN-0001 / PN-0002 两单里 → 命中 2 单；
          * 数量 3 + 2 = 5，所以权威模式下的成本是 单价 × 5。
        同一响应里的 `matched_skus` 恒等于命中的**货号数**，
        `affected_orders` 恒等于这些货号所在的**订单数** —— 两者不能自相矛盾。
        """
        body = self.preview(csv_bytes('OFFER-MISS-A,10.00,2026-09-01')).json()
        impact = body['impact']
        self.assertEqual(1, impact['matched_skus'])
        self.assertEqual(MISS_A_ORDERS, impact['book_authoritative']['affected_orders'])
        # PN-0001 有成本 168.50 → 差额 = 10 × 3 − 168.50 = −138.50
        # PN-0002 没有成本（归不到货号）→ 库优先模式下由成本库补上：10 × 2 = 20.00
        self.assertEqual(MISS_A_ORDERS - 1, impact['book_first']['affected_orders'],
                         '只有「库里成本为空」的那一单会在库优先模式下改变')
        self.assertEqual('20.00', impact['book_first']['delta_purchase_cost_cny'])
        self.assertEqual('-118.50', impact['book_authoritative']['delta_purchase_cost_cny'])

    def test_preview_impact_of_a_fully_priced_offer_is_zero_in_book_first(self):
        """库优先模式下的经典结论：库里有成本 → 这次导入不改任何数字。"""
        body = self.preview(csv_bytes('OFFER-MISS-B,50.00,2026-09-01')).json()
        impact = body['impact']
        self.assertEqual(1, impact['matched_skus'])
        self.assertEqual(0, impact['book_first']['affected_orders'],
                         '库里已有成本（20.00）→ 库优先模式下 0 单受影响')
        self.assertEqual('0.00', impact['book_first']['delta_purchase_cost_cny'])
        self.assertEqual(1, impact['book_authoritative']['affected_orders'])
        self.assertEqual('30.00', impact['book_authoritative']['delta_purchase_cost_cny'],
                         '50.00 × 1 − 20.00 = 30.00')

    def test_preview_impact_accumulates_across_offers_without_double_counting(self):
        """两个货号一起导入时，`affected_orders` / `matched_skus` / 差额都按货号累加。

        这条专门盯住「按明细行重复计数」这个坑：
        `OFFER-MISS-A` 有 2 张订单（PN-0001 有价、PN-0002 无价），
        `OFFER-MISS-B` 有 1 张 —— 命中 2 个货号 / 3 张订单，
        权威模式差额 = 10×5 − 168.50 + 50×1 − 20.00 = −118.50 + 30.00 = −88.50。
        """
        body = self.preview(csv_bytes('OFFER-MISS-A,10.00,2026-09-01',
                                      'OFFER-MISS-B,50.00,2026-09-01')).json()
        impact = body['impact']
        self.assertEqual(2, impact['matched_skus'])
        self.assertEqual(MISS_A_ORDERS + MISS_B_ORDERS,
                         impact['book_authoritative']['affected_orders'])
        self.assertEqual('-88.50', impact['book_authoritative']['delta_purchase_cost_cny'])
        # 库优先：只有 OFFER-MISS-A 的 PN-0002 那一单（数量 2）会变 → 10 × 2 = 20.00
        self.assertEqual(1, impact['book_first']['affected_orders'])
        self.assertEqual('20.00', impact['book_first']['delta_purchase_cost_cny'])


class ImportApplyTest(CostApiTestBase):
    def test_apply_writes_rows_events_and_records_the_actor(self):
        body = self.apply(csv_bytes('OFFER-A,12.20,2026-09-01',
                                    'OFFER-B,7.00,2026-09-02')).json()
        self.assertEqual({'created': 2, 'updated': 0, 'unchanged': 0},
                         body['applied'])
        self.assertEqual(2, body['total_after'])
        self.assertEqual(2, self.count())
        self.assertEqual(self.book_path, body['book_path'])
        self.assertEqual('finance01', body['actor'])

        events = self.read_events()
        self.assertEqual(2, len(events))
        self.assertEqual({'finance01'}, {e['actor_id'] for e in events})
        self.assertEqual({'insert'}, {e['action'] for e in events})

    def test_apply_reports_updated_and_unchanged_on_second_import(self):
        """改价 → `updated`；原样再导 → `unchanged`，且台账不涨行。"""
        self.apply(csv_bytes('OFFER-A,12.20,2026-09-01'))
        again = self.apply(csv_bytes('OFFER-A,12.20,2026-09-01')).json()
        self.assertEqual({'created': 0, 'updated': 0, 'unchanged': 1},
                         again['applied'])
        self.assertEqual(1, again['total_after'])

        changed = self.apply(csv_bytes('OFFER-A,15.00,2026-09-01')).json()
        self.assertEqual({'created': 0, 'updated': 1, 'unchanged': 0},
                         changed['applied'])
        self.assertEqual(1, changed['total_after'], '同一天改价是原地更新')
        actions = [e['action'] for e in self.read_events()]
        self.assertIn('update', actions)

    def test_apply_effective_date_keeps_history(self):
        """接口层也要按生效日期回溯：两天的价并存，历史订单不被改写。"""
        self.apply(csv_bytes('OFFER-A,12.20,2026-09-01'))
        self.apply(csv_bytes('OFFER-A,15.00,2026-09-10'))
        book = CostBook(self.book_path, readonly=True)
        self.addCleanup(book.close)
        self.assertEqual(Decimal('12.20'), book.unit_cost('OFFER-A', '2026-09-05'))
        self.assertEqual(Decimal('15.00'), book.unit_cost('OFFER-A', '2026-09-12'))
        self.assertEqual(2, self.count(), '两个生效日 = 两行台账')

    def test_apply_rejects_the_whole_batch_on_any_invalid_row(self):
        """**文件里只要有一行非法，整批拒绝**（422）且一行都不写。

        不做「部分导入」：用户拿到的结果会是一半新一半旧，
        而他无法知道哪一半进去了 —— 那比直接拒绝危险得多。
        """
        self.apply(csv_bytes('OFFER-KEEP,1.00,2026-09-01'))
        before = self.count()
        events_before = self.read_events()

        resp = self.apply(csv_bytes(
            'OFFER-GOOD,2.00,2026-09-01',       # 合法
            'OFFER-BAD,abc,2026-09-01'))        # 非法：单价解析不了
        self.assertEqual(422, resp.status_code, resp.text)
        detail = resp.json()['detail']
        self.assertIn('整批未导入', detail)
        self.assertIn('第 3 行', detail, '要指出第一处非法行的行号')
        self.assertEqual(before, self.count(), '非法批次里合法的行也不许写进去')
        self.assertEqual(events_before, self.read_events(), '不得留下变更事件')

    def test_apply_rejects_file_without_any_valid_row(self):
        resp = self.apply(csv_bytes(',5.00,2026-09-01'))
        self.assertEqual(422, resp.status_code, resp.text)
        self.assertEqual(0, self.count())

    def test_apply_rejects_header_only_file(self):
        """只有表头的文件 → 422「文件里没有成本行」，不是成功导入 0 条。"""
        resp = self.apply(CSV_HEADER.encode('utf-8-sig'))
        self.assertEqual(422, resp.status_code, resp.text)
        self.assertIn('没有成本行', resp.json()['detail'])
        self.assertEqual(0, self.count())

    def test_apply_rejects_unsupported_file_type(self):
        resp = self.apply(b'not a cost table', name='costs.txt')
        self.assertEqual(422, resp.status_code, resp.text)
        self.assertIn('.xlsx', resp.json()['detail'])
        self.assertEqual(0, self.count())

    def test_apply_rejects_empty_file(self):
        resp = self.apply(b'', name='costs.csv')
        self.assertEqual(422, resp.status_code, resp.text)
        self.assertIn('空', resp.json()['detail'])
        self.assertEqual(0, self.count())

    def test_apply_rejects_alias_outside_the_whitelist(self):
        """`alias` 不在白名单 → 404，而且**在解析文件之前**就拒绝。"""
        resp = self.apply(csv_bytes('OFFER-A,1.00,2026-09-01'),
                          form={'alias': 'nope', 'days': '14'})
        self.assertEqual(404, resp.status_code, resp.text)
        self.assertEqual(0, self.count())

    def test_apply_requires_write_permission_for_the_store(self):
        """有权写成本、但无权访问那个店铺 → 403（不泄露别的店的窗口数据）。"""
        resp = self.apply(csv_bytes('OFFER-A,1.00,2026-09-01'),
                          form={'alias': 'store_beta', 'days': '14'},
                          username='finance01')
        self.assertEqual(403, resp.status_code, resp.text)
        self.assertEqual(0, self.count())

    def test_apply_validates_days(self):
        resp = self.apply(csv_bytes('OFFER-A,1.00,2026-09-01'),
                          form={'alias': 'store_alpha', 'days': '0'})
        self.assertEqual(422, resp.status_code, resp.text)
        self.assertEqual(0, self.count())

    def test_xlsx_upload_is_accepted(self):
        """接口要能收 .xlsx（模板就是 xlsx）—— csv 能过不代表 xlsx 能过。"""
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['货号', '单价', '生效日期'])
        ws.append(['OFFER-XLSX', '12.34', '2026-09-01'])
        buf = io.BytesIO()
        wb.save(buf)
        resp = self.apply(buf.getvalue(), name='成本模板.xlsx')
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual({'created': 1, 'updated': 0, 'unchanged': 0},
                         resp.json()['applied'])
        self.assertEqual(1, self.count())


# ══════════════════════════════════════════════════════════════════
# 4. 缺成本清单
# ══════════════════════════════════════════════════════════════════
class MissingCostsTest(CostApiTestBase):
    def test_all_offers_missing_when_the_book_is_empty(self):
        """成本库空 → 窗口内**每个货号都缺价**，且数量/订单数按货号合并。

        `OFFER-MISS-A` 在 PN-0001（数量 3）与 PN-0002（数量 2）里各出现一次：
        数量 5、订单数 2。这里就是「订单数不能恒为 0」的验算点。
        """
        self.apply(csv_bytes('OFFER-UNRELATED,1.00,2026-09-01'))
        body = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                               headers=self.auth('finance01')).json()
        self.assertEqual('store_alpha', body['store_alias'])
        self.assertEqual({'start': '2026-09-05', 'end': '2026-09-18', 'days': 14},
                         body['period'])
        self.assertEqual(DETAIL_OFFER_IN_WINDOW, body['offer_in_window'])
        self.assertEqual(0, body['offer_with_price'])
        self.assertEqual(DETAIL_MISSING_OFFERS, body['missing_count'])
        self.assertEqual(self.book_path, body['price_source'])

        by_offer = {m['seller_sku']: m for m in body['missing']}
        self.assertEqual({'OFFER-MISS-A', 'OFFER-MISS-B'}, set(by_offer))
        miss_a = by_offer['OFFER-MISS-A']
        self.assertEqual(MISS_A_QTY, miss_a['quantity'], '同一货号的数量要累加')
        self.assertEqual(MISS_A_ORDERS, miss_a['order_count'],
                         '订单数必须是单号去重后的个数，不是明细行数、更不是恒 0')
        self.assertEqual('900001', miss_a['sku'])
        self.assertEqual('测试品 A', miss_a['product_name'])
        self.assertEqual('168.50', miss_a['attributed_cost_cny'],
                         '库里订单级成本要如实带上（供人工判断是否真的缺）')
        self.assertEqual('成本库里没有这个货号的单价', miss_a['reason'])
        miss_b = by_offer['OFFER-MISS-B']
        self.assertEqual(MISS_B_QTY, miss_b['quantity'])
        self.assertEqual(MISS_B_ORDERS, miss_b['order_count'])
        # 排序按订单数倒序：OFFER-MISS-A（2 单）在 OFFER-MISS-B（1 单）前面
        self.assertEqual(['OFFER-MISS-A', 'OFFER-MISS-B'],
                         [m['seller_sku'] for m in body['missing']])

    def test_priced_offers_are_excluded(self):
        """成本库里有价的货号不进缺成本清单（口径与看板的缺成本 SKU 数一致）。"""
        self.apply(csv_bytes('OFFER-MISS-A,12.20,2026-09-01'))
        body = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                               headers=self.auth('finance01')).json()
        self.assertEqual(1, body['offer_with_price'])
        self.assertEqual(1, body['missing_count'])
        self.assertEqual('OFFER-MISS-B', body['missing'][0]['seller_sku'])
        self.assertEqual(1, body['book_offer_count'])

    def test_price_effective_after_the_window_does_not_paper_over_it(self):
        """生效日期**晚于窗口右端**的价不参与窗口取价：仍算缺价。

        这是刻意的口径，也是记录在案的一条边界：窗口内取价用的是窗口右端
        （`price_map(end_date)`），不是「今天」。否则「今天录一个价」会去
        追认一个月前那批订单，而用户的心智是「我今天才开始用这个价」。
        要覆盖历史订单，就该把生效日期写成那一天。
        """
        self.apply(csv_bytes('OFFER-MISS-A,12.20,2026-09-30'))
        body = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                               headers=self.auth('finance01')).json()
        self.assertEqual(0, body['offer_with_price'],
                         '09-30 晚于窗口右端 09-18 → 这个价对窗口内的订单还没生效')
        self.assertEqual(DETAIL_MISSING_OFFERS, body['missing_count'])
        self.assertEqual({'OFFER-MISS-A', 'OFFER-MISS-B'},
                         {m['seller_sku'] for m in body['missing']})

    def test_price_effective_inside_the_window_is_used(self):
        """落在窗口内的生效日会被采纳 —— 证明取价确实按**订单日**回溯。"""
        self.apply(csv_bytes('OFFER-MISS-A,10.00,2026-09-17'))
        body = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                               headers=self.auth('finance01')).json()
        self.assertEqual(1, body['offer_with_price'])
        self.assertEqual(1, body['missing_count'])
        self.assertEqual('OFFER-MISS-B', body['missing'][0]['seller_sku'])

    def test_missing_book_is_503_with_actionable_message(self):
        """成本库不存在 → 503 且告诉用户路径（而不是 500）。"""
        resp = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                               headers=self.auth('finance01'))
        self.assertEqual(503, resp.status_code, resp.text)
        self.assertIn(self.book_path, resp.json()['detail'])
        self.assertFalse(os.path.isfile(self.book_path), '读接口不得建库')

    def test_source_without_sku_detail_is_not_silently_empty(self):
        """数据源给不出逐 SKU 明细时，必须是 501，**不能返回"没有缺成本"**。

        「查不出来」伪装成「一个都不缺」是最危险的一种失败：
        用户会以为成本录全了，于是再也不会去补那 155 个货号。
        """
        self.apply(csv_bytes('OFFER-ANY,1.00,2026-09-01'))
        client, restore = make_client(
            source_factory=lambda store: NoDetailSource(),
            registry=TEST_REGISTRY, cost_book_path=self.book_path,
            legacy_cost_book_path=self.legacy_path)
        self.addCleanup(restore)
        resp = client.get('/api/costs/missing?alias=store_alpha&days=14',
                          headers=self.auth('finance01'))
        self.assertEqual(501, resp.status_code, resp.text)

    def test_alias_pattern_is_validated(self):
        self.assertEqual(422, self.client.get(
            '/api/costs/missing?alias=..%2Fetc&days=14',
            headers=self.auth('admin')).status_code)


# ══════════════════════════════════════════════════════════════════
# 5. 从旧产品成本库迁移
# ══════════════════════════════════════════════════════════════════
class MigrateLegacyApiTest(CostApiTestBase):
    def test_migrate_copies_valid_rows_and_reports_skips(self):
        self.make_legacy()
        resp = self.migrate('admin')
        self.assertEqual(200, resp.status_code, resp.text)
        body = resp.json()
        self.assertEqual(2, body['created'])
        self.assertEqual(1, body['skipped'])
        self.assertEqual(3, body['scanned'])
        self.assertIn('货号为空', body['skipped_detail'][0]['reason'])
        self.assertEqual(2, body['total_after'])
        self.assertEqual(self.legacy_path, body['legacy_path'])
        self.assertTrue(body['legacy_readonly'], '迁移必须声明旧库是只读打开的')
        self.assertEqual({'admin'}, {e['actor_id'] for e in self.read_events()})

    def test_migrate_is_idempotent_and_legacy_file_is_untouched(self):
        """迁两次：第二次全 `unchanged`，且旧库文件的哈希与 mtime 都不变。"""
        self.make_legacy()
        before_sha = sha256_of(self.legacy_path)
        before_mtime = os.path.getmtime(self.legacy_path)

        first = self.migrate().json()
        self.assertEqual(2, first['created'])
        self.assertTrue(first['legacy_mtime_unchanged'])

        second = self.migrate().json()
        self.assertEqual(0, second['created'])
        self.assertEqual(0, second['updated'])
        self.assertEqual(2, second['unchanged'])
        self.assertEqual(1, second['skipped'], '跳过的行第二次仍然要报出来')
        self.assertTrue(second['legacy_mtime_unchanged'])
        self.assertEqual(2, self.count())
        self.assertEqual(before_sha, sha256_of(self.legacy_path),
                         '迁移改动了旧产品成本库！')
        self.assertEqual(before_mtime, os.path.getmtime(self.legacy_path))
        self.assertEqual(2, len(self.read_events()), '第二次迁移不得产生新事件')

    def test_migrate_without_legacy_file_is_404(self):
        resp = self.migrate()
        self.assertEqual(404, resp.status_code, resp.text)
        self.assertIn('找不到原产品成本库', resp.json()['detail'])
        self.assertEqual(0, self.count())

    def test_migrate_legacy_with_wrong_schema_is_422(self):
        """旧库不是我们认得的形状 → 422（明确说出缺哪张表），不是 500。"""
        conn = sqlite3.connect(self.legacy_path)
        conn.execute('CREATE TABLE unrelated(x)')
        conn.commit()
        conn.close()
        resp = self.migrate()
        self.assertEqual(422, resp.status_code, resp.text)
        self.assertIn('purchase_costs', resp.json()['detail'])

    def test_migrate_uses_the_runtime_configured_path(self):
        """迁移源取自 `Runtime.legacy_cost_book_path` —— 测试与生产同一装配点。"""
        self.make_legacy()
        body = self.migrate().json()
        self.assertEqual(self.legacy_path, body['legacy_path'])


# ══════════════════════════════════════════════════════════════════
# 6. 写边界（ADR-0009 §五，最重要的一条）
# ══════════════════════════════════════════════════════════════════
@unittest.skipUnless(os.path.isfile(PROD_STORE),
                     '生产店铺库不存在（%s），跳过写边界哈希测试：'
                     '这条断言必须在真实店铺库上跑才有意义' % PROD_STORE)
class WriteBoundaryTest(CostApiTestBase):
    """调用成本导入接口前后，**生产店铺库文件的内容 sha256 必须不变**。

    这是 ADR-0009 §一规则 1 + §五的核心承诺：成本库是独立文件，
    生产店铺库永远 `mode=ro`。ADR 明确要求「必须有一条测试把这条边界钉住」。

    比的是**内容哈希**（不是 mtime）：mtime 粒度粗、也会被别的进程碰到，
    而 sha256 一旦变化就是真的写进去了。
    """

    def test_import_apply_does_not_touch_the_production_store(self):
        before = sha256_of(PROD_STORE)
        resp = self.apply(csv_bytes('BOUNDARY-1,1.23,2026-09-01'))
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(1, self.count(), '成本库应该被写了（否则这条测试是空转）')
        self.assertTrue(os.path.isfile(self.book_path))
        self.assertNotEqual(os.path.abspath(self.book_path),
                            os.path.abspath(PROD_STORE),
                            '成本库不能就是生产店铺库')
        self.assertEqual(before, sha256_of(PROD_STORE),
                         '导入成本改动了生产店铺库 —— 写边界破了')

    def test_preview_and_migrate_do_not_touch_the_production_store(self):
        """预览与迁移同样不许碰生产店铺库（迁移的源是**旧成本库**，不是店铺库）。"""
        before = sha256_of(PROD_STORE)
        self.assertEqual(200, self.preview(
            csv_bytes('BOUNDARY-2,1.00,2026-09-01')).status_code)
        self.make_legacy()
        self.assertEqual(200, self.migrate().status_code)
        self.assertEqual(before, sha256_of(PROD_STORE),
                         '预览/迁移改动了生产店铺库')

    def test_real_sqlite_source_is_still_readonly_under_the_cost_flow(self):
        """用**真实** `SqliteSource` 装配一遍成本接口：它仍拒绝写入。

        上一条测的是文件哈希；这条测的是连接本身 ——
        两层都钉住，才不会出现「哈希没变但其实换了连接方式」这种情况。
        """
        from core.repository import sqlite_source as sq

        client, restore = make_client(
            source_factory=lambda store: sq.SqliteSource(store.db_path,
                                                         alias=store.alias),
            registry=StoreRegistry([Store('prod', '生产店铺', PROD_STORE)]),
            cost_book_path=self.book_path,
            legacy_cost_book_path=self.legacy_path)
        self.addCleanup(restore)
        before = sha256_of(PROD_STORE)
        token = client.post('/api/auth/login',
                            json={'username': 'admin',
                                  'password': MOCK_PASSWORDS['admin']}
                            ).json()['access_token']
        headers = {'Authorization': 'Bearer %s' % token}
        resp = client.post('/api/costs/import/apply', headers=headers,
                           data={'alias': 'prod', 'days': '14'},
                           files=upload('costs.csv',
                                        csv_bytes('BOUNDARY-3,2.00,2026-09-01')))
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(before, sha256_of(PROD_STORE))

        with sq.SqliteSource(PROD_STORE) as src:
            with self.assertRaises(sqlite3.OperationalError):
                src._conn.execute('CREATE TABLE __cost_test_probe(x)')


# ══════════════════════════════════════════════════════════════════
# 7. 端到端：导入 → 缺成本清单 → 影响面
# ══════════════════════════════════════════════════════════════════
class CostClosedLoopTest(CostApiTestBase):
    def test_loop_from_preview_to_missing_list_shrinking(self):
        """一条闭环：预览 → 落库 → 缺成本清单少一个货号。

        这条测试保护的是**接口之间的一致性**：三条路由各自都对，
        但合起来算的口径不一致（一个说缺 2 个、一个说缺 0 个），
        用户就不知道该信谁。ADR-0009 §三之三专门点了这个反直觉结论。
        """
        # 先写入一个不相关的货号：成本库文件因此存在
        # （`/missing` 在成本库不存在时是 503，那是另一条测试）
        self.apply(csv_bytes('OFFER-UNRELATED,1.00,2026-09-01'))

        before = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                                 headers=self.auth('finance01')).json()
        self.assertEqual(DETAIL_MISSING_OFFERS, before['missing_count'])

        preview = self.preview(csv_bytes('OFFER-MISS-A,12.20,2026-09-01')).json()
        self.assertEqual(1, preview['preview']['created_count'])
        self.assertEqual(1, preview['impact']['matched_skus'])

        applied = self.apply(csv_bytes('OFFER-MISS-A,12.20,2026-09-01')).json()
        self.assertEqual({'created': 1, 'updated': 0, 'unchanged': 0},
                         applied['applied'])

        after = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                                headers=self.auth('finance01')).json()
        self.assertEqual(DETAIL_MISSING_OFFERS - 1, after['missing_count'])
        self.assertEqual(1, after['offer_with_price'])
        self.assertEqual('OFFER-MISS-B', after['missing'][0]['seller_sku'])

        # 再导一次同一批：全 unchanged，缺成本清单也不再变
        again = self.apply(csv_bytes('OFFER-MISS-A,12.20,2026-09-01')).json()
        self.assertEqual({'created': 0, 'updated': 0, 'unchanged': 1},
                         again['applied'])
        stable = self.client.get('/api/costs/missing?alias=store_alpha&days=14',
                                 headers=self.auth('finance01')).json()
        self.assertEqual(after, stable)


if __name__ == '__main__':
    unittest.main(verbosity=2)
