# -*- coding: utf-8 -*-
"""数据来源接口。

这是「DB 可替换」的落点：领域层只认这个 Protocol，
换 SQLite / PostgreSQL / 夹具文件，都只换实现，业务代码一行不动。

关键点：接口同时提供两类数据——
  * 输入（快照、流水、发货单）        → 喂给领域层计算
  * 参照（现有系统存下的结果）        → 供回归测试对比
这样同一套测试既能跑在冻结夹具上，也能直接跑在真实生产库上。

第二组方法是**只读聚合查询**（看板用）。它们的存在理由只有一个：
让 API 层不必自己写 SQL。API 只做「参数校验 → 取数 → 调领域层 → 组装 JSON」，
一旦把 SUM/GROUP BY 写进 API，就又会退化成上一版「Web 层自建一套模型」的老路。

聚合方法**不得包含利润口径**：它们只负责取数与求和，
凡是「实际利润 / 预估利润 / 完成率」都必须在 core/domain/profit.py 里算。
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Protocol, Sequence

from ..domain.profit import Operation, Posting, SettlementSnapshot


# ── 聚合查询的输出结构 ───────────────────────────────────────────
@dataclass(frozen=True)
class PeriodOrderRow:
    """看板订单列表的一行 —— 原始数据，不含任何利润口径。

    `snapshot` / `posting` 是喂给领域层的输入；`expected_operation_ids`
    是 §7.1 的权威操作集来源（事实表），**不是**快照的 operation_ids。
    """

    posting_number: str
    settlement_date: Optional[str]
    snapshot: Optional[SettlementSnapshot]
    posting: Optional[Posting]
    expected_operation_ids: Sequence[str]
    operations: Sequence[Operation]
    reference_actual_profit_cny: Optional[Decimal] = None
    reference_estimated_profit_cny: Optional[Decimal] = None


@dataclass(frozen=True)
class PeriodAmounts:
    """一个时间窗内的合计。金额为 None 表示「该口径在本窗口取不到数据」。"""

    total_order_count: int = 0
    complete_order_count: int = 0
    actual_profit_cny: Optional[Decimal] = None
    estimated_profit_cny: Optional[Decimal] = None
    direct_net_cny: Optional[Decimal] = None
    purchase_cost_cny: Optional[Decimal] = None
    platform_fee_cny: Optional[Decimal] = None
    logistics_cost_cny: Optional[Decimal] = None


@dataclass(frozen=True)
class DailyAmounts:
    """按天汇总的一行。"""

    date: str
    actual_profit_cny: Optional[Decimal] = None
    estimated_profit_cny: Optional[Decimal] = None
    order_count: int = 0
    complete_order_count: int = 0


@dataclass(frozen=True)
class OverdueInfo:
    """逾期单信息。`available=False` 表示这个数据源/库里查不到逾期口径。"""

    store_alias: str
    overdue_count: Optional[int] = None
    source: str = 'unknown'
    as_of: Optional[str] = None
    note: Optional[str] = None
    available: bool = True


@dataclass(frozen=True)
class DataCutoff:
    """数据源的两个时间边界。它们**不是一回事**，混用会算出空窗口。

    * `cutoff` —— 库里最后一个可观测的写入时间。**只用于展示**（看板顶部
      「数据截止」）与兜底。它回答的是「这份数据是什么时候同步进来的」。
    * `window_end` —— 看板时间窗的**右端**，必须由「窗口查询真的能取到数」
      的那张表给出，也就是最后一个**已结算**的日期。它回答的是
      「窗口排到哪天为止，最后一天还有数据」。

    为什么要拆开：写入时间来自同步动作，而同步动作与财务数据的新鲜度
    可以完全脱钩。真实踩到的例子 —— 财务同步停在 2026-09-10，而运营的
    逾期扫描在 2026-09-19 跑过一次，若把扫描时间当成窗口右端，
    `days=7` 的窗口（09-13 ~ 09-19）里**一天财务数据都没有**，
    看板显示「0 单 / 实际利润为空」。那不是数据缺失，是窗口取错了。

    `window_end is None` 表示该数据源给不出这个边界（夹具 / 测试替身），
    调用方退回 `cutoff.date()`，行为与拆分之前逐字相同。
    """

    cutoff: Optional[datetime] = None
    source: str = 'unknown'
    candidates: dict = field(default_factory=dict)
    window_end: Optional[date] = None


# ── 逐 SKU 下钻的输出结构（ADR-0006）─────────────────────────────
#
# 为什么这些结构在 repository 层而不是 API 层：
# 「把订单的金额落到哪个货号上」是**取数与分组**问题（分组键来自数据本身），
# 不是利润口径问题。放进 API 就又变成「Web 层自建一套模型」。
# 但**利润本身**一律不在这里算：这里只给出归属后的原始数。
@dataclass(frozen=True)
class PeriodSkuRow:
    """窗口内「一个订单 × 一个货号」的明细行 —— 逐 SKU 下钻的取数单位。

    每一行带的金额都是**它自己名下**的：一个订单只有一个货号时整单归属到该行
    （不需要、也不允许分摊）；多个货号时按数据自带的按行金额/单件成本落位，
    落不下去的行留空并写明原因。

    §7.1 的三个输入（snapshot / operations / expected_operation_ids）
    只有整单归属到这一行时才有值 —— 多货号订单的流水无法按货号归属，
    此时 `actual_attributable=False`，原因在 `actual_unknown_reason`。
    """

    posting_number: str
    settlement_date: Optional[str]
    offer_id: str
    sku: Optional[str] = None
    product_name: Optional[str] = None
    quantity: int = 0
    revenue_cny: Optional[Decimal] = None
    purchase_cost_cny: Optional[Decimal] = None
    logistics_cost_cny: Optional[Decimal] = None
    platform_fee_cny: Optional[Decimal] = None
    #: 归属依据：single_item（整单归属）/ per_line（按行金额归属）
    attribution: str = 'single_item'
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
class UnattributedRecord:
    """一笔**无法归属到任何货号**的金额，以及原因。

    存在的理由与 `core/README.md` 里「3 行 ID начисления 为空」的处理一致：
    一行都不静默丢弃。这些金额会以「未归属」区块如实下发，
    于是「逐 SKU 合计 + 未归属 = 订单口径合计」是一条可检验的等式。
    """

    field: str
    reason: str
    amount: Optional[Decimal] = None
    posting_number: Optional[str] = None


@dataclass(frozen=True)
class UnattributedAmounts:
    """窗口内无法归属到货号的金额汇总（按字段）。

    `records` 里**一行都不丢弃**：归不出去的每一笔都带着原因和来源单号，
    调用方可以据此汇总、分组或直接展示。
    """

    posting_count: int = 0
    records: Sequence[UnattributedRecord] = ()


@dataclass(frozen=True)
class SkuDetail:
    """逐 SKU 下钻的取数结果。**不含任何利润口径。**"""

    rows: Sequence[PeriodSkuRow] = ()
    unattributed: UnattributedAmounts = UnattributedAmounts()
    #: 实际利润整单都无法归属的订单（多货号订单）—— 交给调用方走 evaluate_actual
    actual_unattributed: Sequence[PeriodOrderRow] = ()
    order_count: int = 0


class ProfitDataSource(Protocol):
    """利润核算的数据来源。实现必须只读，且不得包含任何凭据。"""

    # ── 逐单输入 ──
    def list_posting_numbers(self) -> Sequence[str]:
        """返回本数据源覆盖的全部订单号。"""
        ...

    def settlement_snapshot(self, posting_number: str) -> Optional[SettlementSnapshot]:
        """取该订单最新的锁定结算快照；没有则返回 None。"""
        ...

    def operations(self, posting_number: str) -> Sequence[Operation]:
        """取该订单的全部财务流水操作（快照集合 ∪ 事实表集合）。"""
        ...

    def operations_for(self, posting_number: str,
                       ids: Optional[Sequence[str]]) -> Sequence[Operation]:
        """取指定 operation_id 集合对应的流水操作；ids 为 None 时返回全部。

        注意：必须能按**任意 id 集合**取数 —— 手续费可能挂在基单号上
        （如订单 FAKE-11D63D3B 的 ACQUIRING 挂在 FAKE-015C8A22），
        按 posting_number 过滤会漏（实测吻合率 35.1% vs 100%）。
        """
        ...

    def linked_operation_ids(self, posting_number: str) -> Sequence[str]:
        """事实表登记的操作集，即 §7.1 的权威口径来源。"""
        ...

    def posting(self, posting_number: str) -> Optional[Posting]:
        """取发货单（§7.2 的输入）。"""
        ...

    # ── 参照 ──
    def reference_actual(self, posting_number: str) -> dict:
        """现有系统为该订单存下的 §7.1 结果，用作回归基准。"""
        ...

    def reference_estimated(self, posting_number: str) -> Optional[str]:
        """现有系统为该订单存下的 §7.2 结果。"""
        ...

    def reference_completion(self) -> dict:
        """现有系统的全库核算完成率基准。"""
        ...

    # ── 只读聚合（看板用）────────────────────────────────────────
    #
    # 时间窗的口径约定（实现必须一致，否则趋势与合计会对不上）：
    #   * 一个订单属于哪个窗口，由**锁定结算快照的 settlement_date**决定。
    #     快照日期一旦锁定就不再变化，所以同一订单在重复查询中落点稳定。
    #   * 只有 state='locked' 的快照才算「已结算」。没有锁定快照的订单
    #     不属于任何窗口 —— 它还没结算，既不该进实际利润，也不该进预估利润。
    #   * 日期一律按 `YYYY-MM-DD` 字符串比较（与库中存储格式一致）。
    def data_cutoff(self, store_alias: str) -> DataCutoff:
        """返回本数据源的时间边界（见 `DataCutoff`）。

        `cutoff` 是最后一个可观测的写入时间；`window_end` 是**窗口右端**，
        取自最后一个已结算的日期。实现不得用运营类表（逾期扫描等）的时间
        去决定 `window_end` —— 那会让窗口右端越过财务数据，
        直接后果是短窗口返回空数据。
        """
        ...

    def orders_for_period(self, store_alias: str, start_date: str, end_date: str,
                          limit: Optional[int] = None,
                          offset: int = 0) -> Sequence[PeriodOrderRow]:
        """取窗口内的订单（原始数据，按结算日倒序）。`limit=None` 表示取整个窗口。

        **这是看板的权威取数路径。** 取回来的每一行都带齐了喂给
        `evaluate_actual` / `evaluate_estimated` 的输入，所以金额只能由
        core/domain/profit.py 算出，调用方不需要（也不允许）自己补口径。

        实现必须先按权威操作集（事实表 linked_operation_ids）批量取齐
        operations，再交给调用方 —— 逐单 N+1 查询在 2000 单规模上不可接受。
        """
        ...

    def amounts_for_period(self, store_alias: str, start_date: str,
                           end_date: str) -> PeriodAmounts:
        """窗口合计的**廉价路径**：只做一次扫描，不逐单取操作明细。

        用途是对账/初筛/告警，**不是**看板的权威数字来源 ——
        它的金额若在 SQL 里做浮点聚合，会有 1 分以内的尾差。
        看板一律走 `orders_for_period` + 领域层。

        actual_profit_cny / estimated_profit_cny / complete_order_count
        只统计库里 `actual_complete=1` / `estimated_complete=1` 的订单，
        缺失值跳过而不是用 0 顶替。
        """
        ...

    def daily_amounts(self, store_alias: str, start_date: str,
                      end_date: str) -> Sequence[DailyAmounts]:
        """按天汇总的廉价路径。**只返回有数据的日期**，不补零日期行 ——
        「当天没数据」和「当天利润为 0」是两件事。"""
        ...

    def sku_detail_for_period(self, store_alias: str, start_date: str,
                              end_date: str) -> SkuDetail:
        """窗口内**逐 SKU**（按货号）的取数结果 —— 看板下钻用。

        分组键必须来自**数据本身**：应计报表的 `Артикул` / `SKU`、以及
        `postings.csv`（库里是 `posting_items`）的 `货号` / `SKU` / `数量`。
        **严禁按比例分摊**：一笔金额归不到某个货号时，必须留在
        `SkuDetail.unattributed` 里并写明原因，绝不允许摊到各 SKU 头上，
        也绝不允许用 0 顶替（详见 docs/adr/0006-逐SKU利润下钻.md）。

        时间窗口的口径与 `orders_for_period` **完全相同**（按锁定快照的
        `settlement_date`），否则「逐 SKU 合计」与「订单口径合计」会对不上。

        本方法**不得包含任何利润口径**：它只做「按分组键取数与归属」，
        §7.1/§7.2 一律由调用方交给 core/domain/profit.py 计算。
        """
        ...

    def overdue_count(self, store_alias: str) -> OverdueInfo:
        """逾期单数。库里查不到时返回 available=False，**不得返回 0 冒充**。"""
        ...
