# -*- coding: utf-8 -*-
"""成本管理接口的载荷组装（ADR-0009）。

与 `api/dashboard.py` 一样：**本文件不出现任何利润公式**，
只做「取数 → 调 core 的成本库 → 组装 JSON」。
清单、缺成本、影响面全部是**计数与差值**，不是利润口径。

四个入口：

    build_cost_list      台账（分页 + 搜索）
    build_missing_costs  缺成本清单（窗口内有订单、成本库没价的货号）
    build_import_preview 导入预览（**不落库**）+ 两种策略下的影响面
    apply_import         确认后落库 + 变更留痕
"""
import hashlib
import os
from decimal import Decimal
from typing import Optional, Sequence

from core.repository.cost_book import CostBook, CostRow, ImportPreview

from . import config
from .dashboard import money

#: 与 core 的策略常量保持一致（放在这里是为了让 API 层不 import sqlite_source 的实现细节）
POLICY_BOOK_FIRST = 'book_first'
POLICY_BOOK_AUTHORITATIVE = 'book_authoritative'

#: 只有这两个角色能写成本库（运营只看数，不该改成本）
COST_WRITE_ROLES = ('root', 'finance')


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


# ── 台账 ─────────────────────────────────────────────────────────
def build_cost_list(book: CostBook, keyword: Optional[str] = None,
                    limit: int = 50, offset: int = 0) -> dict:
    rows, total = book.list_costs(keyword=keyword, limit=limit, offset=offset)
    events, event_total = book.change_events(limit=1)
    items = []
    for r in rows:
        items.append({
            'cost_id': r['cost_id'],
            'seller_sku': r['seller_sku'],
            'platform_sku': r['platform_sku'],
            'unit_cost_cny': r['unit_cost_cny'],
            'effective_from': r['effective_from'],
            'scope': r['scope'],
            'store_alias': r['store_alias'],
            'status': r['status'],
            'source': r['source'],
            'note': r['note'],
            'created_at': r['created_at'],
            'updated_at': r['updated_at'],
        })
    return {
        'book_path': book.path,
        'total': total,
        'returned': len(items),
        'limit': limit,
        'offset': offset,
        'keyword': keyword,
        'rows': items,
        'change_event_total': event_total,
        'last_change': (dict(events[0]) if events else None),
    }


def build_change_events(book: CostBook, seller_sku: Optional[str] = None,
                        limit: int = 50, offset: int = 0) -> dict:
    rows, total = book.change_events(seller_sku=seller_sku, limit=limit, offset=offset)
    return {'total': total, 'rows': rows, 'limit': limit, 'offset': offset}


# ── 缺成本清单 ───────────────────────────────────────────────────
def _order_count_by_offer(rows) -> dict:
    """货号 → 它出现在**多少个订单**里（去重）。

    `PeriodSkuRow` 是「订单 × 货号」的明细行，它**没有** `order_count` 字段
    （那是 `PeriodAmounts` / `DailyAmounts` 的字段）。这里以前写的是
    `getattr(row, 'order_count', 0)`，于是缺成本清单的订单数恒为 0、
    「按订单数排序」退化成按取数顺序，影响面预览的
    `affected_orders` 也恒为 0（与同一条响应里的 `matched_skus` 自相矛盾）。
    订单数只能从明细行的**单号**数出来 —— 一条明细行属于且只属于一个订单。
    """
    counts: dict = {}
    seen: dict = {}
    for row in rows:
        offer = getattr(row, 'offer_id', None)
        if not offer:
            continue
        posting = getattr(row, 'posting_number', None)
        bucket = seen.setdefault(offer, set())
        if posting is not None:
            bucket.add(posting)
        counts[offer] = len(bucket)
    return counts


def build_missing_costs(source, book: CostBook, store_alias: str,
                        start_date: str, end_date: str, days: int,
                        limit: int = 200) -> dict:
    """窗口内有订单、但**成本库里没有有效单价**的货号。

    ⚠️ 必须和「库里订单级成本归不到货号」区分开（那由 SKU 下钻的
    `cost_not_attributable` 表达）—— 一个是**没录价**，一个是**价格落不到位**。
    界面上合成一句话会让人去补一个并不缺的价。
    """
    detail = source.sku_detail_for_period(store_alias, start_date, end_date)
    prices = book.price_map(end_date)
    # 窗口内出现的**货号**（去重）—— 不是「订单×货号」的行数。
    # 看板上的「缺成本 SKU 数」也是按货号数算的，两边口径必须一致，否则用户会看到
    # 两个不等的「缺 SKU 数」而不知道该信哪个。
    offers = {row.offer_id for row in detail.rows if row.offer_id}
    order_counts = _order_count_by_offer(detail.rows)
    missing = []
    for row in detail.rows:
        offer = row.offer_id
        if not offer or offer in prices:
            continue
        missing.append({
            'seller_sku': offer,
            'sku': getattr(row, 'sku', None),
            'product_name': getattr(row, 'product_name', None),
            'quantity': int(getattr(row, 'quantity', 0) or 0),
            'order_count': order_counts.get(offer, 0),
            'attributed_cost_cny': _money(getattr(row, 'purchase_cost_cny', None)),
            'reason': '成本库里没有这个货号的单价',
        })
    # 同一个货号可能在多行出现（多货号订单 / 同一个货号的多张订单）：合并展示。
    # ⚠️ `quantity` 是**按行**的量，要累加；`order_count` 是**按货号**的数
    # （已经数过单号去重了），合并时只能取最大值 —— 累加会把「同一个货号有 2 单」
    # 变成 4 单（每行各加一遍）。踩过：旧写法 `cur['order_count'] += item[...]`。
    merged: dict = {}
    for item in missing:
        cur = merged.get(item['seller_sku'])
        if cur is None:
            merged[item['seller_sku']] = item
        else:
            cur['quantity'] += item['quantity']
            cur['order_count'] = max(cur['order_count'], item['order_count'])
            if cur['attributed_cost_cny'] and item['attributed_cost_cny']:
                cur['attributed_cost_cny'] = money(
                    Decimal(cur['attributed_cost_cny']) + Decimal(item['attributed_cost_cny']))
    rows_missing = sorted(merged.values(), key=lambda x: -x['order_count'])
    return {
        'store_alias': store_alias,
        'period': {'start': start_date, 'end': end_date, 'days': days},
        # 三个数都是**货号级**（去重），与看板的「缺成本 SKU 数」同口径
        'offer_in_window': len(offers),
        'offer_with_price': len(offers & set(prices)),
        'missing_count': len(rows_missing),
        'missing': rows_missing[:limit],
        'truncated': len(rows_missing) > limit,
        'price_source': book.path,
        'book_offer_count': len(prices),
    }


def _money(value) -> Optional[str]:
    return money(value) if isinstance(value, Decimal) else None


# ── 导入预览（不落库）────────────────────────────────────────────
def build_import_preview(book: CostBook, rows: Sequence[CostRow],
                         invalid: Sequence[tuple], source, store_alias: str,
                         start_date: str, end_date: str, days: int,
                         file_name: Optional[str] = None,
                         file_sha: Optional[str] = None) -> dict:
    preview: ImportPreview = book.preview(rows)
    preview.total += 0                       # total 在 preview() 里已计
    impact = _impact(book, preview, source, store_alias, start_date, end_date, days)
    return {
        'file': {'name': file_name, 'sha256': file_sha},
        'book_path': book.path,
        'preview': dict(preview.as_dict(),
                        invalid=[{'line': n, 'value': v, 'reason': why}
                                 for n, v, why in invalid[:50]],
                        invalid_count=len(invalid) + preview.as_dict()['invalid_count'],
                        ok=len(invalid) == 0 and preview.ok),
        'impact': impact,
    }


def _impact(book: CostBook, preview: ImportPreview, source, store_alias: str,
            start_date: str, end_date: str, days: int) -> dict:
    """两种策略下的影响面（ADR-0009 §四）。

    * **库优先**：只有「库里订单级成本为空」的订单才会因这次导入而改变 ——
      当前生产库已锁定订单的成本非空率 100%，所以这里通常是 0 单。
    * **成本库权威**：窗口内命中这些货号的订单，成本会变成「单价 × 数量」，
      与当前取的值得出差额。这是阶段 D 翻开关时的真实影响。
    """
    detail = source.sku_detail_for_period(store_alias, start_date, end_date)
    new_prices = {r.seller_sku: r.unit_cost_cny for r in
                  list(preview.created) + [x[0] for x in preview.updated]}
    order_counts = _order_count_by_offer(detail.rows)

    # 先按货号聚合。**单位是货号，不是明细行** —— `_order_count_by_offer` 给的
    # 已经是「这个货号在窗口里的订单数（单号去重）」，按行各加一遍会把它乘上
    # 明细行数（同一个货号的多张订单会各自再加一次）。
    per_offer: dict = {}
    for row in detail.rows:
        offer = row.offer_id
        if not offer or offer not in new_prices:
            continue
        bucket = per_offer.setdefault(offer, {'qty': 0, 'stored': Decimal('0'),
                                              'priced_orders': 0, 'null_rows': 0})
        bucket['qty'] += int(getattr(row, 'quantity', 0) or 0)
        current = getattr(row, 'purchase_cost_cny', None)
        if current is None:
            bucket['null_rows'] += 1
        else:
            bucket['stored'] += current
            bucket['priced_orders'] += 1

    authoritative_orders = 0
    authoritative_delta = Decimal('0')
    book_first_orders = 0
    book_first_delta = Decimal('0')
    for offer, bucket in per_offer.items():
        unit = new_prices[offer]
        orders = order_counts.get(offer, 0)
        new_cost = unit * Decimal(bucket['qty'])
        authoritative_orders += orders
        authoritative_delta += new_cost - bucket['stored']
        if bucket['null_rows']:
            # 库优先：只有「库里订单级成本为空」的订单会变。同一个货号在窗口里
            # 既有有价单、又有空价单时，受影响的是**空价的那部分数量**，
            # 所以这里按空价行的数量合成，而不是整单数量（否则会虚报影响面）。
            null_qty = sum(int(getattr(r, 'quantity', 0) or 0) for r in detail.rows
                           if getattr(r, 'offer_id', None) == offer
                           and getattr(r, 'purchase_cost_cny', None) is None)
            book_first_delta += unit * Decimal(null_qty)
            book_first_orders += orders - bucket['priced_orders']
    return {
        'window': {'start': start_date, 'end': end_date, 'days': days},
        'matched_skus': len(per_offer),
        'book_first': {
            'affected_orders': book_first_orders,
            'delta_purchase_cost_cny': _money(book_first_delta) if book_first_orders else '0.00',
            'note': ('库优先：只有库里成本为空的订单会变。当前已锁定订单成本非空率 100%，'
                     '所以正常情况下这里是 0 单 —— 数字与导入前一致。'),
        },
        'book_authoritative': {
            'affected_orders': authoritative_orders,
            'delta_purchase_cost_cny': money(authoritative_delta),
            'note': ('成本库为权威：窗口内命中货号的订单成本会变成「成本库单价 × 数量」。'
                     '实测 398 个单货号订单的合成值与库里存量值 100% 吻合，'
                     '所以差额通常只来自多货号订单与成本变动。'),
        },
        'current_policy': config.COST_SOURCE_POLICY,
    }


# ── 落库 ─────────────────────────────────────────────────────────
def apply_import(book: CostBook, rows: Sequence[CostRow], actor: str,
                 file_name: Optional[str] = None,
                 file_sha: Optional[str] = None) -> dict:
    """写入台账 + 变更留痕。入库前**再算一次预览**做校验，防止「预览和实际不一致」。"""
    recheck = book.preview(rows)
    result = book.apply(rows, actor=actor, file_sha256=file_sha,
                        action_prefix='')
    return {
        'applied': result,
        'preview_at_apply': recheck.as_dict(),
        'book_path': book.path,
        'total_after': book.count(),
        'actor': actor,
        'file': {'name': file_name, 'sha256': file_sha},
    }
