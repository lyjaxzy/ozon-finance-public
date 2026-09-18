# -*- coding: utf-8 -*-
"""数据来源接口。

这是「DB 可替换」的落点：领域层只认这个 Protocol，
换 SQLite / PostgreSQL / 夹具文件，都只换实现，业务代码一行不动。

关键点：接口同时提供两类数据——
  * 输入（快照、流水、发货单）        → 喂给领域层计算
  * 参照（现有系统存下的结果）        → 供回归测试对比
这样同一套测试既能跑在冻结夹具上，也能直接跑在真实生产库上。
"""
from typing import Optional, Protocol, Sequence

from ..domain.profit import Operation, Posting, SettlementSnapshot


class ProfitDataSource(Protocol):
    """利润核算的数据来源。实现必须只读，且不得包含任何凭据。"""

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

    def reference_actual(self, posting_number: str) -> dict:
        """现有系统为该订单存下的 §7.1 结果，用作回归基准。"""
        ...

    def reference_estimated(self, posting_number: str) -> Optional[str]:
        """现有系统为该订单存下的 §7.2 结果。"""
        ...

    def reference_completion(self) -> dict:
        """现有系统的全库核算完成率基准。"""
        ...
