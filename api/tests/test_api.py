# -*- coding: utf-8 -*-
"""API 测试。

用 unittest（项目其它地方用的就是它，刻意不引入 pytest）。

两条测试策略，各自解决一个不同的问题：

  * `DashboardContractTest` / `AuthPermissionTest`  —— 用**内存夹具数据源**，
    数字是人工构造的，所以每一项断言都能手算核对。它证明「组装逻辑」对。
  * `RealDatabaseConsistencyTest` —— 打**真实生产库**（只读）。它证明
    「API 发出去的实际利润 == core.domain.profit.evaluate_actual 直接算的」，
    而不是「API 自己算了一套相近的数」。生产库不存在时自动跳过。

跑法：
    python -m unittest discover -s api/tests -t .
"""
import calendar
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from api.app import app
from api.deps import Runtime
from api.stores import Store, StoreRegistry
from api.users import UserStore
from core.domain.profit import (Operation, Posting, SettlementSnapshot,
                                UnknownReason, evaluate_actual,
                                evaluate_estimated, quantize_rate)
from core.repository import sqlite_source as sq
from core.repository.base import (DailyAmounts, DataCutoff, OverdueInfo,
                                  PeriodAmounts, PeriodOrderRow)
from core.repository.fixture_source import DEFAULT_FIXTURE
from core.repository.sqlite_source import DEFAULT_STORE

CN = timezone(timedelta(hours=8))

# ── mock 账号（与 api/data/users.json 的角色一致，口令是测试专用的）──
MOCK_PASSWORDS = {
    'admin': 'test-root',
    'finance01': 'test-finance',
    'operator01': 'test-operator',
}
MOCK_SPECS = [
    {'username': 'admin', 'display_name': '系统管理员', 'role': 'root',
     'password': MOCK_PASSWORDS['admin'], 'store_aliases': []},
    {'username': 'finance01', 'display_name': '财务 01', 'role': 'finance',
     'password': MOCK_PASSWORDS['finance01'], 'store_aliases': ['store_alpha']},
    {'username': 'operator01', 'display_name': '店铺运营 01', 'role': 'store_operator',
     'password': MOCK_PASSWORDS['operator01'], 'store_aliases': ['store_alpha']},
]

#: 两个店铺：store_alpha 授权给运营/财务，store_beta **只给 root**。
#: 没有第二个店铺，403 根本测不出来 —— 而「跨店返回 403 而不是空数据」
#: 正是 PRD §9.2 的硬性要求，必须有真实可复现的用例。
TEST_REGISTRY = StoreRegistry([
    Store('store_alpha', '测试店铺 001', 'C:/nonexistent/store_alpha.db'),
    Store('store_beta', '测试店铺 002', 'C:/nonexistent/store_beta.db'),
])

FAKE_CUTOFF = datetime(2026, 9, 18, 17, 54, 0, tzinfo=CN)

#: 测试用的 PBKDF2 轮数。生产是 20 万轮（防爆破），但每建一次用户要几百毫秒，
#: 整套测试会从 3 秒涨到 2 分钟。这里降到 1000 —— 只影响测试耗时，
#: 不触碰生产强度（生产读的是 users.json 里记录的轮数）。
TEST_ITERATIONS = 1000


def _mock_user_store():
    return UserStore.from_plain_passwords(MOCK_SPECS, iterations=TEST_ITERATIONS)


# ── 内存数据源 ───────────────────────────────────────────────────
class FakeSource:
    """只实现看板路径用到的方法，返回人工构造的数字。

    构造依据（全部手算可核）：
      R1 0571-0234-8901  2026-09-18  净额 8460 汇率 10.0  成本 168.50  → 实际 677.50
      R2 0571-0234-8902  2026-09-17  净额 1000 汇率 10.0  成本 52.00   → 实际 48.00
      R3 0571-0234-8903  2026-09-18  未结算（settled_sales=0）        → 不完整
    """

    def __init__(self):
        d1 = '0571-0234-8901'
        d2 = '0571-0234-8902'
        d3 = '0571-0234-8903'
        self.rows = (
            self._row(d1, '2026-09-18', direct_net_rub='8460.00', rate='10.0000',
                      cost_cny='168.50', settled_sales_rub='1000.00',
                      revenue='900.00', logistics='12.30', fee=None, n_ops=2),
            self._row(d2, '2026-09-17', direct_net_rub='1000.00', rate='10.0000',
                      cost_cny='52.00', settled_sales_rub='120.00',
                      revenue='120.00', logistics='12.37', fee=None, n_ops=1),
            # 第三单：没有已结算销售额 —— 领域层必须给出 no_settled_sale，
            # 且它不得进入实际利润合计（「绝不用 0 顶替」的落点）
            self._row(d3, '2026-09-18', direct_net_rub='770.00', rate='10.0000',
                      cost_cny='18.80', settled_sales_rub='0',
                      revenue=None, logistics='5.61', fee=None, n_ops=1,
                      reference_estimated='42.00'),
        )

    @staticmethod
    def _row(pn, day, direct_net_rub, rate, cost_cny, settled_sales_rub,
             revenue, logistics, fee, n_ops, reference_estimated=None):
        net = Decimal(direct_net_rub)
        ops = tuple(
            Operation(operation_id='%s-op%d' % (pn, i),
                      amount_rub=(net / n_ops).quantize(Decimal('0.01')))
            for i in range(n_ops))
        expected = tuple(o.operation_id for o in ops)
        return PeriodOrderRow(
            posting_number=pn,
            settlement_date=day,
            snapshot=SettlementSnapshot(
                posting_number=pn, settlement_date=day, state='locked',
                direct_net_rub=net,
                settled_sales_rub=Decimal(settled_sales_rub),
                exchange_rate_rub_per_cny=Decimal(rate),
                purchase_cost_cny=Decimal(cost_cny),
                operation_ids=expected,
                unknown_reason=None),
            posting=Posting(
                posting_number=pn, status='delivered',
                revenue_cny=None if revenue is None else Decimal(revenue),
                purchase_cost_cny=Decimal(cost_cny),
                logistics_cost_cny=None if logistics is None else Decimal(logistics),
                estimated_platform_fee_cny=None if fee is None else Decimal(fee)),
            expected_operation_ids=expected,
            operations=ops,
            reference_estimated_profit_cny=(
                None if reference_estimated is None else Decimal(reference_estimated)),
        )

    # ── 看板路径用到的方法 ──
    def data_cutoff(self, store_alias):
        return DataCutoff(FAKE_CUTOFF, 'fake.updated_at', {})

    def orders_for_period(self, store_alias, start_date, end_date,
                          limit=None, offset=0):
        rows = [r for r in self.rows
                if r.settlement_date and start_date <= r.settlement_date <= end_date]
        rows.sort(key=lambda r: r.settlement_date, reverse=True)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return tuple(rows)

    def amounts_for_period(self, store_alias, start_date, end_date):
        rows = self.orders_for_period(store_alias, start_date, end_date)
        actuals = [evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
                   for r in rows]
        est = [evaluate_estimated(r.posting).estimated_profit_cny
               for r in rows if r.posting is not None]
        return PeriodAmounts(
            total_order_count=len(rows),
            complete_order_count=sum(1 for a in actuals if a.complete),
            actual_profit_cny=sum((a.actual_profit_cny for a in actuals
                                   if a.complete), Decimal('0')),
            estimated_profit_cny=sum(est, Decimal('0')) if est else None,
            direct_net_cny=Decimal('946.00'),
            purchase_cost_cny=Decimal('239.30'),
            platform_fee_cny=None,
            logistics_cost_cny=Decimal('30.28'))

    def daily_amounts(self, store_alias, start_date, end_date):
        return (DailyAmounts('2026-09-17', Decimal('48.00'), Decimal('55.63'), 1, 1),
                DailyAmounts('2026-09-18', Decimal('677.50'), Decimal('719.20'), 2, 1))

    def overdue_count(self, store_alias):
        return OverdueInfo(store_alias, 4, 'fake.scan', '2026-09-18T17:16:00+08:00')


class NoOverdueSource(FakeSource):
    """逾期口径缺失的数据源：必须下发 null，而不是 0。"""

    def overdue_count(self, store_alias):
        return OverdueInfo(store_alias, None, 'fake', available=False,
                           note='测试用：没有逾期扫描数据')


def make_client(source_factory=None, users=None, registry=None):
    """装配一个测试客户端，并返回 (client, restore)。

    替换点是 `app.state.runtime` —— 生产代码里这是唯一的可替换件，
    所以测试没有触碰任何业务分支，也没改模块属性。

    （曾经的写法是替换 `api.deps` 的模块级函数，结果路由模块用
    `from ..deps import user_store` 早就绑定了原函数对象，替换无效、
    登录一直 401。教训：可替换件要显式放进一个对象里，别靠改模块属性。）
    """
    runtime = Runtime(
        source_factory=source_factory or (lambda store: FakeSource()),
        user_store=users or _mock_user_store(),
        registry=registry or TEST_REGISTRY,
    )
    old = getattr(app.state, 'runtime', None)
    app.state.runtime = runtime

    def restore():
        app.state.runtime = old

    return TestClient(app), restore


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.client, self._restore = make_client()
        self.addCleanup(self._restore)

    def token(self, username):
        resp = self.client.post('/api/auth/login',
                                json={'username': username,
                                      'password': MOCK_PASSWORDS[username]})
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()['access_token']

    def auth(self, username):
        return {'Authorization': 'Bearer %s' % self.token(username)}


# ── 认证 ─────────────────────────────────────────────────────────
class AuthTest(ApiTestBase):
    def test_login_returns_token_and_user(self):
        resp = self.client.post('/api/auth/login',
                                json={'username': 'admin',
                                      'password': MOCK_PASSWORDS['admin']})
        self.assertEqual(200, resp.status_code)
        body = resp.json()
        self.assertTrue(body['access_token'])
        self.assertEqual('admin', body['user']['username'])
        self.assertEqual('root', body['user']['role'])
        self.assertNotIn('password_hash', body['user'], '口令哈希绝不能下发')

    def test_login_wrong_password_is_401(self):
        resp = self.client.post('/api/auth/login',
                                json={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(401, resp.status_code)

    def test_login_unknown_user_is_401_with_same_message(self):
        a = self.client.post('/api/auth/login',
                             json={'username': 'admin', 'password': 'wrong'})
        b = self.client.post('/api/auth/login',
                             json={'username': 'nobody', 'password': 'wrong'})
        self.assertEqual(401, a.status_code)
        self.assertEqual(401, b.status_code)
        # 两种失败不能给出可区分的提示，否则等于提供了用户名枚举接口
        self.assertEqual(a.json()['detail'], b.json()['detail'])

    def test_me_requires_login(self):
        resp = self.client.get('/api/auth/me')
        self.assertEqual(401, resp.status_code)
        self.assertEqual({'detail': '请先登录'}, resp.json())

    def test_me_lists_only_authorized_stores(self):
        resp = self.client.get('/api/auth/me', headers=self.auth('operator01'))
        self.assertEqual(200, resp.status_code)
        body = resp.json()
        self.assertEqual(['store_alpha'], body['user']['store_aliases'])
        self.assertEqual(['store_alpha'], [s['alias'] for s in body['stores']])

    def test_root_sees_all_stores(self):
        resp = self.client.get('/api/auth/me', headers=self.auth('admin'))
        aliases = [s['alias'] for s in resp.json()['stores']]
        self.assertEqual(['store_alpha', 'store_beta'], sorted(aliases))

    def test_garbage_token_is_401(self):
        resp = self.client.get('/api/dashboard/store/store_alpha',
                               headers={'Authorization': 'Bearer not-a-jwt'})
        self.assertEqual(401, resp.status_code)
        self.assertEqual({'detail': '请先登录'}, resp.json())

    def test_health_is_public_and_declares_readonly(self):
        resp = self.client.get('/api/health')
        self.assertEqual(200, resp.status_code, resp.text)
        body = resp.json()
        self.assertEqual('ok', body['status'])
        self.assertIs(True, body['data_source_readonly'])


# ── 数据隔离（PRD §9.2 硬性要求）──────────────────────────────────
class AuthPermissionTest(ApiTestBase):
    def test_unauthenticated_dashboard_is_401(self):
        resp = self.client.get('/api/dashboard/store/store_alpha')
        self.assertEqual(401, resp.status_code, resp.text)
        self.assertEqual({'detail': '请先登录'}, resp.json())

    def test_unauthorized_store_is_403_not_empty_data(self):
        """核心用例：跨店必须 403，且响应体里**不能有任何业务数据**。

        「返回 0 单 / 空数组 + 200」是上一版的错误做法：调用方会把
        「没权限」当成「这段时间没订单」，于是问题被静默掩盖。
        """
        resp = self.client.get('/api/dashboard/store/store_beta',
                               headers=self.auth('operator01'))
        self.assertEqual(403, resp.status_code, resp.text)
        self.assertEqual({'detail': '无权访问该店铺'}, resp.json())
        body = resp.json()
        # 响应体里只能有 detail 这一个键 —— 任何 totals/orders/trend 都是泄漏
        self.assertEqual({'detail'}, set(body.keys()))
        for leak in ('totals', 'orders', 'trend', 'composition', 'period', 'data_cutoff'):
            self.assertNotIn(leak, body, '越权响应里出现了业务字段 %s' % leak)

    def test_finance_role_is_also_blocked_across_stores(self):
        resp = self.client.get('/api/dashboard/store/store_beta',
                               headers=self.auth('finance01'))
        self.assertEqual(403, resp.status_code, resp.text)
        self.assertEqual({'detail': '无权访问该店铺'}, resp.json())

    def test_root_can_access_second_store(self):
        resp = self.client.get('/api/dashboard/store/store_beta',
                               headers=self.auth('admin'))
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual('store_beta', resp.json()['store_alias'])

    def test_unregistered_alias_is_404(self):
        resp = self.client.get('/api/dashboard/store/nope',
                               headers=self.auth('admin'))
        self.assertEqual(404, resp.status_code, resp.text)

    def test_alias_pattern_rejects_path_like_input(self):
        resp = self.client.get('/api/dashboard/store/..%2F..%2Fetc%2Fpasswd',
                               headers=self.auth('admin'))
        self.assertIn(resp.status_code, (404, 422), resp.text)

    def test_root_cannot_delete_self(self):
        """PRD §9.2：root 不能停用/删除自己（模型层约束，为后续写接口预留）。"""
        store = _mock_user_store()
        self.assertFalse(store.get('admin').can_delete_self())
        self.assertTrue(store.get('operator01').can_delete_self())


# ── 看板契约 ─────────────────────────────────────────────────────
class DashboardContractTest(ApiTestBase):
    def dashboard(self, alias='store_alpha', days=14, username='admin'):
        resp = self.client.get('/api/dashboard/store/%s?days=%d' % (alias, days),
                               headers=self.auth(username))
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()

    def test_top_level_shape_matches_frontend_contract(self):
        body = self.dashboard()
        self.assertEqual(
            {'store_alias', 'data_cutoff', 'period', 'totals', 'trend',
             'composition', 'orders'},
            set(body.keys()))
        self.assertEqual('store_alpha', body['store_alias'])

    def test_period_is_inclusive_and_ends_at_data_cutoff(self):
        """窗口右端必须是数据截止日，不是系统当天 —— 否则最后几天永远为空。"""
        body = self.dashboard(days=14)
        period = body['period']
        self.assertEqual('2026-09-18', period['end'])
        self.assertEqual('2026-09-05', period['start'])
        self.assertEqual(14, period['days'])
        self.assertEqual('2026-09-18T17:54:00+08:00', body['data_cutoff'])

    def test_totals_are_hand_checkable(self):
        totals = self.dashboard()['totals']
        # 实际利润：677.50 + 48.00 = 725.50（第三单不完整，不得计入）
        self.assertEqual('725.50', totals['actual_profit_cny'])
        # 预估利润：719.20 + (−24.41) + 55.63 = 750.42
        # 第三单没有 revenue，evaluate_estimated 按 0 顶 revenue 算出 −24.41，
        # 这是 §7.2 的既有行为（缺失项按 0 参与，完整性另用 complete 标记），
        # 所以它**可以**进预估合计 —— 与 §7.1「不完整就不合计」不是一回事。
        self.assertEqual('750.42', totals['estimated_profit_cny'])
        self.assertEqual('0.6667', totals['completion_rate'])
        self.assertEqual(2, totals['complete_order_count'])
        self.assertEqual(3, totals['total_order_count'])
        self.assertEqual(4, totals['overdue_count'])

    def test_amounts_are_strings_and_convertible_to_decimal(self):
        """金额字段要么是能转成 Decimal 的字符串，要么是 null。

        不允许出现 JSON 数字：JS 的 Number 是双精度浮点，金额一旦变成
        number，前端做一次累加就可能有 0.1+0.2 的尾差。
        """
        body = self.dashboard()
        amounts = [body['totals']['actual_profit_cny'],
                   body['totals']['estimated_profit_cny']]
        amounts += [p['actual_profit_cny'] for p in body['trend']]
        amounts += [p['estimated_profit_cny'] for p in body['trend']]
        amounts += [c['value_cny'] for c in body['composition']]
        for o in body['orders']:
            amounts += [o['direct_net_rub'], o['exchange_rate_rub_per_cny'],
                        o['purchase_cost_cny'], o['actual_profit_cny']]
        seen_null = 0
        for value in amounts:
            if value is None:
                seen_null += 1
                continue
            self.assertIsInstance(value, str, '金额必须是字符串: %r' % (value,))
            Decimal(value)  # 不能转成 Decimal 就是契约破裂
        # 缺失金额必须是 null，而不是 "0.00"
        self.assertGreater(seen_null, 0, '夹具应当含缺失金额，否则这条断言没验证到东西')
        self.assertEqual('0.6667', body['totals']['completion_rate'])
        self.assertIsInstance(body['totals']['overdue_count'], int)
        self.assertIsInstance(body['totals']['complete_order_count'], int)

    def test_incomplete_order_reports_reason_and_null_profit(self):
        orders = {o['posting_number']: o for o in self.dashboard()['orders']}
        bad = orders['0571-0234-8903']
        self.assertFalse(bad['complete'])
        self.assertEqual(UnknownReason.NO_SETTLED_SALE.value, bad['unknown_reason'])
        # 关键：算不出利润时是 null，不是 "0.00"
        self.assertIsNone(bad['actual_profit_cny'])
        good = orders['0571-0234-8901']
        self.assertTrue(good['complete'])
        self.assertIsNone(good['unknown_reason'])
        self.assertEqual('677.50', good['actual_profit_cny'])
        self.assertEqual('8460.00', good['direct_net_rub'])
        self.assertEqual('10.0000', good['exchange_rate_rub_per_cny'])
        self.assertEqual('168.50', good['purchase_cost_cny'])
        self.assertEqual('2026-09-18', good['settlement_date'])

    def test_trend_only_contains_dates_with_data(self):
        trend = self.dashboard()['trend']
        self.assertEqual(['2026-09-17', '2026-09-18'], [p['date'] for p in trend])
        self.assertEqual('48.00', trend[0]['actual_profit_cny'])
        self.assertEqual('677.50', trend[1]['actual_profit_cny'])
        self.assertEqual('55.63', trend[0]['estimated_profit_cny'])
        self.assertEqual('694.79', trend[1]['estimated_profit_cny'])  # 719.20 − 24.41
        # 两天合计必须等于顶部合计 —— 明细与合计出自同一批数
        self.assertEqual('725.50',
                         str(sum(Decimal(p['actual_profit_cny']) for p in trend)))
        self.assertEqual('750.42',
                         str(sum(Decimal(p['estimated_profit_cny']) for p in trend)))

    def test_composition_has_four_fixed_items(self):
        comp = self.dashboard()['composition']
        self.assertEqual(['direct_net', 'purchase', 'platform', 'logistics'],
                         [c['key'] for c in comp])
        self.assertEqual(['直接净额', '采购成本', '平台费用', '物流费用'],
                         [c['label'] for c in comp])
        values = {c['key']: c['value_cny'] for c in comp}
        # 8460/10 + 1000/10 = 946.00（第三单没有直接净额，跳过而不是按 0 计入）
        self.assertEqual('946.00', values['direct_net'])
        # 168.50 + 52.00 = 220.50。
        # 第三单的 18.80 不在里面：evaluate_actual 在「未结算销售」这一关就返回了，
        # 它的 ActualProfit 里 purchase_cost_cny 是 None。这是 core 的既有行为，
        # 不是这里漏加 —— 与 trend 里预估利润含 −24.41 并不矛盾（两套口径的失败点不同）。
        self.assertEqual('220.50', values['purchase'])
        self.assertEqual('0.00', values['platform'])       # 该列全库为空，按展示折 0
        self.assertEqual('30.28', values['logistics'])     # 12.30 + 12.37 + 5.61

    def test_days_parameter_is_validated(self):
        self.assertEqual(422, self.client.get(
            '/api/dashboard/store/store_alpha?days=0',
            headers=self.auth('admin')).status_code)
        self.assertEqual(422, self.client.get(
            '/api/dashboard/store/store_alpha?days=999',
            headers=self.auth('admin')).status_code)

    def test_missing_overdue_data_is_null_not_zero(self):
        client, restore = make_client(
            source_factory=lambda store: NoOverdueSource())
        self.addCleanup(restore)
        token = client.post('/api/auth/login',
                            json={'username': 'admin',
                                  'password': MOCK_PASSWORDS['admin']}
                            ).json()['access_token']
        body = client.get('/api/dashboard/store/store_alpha',
                          headers={'Authorization': 'Bearer %s' % token}).json()
        self.assertIsNone(body['totals']['overdue_count'],
                          '查不到逾期口径时必须下发 null，不能用 0 冒充')

    def test_operators_see_identical_numbers_for_their_own_store(self):
        """同店铺不同角色，数字必须一样 —— 隔离只影响「能不能看」，不影响「看到什么」。"""
        a = self.dashboard(username='admin')['totals']
        b = self.dashboard(username='operator01')['totals']
        c = self.dashboard(username='finance01')['totals']
        self.assertEqual(a, b)
        self.assertEqual(a, c)


# ── 口径一致性：与 core.domain.profit 直算逐单比对 ────────────────
class CanonicalFormulaTest(ApiTestBase):
    """证明 API 的每个订单金额就是 evaluate_actual 的结果，而不是另算的一套。"""

    def test_each_order_matches_evaluate_actual(self):
        source = FakeSource()
        body = self.client.get('/api/dashboard/store/store_alpha',
                               headers=self.auth('admin')).json()
        by_pn = {r.posting_number: r for r in source.rows}
        checked = 0
        for order in body['orders']:
            row = by_pn[order['posting_number']]
            direct = evaluate_actual(row.snapshot, row.operations,
                                     row.expected_operation_ids)
            self.assertEqual(direct.complete, order['complete'])
            self.assertEqual(direct.unknown_reason, order['unknown_reason'])
            for api_value, core_value in (
                    (order['actual_profit_cny'], direct.actual_profit_cny),
                    (order['direct_net_rub'], direct.direct_net_rub),
                    (order['exchange_rate_rub_per_cny'], direct.exchange_rate),
                    (order['purchase_cost_cny'], direct.purchase_cost_cny)):
                if core_value is None:
                    self.assertIsNone(api_value)
                else:
                    self.assertIsNotNone(api_value)
                    self.assertEqual(core_value, Decimal(api_value))
            checked += 1
        self.assertEqual(3, checked)

    def test_totals_match_core_sum_over_complete_orders(self):
        body = self.client.get('/api/dashboard/store/store_alpha',
                               headers=self.auth('admin')).json()
        source = FakeSource()
        results = [evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
                   for r in source.rows]
        want = sum((r.actual_profit_cny for r in results
                    if r.complete and r.actual_profit_cny is not None), Decimal('0'))
        self.assertEqual(want, Decimal(body['totals']['actual_profit_cny']))

    def test_composition_direct_net_equals_core_operation_sum_over_rate(self):
        body = self.client.get('/api/dashboard/store/store_alpha',
                               headers=self.auth('admin')).json()
        source = FakeSource()
        want = Decimal('0')
        for row in source.rows:
            direct = evaluate_actual(row.snapshot, row.operations,
                                     row.expected_operation_ids)
            if direct.direct_net_rub is not None and direct.exchange_rate is not None:
                want += direct.direct_net_rub / direct.exchange_rate
        got = [c for c in body['composition'] if c['key'] == 'direct_net'][0]['value_cny']
        self.assertEqual(want, Decimal(got))

    def test_estimated_total_equals_core_evaluate_estimated_sum(self):
        body = self.client.get('/api/dashboard/store/store_alpha',
                               headers=self.auth('admin')).json()
        source = FakeSource()
        want = sum((evaluate_estimated(r.posting).estimated_profit_cny
                    for r in source.rows if r.posting is not None), Decimal('0'))
        self.assertEqual(want, Decimal(body['totals']['estimated_profit_cny']))


# ── 真实生产库（只读）─────────────────────────────────────────────
def _make_live_client():
    """用真实 SqliteSource 装配客户端。生产库不存在时调用方会 skip。"""
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


@unittest.skipUnless(os.path.isfile(DEFAULT_STORE), '生产库不存在，跳过真实库一致性测试')
class RealDatabaseConsistencyTest(unittest.TestCase):
    """打真实生产库（只读，带写保护自检）。

    这一组测试回答的问题是：**API 真的连到了生产库、真的按 core 算出了数**，
    而不是「接口能返回 200」。任何一项在这里失败，都说明 API 在偷偷自算。
    """

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

    def live_body(self, days=14):
        resp = self.client.get('/api/dashboard/store/store_alpha?days=%d' % days,
                               headers=self.headers)
        self.assertEqual(200, resp.status_code, resp.text)
        return resp.json()

    def test_source_is_readonly(self):
        with sq.SqliteSource(DEFAULT_STORE) as src:
            with self.assertRaises(Exception):
                src._conn.execute('CREATE TABLE __api_probe(x)')

    def test_dashboard_reflects_real_production_numbers(self):
        body = self.live_body()
        self.assertEqual('store_alpha', body['store_alias'])
        totals = body['totals']
        self.assertGreater(totals['total_order_count'], 100,
                           '14 天窗口的真实订单数不可能这么少 —— 疑似没连上生产库')
        self.assertGreater(Decimal(totals['actual_profit_cny']), Decimal('0'))
        self.assertIsInstance(totals['overdue_count'], int)
        self.assertEqual(14, len(body['trend']))
        self.assertEqual(14, body['period']['days'])
        # 库里的时间戳是 UTC；下发的截止时间必须是北京时间带 +08:00
        self.assertTrue(body['data_cutoff'].endswith('+08:00'),
                        'data_cutoff 未转成北京时间: %s' % body['data_cutoff'])
        self.assertRegex(body['data_cutoff'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')

    def test_each_api_order_matches_evaluate_actual_on_real_db(self):
        """核心断言：真实库上，API 的订单金额与 evaluate_actual 直算**逐个相同**。

        取数走 core 自己的 `operations_for_linked_ids`，与看板走同一条口径，
        这样比的才是「有没有另算」，而不是「取数方式不同」。
        """
        body = self.live_body()
        with sq.SqliteSource(DEFAULT_STORE) as src:
            checked = 0
            for order in body['orders']:
                pn = order['posting_number']
                snap = src.settlement_snapshot(pn)
                ids = list(src.linked_operation_ids(pn))
                ops = src.operations_for_linked_ids(pn)
                direct = evaluate_actual(snap, ops, ids)
                self.assertEqual(direct.complete, order['complete'], pn)
                self.assertEqual(direct.unknown_reason, order['unknown_reason'], pn)
                if direct.actual_profit_cny is None:
                    self.assertIsNone(order['actual_profit_cny'], pn)
                else:
                    self.assertEqual(direct.actual_profit_cny,
                                     Decimal(order['actual_profit_cny']), pn)
                    self.assertEqual(direct.direct_net_rub,
                                     Decimal(order['direct_net_rub']), pn)
                    # 汇率下发前按 PRD §7.5 收敛到 4 位小数，所以比的是收敛后的值。
                    # 库里存的是 28 位精度的完整小数，直接比会「永远不等」。
                    self.assertEqual(quantize_rate(direct.exchange_rate),
                                     Decimal(order['exchange_rate_rub_per_cny']), pn)
                    self.assertEqual(direct.purchase_cost_cny,
                                     Decimal(order['purchase_cost_cny']), pn)
                checked += 1
            self.assertGreater(checked, 0, '真实库里没有可比对的订单')
            print('\n[真实库] 逐单核对 %d 单，全部与 evaluate_actual 一致' % checked)

    def test_totals_equal_sum_of_all_period_orders(self):
        """顶部合计必须等于全窗口逐单精算之和（orders 数组可能被截断）。"""
        body = self.live_body()
        source = sq.SqliteSource(DEFAULT_STORE)
        self.addCleanup(source.close)
        rows = source.orders_for_period('store_alpha', body['period']['start'],
                                        body['period']['end'])
        results = [evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
                   for r in rows]
        want_actual = sum((r.actual_profit_cny for r in results
                           if r.complete and r.actual_profit_cny is not None),
                          Decimal('0'))
        want_est = sum((evaluate_estimated(r.posting).estimated_profit_cny for r in rows
                        if r.posting is not None), Decimal('0'))
        self.assertEqual(want_actual, Decimal(body['totals']['actual_profit_cny']))
        self.assertEqual(want_est, Decimal(body['totals']['estimated_profit_cny']))
        self.assertEqual(len(rows), body['totals']['total_order_count'])
        self.assertEqual(sum(1 for r in results if r.complete),
                         body['totals']['complete_order_count'])

    def test_cheap_aggregate_methods_agree_with_order_level_path(self):
        """仓储层的两个廉价聚合方法必须与逐单路径给出同一个合计。

        它们走 SQL 的 REAL 聚合，与 Decimal 精算天然有一丁点差别；
        这条测试就是那道闸门 —— 一旦差出 1 分以上，说明两条路径的口径漂了。
        （开发过程中这里真的抓到过一个 bug：`estimated_complete=1` 这个
        全库标记会把窗口内 56% 的订单挡掉，导致按天合计只有总计的 44%。）
        """
        body = self.live_body()
        source = sq.SqliteSource(DEFAULT_STORE)
        self.addCleanup(source.close)
        start, end = body['period']['start'], body['period']['end']
        cheap = source.amounts_for_period('store_alpha', start, end)
        daily = source.daily_amounts('store_alpha', start, end)
        self.assertEqual(Decimal(body['totals']['actual_profit_cny']),
                         cheap.actual_profit_cny)
        self.assertEqual(Decimal(body['totals']['estimated_profit_cny']),
                         cheap.estimated_profit_cny)
        self.assertEqual(cheap.actual_profit_cny,
                         sum((d.actual_profit_cny for d in daily
                              if d.actual_profit_cny is not None), Decimal('0')))
        self.assertEqual(cheap.estimated_profit_cny,
                         sum((d.estimated_profit_cny for d in daily
                              if d.estimated_profit_cny is not None), Decimal('0')))

    def test_period_totals_change_with_days(self):
        """窗口变长，合计必须跟着变 —— 防止 days 参数被忽略（写死缓存之类）。"""
        small = Decimal(self.live_body(days=7)['totals']['actual_profit_cny'])
        large = Decimal(self.live_body(days=14)['totals']['actual_profit_cny'])
        self.assertNotEqual(small, large)
        self.assertLess(small, large)


# ── 夹具数据源（无生产库的机器也能跑）────────────────────────────
def _fixture_period(src):
    """从夹具自身推出一个整月对账窗口 —— 不写死 '2026-05'。

    为什么不能写死：公开子集的夹具是**合成**的（`ops/publish/publish.json`
    只保留 24 单、月份与真实夹具不同），写死月份会让窗口在合成夹具上变空。
    空窗口不是「测试通过」，而是两条断言同时失去意义并报错：
    `orders_for_period` 返回 0 条 → `assertGreater(len(rows), 0)` 失败；
    `amounts_for_period` 对空集返回 `actual_profit_cny=None` →
    `Decimal('0') != None` 失败。也就是说原来那两条断言把
    「私密夹具恰好有 2026-05 的订单」当成了前提，而不是在验证代码。

    取「订单数最多的那个月」：保证窗口非空、且真的走过日期过滤逻辑；
    并列时取月份最早的一个，让结果稳定可复现。
    真实夹具推出 2026-05（与改动前逐字相同），合成夹具推出它自己的月份。
    """
    per_month = {}
    for pn in src.list_posting_numbers():
        snap = src.settlement_snapshot(pn)
        day = snap.settlement_date if snap is not None else None
        if not day:
            continue
        month = str(day)[:7]
        per_month[month] = per_month.get(month, 0) + 1
    if not per_month:
        return None
    month = sorted(per_month, key=lambda m: (-per_month[m], m))[0]
    year, mon = month.split('-')
    last_day = calendar.monthrange(int(year), int(mon))[1]
    return '%s-01' % month, '%s-%02d' % (month, last_day)


@unittest.skipUnless(os.path.isfile(DEFAULT_FIXTURE), '黄金夹具不存在，跳过')
class FixtureSourceAggregateTest(unittest.TestCase):
    """夹具数据源能实现的部分必须与逐单路径一致；实现不了的要明确抛错。"""

    def setUp(self):
        from core.repository.fixture_source import FixtureSource
        self.src = FixtureSource(DEFAULT_FIXTURE)  # 纯内存对象，无需关闭
        # 预期窗口由夹具自身推导，真实夹具与合成夹具都能跑（见 _fixture_period）
        period = _fixture_period(self.src)
        self.assertIsNotNone(period, '夹具里没有带结算日期的订单，无法确定对账窗口')
        self.start_date, self.end_date = period

    def _manual_window(self):
        """独立预期：不经过 orders_for_period，直接逐单扫 settlement_date。

        聚合方法的订单数必须锚在这个数上 —— 否则「方法 A 与它自己派生的
        方法 B 一致」这种断言，在被测方法整体漏单时是查不出来的。
        """
        out = []
        for pn in self.src.list_posting_numbers():
            snap = self.src.settlement_snapshot(pn)
            if snap and snap.settlement_date \
                    and self.start_date <= snap.settlement_date[:10] <= self.end_date:
                out.append(pn)
        return out

    def test_fixture_orders_for_period_matches_manual_scan(self):
        """区间取单必须与「逐单扫 settlement_date」得到同一批订单。"""
        rows = self.src.orders_for_period('store_alpha',
                                          self.start_date, self.end_date)
        manual = self._manual_window()
        self.assertGreater(len(manual), 0,
                           '推导出的窗口里没有订单，下面的比对会退化成 0 == 0')
        self.assertEqual(sorted(manual), sorted(r.posting_number for r in rows))
        # 每条返回的行都必须真的落在窗口内（防止窗口比较用错方向/用错字段）
        for r in rows:
            self.assertIsNotNone(r.settlement_date, r.posting_number)
            self.assertLessEqual(self.start_date, r.settlement_date[:10])
            self.assertLessEqual(r.settlement_date[:10], self.end_date)

    def test_fixture_daily_equals_order_level(self):
        """按天合计与区间合计都必须等于逐单精算之和 —— 三条路径一个口径。"""
        start, end = self.start_date, self.end_date
        rows = self.src.orders_for_period('store_alpha', start, end)
        self.assertGreater(len(rows), 0, '窗口为空，等于什么都没验证')
        results = [evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
                   for r in rows]
        want = sum((x.actual_profit_cny for x in results
                    if x.complete and x.actual_profit_cny is not None),
                   Decimal('0'))
        daily = self.src.daily_amounts('store_alpha', start, end)
        got = sum((d.actual_profit_cny for d in daily
                   if d.actual_profit_cny is not None), Decimal('0'))
        self.assertEqual(want, got)
        # 逐日桶的订单数之和必须等于独立扫描出来的窗口订单数
        # （防止某天被静默丢桶 / 整批漏单）
        self.assertEqual(len(self._manual_window()),
                         sum(d.order_count for d in daily))
        # 廉价聚合路径与按天路径必须是同一个口径
        self.assertEqual(got, self.src.amounts_for_period(
            'store_alpha', start, end).actual_profit_cny)

    def test_fixture_amounts_for_period_matches_order_level(self):
        """区间合计必须等于逐单精算之和，而不是另算一套口径。

        预期值全部由夹具自身推导（逐单 reference / evaluate_estimated），
        不写死金额或订单数 —— 真实夹具与合成夹具的数值完全不同，
        写死任何一个数都只在私密仓库成立。
        """
        start, end = self.start_date, self.end_date
        rows = self.src.orders_for_period('store_alpha', start, end)
        self.assertGreater(len(rows), 0, '窗口为空，合计断言会退化成 0 == None')

        want_actual = sum((r.reference_actual_profit_cny for r in rows
                           if r.reference_actual_profit_cny is not None),
                          Decimal('0'))
        want_complete = sum(1 for r in rows
                            if r.reference_actual_profit_cny is not None)
        want_estimated = sum((evaluate_estimated(r.posting).estimated_profit_cny
                              for r in rows if r.posting is not None),
                             Decimal('0'))
        self.assertGreater(want_complete, 0,
                           '窗口里没有一单完整订单，金额断言没有内容可验')

        got = self.src.amounts_for_period('store_alpha', start, end)
        # 订单数锚在独立扫描上，而不是锚在「同一个方法返回的行数」上
        self.assertEqual(len(self._manual_window()), got.total_order_count)
        self.assertEqual(len(rows), got.total_order_count)
        self.assertEqual(want_complete, got.complete_order_count)
        self.assertEqual(want_actual, got.actual_profit_cny)
        self.assertEqual(want_estimated, got.estimated_profit_cny)

    def test_fixture_overdue_count_raises_not_implemented(self):
        """夹具没有冻结逾期扫描数据 —— 必须明确抛错，不能悄悄返回 0。"""
        with self.assertRaises(NotImplementedError):
            self.src.overdue_count('store_alpha')


if __name__ == '__main__':
    unittest.main(verbosity=2)
