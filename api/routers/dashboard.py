# -*- coding: utf-8 -*-
"""看板接口。

    GET /api/dashboard/store/{alias}?days=14&orders_offset=0&orders_limit=100
    GET /api/dashboard/aggregate?stores=a,b&days=14

本文件**没有任何计算**：授权 → 打开只读库 → 交给 api/dashboard.build_dashboard
或 api/dashboard.build_multi_store_summary（它们再交给 core.domain.profit）。
这里只负责把异常翻译成正确的 HTTP 语义。

分页（ADR-0008）：`orders_offset` / `orders_limit` 是**可选查询参数**，
响应形状一个字段都没动。合计、趋势、利润构成永远覆盖整个窗口 ——
分页只决定 orders 数组下发哪一段，这一条在 README 与界面上都写明了。
"""
from contextlib import ExitStack
import re
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from .. import config
from ..dashboard import build_dashboard, build_multi_store_summary
from ..deps import Runtime, get_current_user, get_runtime, resolve_store

router = APIRouter(prefix='/api/dashboard', tags=['dashboard'])

#: 别名格式：只允许小写字母、数字、下划线、连字符。
#: 显式约束格式，就不必依赖「注册表白名单」这一层去挡路径穿越。
_ALIAS_PATTERN = r'^[a-z0-9_-]{1,64}$'
_ALIAS_RX = re.compile(_ALIAS_PATTERN)


def _split_stores(raw: str) -> List[str]:
    """把 `a,b` 解析成别名列表：去空白、去空项、去重（保持顺序）。

    故意**不**在这里做存在性/授权判断 —— 那是 `resolve_store` 的职责，
    它要给出 404 与 403 的区分。这里只做形状解析。
    """
    out: List[str] = []
    for chunk in (raw or '').split(','):
        alias = chunk.strip()
        if alias and alias not in out:
            out.append(alias)
    return out


@router.get('/store/{alias}')
def store_dashboard(
    alias: Annotated[str, Path(pattern=_ALIAS_PATTERN, description='店铺别名')],
    days: Annotated[int, Query(ge=1, le=config.MAX_DAYS,
                               description='统计天数，含截止日')] = config.DEFAULT_DAYS,
    orders_limit: Annotated[int, Query(ge=1, le=config.MAX_ORDERS_IN_RESPONSE,
                                       description='本页下发的订单条数上限')] = config.MAX_ORDERS_IN_RESPONSE,
    orders_offset: Annotated[int, Query(ge=0,
                                        description='订单明细的起始下标（服务端分页）')] = 0,
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    # 1) 授权（白名单 → 404 / 授权 → 403）。这一步在打开库之前，
    #    跨店请求根本不会碰到数据，也就没有「先查出来再过滤」的泄漏窗口。
    store = resolve_store(alias, user, runtime)
    # 2) 取数 + 算 + 组装
    try:
        with runtime.open_source(store) as source:
            return build_dashboard(source, store.alias, days,
                                   order_limit=orders_limit,
                                   order_offset=orders_offset)
    except FileNotFoundError as exc:
        # 注册表指向了一个不存在的库：这是**服务端配置故障**，不是客户端的错。
        # 返回 503 而不是 200 空数据 —— 后者会让运维以为一切正常。
        raise HTTPException(status_code=503, detail='店铺库不可用: %s' % exc)


@router.get('/aggregate')
def store_aggregate(
    stores: Annotated[str, Query(min_length=1, max_length=512,
                                 description='店铺别名，逗号分隔，如 store_alpha,store_beta')],
    days: Annotated[int, Query(ge=1, le=config.MAX_DAYS,
                               description='统计天数，含截止日')] = config.DEFAULT_DAYS,
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    """多店合计（ADR-0008）。

    三个刻意的取舍：

    1. **逐个 `resolve_store`，任一店不通过就整体失败**（404/403）。
       不「跳过无权店铺继续算」—— 那会让用户看着一个少了一家店的合计而不知道
       （PRD §9.2 要禁掉的正是这种「伪装成成功的错误」）。
    2. **所有店用同一个窗口**（共同窗口右端 = 各店最后一个已结算日里最早的那个）。
       否则相加的是几个不同期间，见 ADR-0008 §二。
    3. `stores` 只接受别名，**路径永远不来自请求参数**（与单店接口同一套白名单）。
    """
    aliases = _split_stores(stores)
    if not aliases:
        raise HTTPException(status_code=422, detail='stores 至少需要一个店铺别名')
    if len(aliases) > config.MAX_AGGREGATE_STORES:
        raise HTTPException(
            status_code=422,
            detail='一次最多合计 %d 个店铺，收到 %d 个'
                   % (config.MAX_AGGREGATE_STORES, len(aliases)))
    for alias in aliases:
        if not _ALIAS_RX.match(alias):
            # 形状不对就直接 422，不必走到白名单那一步
            raise HTTPException(status_code=422, detail='店铺别名格式不合法: %r' % alias)

    # 全部店先授权通过，再开库 —— 一个请求里也不会出现「有的店开了库、有的被拒」
    resolved = [resolve_store(alias, user, runtime) for alias in aliases]

    try:
        with ExitStack() as stack:
            entries = [(store.alias, store.display_name,
                        stack.enter_context(runtime.open_source(store)))
                       for store in resolved]
            return build_multi_store_summary(entries, days)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail='店铺库不可用: %s' % exc)
