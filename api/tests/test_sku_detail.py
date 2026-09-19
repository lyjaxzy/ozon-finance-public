# -*- coding: utf-8 -*-
"""逐 SKU 利润下钻的测试（ADR-0006）。

四条要求，一条一个类：

1. `SkuDetailContractTest` —— 接口正常返回、字段齐全、金额是字符串且可转 Decimal，
   排序 / 搜索 / 分页只影响下发条数、不影响合计。
2. `MissingCostTest` —— **缺成本的 SKU 返回 null 而不是 0，且带原因**。
3. `SkuDetailPermissionTest` —— 越权（受限角色访问未授权店铺）返回 403。
4. `SkuReconciliationTest` —— **逐 SKU 合计 == 订单口径合计**，真库上也跑一遍。
   这是最重要的一条：它防的是「两条路径悄悄漂移」。

外加 `SkuAttributionRuleTest`：归属规则本身的单元测试（单货号整单归属、
多货号按行金额、对不上就进未归属桶、绝不分摊）。

跑法：
    python -m unittest discover -s api/tests -t .
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi.testclient import TestClient

from api.app import app
from api.deps import Runtime
from api.stores import Store, StoreRegistry
from core.domain.profit import (Operation, Posting, SettlementSnapshot,
                                evaluate_actual, evaluate_estimated,
                                evaluate_sku_profit, SkuLineInput)
from core.repository import sqlite_source as sq
from core.repository.base import (DailyAmounts, DataCutoff, OverdueInfo,
                                  PeriodAmounts, PeriodOrderRow)
from core.repository.sku_attribution import attribute_order_lines
from core.repository.sqlite_source import DEFAULT_STORE

from .test_api import (MOCK_PASSWORDS, MOCK_SPECS, TEST_REGISTRY,
                       _mock_user_store, make_client)

CN = timezone(timedelta(hours=8))
FAKE_CUTOFF = datetime(2026, 9, 18, 17, 54, 0, tzinfo=CN)


# ══════════════════════════════════════════════════════════════════
# 内存数据源：4 个订单，覆盖「整单归属 / 缺成本 / 多货号 / 无商品明细」
# ══════════════════════════════════════════════════════════════════
#
# 全部数字都手算可核（1 单号 = 1 行）：
#
#  O1 0571-0234-8901  09-18  单货号 A-1×2  收入 200 成本 60 物流 12
#                    净额 2000 ₽ 汇率 10.0 → 实际 = 200 − 60 = 140.00
#  O2 0571-0234-8902  09-17  单货号 B-2×1  收入 100 **成本缺**
#                    → 预估/实际都必须给 None（不是 0），原因 missing_purchase_cost
#  O3 0571-0234-8903  09-18  多货号 A-1×1 + C-3×1  收入 150（行价 90 + 60）
#                    成本 50（单件 30 + 20）物流 15
#                    → 收入/成本按行归属；物流与流水**归不出去**，进未归属桶
#  O4 0571-0234-8904  09-18  **没有商品明细行** 收入 80 成本 20
#                    → 整单进未归属桶（missing_posting_item）
ORDERS = (
    # pn, day, net_rub, rate, revenue, cost, logistics,
    # items[(offer, sku, name, qty)], line_revenues, unit_costs
    ('0571-0234-8901', '2026-09-18', '2000.00', '10.0000', '200.00', '60.00', '12.00',
     (('A-1', '111', '商品甲', 2),), None, None),
    ('0571-0234-8902', '2026-09-17', '1000.00', '10.0000', '100.00', None, None,
     (('B-2', '222', '商品乙', 1),), None, None),
    ('0571-0234-8903', '2026-09-18', '1500.00', '10.0000', '150.00', '50.00', '15.00',
     (('A-1', '111', '商品甲', 1), ('C-3', '333', '商品丙', 1)),
     {'A-1': '90.00', 'C-3': '60.00'}, {'A-1': '30.00', 'C-3': '20.00'}),
    ('0571-0234-8904', '2026-09-18', '800.00', '10.0000', '80.00', '20.00', None,
     (), None, None),
)


def _order_row(spec):
    pn, day, net, rate, revenue, cost, logistics, items, _rev, _cost = spec
    ops = tuple(Operation(operation_id='%s-op%d' % (pn, i),
                          amount_rub=(Decimal(net) / 2).quantize(Decimal('0.01')))
                for i in range(2))
    return PeriodOrderRow(
        posting_number=pn,
        settlement_date=day,
        snapshot=SettlementSnapshot(
            posting_number=pn, settlement_date=day, state='locked',
            direct_net_rub=Decimal(net),
            settled_sales_rub=(Decimal(revenue) if revenue else Decimal('0')),
            exchange_rate_rub_per_cny=Decimal(rate),
            purchase_cost_cny=None if cost is None else Decimal(cost),
            operation_ids=tuple(o.operation_id for o in ops),
            unknown_reason=None),
        posting=Posting(
            posting_number=pn, status='delivered',
            revenue_cny=None if revenue is None else Decimal(revenue),
            purchase_cost_cny=None if cost is None else Decimal(cost),
            logistics_cost_cny=None if logistics is None else Decimal(logistics),
            estimated_platform_fee_cny=None),
        expected_operation_ids=tuple(o.operation_id for o in ops),
        operations=ops,
    )


class SkuFakeSource:
    """把 ORDERS 喂给**真实的归属实现**，产出的结构与生产源逐字同形。"""

    def __init__(self):
        self.rows = tuple(_order_row(s) for s in ORDERS)

    # ── 看板路径 ──
    def data_cutoff(self, store_alias):
        return DataCutoff(FAKE_CUTOFF, 'fake.updated_at', {})

    def orders_for_period(self, store_alias, start_date, end_date,
                          limit=None, offset=0):
        rows = [r for r in self.rows
                if r.settlement_date and start_date <= r.settlement_date <= end_date]
        rows.sort(key=lambda r: (r.settlement_date, r.posting_number), reverse=True)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return tuple(rows)

    def amounts_for_period(self, store_alias, start_date, end_date):
        return PeriodAmounts()

    def daily_amounts(self, store_alias, start_date, end_date):
        return ()

    def overdue_count(self, store_alias):
        return OverdueInfo(store_alias, 3, 'fake.scan', '2026-09-18T17:16:00+08:00')

    # ── 下钻路径 ──
    def sku_detail_for_period(self, store_alias, start_date, end_date):
        from core.repository.base import SkuDetail, UnattributedAmounts
        sku_rows, records, actual_unattributed = [], [], []
        for spec, row in zip(ORDERS, self.rows):
            if not (start_date <= row.settlement_date <= end_date):
                continue
            _pn, _day, _net, _rate, revenue, cost, logistics, items, line_rev, units = spec
            line_revenues = (None if line_rev is None else
                             {k: Decimal(v) for k, v in line_rev.items()})
            unit_costs = (None if units is None else
                          {k: Decimal(v) for k, v in units.items()})
            lines, recs = attribute_order_lines(
                posting_number=row.posting_number,
                settlement_date=row.settlement_date,
                items=items,
                revenue_cny=None if revenue is None else Decimal(revenue),
                purchase_cost_cny=None if cost is None else Decimal(cost),
                unit_cost_by_offer=unit_costs,
                line_revenues=line_revenues,
                logistics_cost_cny=None if logistics is None else Decimal(logistics),
                platform_fee_cny=None,
                snapshot=row.snapshot,
                operations=row.operations,
                expected_operation_ids=row.expected_operation_ids,
            )
            sku_rows.extend(lines)
            records.extend(recs)
            if not lines or not lines[0].actual_attributable:
                actual_unattributed.append(row)
        return SkuDetail(
            rows=tuple(sku_rows),
            unattributed=UnattributedAmounts(
                posting_count=len({r.posting_number for r in records
                                   if r.posting_number}),
                records=tuple(records)),
            actual_unattributed=tuple(actual_unattributed),
            order_count=len([r for r in self.rows
                             if start_date <= r.settlement_date <= end_date]),
        )


#: 两个店铺，store_beta **只给 root** —— 没有第二个店铺，403 就测不出来。
SKU_TEST_REGISTRY = StoreRegistry([
    Store('store_alpha', '测试店铺 001', 'C:/nonexistent/store_alpha.db'),
    Store('store_beta', '测试店铺 002', 'C:/nonexistent/store_beta.db'),
])
SKU_TEST_SPECS = MOCK_SPECS


class SkuDetailTestBase(unittest.TestCase):
    def setUp(self):
        self.client, self._restore = make_client(
            source_factory=lambda store: SkuFakeSource(),
            registry=SKU_TEST_REGISTRY)
        self.addCleanup(self._restore)

    def headers(self, username='admin'):
        resp = self.client.post('/api/auth/login',
                                json={'username': username,
                                      'password': MOCK_PASSWORDS[username]})
        self.assertEqual(200, resp.status_code, resp.text)
        return {'Authorization': 'Bearer %s' % resp.json()['access_token']}

    def detail(self, alias='store_alpha', days=2, username='admin', **params):
        query = '&'.join('%s=%s' % (k, v) for k, v in params.items())
        url = '/api/dashboard/store/%s/sku-detail?days=%d' % (alias, days)
        if query:
            url += '&' + query
        resp = self.client.get(url, headers=self.headers(username))
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()


# ══════════════════════════════════════════════════════════════════
# 1. 接口契约
# ══════════════════════════════════════════════════════════════════
REQUIRED_ROW_FIELDS = (
    'offer_id', 'sku', 'product_name', 'quantity', 'order_count',
    'revenue_cny', 'purchase_cost_cny', 'logistics_cny', 'platform_fee_cny',
    'estimated_profit_cny', 'actual_profit_cny', 'complete', 'unknown_reason',
)


class SkuDetailContractTest(SkuDetailTestBase):
    def test_response_shape_and_row_fields(self):
        body = self.detail()
        for key in ('store_alias', 'data_cutoff', 'period', 'query', 'totals',
                    'unattributed', 'reconciliation', 'rows'):
            self.assertIn(key, body)
        self.assertEqual('store_alpha', body['store_alias'])
        self.assertEqual(4, body['totals']['order_count'])
        self.assertEqual(3, body['totals']['sku_count'], body['totals'])
        self.assertTrue(body['rows'])
        for row in body['rows']:
            for field in REQUIRED_ROW_FIELDS:
                self.assertIn(field, row, field)
            self.assertIsInstance(row['complete'], bool)

    def test_period_matches_the_dashboard_window(self):
        """窗口由 data_cutoff 决定（不是系统当天），与主流看板逐字一致。"""
        body = self.detail(days=2)
        self.assertEqual({'start': '2026-09-17', 'end': '2026-09-18', 'days': 2},
                         body['period'])

    def test_amounts_are_strings_convertible_to_decimal(self):
        """金额一律字符串 + 固定 2 位小数（避免 JS 浮点误差）。"""
        body = self.detail()
        money_fields = ('revenue_cny', 'purchase_cost_cny', 'logistics_cny',
                        'platform_fee_cny', 'estimated_profit_cny',
                        'actual_profit_cny')
        checked = 0
        for row in body['rows']:
            for field in money_fields:
                value = row[field]
                if value is None:
                    continue
                self.assertIsInstance(value, str, '%s 不是字符串' % field)
                try:
                    Decimal(value)
                except InvalidOperation:
                    self.fail('%s 不能转成 Decimal: %r' % (field, value))
                self.assertRegex(value, r'^-?\d+\.\d{2}$')
                checked += 1
        self.assertGreater(checked, 0, '一个金额都没检查到，测试等于没跑')
        # 合计同样必须是字符串
        for field in money_fields:
            value = body['totals'][field]
            if value is not None:
                Decimal(value)

    def test_sku_totals_are_hand_checkable(self):
        """手算核对：三个货号的合计（见文件头 ORDERS 的注释）。"""
        body = self.detail()
        totals = body['totals']
        self.assertEqual(5, totals['quantity'])            # 2 + 1 + 1 + 1
        self.assertEqual('450.00', totals['revenue_cny'])  # 290 + 100 + 60
        self.assertEqual('110.00', totals['purchase_cost_cny'])  # 90 + 0 + 20
        self.assertEqual('12.00', totals['logistics_cny'])       # 只有 O1 能归属
        # 预估利润按 §7.2 的定义合计（缺成本按 0 参与）：
        #   A-1 = 128 + 60 = 188；B-2 = 100（成本缺，按 0）；C-3 = 40
        self.assertEqual('328.00', totals['estimated_profit_cny'])
        # 实际利润：只有 O1 整单归属且完整 → 140.00
        self.assertEqual('140.00', totals['actual_profit_cny'])

    def test_multi_item_order_is_not_allocated(self):
        """多货号订单的物流费必须进未归属桶，**不许摊到 A-1 / C-3 头上**。"""
        body = self.detail()
        self.assertEqual('15.00', body['unattributed']['logistics_cny'])
        self.assertIsNone(next(r for r in body['rows']
                               if r['offer_id'] == 'C-3')['logistics_cny'])
        reasons = {(item['reason'], item['field']) for item in
                   body['unattributed']['reasons']}
        self.assertIn(('multi_item_posting', 'logistics_cny'), reasons)

    def test_sorting_only_changes_order_not_totals(self):
        asc = self.detail(sort='estimated_profit_cny', desc='false')
        desc = self.detail(sort='estimated_profit_cny', desc='true')
        self.assertEqual(asc['totals'], desc['totals'])
        asc_ids = [r['offer_id'] for r in asc['rows']]
        desc_ids = [r['offer_id'] for r in desc['rows']]
        # A-1 预估 188 > C-3 40 > B-2（算不出来）
        self.assertEqual(['A-1', 'C-3', 'B-2'], desc_ids)
        self.assertEqual(['C-3', 'A-1', 'B-2'], asc_ids)
        # 缺利润的 SKU 永远排在最后，不会因为「排最高」被顶到第一位
        self.assertEqual('B-2', asc_ids[-1])
        self.assertEqual('B-2', desc_ids[-1])

    def test_search_filters_rows_but_not_totals(self):
        body = self.detail(q='C-3')
        self.assertEqual(1, body['totals']['matched_sku_count'])
        self.assertEqual(['C-3'], [r['offer_id'] for r in body['rows']])
        # 合计仍是全窗口口径 —— 否则「搜索后的和」会和指标卡对不上
        self.assertEqual('450.00', body['totals']['revenue_cny'])
        self.assertEqual(3, body['totals']['sku_count'])

    def test_search_by_product_name_and_wrong_sort_key(self):
        self.assertEqual(1, len(self.detail(q='商品丙')['rows']))
        resp = self.client.get(
            '/api/dashboard/store/store_alpha/sku-detail?days=2&sort=drop_table',
            headers=self.headers())
        self.assertEqual(422, resp.status_code)

    def test_pagination_caps_rows_but_not_totals(self):
        body = self.detail(limit=1)
        self.assertEqual(1, body['totals']['returned_count'])
        self.assertTrue(body['totals']['truncated'])
        self.assertEqual(3, body['totals']['sku_count'])
        self.assertEqual('450.00', body['totals']['revenue_cny'])
        second = self.detail(limit=1, offset=1)
        self.assertNotEqual(body['rows'][0]['offer_id'], second['rows'][0]['offer_id'])

    def test_days_parameter_is_validated(self):
        resp = self.client.get('/api/dashboard/store/store_alpha/sku-detail?days=0',
                               headers=self.headers())
        self.assertEqual(422, resp.status_code)
        resp = self.client.get('/api/dashboard/store/store_alpha/sku-detail?days=9999',
                               headers=self.headers())
        self.assertEqual(422, resp.status_code)


# ══════════════════════════════════════════════════════════════════
# 2. 缺成本：null 而不是 0，且带原因
# ══════════════════════════════════════════════════════════════════
class MissingCostTest(SkuDetailTestBase):
    def test_missing_cost_is_null_with_reason(self):
        body = self.detail()
        row = next(r for r in body['rows'] if r['offer_id'] == 'B-2')
        self.assertIsNone(row['purchase_cost_cny'], '缺成本必须是 null，不能是 0')
        self.assertEqual('missing_purchase_cost', row['unknown_reason'])
        self.assertEqual('缺采购成本', row['unknown_reason_label'])
        # 成本缺失时利润**不可知**：预估与实际都必须为 null
        self.assertIsNone(row['estimated_profit_cny'])
        self.assertIsNone(row['actual_profit_cny'])
        self.assertFalse(row['estimated_complete'])
        self.assertFalse(row['complete'])
        self.assertIn('purchase_cost_cny', row['missing_fields'])

    def test_summary_counts_missing_cost_skus(self):
        totals = self.detail()['totals']
        self.assertEqual(1, totals['missing_cost_sku_count'])
        self.assertEqual('110.00', totals['purchase_cost_cny'],
                         '缺成本的行不进合计，也不许补 0')

    def test_zero_is_never_used_as_a_placeholder_for_cost(self):
        """全表扫一遍：`purchase_cost_cny` 只能是金额字符串或 null。"""
        for row in self.detail()['rows']:
            value = row['purchase_cost_cny']
            if value is not None:
                self.assertNotEqual('0.00', value,
                                    '%s 的成本被写成了 0.00' % row['offer_id'])

    def test_complete_sku_has_no_reason(self):
        row = next(r for r in self.detail()['rows'] if r['offer_id'] == 'A-1')
        self.assertTrue(row['estimated_complete'])
        self.assertFalse(row['actual_complete'])   # O3 的流水归不出去


# ══════════════════════════════════════════════════════════════════
# 3. 越权：403 而不是空数据
# ══════════════════════════════════════════════════════════════════
class SkuDetailPermissionTest(SkuDetailTestBase):
    def test_unauthenticated_is_401(self):
        resp = self.client.get('/api/dashboard/store/store_alpha/sku-detail?days=2')
        self.assertEqual(401, resp.status_code)

    def test_restricted_role_on_unauthorized_store_is_403(self):
        """财务/运营只授权了 store_alpha；访问 store_beta 必须 403，不能返回空表。"""
        for username in ('finance01', 'operator01'):
            resp = self.client.get(
                '/api/dashboard/store/store_beta/sku-detail?days=2',
                headers=self.headers(username))
            self.assertEqual(403, resp.status_code, username)
            self.assertEqual('无权访问该店铺', resp.json()['detail'])
            self.assertNotIn('rows', resp.json())

    def test_authorized_store_still_works_for_restricted_role(self):
        body = self.detail(username='finance01')
        self.assertTrue(body['rows'])

    def test_unknown_alias_is_404(self):
        resp = self.client.get('/api/dashboard/store/nope/sku-detail?days=2',
                               headers=self.headers())
        self.assertEqual(404, resp.status_code)

    def test_path_like_alias_is_rejected(self):
        resp = self.client.get(
            '/api/dashboard/store/..%2F..%2Fetc/sku-detail?days=2',
            headers=self.headers())
        self.assertIn(resp.status_code, (403, 404, 422))


# ══════════════════════════════════════════════════════════════════
# 4. 一致性：逐 SKU 合计 == 订单口径合计
# ══════════════════════════════════════════════════════════════════
class SkuReconciliationTest(SkuDetailTestBase):
    """**最重要的一组**：两条独立路径必须给出同一个合计。"""

    FIELDS = ('revenue_cny', 'purchase_cost_cny', 'logistics_cny',
              'platform_fee_cny', 'estimated_profit_cny', 'actual_profit_cny')

    def test_every_field_matches_the_order_level_path(self):
        body = self.detail()
        rec = body['reconciliation']
        for field in self.FIELDS:
            self.assertTrue(rec['matches'][field],
                            '%s 两条口径不一致：逐 SKU %s vs 订单 %s'
                            % (field, rec['sku_level'][field], rec['order_level'][field]))
        self.assertTrue(rec['matches']['order_count'])
        self.assertEqual(4, rec['order_level']['order_count'])

    def test_order_level_numbers_are_hand_checkable(self):
        """订单口径 = evaluate_actual/evaluate_estimated 直接算的结果。"""
        order = self.detail()['reconciliation']['order_level']
        self.assertEqual('530.00', order['revenue_cny'])        # 200+100+150+80
        self.assertEqual('130.00', order['purchase_cost_cny'])  # 60+0+50+20
        self.assertEqual('27.00', order['logistics_cny'])       # 12+0+15+0
        self.assertIsNone(order['platform_fee_cny'])            # 一个数都没有 → null
        self.assertEqual('373.00', order['estimated_profit_cny'])  # 128+100+85+60
        self.assertEqual('300.00', order['actual_profit_cny'])     # 140+100+60

    def test_sku_plus_unattributed_equals_order_level(self):
        """逐 SKU 合计 + 未归属 == 订单口径合计（逐字段，精确到分）。"""
        body = self.detail()
        rec, un = body['reconciliation'], body['unattributed']
        sku = rec['sku_level']
        for field in ('revenue_cny', 'purchase_cost_cny', 'logistics_cny',
                      'platform_fee_cny'):
            left = (Decimal(sku[field]) if sku[field] else Decimal('0')) \
                + (Decimal(un[field]) if un[field] else Decimal('0'))
            right = Decimal(rec['order_level'][field]) if rec['order_level'][field] \
                else Decimal('0')
            self.assertEqual(right, left, field)
        # §7.2 是线性式，所以「合计的差」= 「差的合计」
        est_left = Decimal(sku['estimated_profit_cny']) \
            + Decimal(un['estimated_profit_cny'])
        self.assertEqual(Decimal(rec['order_level']['estimated_profit_cny']), est_left)
        # §7.1 只能累加完整订单，未归属里的实际利润单独给
        act_left = Decimal(sku['actual_profit_cny']) + Decimal(un['actual_profit_cny'])
        self.assertEqual(Decimal(rec['order_level']['actual_profit_cny']), act_left)

    def test_unattributed_amounts_are_reported_not_dropped(self):
        un = self.detail()['unattributed']
        self.assertEqual('80.00', un['revenue_cny'])        # O4 没有商品明细行
        self.assertEqual('20.00', un['purchase_cost_cny'])
        self.assertEqual('15.00', un['logistics_cny'])      # O3 多货号
        self.assertEqual('45.00', un['estimated_profit_cny'])  # 80 − 20 − 15
        self.assertEqual('160.00', un['actual_profit_cny'])  # O3 100 + O4 60
        self.assertGreater(un['posting_count'], 0)


# ══════════════════════════════════════════════════════════════════
# 归属规则本身（不经 HTTP）
# ══════════════════════════════════════════════════════════════════
def _snapshot(pn, net, cost):
    return SettlementSnapshot(
        posting_number=pn, settlement_date='2026-09-18', state='locked',
        direct_net_rub=Decimal(net), settled_sales_rub=Decimal('100'),
        exchange_rate_rub_per_cny=Decimal('10.0000'),
        purchase_cost_cny=None if cost is None else Decimal(cost),
        operation_ids=('op1',), unknown_reason=None)


class SkuAttributionRuleTest(unittest.TestCase):
    """归属规则：全部是「按数据自带的分组键」，一条摊分逻辑都没有。"""

    def test_single_item_order_is_attributed_whole(self):
        rows, records = attribute_order_lines(
            posting_number='P1', settlement_date='2026-09-18',
            items=(('A-1', '111', '甲', 3),),
            revenue_cny=Decimal('300.00'), purchase_cost_cny=Decimal('90.00'),
            logistics_cost_cny=Decimal('15.00'), platform_fee_cny=None,
            snapshot=_snapshot('P1', '3000', '90'),
            operations=(Operation('op1', Decimal('3000')),),
            expected_operation_ids=('op1',))
        self.assertEqual([], list(records))
        self.assertEqual(1, len(rows))
        self.assertEqual(Decimal('300.00'), rows[0].revenue_cny)
        self.assertEqual(Decimal('90.00'), rows[0].purchase_cost_cny)
        self.assertEqual(Decimal('15.00'), rows[0].logistics_cost_cny)
        self.assertTrue(rows[0].actual_attributable)

    def test_multi_item_revenue_mismatch_goes_to_unattributed_not_allocated(self):
        rows, records = attribute_order_lines(
            posting_number='P2', settlement_date='2026-09-18',
            items=(('A-1', '1', '甲', 1), ('C-3', '3', '丙', 1)),
            revenue_cny=Decimal('150.00'),
            purchase_cost_cny=Decimal('50.00'),
            unit_cost_by_offer={'A-1': Decimal('30.00'), 'C-3': Decimal('20.00')},
            # 行价之和只有 120 ≠ 150 → 拒绝用一个对不上的和冒充
            line_revenues={'A-1': Decimal('120.00')},
            logistics_cost_cny=Decimal('9.00'))
        self.assertIsNone(rows[0].revenue_cny)
        self.assertIsNone(rows[1].revenue_cny)
        # 成本能对上，照样按行归属（互不牵连）
        self.assertEqual(Decimal('30.00'), rows[0].purchase_cost_cny)
        self.assertEqual(Decimal('20.00'), rows[1].purchase_cost_cny)
        # 物流费与流水归不出去
        self.assertIsNone(rows[0].logistics_cost_cny)
        self.assertFalse(rows[0].actual_attributable)
        fields = {(r.field, r.reason) for r in records}
        self.assertIn(('revenue_cny', 'revenue_not_attributable'), fields)
        self.assertIn(('logistics_cny', 'multi_item_posting'), fields)

    def test_multi_item_cost_without_library_is_unattributed(self):
        rows, records = attribute_order_lines(
            posting_number='P3', settlement_date='2026-09-18',
            items=(('A-1', '1', '甲', 1), ('C-3', '3', '丙', 1)),
            revenue_cny=Decimal('150.00'),
            purchase_cost_cny=Decimal('50.00'),
            unit_cost_by_offer=None,           # 没有成本库
            line_revenues={'A-1': Decimal('90.00'), 'C-3': Decimal('60.00')})
        self.assertEqual(Decimal('90.00'), rows[0].revenue_cny)
        self.assertIsNone(rows[0].purchase_cost_cny)
        self.assertIn(('purchase_cost_cny', 'cost_not_attributable'),
                      {(r.field, r.reason) for r in records})

    def test_no_items_means_whole_order_is_unattributed(self):
        rows, records = attribute_order_lines(
            posting_number='P4', settlement_date='2026-09-18', items=(),
            revenue_cny=Decimal('80.00'), purchase_cost_cny=Decimal('20.00'))
        self.assertEqual((), rows)
        # 四个金额字段**都要留下原因**（金额可能是 None，但「为什么归不出去」必须可查）
        self.assertEqual({'missing_posting_item'},
                         {r.reason for r in records})
        self.assertEqual({'revenue_cny', 'purchase_cost_cny', 'logistics_cny',
                          'platform_fee_cny'}, {r.field for r in records})

    def test_missing_cost_is_null_and_never_zero(self):
        rows, _records = attribute_order_lines(
            posting_number='P5', settlement_date='2026-09-18',
            items=(('A-1', '1', '甲', 1),),
            revenue_cny=Decimal('100.00'), purchase_cost_cny=None)
        self.assertIsNone(rows[0].purchase_cost_cny)
        self.assertEqual('missing_purchase_cost', rows[0].cost_unknown_reason)
        self.assertIsNotNone(rows[0].revenue_cny)

    def test_duplicate_offer_lines_are_not_silently_folded(self):
        rows, records = attribute_order_lines(
            posting_number='P6', settlement_date='2026-09-18',
            items=(('A-1', '1', '甲', 1), ('A-1', '1', '甲', 1)),
            revenue_cny=Decimal('100.00'), purchase_cost_cny=Decimal('30.00'),
            line_revenues={'A-1': Decimal('50.00')},
            unit_cost_by_offer={'A-1': Decimal('15.00')})
        self.assertIsNone(rows[0].revenue_cny)
        self.assertIsNone(rows[0].purchase_cost_cny)
        self.assertEqual(2, len(rows))
        fields = {(r.field, r.reason) for r in records}
        self.assertIn(('revenue_cny', 'revenue_not_attributable'), fields)
        self.assertIn(('purchase_cost_cny', 'cost_not_attributable'), fields)


class SkuProfitDomainTest(unittest.TestCase):
    """领域层的逐 SKU 汇总规则（含「不用部分和冒充」）。"""

    def test_partial_cost_makes_the_whole_sku_cost_null(self):
        lines = [
            SkuLineInput(offer_id='A-1', posting_number='P1', quantity=1,
                         revenue_cny=Decimal('100'), purchase_cost_cny=Decimal('30')),
            SkuLineInput(offer_id='A-1', posting_number='P2', quantity=1,
                         revenue_cny=Decimal('100'), purchase_cost_cny=None,
                         cost_unknown_reason='missing_purchase_cost'),
        ]
        profit = evaluate_sku_profit(lines)[0]
        self.assertIsNone(profit.purchase_cost_cny,
                          '两行里只有一行有成本时，不许拿那一行当真')
        self.assertEqual(Decimal('30'), profit.purchase_cost_reconciled_cny,
                         '对账值按「缺失跳过」求和')
        self.assertIn('purchase_cost_cny', profit.missing_fields)

    def test_estimated_uses_the_domain_function(self):
        lines = [SkuLineInput(offer_id='A-1', posting_number='P1', quantity=2,
                              revenue_cny=Decimal('200'),
                              purchase_cost_cny=Decimal('60'),
                              logistics_cost_cny=Decimal('12'))]
        profit = evaluate_sku_profit(lines)[0]
        self.assertEqual(Decimal('128.00'), profit.estimated_profit_cny)
        self.assertEqual(
            evaluate_estimated(Posting('P1', None, Decimal('200'),
                                       Decimal('60'), Decimal('12'), None)
                               ).estimated_profit_cny,
            profit.estimated_profit_cny)

    def test_actual_uses_the_domain_function_and_needs_attribution(self):
        snap = _snapshot('P1', '2000', '60')
        ops = (Operation('op1', Decimal('2000')),)
        lines = [SkuLineInput(offer_id='A-1', posting_number='P1', quantity=2,
                              revenue_cny=Decimal('200'),
                              purchase_cost_cny=Decimal('60'),
                              snapshot=snap, operations=ops,
                              expected_operation_ids=('op1',))]
        profit = evaluate_sku_profit(lines)[0]
        self.assertEqual(Decimal('140.00'), profit.actual_profit_cny)
        self.assertEqual(
            evaluate_actual(snap, ops, ('op1',)).actual_profit_cny,
            profit.actual_profit_cny)

        unattributed = evaluate_sku_profit([
            SkuLineInput(offer_id='A-1', posting_number='P1', quantity=2,
                         revenue_cny=Decimal('200'),
                         purchase_cost_cny=Decimal('60'),
                         actual_attributable=False,
                         actual_unknown_reason='multi_item_posting')])[0]
        self.assertIsNone(unattributed.actual_profit_cny)
        self.assertEqual('multi_item_posting', unattributed.unknown_reason)

    def test_grouping_never_splits_by_ratio(self):
        """两个货号各自拿到自己那部分，合计等于输入之和（没有按比例摊）。"""
        lines = [
            SkuLineInput(offer_id='A-1', posting_number='P1', quantity=1,
                         revenue_cny=Decimal('90'), purchase_cost_cny=Decimal('30')),
            SkuLineInput(offer_id='C-3', posting_number='P1', quantity=1,
                         revenue_cny=Decimal('60'), purchase_cost_cny=Decimal('20')),
        ]
        profits = {p.offer_id: p for p in evaluate_sku_profit(lines)}
        self.assertEqual(Decimal('90'), profits['A-1'].revenue_cny)
        self.assertEqual(Decimal('60'), profits['C-3'].revenue_cny)
        self.assertEqual(Decimal('60.00'), profits['A-1'].estimated_profit_cny)  # 90 − 30
        self.assertEqual(Decimal('40.00'), profits['C-3'].estimated_profit_cny)  # 60 − 20


# ══════════════════════════════════════════════════════════════════
# 真库上的一致性（生产库不存在时整类跳过）
# ══════════════════════════════════════════════════════════════════
def _make_live_client():
    runtime = Runtime(
        source_factory=lambda store: sq.SqliteSource(store.db_path, alias=store.alias),
        user_store=_mock_user_store(),
        registry=StoreRegistry([
            Store('store_alpha', '生产店铺 001', DEFAULT_STORE),
            Store('store_beta', '生产店铺 002',
                  DEFAULT_STORE.replace('store_alpha', 'store_beta')),
        ]),
    )
    old = getattr(app.state, 'runtime', None)
    app.state.runtime = runtime

    def restore():
        app.state.runtime = old

    return TestClient(app), restore


@unittest.skipUnless(os.path.isfile(DEFAULT_STORE), '生产库不存在，跳过真实库下钻测试')
class LiveSkuDetailTest(unittest.TestCase):
    """真库：逐 SKU 合计必须等于订单口径合计，且**六个字段全部精确相等**。"""

    @classmethod
    def setUpClass(cls):
        cls.client, cls._restore = _make_live_client()
        resp = cls.client.post('/api/auth/login',
                               json={'username': 'admin',
                                     'password': MOCK_PASSWORDS['admin']})
        cls.headers = {'Authorization': 'Bearer %s' % resp.json()['access_token']}

    @classmethod
    def tearDownClass(cls):
        cls._restore()

    def body(self, days=14, **params):
        query = '&'.join('%s=%s' % (k, v) for k, v in params.items())
        url = '/api/dashboard/store/store_alpha/sku-detail?days=%d' % days
        if query:
            url += '&' + query
        resp = self.client.get(url, headers=self.headers)
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()

    def test_endpoint_returns_real_sku_data(self):
        body = self.body()
        self.assertGreater(body['totals']['sku_count'], 10,
                           '真库窗口里的 SKU 数不可能这么少')
        self.assertGreater(body['totals']['order_count'], 100)
        self.assertGreater(len(body['rows']), 0)
        self.assertTrue(all(r['offer_id'] for r in body['rows']))
        self.assertTrue(any(r['sku'] for r in body['rows']))
        self.assertTrue(any(r['product_name'] for r in body['rows']))

    def test_all_six_fields_reconcile_exactly_on_the_real_database(self):
        """最重要的一条：真库上「逐 SKU 合计 + 未归属 == 订单口径合计」。"""
        rec = self.body()['reconciliation']
        for field, ok in rec['matches'].items():
            self.assertTrue(ok, '%s 不一致：%s' % (field, rec))
        # 逐字段再独立核一遍（不依赖服务端自己算的 matches）
        for field in ('revenue_cny', 'purchase_cost_cny', 'logistics_cny',
                      'platform_fee_cny', 'estimated_profit_cny', 'actual_profit_cny'):
            left = Decimal(rec['sku_level'][field] or '0')
            right = Decimal(rec['order_level'][field] or '0')
            left += Decimal(self.body()['unattributed'].get(field) or '0')
            self.assertEqual(right, left, field)
        self.assertTrue(rec['matches']['order_count'])

    def test_sku_quantity_total_matches_the_raw_item_table(self):
        """数量走一条**完全独立**的 SQL 路径核对（不经过逐 SKU 归属代码）。"""
        body = self.body()
        start, end = body['period']['start'], body['period']['end']
        source = sq.SqliteSource(DEFAULT_STORE)
        self.addCleanup(source.close)
        raw = source._conn.execute("""
            SELECT sum(i.quantity) FROM posting_items i
             WHERE i.posting_number IN (
                 SELECT s.posting_number FROM settlement_snapshots s
                   JOIN posting_profit_facts f ON f.posting_number = s.posting_number
                  WHERE s.state='locked' AND s.settlement_date BETWEEN ? AND ?)
        """, (start, end)).fetchone()[0]
        self.assertEqual(int(raw), body['totals']['quantity'])

    def test_order_level_matches_the_dashboard_endpoint(self):
        """下钻的订单口径必须与看板接口的 totals 是同一批数。"""
        body = self.body()
        resp = self.client.get('/api/dashboard/store/store_alpha?days=14',
                               headers=self.headers)
        self.assertEqual(200, resp.status_code, resp.text)
        dashboard = resp.json()
        self.assertEqual(dashboard['period'], body['period'])
        self.assertEqual(dashboard['totals']['total_order_count'],
                         body['reconciliation']['order_level']['order_count'])
        self.assertEqual(dashboard['totals']['estimated_profit_cny'],
                         body['reconciliation']['order_level']['estimated_profit_cny'])
        self.assertEqual(dashboard['totals']['actual_profit_cny'],
                         body['reconciliation']['order_level']['actual_profit_cny'])

    def test_pagination_does_not_change_totals(self):
        full = self.body(limit=500)
        page = self.body(limit=5)
        # 只有「下发条数」三个字段允许不同，合计数一个都不许变
        skip = ('returned_count', 'truncated', 'matched_sku_count')
        self.assertEqual({k: v for k, v in full['totals'].items() if k not in skip},
                         {k: v for k, v in page['totals'].items() if k not in skip})
        self.assertEqual(5, page['totals']['returned_count'])
        self.assertTrue(page['totals']['truncated'])
        self.assertFalse(full['totals']['truncated'])
        self.assertEqual(5, len(page['rows']))

    def test_cost_is_never_zero_filled(self):
        body = self.body()
        for row in body['rows']:
            if row['purchase_cost_cny'] is None:
                self.assertIsNotNone(row['unknown_reason'],
                                     '%s 缺成本但没有原因' % row['offer_id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
