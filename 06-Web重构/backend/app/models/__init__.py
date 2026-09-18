# -*- coding: utf-8 -*-
"""OZON 云端看板 — 数据模型。

本模块定义整个系统的数据表:
  - 用户与角色 (root / store_operator / finance)
  - 店铺注册表 (多店隔离的核心)
  - 角色-店铺授权 (哪些角色可访问哪些店铺)
  - 看板聚合数据 (从现有 SQLite 导入的经营指标)
"""
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, JSON, UniqueConstraint, Index, Date
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


def _now():
    return datetime.utcnow()


# ============================================================================
# 用户与认证
# ============================================================================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(128), default="")
    # role: root | store_operator | finance
    role = Column(String(32), nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def to_dict(self, **extra):
        d = {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        d.update(extra)
        return d


class UserStoreGrant(Base):
    """角色权限: 某个用户被授权访问哪些店铺。

    - root: 无需记录(默认全部店铺)
    - store_operator: 只能访问被授权的店铺
    - finance: 只能访问被授权店铺的财务数据
    """
    __tablename__ = "user_store_grants"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    store_alias = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("user_id", "store_alias", name="uq_user_store"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "store_alias": self.store_alias,
        }


# ============================================================================
# 店铺注册表
# ============================================================================

class StoreProfile(Base):
    """多店铺注册表。每个店铺可以有独立的数据库/数据来源。"""
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True)
    store_alias = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(128), default="")
    enabled = Column(Boolean, default=True)
    # 该店铺数据来源说明(本地 db 路径 / 上传批次标识)
    source_db_path = Column(String(255), default="")
    currency = Column(String(16), default="CNY")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            "id": self.id,
            "store_alias": self.store_alias,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "source_db_path": self.source_db_path,
            "currency": self.currency,
        }


# ============================================================================
# 看板聚合数据 (从现有 SQLite 导入的经营指标)
# ============================================================================
# 设计原则: 后端从店铺库汇总出"每日/每月/累计"的关键指标, 存这里供前端查询。
# 这样前端不需要直接啃庞大的原始表, 响应快且稳定。

class StoreDailyMetric(Base):
    """单店每日经营指标(营业额/利润/成本/费用)。"""
    __tablename__ = "store_daily_metrics"

    id = Column(Integer, primary_key=True)
    store_alias = Column(String(64), nullable=False, index=True)
    metric_date = Column(Date, nullable=False, index=True)
    # 营业额(CNY)
    revenue_cny = Column(Float, default=0)
    sale_value_rub = Column(Float, default=0)
    # 成本
    purchase_cost_cny = Column(Float, default=0)
    logistics_cost_cny = Column(Float, default=0)
    platform_fees_cny = Column(Float, default=0)
    # 利润(预估/实际)
    estimated_profit_cny = Column(Float, default=0)
    actual_profit_cny = Column(Float, default=0)
    # 订单数
    order_count = Column(Integer, default=0)
    # 汇率
    exchange_rate_rub_per_cny = Column(Float, default=0)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("store_alias", "metric_date", name="uq_store_date"),
        Index("ix_store_date", "store_alias", "metric_date"),
    )

    def to_dict(self):
        return {
            "store_alias": self.store_alias,
            "metric_date": self.metric_date.isoformat() if self.metric_date else None,
            "revenue_cny": self.revenue_cny,
            "sale_value_rub": self.sale_value_rub,
            "purchase_cost_cny": self.purchase_cost_cny,
            "logistics_cost_cny": self.logistics_cost_cny,
            "platform_fees_cny": self.platform_fees_cny,
            "estimated_profit_cny": self.estimated_profit_cny,
            "actual_profit_cny": self.actual_profit_cny,
            "order_count": self.order_count,
            "exchange_rate_rub_per_cny": self.exchange_rate_rub_per_cny,
        }


class StoreOverdueMetric(Base):
    """单店逾期订单快照(来自 overdue_fast)。"""
    __tablename__ = "store_overdue_metrics"

    id = Column(Integer, primary_key=True)
    store_alias = Column(String(64), nullable=False, index=True)
    scan_id = Column(String(64), default="")
    as_of = Column(DateTime, default=None)
    active_order_count = Column(Integer, default=0)
    overdue_order_count = Column(Integer, default=0)
    overdue_hours_sum = Column(Float, default=0)
    created_at = Column(DateTime, default=_now)

    def to_dict(self):
        return {
            "store_alias": self.store_alias,
            "scan_id": self.scan_id,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "active_order_count": self.active_order_count,
            "overdue_order_count": self.overdue_order_count,
            "overdue_hours_sum": self.overdue_hours_sum,
        }


class StoreProductLossMetric(Base):
    """单店商品/亏损 issue 汇总(来自 product_loss / handled_operational_issues)。"""
    __tablename__ = "store_product_loss_metrics"

    id = Column(Integer, primary_key=True)
    store_alias = Column(String(64), nullable=False, index=True)
    issue_type = Column(String(64), default="")          # estimated_loss / actual_loss / overdue...
    issue_count = Column(Integer, default=0)
    estimated_loss_cny = Column(Float, default=0)
    created_at = Column(DateTime, default=_now)

    def to_dict(self):
        return {
            "store_alias": self.store_alias,
            "issue_type": self.issue_type,
            "issue_count": self.issue_count,
            "estimated_loss_cny": self.estimated_loss_cny,
        }
