# -*- coding: utf-8 -*-
"""FastAPI 应用装配。

    python -m uvicorn api.app:app --port 8849

刻意**不加** CORS 中间件：前端走 vite 代理（`VITE_PROXY`）同源访问，
浏览器不产生跨域预检。加了 `allow_origins=["*"]` 反而会把只读接口
暴露给任意页面。将来真要跨域部署，再按白名单显式加。

刻意**不加**任何写路由：本步骤交付的是只读 API。店铺库由
SqliteSource 以 mode=ro 打开，并且构造时做写保护自检。
"""
import logging
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from core.repository.sqlite_source import DEFAULT_STORE

from . import config
from .deps import Runtime, get_runtime
from .routers import auth as auth_router
from .routers import dashboard as dashboard_router
from .routers import sku_detail as sku_detail_router
from .routers import stores as stores_router
from .schemas import HealthResponse

logger = logging.getLogger('ozon.api')

app = FastAPI(
    title='OZON 跨境电商财务系统 API（只读）',
    description='数据全部来自 core/repository 的只读数据源，利润口径来自 core/domain/profit.py。',
    version='0.1.0',
)

app.include_router(auth_router.router)
app.include_router(dashboard_router.router)
# 逐 SKU 利润下钻（ADR-0006）。单独一个路由文件，是因为它**不改**现有看板接口的
# 响应形状 —— 前端依赖那个形状，加字段进去会一起坏。
app.include_router(sku_detail_router.router)
# 店铺列表（ADR-0008）。只读：只下发「用户可见的店铺 + 能不能打开 + 数据到哪天」，
# 新增/下线店铺仍然改服务端 OZON_STORES 配置。
app.include_router(stores_router.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底：不让栈回溯漏给客户端，同时把真实原因记进日志。

    只读接口出 500 基本都是服务端问题（库结构变了、库被锁），
    返回体里给出稳定的形状，方便前端统一处理。
    """
    logger.exception('未处理异常: %s %s', request.method, request.url.path)
    return JSONResponse(status_code=500, content={'detail': '服务内部错误'})


@app.get('/api/health', response_model=HealthResponse, tags=['system'])
def health(runtime: Runtime = Depends(get_runtime)) -> HealthResponse:
    """健康检查。

    `data_source_readonly=true` 是硬事实，不是声明：它来自
    SqliteSource 构造时的写保护自检（尝试建表必须失败）。
    这里只报这一条，不报「库可读」之类的模糊状态。
    """
    return HealthResponse(
        status='ok',
        data_source_readonly=True,
        store_aliases=list(runtime.registry.aliases()),
        orders_in_response_limit=config.MAX_ORDERS_IN_RESPONSE,
    )


@app.get('/api/health/store/{alias}', tags=['system'])
def health_store(alias: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    """店铺库连通性自检（未登录也可访问）。

    返回的信息刻意只有「能不能以只读方式打开」和路径是否存在，
    **不含**任何金额、订单数，所以未登录暴露它没有数据风险。
    """
    store = runtime.store(alias)
    if store is None:
        return {'alias': alias, 'registered': False, 'readonly_openable': False}
    try:
        with runtime.open_source(store) as source:
            source.list_posting_numbers()[:1]
        return {'alias': alias, 'registered': True, 'readonly_openable': True,
                'db_exists': os.path.isfile(store.db_path)}
    except Exception as exc:  # noqa: BLE001 - 自检接口要吞掉所有异常并如实回报
        return {'alias': alias, 'registered': True, 'readonly_openable': False,
                'error': str(exc)}


def main() -> None:  # pragma: no cover - 便捷入口
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    logger.info('店铺库默认路径: %s', DEFAULT_STORE)
    uvicorn.run(app, host='127.0.0.1', port=config.DEFAULT_PORT)


if __name__ == '__main__':  # pragma: no cover
    main()
