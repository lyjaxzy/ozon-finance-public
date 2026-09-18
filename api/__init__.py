# -*- coding: utf-8 -*-
"""OZON 跨境电商财务系统 —— 只读后端 API。

设计红线（PRD §12.2 / 上一版失败教训）：
  1. 利润口径只存在于 `core/domain/profit.py`。本包**不得**出现任何
     加减乘除金额的公式。
  2. 数据只经 `core/repository/` 取。本包**不得**自己写 SQL、也不得自建数据模型。
  3. 店铺库一律 `mode=ro` 打开（由 SqliteSource 保证，带写保护自检）。

一次请求的数据流：
    参数校验 → 取仓储层数据 → 调 core.domain.profit → 组装 JSON
"""
