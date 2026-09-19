# -*- coding: utf-8 -*-
"""数据源切换（`OZON_DATA_SOURCE=excel`）的接线测试。

只验证接线本身，不重复 core 的单元测试：
  1. `sqlite` 下工厂给的是 SqliteSource；
  2. `excel` 下工厂给的是 ExcelSource，且参数来自 config；
  3. 非法取值**当场报错**，不悄悄退回 sqlite（悄悄退回会让看板换一套口径出数）；
  4. 走完整 HTTP 链路（登录 → `/api/dashboard/store/{alias}`）能拿到 Excel 数据。

真实导出文件不存在时整类跳过。
"""
import importlib.util
import io
import os
import shutil
import unittest
from decimal import Decimal

from core.repository.excel_source import ExcelSource
from core.repository.sqlite_source import SqliteSource

from api import config
from api.deps import _default_source_factory, _load_purchase_costs
from api.stores import Store, StoreRegistry

HAVE_OPENPYXL = importlib.util.find_spec('openpyxl') is not None

EXPORT_DIR = r'D:\Downloads'
REAL_ACCRUAL = os.path.join(EXPORT_DIR, 'Отчет по начислениям_01.09.2026-11.09.2026.xlsx')
REAL_POSTINGS = os.path.join(EXPORT_DIR, 'postings.csv')

STORE = Store('store_alpha', 'OZON 俄罗斯站 · 主力店', r'<DATA_ROOT>\data\stores\store_alpha.db')


class SourceFactorySwitchTest(unittest.TestCase):
    """按环境变量选实现 —— 直接改模块属性再还原。"""

    def _with_mode(self, mode):
        old = config.DATA_SOURCE
        config.DATA_SOURCE = mode
        self.addCleanup(setattr, config, 'DATA_SOURCE', old)

    @unittest.skipUnless(os.path.isfile(r'<DATA_ROOT>\data\stores\store_alpha.db'),
                         '生产库不存在，跳过')
    def test_default_is_sqlite(self):
        self._with_mode('sqlite')
        src = _default_source_factory(STORE)
        self.addCleanup(src.close)
        self.assertIsInstance(src, SqliteSource)

    def test_invalid_mode_raises_instead_of_falling_back(self):
        self._with_mode('csv')
        with self.assertRaises(RuntimeError) as cm:
            _default_source_factory(STORE)
        self.assertIn('OZON_DATA_SOURCE', str(cm.exception))
        self.assertIn('csv', str(cm.exception))

    @unittest.skipUnless(os.path.isfile(REAL_ACCRUAL) and os.path.isfile(REAL_POSTINGS),
                         '本机没有真实 OZON 导出文件，跳过')
    def test_excel_mode_builds_an_excel_source_from_config(self):
        self._with_mode('excel')
        old_dir = config.EXCEL_EXPORT_DIR
        config.EXCEL_EXPORT_DIR = EXPORT_DIR
        self.addCleanup(setattr, config, 'EXCEL_EXPORT_DIR', old_dir)
        src = _default_source_factory(STORE)
        self.addCleanup(src.close)
        self.assertIsInstance(src, ExcelSource)
        self.assertEqual('store_alpha', src.alias)
        self.assertGreater(len(src.list_posting_numbers()), 1000)
        # 店铺注册表里的 db_path 不该被 excel 模式用到 —— 导出目录才是取数来源
        self.assertTrue(src.accrual_files)


@unittest.skipUnless(os.path.isfile(REAL_ACCRUAL) and os.path.isfile(REAL_POSTINGS),
                     '本机没有真实 OZON 导出文件，跳过')
class DashboardThroughExcelSourceTest(unittest.TestCase):
    """端到端：`OZON_DATA_SOURCE=excel` 时看板接口真的能出数。

    复用既有的 `make_client` 装配工具，只把 `source_factory` 换成走
    `_default_source_factory`（也就是生产路径上的那个工厂）—— 这样被测的是
    真实接线，不是测试里另写一遍装配。
    """

    def test_dashboard_endpoint_returns_excel_data(self):
        from api.tests.test_api import MOCK_PASSWORDS, make_client

        old_mode, old_dir = config.DATA_SOURCE, config.EXCEL_EXPORT_DIR
        old_cache = config.EXCEL_CACHE
        config.DATA_SOURCE = 'excel'
        config.EXCEL_EXPORT_DIR = EXPORT_DIR
        config.EXCEL_CACHE = False        # 免测试之间抢临时缓存文件
        self.addCleanup(setattr, config, 'DATA_SOURCE', old_mode)
        self.addCleanup(setattr, config, 'EXCEL_EXPORT_DIR', old_dir)
        self.addCleanup(setattr, config, 'EXCEL_CACHE', old_cache)

        store = Store('store_alpha', '测试店铺 001', 'C:/nonexistent/store_alpha.db')
        client, restore = make_client(
            source_factory=_default_source_factory,
            registry=StoreRegistry([store]))
        self.addCleanup(restore)

        resp = client.post('/api/auth/login',
                           json={'username': 'finance01',
                                 'password': MOCK_PASSWORDS['finance01']})
        self.assertEqual(200, resp.status_code, resp.text)
        headers = {'Authorization': 'Bearer %s' % resp.json()['access_token']}

        resp = client.get('/api/dashboard/store/store_alpha?days=14', headers=headers)
        self.assertEqual(200, resp.status_code, resp.text)
        body = resp.json()
        self.assertEqual('store_alpha', body['store_alias'])
        self.assertGreater(body['totals']['total_order_count'], 0,
                           '切到 Excel 数据源后看板没有订单 —— 接线没生效')
        self.assertTrue(body['orders'], 'orders 数组为空')
        self.assertIsNotNone(body['data_cutoff'])


class PurchaseCostLoaderTest(unittest.TestCase):
    """货号 → 单件采购成本 的载入（两份导出里都没有这个字段，只能外部注入）。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='ozon_cost_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_no_path_returns_empty(self):
        self.assertEqual({}, _load_purchase_costs(None))
        self.assertEqual({}, _load_purchase_costs(''))

    def test_missing_file_raises_instead_of_silently_skipping(self):
        """成本文件配了却读不到 → 当场报错。

        静默降级会让看板的预估利润悄悄变成销售额（领域层把缺失项当 0），
        那正是本项目最反对的「伪装成成功的错误」。
        """
        with self.assertRaises(FileNotFoundError) as cm:
            _load_purchase_costs(os.path.join(self.tmp, 'nope.xlsx'))
        self.assertIn('找不到采购成本文件', str(cm.exception))
        self.assertIn('OZON_EXCEL_PURCHASE_COST_FILE', str(cm.exception))

    def test_csv_with_chinese_header(self):
        p = os.path.join(self.tmp, 'cost.csv')
        with io.open(p, 'w', encoding='utf-8-sig', newline='') as fh:
            fh.write('货号;OZON 数字 SKU;单价;备注\n')
            fh.write('A-1;;2.61;\n')
            fh.write('B-2;;4.5;调价\n')
            fh.write(';;9.99;没有货号，跳过\n')
            fh.write('C-3;;;单价为空，跳过\n')
        costs = _load_purchase_costs(p)
        self.assertEqual({'A-1': Decimal('2.61'), 'B-2': Decimal('4.5')}, costs)

    @unittest.skipUnless(HAVE_OPENPYXL, '未安装 openpyxl')
    def test_xlsx_with_a_title_row_before_the_header(self):
        """表头不在第一行时也要能找到（真实模板的第 1 行就是表头，但不能写死）。"""
        import openpyxl
        p = os.path.join(self.tmp, 'cost.xlsx')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['采购成本表（说明行）'])
        ws.append(['货号', '单价'])
        ws.append(['A-1', 2.61])
        ws.append(['B-2', '4.5'])
        wb.save(p)
        self.assertEqual({'A-1': Decimal('2.61'), 'B-2': Decimal('4.5')},
                         _load_purchase_costs(p))

    @unittest.skipUnless(HAVE_OPENPYXL, '未安装 openpyxl')
    def test_unreadable_header_raises_a_clear_error(self):
        import openpyxl
        p = os.path.join(self.tmp, 'bad.xlsx')
        wb = openpyxl.Workbook()
        wb.active.append(['商品', '价格'])
        wb.save(p)
        with self.assertRaises(FileNotFoundError) as cm:
            _load_purchase_costs(p)
        self.assertIn('读不出表头', str(cm.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
