# -*- coding: utf-8 -*-
"""响应/请求模型。

这些模型**只是契约声明**，不承担计算职责 —— 校验参数、序列化输出而已。
注意：`/api/dashboard/store/{alias}` 的响应形状由前端定死，
所以那里的字段名一律照抄 PRD，不做「优化」。
"""
from typing import List, Optional

from pydantic import BaseModel, Field


# ── 认证 ─────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str
    display_name: str
    role: str
    store_aliases: List[str] = Field(default_factory=list)


class StoreOut(BaseModel):
    alias: str
    display_name: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    expires_in: int
    user: UserOut


class MeResponse(BaseModel):
    user: UserOut
    stores: List[StoreOut] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    data_source_readonly: bool
    store_aliases: List[str] = Field(default_factory=list)
    orders_in_response_limit: int


# ── 看板：与前端约定的响应形状 ────────────────────────────────────
class PeriodOut(BaseModel):
    start: str
    end: str
    days: int


class TotalsOut(BaseModel):
    actual_profit_cny: Optional[str] = None
    estimated_profit_cny: Optional[str] = None
    completion_rate: Optional[str] = None
    complete_order_count: int
    total_order_count: int
    #: 查不到逾期口径时为 null —— 「不知道」不等于「0 单逾期」
    overdue_count: Optional[int] = None


class TrendPointOut(BaseModel):
    date: str
    actual_profit_cny: Optional[str] = None
    estimated_profit_cny: Optional[str] = None


class CompositionItemOut(BaseModel):
    key: str
    label: str
    value_cny: Optional[str] = None


class OrderRowOut(BaseModel):
    posting_number: str
    settlement_date: Optional[str] = None
    direct_net_rub: Optional[str] = None
    exchange_rate_rub_per_cny: Optional[str] = None
    purchase_cost_cny: Optional[str] = None
    actual_profit_cny: Optional[str] = None
    complete: bool
    unknown_reason: Optional[str] = None


class DashboardResponse(BaseModel):
    store_alias: str
    data_cutoff: Optional[str] = None
    period: PeriodOut
    totals: TotalsOut
    trend: List[TrendPointOut] = Field(default_factory=list)
    composition: List[CompositionItemOut] = Field(default_factory=list)
    orders: List[OrderRowOut] = Field(default_factory=list)
