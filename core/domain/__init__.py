# -*- coding: utf-8 -*-
"""OZON 财务领域核心。

本包是财务计算的唯一权威实现，替代原先只存在于反编译字节码里的逻辑。

约束（刻意为之）：
  * 不依赖 FastAPI / Flask / 任何 web 框架
  * 不依赖 Windows（无 DPAPI、无 taskkill、无计划任务）
  * 不依赖数据库驱动细节（通过 repository 接口取数）
  * 金额一律 Decimal

因此它可以被单元测试直接调用，也可以被将来的 API、CLI、定时任务共同复用。
"""
from .profit import (ActualProfit, EstimatedProfit, Operation, Posting,
                     SettlementSnapshot, UnknownReason, completion_rate,
                     evaluate_actual, evaluate_estimated, reason_histogram,
                     sum_direct_net, to_decimal)

__all__ = [
    'Operation', 'Posting', 'SettlementSnapshot', 'ActualProfit', 'EstimatedProfit',
    'UnknownReason', 'evaluate_actual', 'evaluate_estimated', 'completion_rate',
    'reason_histogram', 'sum_direct_net', 'to_decimal',
]
