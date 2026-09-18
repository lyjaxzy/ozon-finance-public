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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional

from fastapi import HTTPException, Request

from core.repository.sqlite_source import SqliteSource

from . import config
from .stores import Store, StoreRegistry
from .tokens import TokenError, decode_access_token
from .users import User, UserStore


@dataclass
class Runtime:
    """应用的可替换件集合。生产用 make_default_runtime()，测试自己造一个。"""

    source_factory: Callable[[Store], SqliteSource]
    user_store: UserStore
    registry: StoreRegistry

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

    # ── 数据源 ──
    @contextmanager
    def open_source(self, store: Store) -> Iterator[SqliteSource]:
        """取只读数据源，用完关掉。

        只读连接关不关都不影响库内容，但不关会在并发压测下耗尽文件句柄。
        """
        src = self.source_factory(store)
        try:
            yield src
        finally:
            try:
                src.close()
            except Exception:  # noqa: BLE001 - 关连接失败不该影响已经算好的响应
                pass


def _default_source_factory(store: Store) -> SqliteSource:
    """按注册表的路径只读打开店铺库。

    `verify_readonly=True` 时 SqliteSource 会做一次「能不能写」的自检；
    保留它的默认行为 —— 万一哪天有人把 mode=ro 改掉，这里会当场炸。
    """
    return SqliteSource(store.db_path, alias=store.alias)


def make_default_runtime() -> Runtime:
    """生产装配：单店白名单 + JSON 用户表。"""
    return Runtime(
        source_factory=_default_source_factory,
        user_store=UserStore.from_file(config.USERS_PATH),
        registry=StoreRegistry.default(),
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
