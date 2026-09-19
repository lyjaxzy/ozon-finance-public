# -*- coding: utf-8 -*-
"""逐 SKU 利润下钻接口。

    GET /api/dashboard/store/{alias}/sku-detail?days=14&limit=200&sort=...&q=...

**刻意是独立文件、独立路径**：现有的
`GET /api/dashboard/store/{alias}` 响应形状已被前端依赖，加字段进去等于
把下钻功能捆在主流看板的契约上；而且「点指标卡才看明细」的取数开销
（逐 SKU 归属 + 两条口径对账）不该由每次看板刷新买单。

本函数与 `routers/dashboard.py` 一样**没有任何计算**：
授权 → 打开只读库 → 交给 `api/dashboard.build_sku_detail`
（它再交给 `core.domain.profit`）。这里只把异常翻译成正确的 HTTP 语义。
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from .. import config
from ..dashboard import SKU_DEFAULT_SORT, SKU_SORT_KEYS, build_sku_detail
from ..deps import Runtime, get_current_user, get_runtime, resolve_store

router = APIRouter(prefix='/api/dashboard', tags=['dashboard'])

#: 与 `routers/dashboard.py` 保持同一套别名格式约束（显式约束就不用靠白名单挡穿越）
_ALIAS_PATTERN = r'^[a-z0-9_-]{1,64}$'

#: 单次下发上限。SKU 可能上千，一次吐几万行对浏览器和 SQLite 都不友好；
#: 合计数不受这个上限影响（全窗口口径），界面上会写明这一点。
MAX_SKUS_IN_RESPONSE = int(getattr(config, 'MAX_SKUS_IN_RESPONSE', 500))
DEFAULT_SKUS_IN_RESPONSE = 500


@router.get('/store/{alias}/sku-detail')
def store_sku_detail(
    alias: Annotated[str, Path(pattern=_ALIAS_PATTERN, description='店铺别名')],
    days: Annotated[int, Query(ge=1, le=config.MAX_DAYS,
                               description='统计天数，含截止日')] = config.DEFAULT_DAYS,
    limit: Annotated[int, Query(ge=1, le=MAX_SKUS_IN_RESPONSE,
                                description='下发条数上限')] = DEFAULT_SKUS_IN_RESPONSE,
    offset: Annotated[int, Query(ge=0, description='分页偏移')] = 0,
    sort: Annotated[str, Query(description='排序键')] = SKU_DEFAULT_SORT,
    desc: Annotated[bool, Query(description='是否降序')] = True,
    q: Annotated[Optional[str], Query(max_length=64,
                                      description='按 SKU/货号/商品名搜索')] = None,
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    if sort not in SKU_SORT_KEYS:
        # 白名单外一律 422，而不是「忽略排序参数」—— 后者会让调用方以为排过了
        raise HTTPException(
            status_code=422,
            detail='sort 只支持：%s' % '、'.join(SKU_SORT_KEYS))
    # 1) 授权（白名单 → 404 / 未授权 → 403）。在打开库之前，跨店请求碰不到数据。
    store = resolve_store(alias, user, runtime)
    # 2) 取数 + 算 + 组装
    try:
        with runtime.open_source(store) as source:
            return build_sku_detail(source, store.alias, days, limit=limit,
                                    offset=offset, sort=sort, desc=desc, query=q)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail='店铺库不可用: %s' % exc)
    except NotImplementedError as exc:
        # 数据源实现不了下钻（例如夹具未冻结商品明细行）时如实报 501，
        # 而不是返回空表 —— 空表会被当成「这个窗口没有 SKU」。
        raise HTTPException(status_code=501, detail=str(exc))
