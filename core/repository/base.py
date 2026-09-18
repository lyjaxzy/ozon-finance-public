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
from datetime import datetime
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
    """数据截止时间：库里最后一个可观测的写入时间。"""

    cutoff: Optional[datetime] = None
    source: str = 'unknown'
    candidates: dict = field(default_factory=dict)


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
        """本数据源最后一个可观测的写入时间，用作看板的数据截止时间。"""
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

    def overdue_count(self, store_alias: str) -> OverdueInfo:
        """逾期单数。库里查不到时返回 available=False，**不得返回 0 冒充**。"""
        ...
