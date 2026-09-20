# -*- coding: utf-8 -*-
"""FastAPI 依赖：认证、授权、数据源解析。

数据隔离全在这里落地，**不依赖前端**：
  1. `get_current_user`   —— 没令牌/令牌坏 → 401 {"detail": "请先登录"}
  2. `resolve_store`      —— 白名单没有 → 404；有但没授权 → 403 {"detail": "无权访问该店铺"}
  3. `runtime_source`     —— 只在这两步都过了之后才打开库

第 2 步刻意用 403 而不是「返回空数据」：空数据看起来是成功，
调用方会把「没权限」当成「这段时间没单」，这正是 PRD §9.2 要禁掉的。

装配方式：所有可替换件放在 `Runtime` 里，挂在 `app.state.runtime` 上，
通过 FastAPI 的 Depends 注入。

**为什么不用「测试里替换 deps 模块属性」这种写法**：
路由模块用 `from ..deps import user_store` 导入的是函数对象本身，
在路由模块命名空间里改不动；改 deps 模块属性又对已绑定的引用无效。
实测就是踩了这个坑（改了 `deps.user_store`，路由里仍走原函数）。
显式 Runtime 只有一处定义，测试替换 `app.state.runtime` 即可，
生产路径与测试路径走的是同一条代码。
"""
import csv
import io
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Iterator, List, Optional

from fastapi import HTTPException, Request

from core.repository.base import ProfitDataSource
from core.repository.excel_source import ExcelSource
from core.repository.sqlite_source import SqliteSource
from . import config
from .stores import Store, StoreRegistry
from .tokens import TokenError, decode_access_token
from .users import User, UserStore

logger = logging.getLogger('ozon.api')


@dataclass
class Runtime:
    """应用的可替换件集合。生产用 make_default_runtime()，测试自己造一个。"""

    source_factory: Callable[[Store], ProfitDataSource]
    user_store: UserStore
    registry: StoreRegistry
    #: 采购成本库（ADR-0009）。**这是我们唯一允许写的库**；测试里指向临时文件。
    cost_book_path: str = ''
    legacy_cost_book_path: str = ''

    # ── 便捷访问 ──
    def user(self, username: str) -> Optional[User]:
        return self.user_store.get(username)

    def store(self, alias: str) -> Optional[Store]:
        return self.registry.get(alias)

    def visible_aliases(self, user: User) -> List[str]:
        """当前用户可见的店铺别名。root 看全部，其余只看授权白名单。"""
        if user.is_root:
            return list(self.registry.aliases())
        # 授权里可能出现注册表已下线的别名 —— 过滤掉，不下发打不开的入口
        return sorted(a for a in user.store_aliases if self.registry.get(a) is not None)

    # ── 成本库（ADR-0009）──
    @contextmanager
    def open_cost_book(self, readonly: bool = True):
        """取成本库。读默认只读打开；写只在导入接口里显式 `readonly=False`。"""
        from core.repository.cost_book import CostBook
        book = CostBook(self.cost_book_path, readonly=readonly)
        try:
            yield book
        finally:
            try:
                book.close()
            except Exception:  # noqa: BLE001 - 关连接失败不该影响已经写好的结果
                pass

    def unit_costs(self, on_date: Optional[str] = None) -> Dict[str, Decimal]:
        """成本库的单价映射（货号 → 单件 CNY）。

        库不存在时返回空字典而**不报错**：没有成本库不等于没有成本
        （sqlite 模式下的成本本来就在店铺库里）。Excel 模式例外 ——
        那里成本库是唯一来源，缺了要喊出来，见 `_make_excel_source`。
        """
        if not self.cost_book_path or not os.path.isfile(self.cost_book_path):
            return {}
        with self.open_cost_book(readonly=True) as book:
            return book.price_map(on_date)

    # ── 数据源 ──
    @contextmanager
    def open_source(self, store: Store) -> Iterator[ProfitDataSource]:
        """取只读数据源，用完关掉。

        SqliteSource 关的是只读连接，ExcelSource 关的是解析结果库 ——
        两者不关都会在并发压测下耗尽文件句柄。
        """
        src = self.source_factory(store)
        try:
            yield src
        finally:
            try:
                src.close()
            except Exception:  # noqa: BLE001 - 关连接失败不该影响已经算好的响应
                pass


def _default_source_factory(store: Store) -> ProfitDataSource:
    """按 `OZON_DATA_SOURCE` 选数据源实现（ADR-0005 的开关）。

    * `sqlite`（默认）—— 只读打开店铺库，并把**成本库的单价**注入进去：
      策略 `book_first` 下库里有成本就用库里的（当前生产库 100% 有值，数字不变），
      为空才用成本库兜底；`book_authoritative` 下一律按成本库合成（ADR-0009）。
    * `excel` —— 从 OZON 后台导出的两份文件导入。源文件全程只读，
      解析结果落系统临时目录的 SQLite 并以 `mode=ro` 打开。
      成本**优先来自成本库**，没配成本库才退回外部成本文件。

    取值非法时**直接报错**而不是悄悄退回 sqlite：数据源选错会让看板拿另一套口径
    出数，那种错误必须当场暴露。
    """
    mode = config.DATA_SOURCE
    if mode == 'excel':
        return _make_excel_source(store)
    if mode == 'sqlite':
        runtime = _cost_runtime()
        return SqliteSource(store.db_path, alias=store.alias,
                            unit_costs=runtime.unit_costs(),
                            cost_policy=config.COST_SOURCE_POLICY)
    raise RuntimeError(
        'OZON_DATA_SOURCE 只支持 sqlite / excel，收到 %r。'
        '（excel 模式下店铺注册表里的 db_path 不参与取数，'
        '导出目录由 OZON_EXCEL_DIR 指定。）' % (mode,))


def _cost_runtime() -> Runtime:
    """只为了复用 `Runtime.unit_costs()` 的成本库读取逻辑。

    刻意不缓存：成本库是本机小文件（3400 行 / 2.5 MB，`price_map` 一条 SQL），
    缓存反而会引入「刚导入的成本没生效」这种最难解释的问题。
    """
    return Runtime(source_factory=lambda store: None,  # type: ignore[arg-type]
                   user_store=None,  # type: ignore[arg-type]
                   registry=None,  # type: ignore[arg-type]
                   cost_book_path=config.COST_BOOK_PATH,
                   legacy_cost_book_path=config.COST_BOOK_LEGACY_PATH)


def _make_excel_source(store: Store) -> ExcelSource:
    """装配 Excel 导入器，并把「少了什么会算错」在装配时就喊出来。

    成本来源的优先级（ADR-0009）：**成本库 → 外部成本文件**。
    成本库是我们自己的台账；文件是过渡期的临时办法。
    """
    costs = _cost_runtime().unit_costs()
    source_label = '成本库'
    if not costs:
        costs = _load_purchase_costs(config.EXCEL_PURCHASE_COST_FILE)
        source_label = '外部成本文件'
    if not costs:
        logger.warning(
            'OZON_DATA_SOURCE=excel 但没有采购成本（成本库为空，'
            'OZON_EXCEL_PURCHASE_COST_FILE 也没配）。'
            '后果：§7.1 实际利润会全部判 missing_purchase_cost（合计 null，正确），'
            '但看板的 §7.2 预估利润会退化成销售额 —— 因为应计报表与 postings.csv 里'
            '都没有采购成本这个字段。请先导入采购成本（成本管理页）再用于生产。')
    else:
        logger.info('Excel 数据源的采购成本来自%s：%d 个货号', source_label, len(costs))
    if config.EXCEL_EXCHANGE_RATE_MODE == 'implied_buyer_payment':
        logger.warning(
            'OZON_EXCEL_RATE_MODE=implied_buyer_payment：汇率由 '
            '«已由买家支付 / 发货的金额» 推算。实测与生产库结算汇率在 93.8% 的订单上'
            '完全相等，但 6.2% 会正好差一个整数倍（多商品单买家实付不全）。'
            '属临时方案，须业务确认。')
    return ExcelSource(
        accrual_dir=config.EXCEL_EXPORT_DIR,
        postings_csv=config.EXCEL_POSTINGS_CSV,
        alias=store.alias,
        require_postings=config.EXCEL_REQUIRE_POSTINGS,
        exchange_rate_mode=config.EXCEL_EXCHANGE_RATE_MODE,
        purchase_cost_by_offer=costs or None,
        cache=config.EXCEL_CACHE,
    )


# ── 采购成本文件（货号 → 单件成本 CNY）───────────────────────────
#
# 为什么放在 API 层而不是 core：这是**部署配置**，不是财务口径。
# core 只提供 `purchase_cost_by_offer` 这个注入口，成本从哪来由装配方决定
# （将来接了采购成本表，就是从库里来，而不是从文件来）。
_OFFER_COLUMNS = ('货号', 'offer_id', 'Offer ID', 'offer id')
_COST_COLUMNS = ('单价', '采购成本', '采购单价', '成本', '单价(CNY)', 'cost')


def _load_purchase_costs(path: Optional[str]) -> Dict[str, Decimal]:
    """读「货号 → 单件采购成本」。支持 `.xlsx` 与 `.csv`；未配置时返回空。

    文件读不了就**当场报错**，不静默降级 —— 少了成本会让看板的预估利润
    悄悄变成销售额，那正是本项目最反对的「伪装成成功的错误」。
    """
    if not path:
        return {}
    if not os.path.isfile(path):
        raise FileNotFoundError(
            '找不到采购成本文件：%s\n'
            '它由 OZON_EXCEL_PURCHASE_COST_FILE 指定，用来补上两份导出里都没有的采购成本。'
            '（列名形如 `货号` / `单价`，可直接用采购成本模板导出。）' % path)
    rows = _read_table_rows(path)
    header_index, offer_i, cost_i = None, None, None
    for i, row in enumerate(rows[:10]):
        cells = [(c or '').strip() for c in row]
        o = _index_of(cells, _OFFER_COLUMNS)
        c = _index_of(cells, _COST_COLUMNS)
        if o is not None and c is not None:
            header_index, offer_i, cost_i = i, o, c
            break
    if header_index is None:
        raise FileNotFoundError(
            '采购成本文件读不出表头：%s\n'
            '需要一列「货号」和一列「单价」（列名可用：%s / %s）。'
            % (path, '、'.join(_OFFER_COLUMNS), '、'.join(_COST_COLUMNS)))

    costs: Dict[str, Decimal] = {}
    bad = 0
    for row in rows[header_index + 1:]:
        if offer_i >= len(row):
            continue
        offer = (row[offer_i] or '').strip()
        if not offer:
            continue
        raw = (row[cost_i] or '').strip() if cost_i < len(row) else ''
        try:
            value = Decimal(raw.replace(',', ''))
        except (InvalidOperation, ValueError):
            bad += 1
            continue
        costs[offer] = value
    if bad:
        logger.warning('采购成本文件有 %d 行的「单价」解析不了，已跳过：%s', bad, path)
    logger.info('采购成本已载入 %d 个货号：%s', len(costs), path)
    return costs


def _index_of(cells, names):
    lowered = [c.lower() for c in cells]
    for name in names:
        if name.lower() in lowered:
            return lowered.index(name.lower())
    return None


def _read_table_rows(path: str):
    """把 xlsx / csv 读成「行 × 列」的字符串表格。"""
    if path.lower().endswith('.csv'):
        with io.open(path, encoding='utf-8-sig', newline='') as fh:
            sample = fh.readline()
            fh.seek(0)
            delim = ';' if sample.count(';') > sample.count(',') else ','
            return [[c for c in row] for row in csv.reader(fh, delimiter=delim)]
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise FileNotFoundError('读 .xlsx 采购成本需要 openpyxl：%s' % exc)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        return [[None if v is None else str(v) for v in row]
                for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def make_default_runtime() -> Runtime:
    """生产装配：单店白名单 + JSON 用户表 + 自有成本库。"""
    return Runtime(
        source_factory=_default_source_factory,
        user_store=UserStore.from_file(config.USERS_PATH),
        registry=StoreRegistry.default(),
        cost_book_path=config.COST_BOOK_PATH,
        legacy_cost_book_path=config.COST_BOOK_LEGACY_PATH,
    )


def for_request(request: Request) -> Runtime:
    """取当前请求所用应用的 Runtime。首次访问时惰性装配并挂到 app.state。

    `get_current_user` 用它而不是把 Runtime 声明成子依赖 —— 否则
    `Depends(get_current_user)` 会形成自引用。FastAPI 的子依赖必须是
    不同的可调用对象，这里绕开这个坑，同时保持只有一处装配点。
    """
    runtime = getattr(request.app.state, 'runtime', None)
    if runtime is None:
        runtime = make_default_runtime()
        request.app.state.runtime = runtime
    return runtime


def get_runtime(request: Request) -> Runtime:
    """给不需要登录的接口（如 /api/health）用的依赖包装。"""
    return for_request(request)


# ── 认证 ─────────────────────────────────────────────────────────
def _bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get('authorization') or ''
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == 'bearer':
        return parts[1].strip()
    return None


def get_current_user(request: Request) -> User:
    """未登录 / 令牌无效 → 401 {"detail": "请先登录"}。

    作为 FastAPI 依赖使用：`user: User = Depends(get_current_user)`。
    """
    token = _bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail='请先登录')
    try:
        payload = decode_access_token(token)
    except TokenError:
        # 刻意不区分「过期」和「伪造」—— 攻击者不需要知道是哪一种
        raise HTTPException(status_code=401, detail='请先登录')
    user = for_request(request).user(payload['sub'])
    if user is None:
        # 令牌有效但用户已被删除：同样按未登录处理，不带幽灵身份继续用
        raise HTTPException(status_code=401, detail='请先登录')
    return user


# ── 授权 ─────────────────────────────────────────────────────────
def resolve_store(alias: str, user: User, runtime: Runtime) -> Store:
    """返回被授权的 Store；不通过一律抛异常。

    * 别名不在白名单 → 404（这个店铺根本不存在，也顺便不泄露授权信息）
    * 在白名单但用户无权 → 403 {"detail": "无权访问该店铺"}
    """
    store = runtime.store(alias)
    if store is None:
        raise HTTPException(status_code=404, detail='店铺不存在')
    if not user.can_access_store(alias):
        # 关键：绝不返回空数据伪装成功
        raise HTTPException(status_code=403, detail='无权访问该店铺')
    return store
