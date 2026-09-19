# -*- coding: utf-8 -*-
"""逐 SKU 下钻的**归属规则**（唯一实现，被 SqliteSource 与 ExcelSource 共用）。

为什么单独一个模块：两个数据源的取数方式不同（一个查 `postings.raw_json`，
一个读 `postings.csv` 的按行金额），但**「这一笔钱属于哪个货号」的判定规则
必须是同一条** —— 否则同一个窗口在两个数据源下会得到两套逐 SKU 数字，
而那正是 ADR-0005 反复强调要避免的「悄悄换一套口径出数」。

⚠️ 本模块**不含任何利润公式**。它只做归属：
   一个订单 → 若干（订单 × 货号）行 + 若干「归不出去」的金额记录。
利润一律由调用方交给 `core/domain/profit.py`。

三条规则（都是「按数据自带的分组键」，不是分摊）：

1. **单货号订单**：整单金额直接归给那个货号。这不是分摊 —— 该订单本来
   就只有这一个货号（实测本机 11002 单里 10953 单是这种）。
2. **多货号订单的收入/成本**：用数据自带的按行金额（应计报表与 postings.csv
   都逐行带 SKU 与行金额；库里 `postings.raw_json.products[].price` 也是按行的）。
   并且**必须逐项加总等于整单金额**才采用 —— 对不上就是归不出去，
   整单金额进 `UnattributedRecord`，绝不用一个对不上的和去冒充。
3. **多货号订单的费用与流水**（物流费/平台佣金/财务流水）：这些在库里是
   **订单级**的单值，没有可用的按行分组键 → 一律判「归不出去」，
   金额整体进未归属桶。不摊分（摊分需要一个人为比例，本项目禁止）。
"""
from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence, Tuple

from ..domain.profit import (Operation, SettlementSnapshot, SkuUnknownReason,
                             UnknownReason, sum_amounts)
from .base import PeriodSkuRow, UnattributedRecord

#: 归属依据的取值（写进 PeriodSkuRow.attribution，便于对账时分辨来源）
ATTR_SINGLE_ITEM = 'single_item'
ATTR_PER_LINE = 'per_line'

_MULTI = SkuUnknownReason.MULTI_ITEM_POSTING.value
_NO_ITEM = SkuUnknownReason.MISSING_POSTING_ITEM.value
_REV_BAD = SkuUnknownReason.REVENUE_NOT_ATTRIBUTABLE.value
_COST_BAD = SkuUnknownReason.COST_NOT_ATTRIBUTABLE.value
_MISSING_COST = UnknownReason.MISSING_PURCHASE_COST.value


def attribute_order_lines(
    *,
    posting_number: str,
    settlement_date: Optional[str],
    items: Sequence[tuple],
    revenue_cny: Optional[Decimal],
    purchase_cost_cny: Optional[Decimal],
    unit_cost_by_offer: Optional[Mapping[str, Decimal]] = None,
    line_revenues: Optional[Mapping[str, Optional[Decimal]]] = None,
    logistics_cost_cny: Optional[Decimal] = None,
    platform_fee_cny: Optional[Decimal] = None,
    snapshot: Optional[SettlementSnapshot] = None,
    operations: Sequence[Operation] = (),
    expected_operation_ids: Sequence[str] = (),
) -> Tuple[Tuple[PeriodSkuRow, ...], Tuple[UnattributedRecord, ...]]:
    """把一个订单的金额落到它的货号行上。

    :param items: `[(offer_id, sku, product_name, quantity), ...]`，来自
        `posting_items`（生产库）或 postings.csv 的商品明细行。
    :param revenue_cny: 整单收入。`None` 表示取不到 —— 不是 0。
    :param purchase_cost_cny: 整单采购成本（库里/上游已经算好的那个数）。
    :param unit_cost_by_offer: 单件成本库（货号 → 单件成本）。只在**多货号**
        订单需要按行拆分成本时用到；单货号订单直接用整单成本，不经过它。
    :param line_revenues: 数据自带的**按行收入**（货号 → 行合计）。只在多货号
        订单需要拆分收入时用到。
    :returns: `(行, 归不出去的记录)`。两条路径的金额加总必然等于整单金额：
        要么整单落在行上，要么整单落在未归属桶里，**没有第三种可能**。
    """
    items = tuple(items)
    unit_cost_by_offer = unit_cost_by_offer or {}
    line_revenues = line_revenues or {}
    records: list = []

    if not items:
        # 连货号都取不到（没有商品明细行）。整单金额只能进未归属桶。
        #
        # 两种成因共用这一个原因码，界面上都表现为「无法归属到货号」：
        #   * 生产库：订单在 `postings` 里有行，但 `posting_items` 没有明细行（本机 0 例）；
        #   * Excel 源：该应计订单根本不在 `postings.csv` 里（实测 2026-09 有 236 单如此），
        #     此时收入/成本本来就都是 None，四个字段各记一条记录，金额为 None。
        # 之所以不拆成两个原因码：调用方（数据源）才知道是哪一种，
        # 而拆开会让 `SkuUnknownReason` 混进「取数来源」这种非口径概念。
        for fname, amount in (('revenue_cny', revenue_cny),
                              ('purchase_cost_cny', purchase_cost_cny),
                              ('logistics_cny', logistics_cost_cny),
                              ('platform_fee_cny', platform_fee_cny)):
            records.append(UnattributedRecord(fname, _NO_ITEM, amount, posting_number))
        return (), tuple(records)

    offers = [str(it[0]) for it in items]
    quantities = [int(it[3] or 0) for it in items]
    single = len(items) == 1
    attribution = ATTR_SINGLE_ITEM if single else ATTR_PER_LINE
    # 同一订单里出现重复货号时，按货号建的映射会把重复行折叠掉 ——
    # 折叠后的和就不再等于整单金额，那不是「归属」而是「悄悄丢一行」。
    # 两个取数路径都不该产生这种数据（生产库的 PK 是 (单号, 货号)），
    # 真出现了就退回未归属桶，让它在界面上暴露出来。
    duplicated = len(set(offers)) != len(offers)

    # ── 收入 ──────────────────────────────────────────────────────
    rev_ok = revenue_cny is not None
    line_rev: Dict[str, Decimal] = {}
    if not rev_ok:
        pass
    elif single:
        line_rev = {offers[0]: revenue_cny}
    elif duplicated:
        rev_ok = False
        records.append(UnattributedRecord(
            'revenue_cny', _REV_BAD, revenue_cny, posting_number))
    else:
        values = [line_revenues.get(o) for o in offers]
        if any(v is None for v in values) or sum_amounts(values) != revenue_cny:
            rev_ok = False
            records.append(UnattributedRecord(
                'revenue_cny', _REV_BAD, revenue_cny, posting_number))
        else:
            line_rev = dict(zip(offers, values))

    # ── 采购成本 ─────────────────────────────────────────────────
    cost_ok = purchase_cost_cny is not None
    line_cost: Dict[str, Decimal] = {}
    if not cost_ok:
        pass
    elif single:
        line_cost = {offers[0]: purchase_cost_cny}
    elif duplicated:
        cost_ok = False
        records.append(UnattributedRecord(
            'purchase_cost_cny', _COST_BAD, purchase_cost_cny, posting_number))
    else:
        values = []
        for offer, qty in zip(offers, quantities):
            unit = unit_cost_by_offer.get(offer)
            values.append(None if unit is None else unit * Decimal(qty))
        if any(v is None for v in values) or sum_amounts(values) != purchase_cost_cny:
            cost_ok = False
            records.append(UnattributedRecord(
                'purchase_cost_cny', _COST_BAD, purchase_cost_cny, posting_number))
        else:
            line_cost = dict(zip(offers, values))

    # ── 物流费 / 平台佣金：订单级单值，多货号时没有按行的分组键 ──
    if single:
        line_log = {offers[0]: logistics_cost_cny}
        line_fee = {offers[0]: platform_fee_cny}
        log_ok = logistics_cost_cny is not None
        fee_ok = platform_fee_cny is not None
    else:
        line_log, line_fee = {}, {}
        log_ok = fee_ok = False
        if logistics_cost_cny is not None:
            records.append(UnattributedRecord(
                'logistics_cny', _MULTI, logistics_cost_cny, posting_number))
        if platform_fee_cny is not None:
            records.append(UnattributedRecord(
                'platform_fee_cny', _MULTI, platform_fee_cny, posting_number))

    # ── §7.1 的流水 / 直接净额：只有单货号订单能整单归属 ──────────
    actual_ok = single
    actual_reason = None if actual_ok else _MULTI

    rows = []
    for offer, sku, name, qty in items:
        rows.append(PeriodSkuRow(
            posting_number=posting_number,
            settlement_date=settlement_date,
            offer_id=str(offer),
            sku=None if sku is None else str(sku),
            product_name=name,
            quantity=int(qty or 0),
            revenue_cny=line_rev.get(str(offer)) if rev_ok else None,
            purchase_cost_cny=line_cost.get(str(offer)) if cost_ok else None,
            logistics_cost_cny=line_log.get(str(offer)) if log_ok else None,
            platform_fee_cny=line_fee.get(str(offer)) if fee_ok else None,
            attribution=attribution,
            snapshot=snapshot if actual_ok else None,
            operations=tuple(operations) if actual_ok else (),
            expected_operation_ids=tuple(expected_operation_ids) if actual_ok else (),
            actual_attributable=actual_ok,
            actual_unknown_reason=actual_reason,
            revenue_unknown_reason=(
                None if rev_ok else
                ('missing_revenue' if revenue_cny is None else _REV_BAD)),
            cost_unknown_reason=(
                None if cost_ok else
                (_MISSING_COST if purchase_cost_cny is None else _COST_BAD)),
            logistics_unknown_reason=(
                None if log_ok else (_MULTI if logistics_cost_cny is not None else None)),
            platform_fee_unknown_reason=(
                None if fee_ok else (_MULTI if platform_fee_cny is not None else None)),
        ))
    return tuple(rows), tuple(records)
