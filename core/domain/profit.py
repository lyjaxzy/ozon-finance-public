# -*- coding: utf-8 -*-
"""三段利润口径的领域实现 —— 本项目财务计算的唯一权威代码。

对应 PRD《7. 计算口径》：
  §7.1 实际利润    evaluate_actual()
  §7.2 预估利润    evaluate_estimated()
  §7.3 核算完成率  completion_rate()

设计约束
--------
1. **纯函数**：不碰数据库、不碰文件、不碰网络、不依赖 Windows。可被单测直接调用。
2. **只用 Decimal**：金额一律 Decimal，禁止 float 进入计算（PRD §14.2 要求金额不用 FLOAT）。
3. **口径可追溯到证据**：
   - §7.1 的 direct_net 必须按「结算快照列出的 operation_ids」逐个取数，
     **不能**按 posting_number 去 JOIN 流水表 —— 实测两种做法吻合率是 100% vs 35.1%。
     原因：ACQUIRING 等手续费挂在基单号（如 FAKE-015C8A22）上，
     而订单号带条目后缀（FAKE-11D63D3B），按单号 JOIN 会漏掉这部分费用。
   - 缺失值一律填 unknown_reason，**不得用 0 代替**（PRD §7.1 完整性规则）。
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Iterable, Optional, Sequence

# ── 常量（PRD §7.5）───────────────────────────────────────────────
MIN_EXCHANGE_RATE = Decimal('5')
MAX_EXCHANGE_RATE = Decimal('20')
RATE_QUANTIZE = Decimal('0.0001')
CNY_QUANTIZE = Decimal('0.01')

ZERO = Decimal('0')


class UnknownReason(str, Enum):
    """actual_unknown_reason 的取值。字符串与库中存量值保持一致。"""

    NO_SETTLED_SALE = 'no_settled_sale'
    NO_DIRECT_SETTLEMENT = 'no_direct_settlement'
    INVALID_EXCHANGE_RATE = 'invalid_exchange_rate'
    MISSING_EXCHANGE_RATE = 'missing_exchange_rate'
    MISSING_PURCHASE_COST = 'missing_purchase_cost'
    PLATFORM_SUBSIDY_ONE_RUBLE_PROMOTION = 'platform_subsidy_one_ruble_promotion'
    MISSING_OPERATION = 'missing_operation'


# ── 输入实体 ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class Operation:
    """一条财务流水操作。amount_rub 是平台给出的净额。"""

    operation_id: str
    amount_rub: Optional[Decimal]
    operation_category: Optional[str] = None
    occurred_at: Optional[str] = None


@dataclass(frozen=True)
class SettlementSnapshot:
    """锁定结算快照。§7.1 的取数边界由 operation_ids 决定。"""

    posting_number: str
    settlement_date: Optional[str]
    state: Optional[str]
    direct_net_rub: Optional[Decimal]
    settled_sales_rub: Optional[Decimal]
    exchange_rate_rub_per_cny: Optional[Decimal]
    purchase_cost_cny: Optional[Decimal]
    operation_ids: Sequence[str]
    unknown_reason: Optional[str] = None


@dataclass(frozen=True)
class Posting:
    """发货单。§7.2 的输入。"""

    posting_number: str
    status: Optional[str]
    revenue_cny: Optional[Decimal]
    purchase_cost_cny: Optional[Decimal]
    logistics_cost_cny: Optional[Decimal]
    estimated_platform_fee_cny: Optional[Decimal]


# ── 输出实体 ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class ActualProfit:
    posting_number: str
    direct_net_rub: Optional[Decimal]
    exchange_rate: Optional[Decimal]
    purchase_cost_cny: Optional[Decimal]
    actual_profit_cny: Optional[Decimal]
    complete: bool
    unknown_reason: Optional[str]


@dataclass(frozen=True)
class EstimatedProfit:
    posting_number: str
    revenue_cny: Decimal
    purchase_cost_cny: Decimal
    logistics_cost_cny: Decimal
    platform_fee_cny: Decimal
    estimated_profit_cny: Decimal
    complete: bool
    unknown_reason: Optional[str]


# ── 工具 ─────────────────────────────────────────────────────────
def to_decimal(value) -> Optional[Decimal]:
    """把库里的 TEXT 金额安全转成 Decimal。空值/非法值返回 None，绝不返回 0 顶替。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    s = str(value).strip()
    if s == '':
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def quantize_cny(value: Decimal) -> Decimal:
    return value.quantize(CNY_QUANTIZE, rounding=ROUND_HALF_UP)


def quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTIZE, rounding=ROUND_HALF_UP)


def rate_is_valid(rate: Optional[Decimal]) -> bool:
    """PRD §7.5：有效区间 5 <= rate <= 20（RUB per CNY）。"""
    return rate is not None and MIN_EXCHANGE_RATE <= rate <= MAX_EXCHANGE_RATE


def sum_amounts(values: Iterable[Optional[Decimal]], default=ZERO) -> Decimal:
    """把一批 Decimal 求和；None 与非法值一律跳过，**不当作 0 参与**。

    存在的理由是「汇总口径一致性」：仓储层与 API 层汇总时若各自手写
    `sum(...)` / `if v is not None`，很容易出现一处跳过、一处补 0 的分歧。
    汇总额一律经过这个函数，行为就只有一个定义。

    注意：这只是求和工具，不是利润口径。§7.1/§7.2/§7.3 的计算仍然只能
    走 evaluate_actual / evaluate_estimated / completion_rate。
    """
    total = default
    for v in values:
        if v is None:
            continue
        amount = v if isinstance(v, Decimal) else to_decimal(v)
        if amount is None:
            continue
        total += amount
    return total


# ── §7.1 实际利润 ────────────────────────────────────────────────
def sum_direct_net(operations: Iterable[Operation]) -> Decimal:
    """按操作明细求和。缺金额的操作按 0 计入求和，但完整性由 evaluate_actual 判定。"""
    total = ZERO
    for op in operations:
        if op.amount_rub is not None:
            total += op.amount_rub
    return total


def evaluate_actual(snapshot: Optional[SettlementSnapshot],
                    operations: Sequence[Operation],
                    expected_ids: Optional[Sequence[str]] = None) -> ActualProfit:
    """§7.1 实际利润。

    actual_profit_cny = (Σ 权威操作集金额) / 结算汇率 - 采购成本

    **权威操作集**由调用方通过 operations/expected_ids 传入。
    实测结论（详见 core/README.md）：权威集是 `posting_profit_facts.linked_operation_ids`，
    而不是结算快照的 operation_ids —— 快照会被重新锁定，退货也会在结算后才到达，
    两者在约 1.8% 的订单上互有出入，且两个方向都有。

    完整性规则（PRD §7.1）：
      actual_complete = 1 当且仅当 流水齐全 且 采购成本齐全 且 汇率可用且在有效区间。
      不完整时必须给出 unknown_reason，且调用方不得把该订单计入实际利润汇总。
    """
    if snapshot is None:
        # 没有任何结算记录。旧实现还会区分 no_direct_settlement，
        # 其判别依据在旧实现内部（该订单是否存在直接净额），输入侧不可观测，
        # 属已记录的待业务确认口径项（PRD T-05）。
        return ActualProfit('', None, None, None, None, False,
                            UnknownReason.NO_SETTLED_SALE.value)

    pn = snapshot.posting_number
    settled_sales = snapshot.settled_sales_rub
    direct_net = snapshot.direct_net_rub
    rate = snapshot.exchange_rate_rub_per_cny
    cost = snapshot.purchase_cost_cny

    def fail(reason, dn=None, rt=None, pc=None):
        return ActualProfit(pn, dn, rt, pc, None, False, reason)

    # 未结算销售 → 该订单不适用实际利润口径
    if settled_sales is None or settled_sales == ZERO:
        return fail(UnknownReason.NO_SETTLED_SALE.value)

    # 直接净额：以调用方给出的权威操作集为准
    expected = tuple(expected_ids) if expected_ids is not None else tuple(snapshot.operation_ids)
    if operations:
        if expected and len(operations) != len(expected):
            # 有操作取不到流水，口径无法闭合，不得用部分和冒充
            return fail(UnknownReason.MISSING_OPERATION.value)
        direct_net = sum_direct_net(operations)
    elif direct_net is None:
        return fail(UnknownReason.NO_DIRECT_SETTLEMENT.value)

    if rate is None:
        return fail(UnknownReason.MISSING_EXCHANGE_RATE.value, dn=direct_net)
    if not rate_is_valid(rate):
        return fail(UnknownReason.INVALID_EXCHANGE_RATE.value, dn=direct_net, rt=rate)
    if cost is None:
        return fail(UnknownReason.MISSING_PURCHASE_COST.value, dn=direct_net, rt=rate)

    profit = quantize_cny(direct_net / rate) - cost
    return ActualProfit(pn, direct_net, rate, cost, profit, True, None)


# ── §7.2 预估利润 ────────────────────────────────────────────────
def evaluate_estimated(posting: Optional[Posting]) -> EstimatedProfit:
    """§7.2 预估利润。

    estimated_profit_cny = 收入 - 采购成本 - 预估物流费 - 预估平台佣金

    缺失项按 0 参与计算（与现有实现一致，已用黄金样本验证），
    但完整性单独用 complete 标记，供汇总时排除不完整订单。
    """
    pn = posting.posting_number if posting else ''
    if posting is None:
        return EstimatedProfit(pn, ZERO, ZERO, ZERO, ZERO, ZERO, False,
                               'missing_posting')

    revenue = to_decimal(posting.revenue_cny)
    cost = to_decimal(posting.purchase_cost_cny)
    logistics = to_decimal(posting.logistics_cost_cny)
    fee = to_decimal(posting.estimated_platform_fee_cny)

    revenue_v = revenue if revenue is not None else ZERO
    cost_v = cost if cost is not None else ZERO
    logistics_v = logistics if logistics is not None else ZERO
    fee_v = fee if fee is not None else ZERO

    profit = quantize_cny(revenue_v - cost_v - logistics_v - fee_v)

    reason = None
    if revenue is None:
        reason = 'missing_revenue'
    elif cost is None:
        reason = UnknownReason.MISSING_PURCHASE_COST.value

    return EstimatedProfit(pn, revenue_v, cost_v, logistics_v, fee_v,
                           profit, reason is None, reason)


# ── §7.3 核算完成率 ──────────────────────────────────────────────
def completion_rate_from_counts(done: int, total: int) -> Decimal:
    """完成率的**唯一定义**：完整核算订单数 / 总订单数。total 为 0 时返回 0。

    为什么单独抽出来（ADR-0008）：多店合计时手上只有各店的
    「完整单数 / 总单数」，没有逐个 ActualProfit 可传。
    若在 API 里另写一次除法，就出现了第二处完成率定义 ——
    两处一旦漂移（例如一处四舍五入、一处不），合计与单店就会对不上。
    所以 `completion_rate()` 也改成调用本函数，全项目只有这一处除法。
    """
    if total <= 0:
        return ZERO
    return (Decimal(done) / Decimal(total)).quantize(
        Decimal('0.0001'), rounding=ROUND_HALF_UP)


def completion_rate(results: Sequence[ActualProfit]) -> Decimal:
    """核算完成率 = 完整核算订单数 / 总订单数。空集合返回 0。"""
    return completion_rate_from_counts(
        sum(1 for r in results if r.complete), len(results))


def reason_histogram(results: Sequence[ActualProfit]) -> dict:
    """unknown_reason 分布。用于审计与告警。"""
    hist = {}
    for r in results:
        if r.complete:
            continue
        key = r.unknown_reason or 'unknown'
        hist[key] = hist.get(key, 0) + 1
    return hist


# ══════════════════════════════════════════════════════════════════
# §7.1 / §7.2 的 SKU 维度投影（ADR-0006「逐 SKU 利润下钻」）
# ══════════════════════════════════════════════════════════════════
#
# 为什么需要这一节：OZON 的两份导出里**唯独没有采购成本**，成本只能由我方按
# 货号录入；既然成本是按货号录的，利润就必须能按货号看。
#
# ⚠️ 核心约束：**逐 SKU 必须靠数据自带的分组键聚合，严禁按比例分摊。**
# 两份导出都自带 SKU 分组键（已核实，见 docs/adr/0006-逐SKU利润下钻.md）：
#   * 应计报表有 `Артикул` / `SKU` / `Количество` / `Сумма итого, руб.`
#   * `postings.csv` 一行一个 SKU，`发货的金额` 就是**该行合计**
# 所以「把一笔金额摊到多个货号上」在本项目里**永远不该发生**：
# 归不到就如实报 None + 原因（沿用 §7.1 的完整性规则），
# 既不拿 0 顶替，也不摊分。本节只做分组求和，不引入任何新公式。


class SkuUnknownReason(str, Enum):
    """逐 SKU 下钻**新增**的缺失原因。

    刻意与 `UnknownReason` 分开：那个枚举的取值是**生产库里的存量字符串**，
    必须逐字保持不变；这里是下钻界面的新分类，混进去会让
    「库里到底有没有这个值」变成一个无法回答的问题。

    收入/成本本身的缺失**不在这里**：它们照旧走 `evaluate_estimated` 给出的
    `missing_revenue` / `unknown_reason`，也就是 `UnknownReason` 的既有风格。
    """

    #: 订单里有多个货号，而这一笔金额（费用/流水）无法按货号归属
    MULTI_ITEM_POSTING = 'multi_item_posting'
    #: 多货号订单：按行价推出来的收入之和 ≠ 订单收入，拒绝用它冒充
    REVENUE_NOT_ATTRIBUTABLE = 'revenue_not_attributable'
    #: 多货号订单：按单件成本算出来的成本之和 ≠ 订单成本，拒绝用它冒充
    COST_NOT_ATTRIBUTABLE = 'cost_not_attributable'
    #: 订单没有商品明细行，连货号都取不到，整单无法归属
    MISSING_POSTING_ITEM = 'missing_posting_item'


@dataclass(frozen=True)
class SkuLineInput:
    """「一个订单 × 一个货号」的一行原始输入 —— 逐 SKU 聚合的输入。

    本结构**不含任何利润口径**，只承载「按分组键已经归属到这一行」的原始数。
    字段留空一律有原因，原因写在对应的 `*_unknown_reason` 里。

    §7.1 的三个输入（snapshot / operations / expected_operation_ids）**只有
    整单归属到这一行时才有值**（即该订单只有一个货号）。多货号订单的费用流水
    无法按货号归属，此时 `actual_attributable=False` 并把原因写在
    `actual_unknown_reason` —— 不是「算不出来就当 0」。
    """

    offer_id: str
    posting_number: str
    sku: Optional[str] = None
    product_name: Optional[str] = None
    quantity: int = 0
    revenue_cny: Optional[Decimal] = None
    purchase_cost_cny: Optional[Decimal] = None
    logistics_cost_cny: Optional[Decimal] = None
    platform_fee_cny: Optional[Decimal] = None
    snapshot: Optional[SettlementSnapshot] = None
    operations: Sequence[Operation] = ()
    expected_operation_ids: Sequence[str] = ()
    actual_attributable: bool = True
    actual_unknown_reason: Optional[str] = None
    revenue_unknown_reason: Optional[str] = None
    cost_unknown_reason: Optional[str] = None
    logistics_unknown_reason: Optional[str] = None
    platform_fee_unknown_reason: Optional[str] = None


@dataclass(frozen=True)
class SkuProfit:
    """一个货号的逐 SKU 利润。

    展示值与对账值是**两个不同的东西**，刻意都留下：

    * 展示字段（`revenue_cny` / `purchase_cost_cny` / `estimated_profit_cny` …）
      遵守「不用部分和冒充」：该货号名下**只要有任一订单缺这一项**，就是 `None`。
    * 对账字段（`*_reconciled_cny`）按「缺失项跳过」求和，用来核对
      「逐 SKU 合计 + 未归属 = 订单口径合计」。它只做对账，不做展示。
    """

    offer_id: str
    sku: Optional[str] = None
    product_name: Optional[str] = None
    quantity: int = 0
    order_count: int = 0
    revenue_cny: Optional[Decimal] = None
    purchase_cost_cny: Optional[Decimal] = None
    logistics_cny: Optional[Decimal] = None
    platform_fee_cny: Optional[Decimal] = None
    estimated_profit_cny: Optional[Decimal] = None
    actual_profit_cny: Optional[Decimal] = None
    estimated_complete: bool = False
    actual_complete: bool = False
    complete: bool = False
    unknown_reason: Optional[str] = None
    missing_fields: Sequence[str] = ()
    incomplete_order_count: int = 0
    #: 对账用：本货号名下已归属的原始数之和（缺失跳过，不补 0）
    revenue_reconciled_cny: Decimal = ZERO
    purchase_cost_reconciled_cny: Decimal = ZERO
    logistics_reconciled_cny: Decimal = ZERO
    platform_fee_reconciled_cny: Decimal = ZERO
    estimated_profit_reconciled_cny: Decimal = ZERO
    actual_profit_reconciled_cny: Decimal = ZERO
    #: 对账用的计数：实际利润真正累加了几行。0 表示「一行的实际利润都算不出来」——
    #: 调用方据此把合计下发成 None 而不是 0.00（0 和「没有数据」是两件事）。
    actual_reconciled_count: int = 0


def _sku_line_estimated(line: SkuLineInput) -> EstimatedProfit:
    """把一行（订单 × 货号）喂给 §7.2 的权威实现。

    刻意**复用 `evaluate_estimated` 本身**而不是在这里重写一遍减法：
    一旦这里自己写公式，逐 SKU 与订单口径就会各有一套，迟早漂移。
    """
    return evaluate_estimated(Posting(
        posting_number=line.posting_number,
        status=None,
        revenue_cny=line.revenue_cny,
        purchase_cost_cny=line.purchase_cost_cny,
        logistics_cost_cny=line.logistics_cost_cny,
        estimated_platform_fee_cny=line.platform_fee_cny,
    ))


def _all_or_none(values: Sequence[Optional[Decimal]]) -> Optional[Decimal]:
    """全都有值才求和；任一项缺失就返回 None。

    这条规则是「**不用部分和冒充**」的落点：一个货号有 3 单，其中 1 单没录采购
    成本，那么它的采购成本合计数是不可知的 —— 拿另外 2 单的和去顶替，
    界面上就会显示一个看起来正常、实际偏小的成本。
    """
    if not values:
        return None
    if any(v is None for v in values):
        return None
    return sum_amounts(values)


def evaluate_sku_profit(lines: Iterable[SkuLineInput]) -> tuple:
    """把（订单 × 货号）明细按货号聚合成逐 SKU 利润。

    分组键就是 `offer_id`（货号）—— 它同时是采购成本库的键，也是两份导出里
    都真实存在的列（应计报表 `Артикул` / postings.csv `货号`）。
    按货号分组之后**只有求和，没有任何摊分**。

    §7.1 与 §7.2 都逐行调用本模块的权威函数：
        estimated ← evaluate_estimated（缺失项按 0 参与，与既有约定一致）
        actual    ← evaluate_actual（只对整单归属的行可算）
    返回顺序 = 货号首次出现的顺序（调用方可自行排序）。
    """
    groups: dict = {}
    order = []
    for line in lines:
        key = line.offer_id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(line)
    return tuple(_evaluate_one_sku(key, groups[key]) for key in order)


def _evaluate_one_sku(offer_id: str, lines: Sequence[SkuLineInput]) -> SkuProfit:
    revenues, costs, logistics, fees = [], [], [], []
    est_values, est_ok = [], []
    act_ok = []
    reasons: dict = {}
    incomplete_orders = 0
    quantity = 0

    def note(field, reason):
        if reason and field not in reasons:
            reasons[field] = reason

    for line in lines:
        quantity += int(line.quantity or 0)
        est = _sku_line_estimated(line)
        est_values.append(est.estimated_profit_cny)
        if est.complete:
            est_ok.append(est.estimated_profit_cny)
        else:
            incomplete_orders += 1
            note('estimated_profit_cny', est.unknown_reason)

        if not line.actual_attributable:
            incomplete_orders += 1
            note('actual_profit_cny', line.actual_unknown_reason)
        else:
            act = evaluate_actual(line.snapshot, line.operations,
                                  line.expected_operation_ids)
            if act.complete and act.actual_profit_cny is not None:
                act_ok.append(act.actual_profit_cny)
            else:
                incomplete_orders += 1
                note('actual_profit_cny', act.unknown_reason)

        revenues.append(line.revenue_cny)
        costs.append(line.purchase_cost_cny)
        logistics.append(line.logistics_cost_cny)
        fees.append(line.platform_fee_cny)
        note('revenue_cny', line.revenue_unknown_reason)
        note('purchase_cost_cny', line.cost_unknown_reason)
        note('logistics_cny', line.logistics_unknown_reason)
        note('platform_fee_cny', line.platform_fee_unknown_reason)

    revenue = _all_or_none(revenues)
    cost = _all_or_none(costs)
    logistics_total = _all_or_none(logistics)
    fee_total = _all_or_none(fees)

    estimated_complete = len(est_ok) == len(lines)
    actual_complete = len(act_ok) == len(lines)
    estimated = sum_amounts(est_ok) if estimated_complete else None
    actual = sum_amounts(act_ok) if actual_complete else None

    # 展示值缺失时要给出原因。缺成本**必须**有原因（PRD §7.1 完整性规则）。
    if cost is None and 'purchase_cost_cny' not in reasons:
        reasons['purchase_cost_cny'] = UnknownReason.MISSING_PURCHASE_COST.value
    if revenue is None and 'revenue_cny' not in reasons:
        reasons['revenue_cny'] = 'missing_revenue'

    missing = [name for name, value in (
        ('revenue_cny', revenue),
        ('purchase_cost_cny', cost),
        ('logistics_cny', logistics_total),
        ('platform_fee_cny', fee_total),
        ('estimated_profit_cny', estimated),
        ('actual_profit_cny', actual),
    ) if value is None]

    # 主原因按「先看利润本身能不能算，再看缺哪一项」排优先级
    primary = None
    for field in ('purchase_cost_cny', 'revenue_cny',
                  'estimated_profit_cny', 'actual_profit_cny',
                  'logistics_cny', 'platform_fee_cny'):
        if field in missing and field in reasons:
            primary = reasons[field]
            break

    return SkuProfit(
        offer_id=offer_id,
        sku=next((ln.sku for ln in lines if ln.sku), None),
        product_name=next((ln.product_name for ln in lines if ln.product_name), None),
        quantity=quantity,
        order_count=len({ln.posting_number for ln in lines}),
        revenue_cny=revenue,
        purchase_cost_cny=cost,
        logistics_cny=logistics_total,
        platform_fee_cny=fee_total,
        estimated_profit_cny=estimated,
        actual_profit_cny=actual,
        estimated_complete=estimated_complete,
        actual_complete=actual_complete,
        complete=estimated_complete and actual_complete,
        unknown_reason=primary,
        missing_fields=tuple(missing),
        incomplete_order_count=incomplete_orders,
        revenue_reconciled_cny=sum_amounts(revenues),
        purchase_cost_reconciled_cny=sum_amounts(costs),
        logistics_reconciled_cny=sum_amounts(logistics),
        platform_fee_reconciled_cny=sum_amounts(fees),
        estimated_profit_reconciled_cny=sum_amounts(est_values),
        actual_profit_reconciled_cny=sum_amounts(act_ok),
        actual_reconciled_count=len(act_ok),
    )
