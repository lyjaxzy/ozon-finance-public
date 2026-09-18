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
STORE_DISPLAY_NAMES = {
    'store_alpha': 'OZON 俄罗斯站 · 主力店',
    'store_beta': 'OZON 俄罗斯站 · 二店（占位，库不存在）',
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

#: 响应里 orders 数组的最大条数。
#: 响应结构是前端定死的，**不能**偷偷加分页字段（会破坏契约），
#: 所以这里给一个上限，超出的部分只体现在 totals/trend 里，并在
#: README 的「已知限制」里写明。
MAX_ORDERS_IN_RESPONSE = int(os.environ.get('OZON_MAX_ORDERS', '200'))

#: 是否允许未登录访问。只读系统默认关闭，且不提供开着的入口。
ALLOW_ANONYMOUS = os.environ.get('OZON_ALLOW_ANONYMOUS', '0') == '1'
