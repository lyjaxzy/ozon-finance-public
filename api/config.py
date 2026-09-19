# -*- coding: utf-8 -*-
"""运行期配置。

所有可变项都从环境变量读，且**全部有本地默认值** —— 这样 `python -m uvicorn api.app:app`
在开发机上开箱即用，部署时不需要改代码。

安全相关：
  * `OZON_JWT_SECRET` 生产必须显式设置；默认值仅用于本机开发，并在 /api/health 里自曝。
  * 店铺库路径固定为「别名 → 路径」白名单，**不接受请求参数传路径**，
    否则会退化成任意文件读取。
"""
import os

#: 服务监听端口。刻意避开前端开发服务器的 8848。
DEFAULT_PORT = 8849

#: 店铺库默认落点（与 core/repository/sqlite_source.py 的 DEFAULT_STORE 一致）
DEFAULT_STORE = r'<DATA_ROOT>\data\stores\store_alpha.db'

#: 店铺白名单，形如 `store_alpha=C:\path\a.db;store_beta=C:\path\b.db`。
#: 为什么需要它：数据隔离只对「已注册的店铺」生效 —— 只有一个别名时，
#: 跨店 403 根本无法产生（只会是 404「店铺不存在」）。要验证或使用跨店隔离，
#: 必须至少注册两个别名，其中一个不授权给运营/财务角色。
STORES_RAW = os.environ.get(
    'OZON_STORES',
    'store_alpha=%s;store_beta=%s' % (
        DEFAULT_STORE,
        DEFAULT_STORE.replace('store_alpha', 'store_beta')))

#: 别名 → 展示名。店铺元信息还没入库，先在这里维护。
#: ⚠️ 2026-09-19 修正：store_beta 的库**确实存在**（7.3 MB / 45 单），
#: 原来的「（占位，库不存在）」是错的，已去掉 —— 店铺管理页会把它显示给用户，
#: 一句过期的说明比没有说明更糟。
STORE_DISPLAY_NAMES = {
    'store_alpha': 'OZON 俄罗斯站 · 主力店',
    'store_beta': 'OZON 俄罗斯站 · 二店',
}

#: JWT 签发密钥。本机默认值 = 开发用，部署必须覆盖。
JWT_SECRET = os.environ.get('OZON_JWT_SECRET', 'dev-only-insecure-secret-change-me')
JWT_ALGORITHM = 'HS256'
JWT_TTL_SECONDS = int(os.environ.get('OZON_JWT_TTL_SECONDS', '28800'))  # 8 小时

#: 用户/授权文件（JSON）。还没有用户表，先落文件。
USERS_PATH = os.environ.get(
    'OZON_USERS_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'users.json'))

#: 看板默认统计天数，以及允许的上限
DEFAULT_DAYS = 14
MAX_DAYS = 365

#: 响应里 orders 数组的**每页**最大条数（`orders_limit` 的上限）。
#: 响应结构本身没有被改动 —— 分页是通过两个**可选查询参数**
#: `orders_offset` / `orders_limit` 实现的（ADR-0008），不传就是原来的行为。
#: 合计与趋势永远覆盖整个窗口，分页只裁剪 orders 数组。
MAX_ORDERS_IN_RESPONSE = int(os.environ.get('OZON_MAX_ORDERS', '200'))

#: 分页可选页面大小（前端下拉里给的档位）。放在后端是为了让「允许哪些档位」
#: 只有一个定义，前端从 /api/health 读，不各写一份。
PAGE_SIZE_OPTIONS = (10, 20, 50, 100)

#: 多店合计一次最多接受多少个店铺。防止有人拿一个 365 天 × 50 店的请求打垮服务。
MAX_AGGREGATE_STORES = int(os.environ.get('OZON_MAX_AGGREGATE_STORES', '10'))

#: 是否允许未登录访问。只读系统默认关闭，且不提供开着的入口。
ALLOW_ANONYMOUS = os.environ.get('OZON_ALLOW_ANONYMOUS', '0') == '1'

# ── 数据源选择（ADR-0005 的落地开关）─────────────────────────────
#
# 看板的数据可以从两个地方来，由 `OZON_DATA_SOURCE` 切换：
#
#   sqlite（默认）—— 直接只读打开店铺库 `<DATA_ROOT>\data\stores\*.db`，
#                   这是**最终形态**（ADR-0001 的「DB 可替换」）。
#   excel         —— 从 OZON 后台导出的两份文件导入（应计报表 + postings.csv），
#                   这是**过渡形态**，见 `docs/adr/0005-Excel导入数据源模板选择.md`。
#
# 为什么用环境变量而不是请求参数：数据源是**部署级**决定，不是每个请求能选的东西。
# 让请求参数决定数据源，等于让调用方绕开一个已经核准过的取数路径。
DATA_SOURCE = (os.environ.get('OZON_DATA_SOURCE') or 'sqlite').strip().lower()

#: Excel 导入器的导出目录（应计报表 `Отчет по начислениям_*.xlsx` 与 `postings.csv` 放这里）
EXCEL_EXPORT_DIR = os.environ.get('OZON_EXCEL_DIR', r'D:\Downloads')

#: postings.csv 的显式路径。留空则取 `EXCEL_EXPORT_DIR\postings.csv`。
EXCEL_POSTINGS_CSV = os.environ.get('OZON_EXCEL_POSTINGS') or None

#: 采购成本文件（货号 → 单件采购成本 CNY）。支持 `.xlsx`（就是
#: `采购成本模板.xlsx`：列 `货号` / `OZON 数字 SKU` / `单价` / `备注`）与 `.csv`。
#:
#: ⚠️ **为什么必须配**：两份导出里都**没有**采购成本。不配的话：
#:   * §7.1 实际利润 → 判 `missing_purchase_cost`，合计为 null（正确，不误导）；
#:   * §7.2 预估利润 → `evaluate_estimated` 按领域层定义把缺失项当 0 参与计算
#:     （`core/domain/profit.py` 的既有约定），而 `api/dashboard.py` 只判
#:     「有没有发货单」、不判 `complete`，于是合计会**退化成销售额**。
#:     这个数字看着正常但是错的 —— 所以本文件一旦置空，
#:     `api/deps.py` 会在装配时打一条 warning，把这件事说清楚。
EXCEL_PURCHASE_COST_FILE = os.environ.get('OZON_EXCEL_PURCHASE_COST_FILE') or None

#: 缺 postings.csv 时是否报错。默认报错 —— ADR-0005 要求每次导出两份；
#: 置 0 则降级为「只有财务流水」，此时逾期单数不可用（返回 null 而不是 0）。
EXCEL_REQUIRE_POSTINGS = os.environ.get('OZON_EXCEL_REQUIRE_POSTINGS', '1') == '1'

#: 汇率来源。`none`（默认）表示**不推算** —— 应计报表里没有结算汇率，
#: §7.1 会因此如实判 missing_exchange_rate，这是正确行为。
#: `implied_buyer_payment` 用 `已由买家支付 / 发货的金额` 推，实测与生产库
#: 结算汇率在 93.8% 的订单上完全相等，但 6.2% 会正好差一个整数倍。
#: ⚠️ 这是临时方案，须业务确认后再开。
EXCEL_EXCHANGE_RATE_MODE = os.environ.get('OZON_EXCEL_RATE_MODE', 'none').strip()

#: 是否复用解析结果的磁盘缓存（按文件内容指纹放在系统临时目录）。
#: 4 份应计报表冷解析约 35 秒，命中缓存重开约 0.01 秒。
EXCEL_CACHE = os.environ.get('OZON_EXCEL_CACHE', '1') == '1'
