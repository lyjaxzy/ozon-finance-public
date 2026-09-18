# -*- coding: utf-8 -*-
"""看板接口。

    GET /api/dashboard/store/{alias}?days=14

本函数**没有任何计算**：授权 → 打开只读库 → 交给 api/dashboard.build_dashboard
（它再交给 core.domain.profit）。这里只负责把异常翻译成正确的 HTTP 语义。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from .. import config
from ..dashboard import build_dashboard
from ..deps import Runtime, get_current_user, get_runtime, resolve_store

router = APIRouter(prefix='/api/dashboard', tags=['dashboard'])

#: 别名格式：只允许小写字母、数字、下划线、连字符。
#: 显式约束格式，就不必依赖「注册表白名单」这一层去挡路径穿越。
_ALIAS_PATTERN = r'^[a-z0-9_-]{1,64}$'


@router.get('/store/{alias}')
def store_dashboard(
    alias: Annotated[str, Path(pattern=_ALIAS_PATTERN, description='店铺别名')],
    days: Annotated[int, Query(ge=1, le=config.MAX_DAYS,
                               description='统计天数，含截止日')] = config.DEFAULT_DAYS,
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    # 1) 授权（白名单 → 404 / 授权 → 403）。这一步在打开库之前，
    #    跨店请求根本不会碰到数据，也就没有「先查出来再过滤」的泄漏窗口。
    store = resolve_store(alias, user, runtime)
    # 2) 取数 + 算 + 组装
    try:
        with runtime.open_source(store) as source:
            return build_dashboard(source, store.alias, days)
    except FileNotFoundError as exc:
        # 注册表指向了一个不存在的库：这是**服务端配置故障**，不是客户端的错。
        # 返回 503 而不是 200 空数据 —— 后者会让运维以为一切正常。
        raise HTTPException(status_code=503, detail='店铺库不可用: %s' % exc)
