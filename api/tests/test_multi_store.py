# -*- coding: utf-8 -*-
"""多店合计、店铺列表、订单分页的测试（ADR-0008）。

三条主线：

1. **合计口径**：`Σ各店 == 顶层合计`，且完成率是 `Σ完整单/Σ总单`
   （**不是**各店完成率的平均 —— 这里专门构造了一个两者不同的用例）。
2. **共同窗口**：合计的窗口右端必须是各店里**最早**的那个已结算日；
   否则落后的那个店会在尾部几天「贡献 0」，看起来像那几天没生意（ADR-0007 的形状）。
3. **服务端分页**：翻页只改 `orders` 数组，**合计与趋势必须逐字不变**。

另外把「权限不足时不返回部分结果」钉死：宁可整体 403，也不给一个少了一家店的合计。

跑法：
    python -m unittest api.tests.test_multi_store -v
"""
import unittest
from datetime import date
from decimal import Decimal

from api.stores import Store, StoreRegistry
from api.tests.test_api import (FAKE_CUTOFF, MOCK_PASSWORDS,
                               ApiTestBase, FakeSource, make_client)
from core.repository.base import DataCutoff, OverdueInfo

CN_DAY = date


class TinySource(FakeSource):
    """一店一份人工构造的订单，金额手算可核。

    `window_end` 由测试显式指定 —— 多店合计的全部争议都在这个值上，
    所以它必须是可控的，不能靠 orders 里的日期反推。
    """

    def __init__(self, rows=(), window_end=None, overdue=0, overdue_available=True):
        self.rows = tuple(rows)
        self._window_end = window_end
        self._overdue = overdue
        self._overdue_available = overdue_available
        self.closed = 0

    def close(self):
        self.closed += 1

    def data_cutoff(self, store_alias):
        return DataCutoff(FAKE_CUTOFF, 'tiny.updated_at', {}, self._window_end)

    def overdue_count(self, store_alias):
        if not self._overdue_available:
            return OverdueInfo(store_alias, None, 'tiny', available=False,
                               note='测试用：没有逾期扫描数据')
        return OverdueInfo(store_alias, self._overdue, 'tiny',
                           '2026-09-18T17:16:00+08:00')


def order(pn, day, net_rub, rate, cost, revenue, n_ops=1):
    """构造一条订单行（复用 FakeSource 的行工厂，金额口径与看板测试一致）。"""
    return FakeSource._row(
        pn, day, direct_net_rub=net_rub, rate=rate, cost_cny=cost,
        settled_sales_rub=net_rub, revenue=revenue, logistics='5.00', fee=None,
        n_ops=n_ops)


#: A 店：1 单，完整（净额 1000 / 汇率 10 → 100 CNY，成本 20 → 实际利润 80）
STORE_A_ROWS = (order('A-0001', '2026-09-08', '1000.00', '10.0000', '20.00',
                      revenue='50.00'),)


def _row_without_net(pn, day, cost, revenue=None):
    """净额为空、结算销售额为 0 的订单 —— §7.1 判不完整（no_settled_sale）。"""
    return FakeSource._row(pn, day, direct_net_rub='0.00', rate='10.0000',
                           cost_cny=cost, settled_sales_rub='0.00',
                           revenue=revenue, logistics='5.00', fee=None, n_ops=1)


#: B 店：3 单，全部不完整（§7.1 缺净额、§7.2 缺收入）。
#: 日期刻意都落在**共同窗口内**（共同右端 = B 的 09-08）——
#: 落在窗口外的行会让合计变成 0，那样测的就不是口径而是日期。
STORE_B_ROWS = tuple(_row_without_net('B-000%d' % i, '2026-09-%02d' % (5 + i),
                                      '10.00') for i in (1, 2, 3))


def registry(*aliases):
    return StoreRegistry([Store(a, '店铺 %s' % a, 'C:/nonexistent/%s.db' % a)
                          for a in aliases])


class MultiStoreTestBase(ApiTestBase):
    """A 店结到 09-10、B 店结到 09-08 —— 共同窗口右端必须是 09-08。

    `SOURCES` 在类上是**模板**，`setUp` 里按测试拷一份：否则某个用例换掉一个店
    的数据源之后，后面跑到的用例会拿到被换过的那个 —— 测试之间互相污染，
    失败的会是「别人改的」那个用例，非常难查（第一版就踩了）。
    """

    SOURCE_TEMPLATE = {
        'store_alpha': TinySource(STORE_A_ROWS, window_end=CN_DAY(2026, 9, 10),
                                overdue=3),
        'store_beta': TinySource(STORE_B_ROWS, window_end=CN_DAY(2026, 9, 8),
                                overdue=2),
    }

    def setUp(self):
        self.SOURCES = dict(type(self).SOURCE_TEMPLATE)
        self.client, self._restore = make_client(
            source_factory=lambda store: self.SOURCES[store.alias],
            registry=registry('store_alpha', 'store_beta'))
        self.addCleanup(self._restore)

    def aggregate(self, stores='store_alpha,store_beta', days=14, username='admin'):
        resp = self.client.get(
            '/api/dashboard/aggregate?stores=%s&days=%d' % (stores, days),
            headers=self.auth(username))
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()

    def single(self, alias='store_alpha', days=14):
        resp = self.client.get('/api/dashboard/store/%s?days=%d' % (alias, days),
                               headers=self.auth('admin'))
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()


class AggregateWindowTest(MultiStoreTestBase):
    def test_common_window_uses_earliest_settlement(self):
        """共同窗口右端 = 各店已结算日里最早的那个（这里是 B 的 09-08）。"""
        body = self.aggregate()
        self.assertEqual('2026-09-08', body['period']['end'])
        self.assertEqual('2026-09-08', body['window_end'])
        self.assertEqual('2026-08-26', body['period']['start'])
        by_alias = {s['alias']: s for s in body['stores']}
        self.assertEqual('2026-09-10', by_alias['store_alpha']['window_end'])
        self.assertEqual('2026-09-08', by_alias['store_beta']['window_end'])
        # 两店下发的 period 必须是同一个（否则「合计」是两个区间相加）
        self.assertEqual(body['period'], by_alias['store_alpha']['period'])
        self.assertEqual(body['period'], by_alias['store_beta']['period'])

    def test_warns_about_the_store_lagging_behind(self):
        """落后的那家店必须被点名 —— 合计最会骗人的地方就是它不说话。"""
        warnings = self.aggregate()['warnings']
        self.assertTrue(any('store_alpha' in w and '2026-09-08' in w
                            for w in warnings), warnings)
        self.assertTrue(any('不在本次合计里' in w for w in warnings), warnings)

    def test_store_without_locked_settlement_is_named(self):
        """给不出 window_end 的店：不进共同窗口计算，但要在 warnings 里点名。"""
        self.SOURCES['store_beta'] = TinySource(STORE_B_ROWS, window_end=None)
        body = self.aggregate()
        self.assertEqual('2026-09-10', body['window_end'])
        self.assertIsNone(body['stores'][1]['window_end'])
        self.assertTrue(any('没有任何已锁定结算' in w for w in body['warnings']),
                        body['warnings'])

    def test_single_store_aggregate_matches_single_store_endpoint(self):
        """同一家店：合计接口与单店接口必须给出同一批数（否则两套口径）。"""
        one = self.aggregate(stores='store_alpha')
        direct = self.single('store_alpha')
        self.assertEqual(direct['totals']['actual_profit_cny'],
                         one['totals']['actual_profit_cny'])
        self.assertEqual(direct['totals']['estimated_profit_cny'],
                         one['totals']['estimated_profit_cny'])
        self.assertEqual(direct['period'], one['period'])
        self.assertEqual([], one['warnings'])


class AggregateFormulaTest(MultiStoreTestBase):
    """合计公式逐项核对 —— 数字全部手算得出来。"""

    def test_sum_of_stores_equals_top_level(self):
        """可核对的不变式：各店合计之和 == 顶层合计（逐字段）。"""
        body = self.aggregate()
        for key in ('actual_profit_cny', 'estimated_profit_cny'):
            parts = [Decimal(s['totals'][key]) for s in body['stores']
                     if s['totals'][key] is not None]
            want = sum(parts, Decimal('0')) if parts else None
            got = body['totals'][key]
            self.assertEqual(None if want is None else str(want.quantize(Decimal('0.01'))),
                             got, key)
        self.assertEqual(sum(s['totals']['total_order_count'] for s in body['stores']),
                         body['totals']['total_order_count'])
        self.assertEqual(sum(s['totals']['complete_order_count'] for s in body['stores']),
                         body['totals']['complete_order_count'])

    def test_completion_rate_is_weighted_not_averaged(self):
        """完成率 = Σ完整单/Σ总单，**不是**各店完成率的平均。

        A 店 1/1 = 100%，B 店 0/3 = 0%：平均是 50%，正确值是 1/4 = 25%。
        两个数差一倍，所以这条断言真的在验证口径而不是在走形式。
        """
        body = self.aggregate()
        self.assertEqual(4, body['totals']['total_order_count'])
        self.assertEqual(1, body['totals']['complete_order_count'])
        self.assertEqual('0.2500', body['totals']['completion_rate'])

    def test_overdue_is_summed(self):
        self.assertEqual(5, self.aggregate()['totals']['overdue_count'])

    def test_missing_overdue_is_null_not_zero(self):
        """任一店给不出逾期数 → 合计给 null，并在 warnings 里说清是哪家店。"""
        self.SOURCES['store_beta'] = TinySource(STORE_B_ROWS,
                                              window_end=CN_DAY(2026, 9, 8),
                                              overdue_available=False)
        body = self.aggregate()
        self.assertIsNone(body['totals']['overdue_count'],
                          '缺失不能当成 0，否则「没扫描」会显示成「没有逾期」')
        self.assertTrue(any('store_beta' in w for w in body['warnings']),
                        body['warnings'])

    def test_data_cutoff_is_the_latest_write(self):
        """展示用的数据截止取各店最晚的一次写入（与窗口右端是两件事）。"""
        body = self.aggregate()
        self.assertEqual('2026-09-18T17:54:00+08:00', body['data_cutoff'])
        self.assertNotEqual(body['data_cutoff'][:10], body['window_end'])

    def test_trend_merges_same_day_across_stores(self):
        """同一天两家店都有数 → 那天的值必须是两店相加，不是二选一。"""
        self.SOURCES['store_beta'] = TinySource(
            (order('B-0009', '2026-09-08', '500.00', '10.0000', '10.00',
                   revenue='50.00'),),
            window_end=CN_DAY(2026, 9, 10), overdue=0)
        body = self.aggregate()
        # A：1000/10 − 20 = 80；B：500/10 − 10 = 40；同一天（09-08）合计 120
        trend = {p['date']: p for p in body['trend']}
        self.assertEqual('120.00', trend['2026-09-08']['actual_profit_cny'])
        self.assertEqual('120.00', body['totals']['actual_profit_cny'])

    def test_composition_is_summed(self):
        body = self.aggregate()
        comp = {c['key']: c['value_cny'] for c in body['composition']}
        # 直接净额：A 1000/10 = 100 CNY；B 的净额是 0，按 0 计入求和（不是跳过）
        self.assertEqual('100.00', comp['direct_net'])
        # 采购成本：只有 A 的 20 —— B 的三单在 §7.1 的「未结算销售」这一关就返回了，
        # purchase_cost_cny 是 None（core 的既有行为，test_api.py 里也记录了这一条）。
        # 也就是说：**不完整订单的采购成本不进利润构成**，这里如实断言，不放宽。
        self.assertEqual('20.00', comp['purchase'])


class AggregateAccessTest(MultiStoreTestBase):
    def test_unauthorized_store_returns_403_without_partial_result(self):
        """一次合计里有任何一家没授权 → 整体 403，**不给少一家的合计**。"""
        resp = self.client.get('/api/dashboard/aggregate?stores=store_alpha,store_beta',
                               headers=self.auth('operator01'))
        self.assertEqual(403, resp.status_code, resp.text)
        self.assertEqual('无权访问该店铺', resp.json()['detail'])

    def test_unknown_store_returns_404(self):
        resp = self.client.get('/api/dashboard/aggregate?stores=store_alpha,nope',
                               headers=self.auth('admin'))
        self.assertEqual(404, resp.status_code)

    def test_alias_shape_is_validated(self):
        resp = self.client.get('/api/dashboard/aggregate?stores=..%2F..%2Fetc%2Fpasswd',
                               headers=self.auth('admin'))
        self.assertEqual(422, resp.status_code)

    def test_requires_login(self):
        self.assertEqual(401, self.client.get(
            '/api/dashboard/aggregate?stores=store_alpha').status_code)

    def test_blank_or_too_many_stores_is_422(self):
        self.assertEqual(422, self.client.get(
            '/api/dashboard/aggregate?stores=,,,',
            headers=self.auth('admin')).status_code)
        many = ','.join('s%02d' % i for i in range(50))
        self.assertEqual(422, self.client.get(
            '/api/dashboard/aggregate?stores=%s' % many,
            headers=self.auth('admin')).status_code)

    def test_duplicate_aliases_are_counted_once(self):
        """`a,a` 不能把同一家店算两遍 —— 去重后再合计。"""
        body = self.aggregate(stores='store_alpha,store_alpha')
        self.assertEqual(['store_alpha'], body['store_aliases'])
        self.assertEqual(1, body['totals']['total_order_count'])


class OrderPaginationTest(ApiTestBase):
    """服务端分页：翻页只改 orders 数组，合计/趋势逐字不变。"""

    def page(self, params=''):
        resp = self.client.get('/api/dashboard/store/store_alpha?days=14' + params,
                               headers=self.auth('admin'))
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()

    def test_offset_walks_the_window_without_touching_totals(self):
        full = self.page()
        self.assertEqual(3, len(full['orders']))
        seen = []
        for offset in range(3):
            page = self.page('&orders_limit=1&orders_offset=%d' % offset)
            self.assertEqual(1, len(page['orders']))
            seen.append(page['orders'][0]['posting_number'])
            # 关键不变式：分页不改变合计、趋势、构成
            self.assertEqual(full['totals'], page['totals'])
            self.assertEqual(full['trend'], page['trend'])
            self.assertEqual(full['composition'], page['composition'])
            self.assertEqual(full['period'], page['period'])
        self.assertEqual([o['posting_number'] for o in full['orders']], seen)

    def test_offset_beyond_the_window_returns_empty_not_error(self):
        page = self.page('&orders_limit=10&orders_offset=99')
        self.assertEqual([], page['orders'])
        self.assertEqual(3, page['totals']['total_order_count'],
                         '明细为空时合计仍然必须是整个窗口的口径')

    def test_limit_above_the_cap_is_422(self):
        resp = self.client.get('/api/dashboard/store/store_alpha?orders_limit=201',
                               headers=self.auth('admin'))
        self.assertEqual(422, resp.status_code)

    def test_default_is_unchanged(self):
        """不传分页参数时，行为必须与加分页之前逐字相同。"""
        body = self.page()
        self.assertEqual(3, len(body['orders']))
        self.assertEqual(
            {'store_alias', 'data_cutoff', 'period', 'totals', 'trend',
             'composition', 'orders'},
            set(body.keys()), '响应形状一个字段都不能变（前端依赖它）')


class StoreListTest(ApiTestBase):
    def test_root_sees_all_stores_with_availability(self):
        resp = self.client.get('/api/stores', headers=self.auth('admin'))
        self.assertEqual(200, resp.status_code, resp.text)
        body = resp.json()
        self.assertEqual(['store_alpha', 'store_beta'],
                         [s['alias'] for s in body['stores']])
        self.assertEqual([10, 20, 50, 100], body['page_size_options'])
        self.assertTrue(all(s['available'] for s in body['stores']))
        # 库路径绝不下发（它是服务端配置，前端只需要别名）
        for store in body['stores']:
            self.assertNotIn('db_path', store)
            self.assertNotIn('C:/nonexistent', str(store))

    def test_operator_only_sees_authorized_stores(self):
        body = self.client.get('/api/stores', headers=self.auth('operator01')).json()
        self.assertEqual(['store_alpha'], [s['alias'] for s in body['stores']])

    def test_unavailable_store_reports_reason_not_silence(self):
        """注册了别名但库打不开：如实给 available=false + 原因，不静默跳过。"""
        def factory(store):
            if store.alias == 'store_beta':
                raise FileNotFoundError('找不到店铺库: %s' % store.db_path)
            return FakeSource()

        client, restore = make_client(source_factory=factory)
        self.addCleanup(restore)
        token = client.post('/api/auth/login',
                            json={'username': 'admin',
                                  'password': MOCK_PASSWORDS['admin']}
                            ).json()['access_token']
        body = client.get('/api/stores',
                          headers={'Authorization': 'Bearer %s' % token}).json()
        by_alias = {s['alias']: s for s in body['stores']}
        self.assertTrue(by_alias['store_alpha']['available'])
        self.assertFalse(by_alias['store_beta']['available'])
        self.assertIn('找不到店铺库', by_alias['store_beta']['error'])

    def test_requires_login(self):
        self.assertEqual(401, self.client.get('/api/stores').status_code)


if __name__ == '__main__':
    unittest.main()
