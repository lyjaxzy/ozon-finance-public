# -*- coding: utf-8 -*-
"""店铺列表接口（ADR-0008）。

    GET /api/stores

只做两件事：
  1. 下发**当前用户可见**的店铺（别名 + 展示名 + 库是否可用 + 最近结算日）；
  2. 让前端不必把别名写死在自己的代码里。

**刻意没有写操作。** 本项目的性质是「`api/` 下没有任何写路由，SQL 全是 SELECT」，
店铺注册表是部署配置（`OZON_STORES` 环境变量）。为了一个管理界面破掉这条性质，
等于把「只读」这个保证换成「大致只读」。所以「新增店铺」的正确做法是改配置后重启，
界面把这句话直接显示给用户（而不是给一个点了没用的按钮）。

为什么每个店都去开一次库：目的就是告诉用户「这个入口现在能不能点」。
注册表里有别名但库文件不在（例如 placeholder 店铺）时，
必须如实给出 `available=false` + 原因，而不是等用户点进去吃 503。
"""
from fastapi import APIRouter, Depends

from .. import config
from ..dashboard import format_cutoff
from ..deps import Runtime, get_current_user, get_runtime

router = APIRouter(prefix='/api/stores', tags=['stores'])


@router.get('')
def list_stores(user=Depends(get_current_user),
                runtime: Runtime = Depends(get_runtime)) -> dict:
    aliases = runtime.visible_aliases(user)
    items = []
    for alias in aliases:
        store = runtime.store(alias)
        if store is None:  # visible_aliases 已经过滤过，这里是兜底
            continue
        items.append(_describe(runtime, store))

    return {
        'stores': items,
        'total': len(items),
        # 前端据此渲染分页档位与「新增店铺怎么做」，两边不各写一份
        'page_size_options': list(config.PAGE_SIZE_OPTIONS),
        'max_aggregate_stores': config.MAX_AGGREGATE_STORES,
        'config_hint': (
            '新增/下线店铺要改服务端 OZON_STORES 环境变量后重启（形如 '
            'alias=库路径;alias=库路径）。本系统是只读的，接口里没有写操作 —— '
            '不给一个点了没用的按钮。'),
    }


def _describe(runtime: Runtime, store) -> dict:
    """探测一个店铺：能不能打开、数据到哪天。**失败也要如实下发。**"""
    item = {
        'alias': store.alias,
        'display_name': store.display_name,
        # 库路径不下发：它是服务端信息，前端只需要别名（路径也属于不该外泄的配置）
        'available': False,
        'error': None,
        'data_cutoff': None,
        'window_end': None,
    }
    try:
        with runtime.open_source(store) as source:
            cutoff = source.data_cutoff(store.alias)
        item['available'] = True
        item['data_cutoff'] = format_cutoff(cutoff.cutoff)
        item['window_end'] = (None if cutoff.window_end is None
                              else cutoff.window_end.isoformat())
    except FileNotFoundError as exc:
        # 配置里有别名、磁盘上没库：服务端配置故障，如实写明而不是静默跳过
        item['error'] = str(exc)
    except Exception as exc:  # noqa: BLE001 - 一个店坏了不该让整个列表打不开
        item['error'] = '%s: %s' % (type(exc).__name__, exc)
    return item
