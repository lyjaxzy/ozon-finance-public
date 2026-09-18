# -*- coding: utf-8 -*-
"""店铺注册表：别名 → 库路径 的**白名单**。

这是服务端数据隔离的第一道闸门：
  * 请求里的 `{alias}` 必须命中白名单，否则 404（库根本不存在）；
  * 命中之后再查授权，不通过 403。

路径永不来自请求参数 —— 否则 `/api/dashboard/store/..%2f..%2fetc%2fpasswd` 之类的
输入就有机会变成任意文件读取。别名与路径的对应关系只在这里定义。
"""
import json
import os
from dataclasses import dataclass
from typing import Dict, Optional, Sequence

from . import config


@dataclass(frozen=True)
class Store:
    alias: str
    display_name: str
    db_path: str


class StoreRegistry:
    def __init__(self, stores: Sequence[Store]):
        self._by_alias: Dict[str, Store] = {s.alias: s for s in stores}

    @classmethod
    def from_file(cls, path: str) -> 'StoreRegistry':
        with open(path, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        stores = []
        for item in raw.get('stores', []):
            stores.append(Store(
                alias=item['alias'],
                display_name=item.get('display_name') or item['alias'],
                db_path=item['db_path'],
            ))
        return cls(stores)

    @classmethod
    def default(cls) -> 'StoreRegistry':
        """从 `OZON_STORES` 解析白名单；解析不出来时退回单店 store_alpha。

        格式：`alias=路径;alias=路径`。别名与路径的对应只在这里定义，
        **请求参数永远不能提供路径**。
        """
        stores = []
        raw = (config.STORES_RAW or '').strip()
        for chunk in raw.split(';'):
            chunk = chunk.strip()
            if not chunk or '=' not in chunk:
                continue
            alias, _, path = chunk.partition('=')
            alias, path = alias.strip(), path.strip()
            if not alias or not path:
                continue
            display = config.STORE_DISPLAY_NAMES.get(
                alias, 'OZON 店铺 · %s' % alias)
            stores.append(Store(alias, display, path))
        if not stores:
            return cls([Store('store_alpha', 'OZON 俄罗斯站 · 主力店',
                              config.DEFAULT_STORE)])
        return cls(stores)

    def get(self, alias: str) -> Optional[Store]:
        return self._by_alias.get(alias)

    def aliases(self) -> Sequence[str]:
        return tuple(self._by_alias.keys())

    def public_list(self, aliases: Sequence[str]) -> list:
        """只把**被授权的**店铺下发给前端。"""
        out = []
        for alias in aliases:
            store = self._by_alias.get(alias)
            if store is None:
                # 授权里出现了注册表没有的别名：宁可少下发，也不要下发一个打不开的入口
                continue
            out.append({'alias': store.alias, 'display_name': store.display_name})
        return out
