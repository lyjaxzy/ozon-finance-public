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
def completion_rate(results: Sequence[ActualProfit]) -> Decimal:
    """核算完成率 = 完整核算订单数 / 总订单数。空集合返回 0。"""
    if not results:
        return ZERO
    done = sum(1 for r in results if r.complete)
    return (Decimal(done) / Decimal(len(results))).quantize(
        Decimal('0.0001'), rounding=ROUND_HALF_UP)


def reason_histogram(results: Sequence[ActualProfit]) -> dict:
    """unknown_reason 分布。用于审计与告警。"""
    hist = {}
    for r in results:
        if r.complete:
            continue
        key = r.unknown_reason or 'unknown'
        hist[key] = hist.get(key, 0) + 1
    return hist
