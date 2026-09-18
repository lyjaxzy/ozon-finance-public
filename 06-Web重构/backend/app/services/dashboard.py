# -*- coding: utf-8 -*-
"""看板聚合服务。从 store_daily_metrics 等表聚合经营指标, 供 API 查询。"""
from sqlalchemy.orm import Session
from sqlalchemy import func, select
from datetime import date, timedelta

from ..models import (
    StoreDailyMetric,
    StoreOverdueMetric,
    StoreProductLossMetric,
    StoreProfile,
)


def _sum(db, model, alias, col, start=None, end=None):
    q = select(func.coalesce(func.sum(col), 0)).where(model.store_alias == alias)
    if start:
        q = q.where(model.metric_date >= start)
    if end:
        q = q.where(model.metric_date <= end)
    return db.execute(q).scalar() or 0


def store_daily_summary(db: Session, alias: str, days: int = 30) -> dict:
    """单店近 N 天经营概况。"""
    end = date.today()
    start = end - timedelta(days=days - 1)

    revenue = _sum(db, StoreDailyMetric, alias, StoreDailyMetric.revenue_cny, start, end)
    purchase = _sum(db, StoreDailyMetric, alias, StoreDailyMetric.purchase_cost_cny, start, end)
    logistics = _sum(db, StoreDailyMetric, alias, StoreDailyMetric.logistics_cost_cny, start, end)
    fees = _sum(db, StoreDailyMetric, alias, StoreDailyMetric.platform_fees_cny, start, end)
    est_profit = _sum(db, StoreDailyMetric, alias, StoreDailyMetric.estimated_profit_cny, start, end)
    act_profit = _sum(db, StoreDailyMetric, alias, StoreDailyMetric.actual_profit_cny, start, end)
    order_count = _sum(db, StoreDailyMetric, alias, StoreDailyMetric.order_count, start, end)

    total_cost = purchase + logistics + fees
    margin = (est_profit / revenue * 100) if revenue else 0

    overdue = db.query(StoreOverdueMetric).filter_by(store_alias=alias).order_by(
        StoreOverdueMetric.id.desc()
    ).first()
    overdue_info = overdue.to_dict() if overdue else {
        "active_order_count": 0, "overdue_order_count": 0, "overdue_hours_sum": 0, "as_of": None,
    }

    return {
        "store_alias": alias,
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "revenue_cny": revenue,
        "purchase_cost_cny": purchase,
        "logistics_cost_cny": logistics,
        "platform_fees_cny": fees,
        "total_cost_cny": total_cost,
        "estimated_profit_cny": est_profit,
        "actual_profit_cny": act_profit,
        "profit_margin_pct": round(margin, 2),
        "order_count": int(order_count),
        "overdue": overdue_info,
    }


def store_daily_trend(db: Session, alias: str, days: int = 30) -> list[dict]:
    """单店每日趋势(折线图数据)。"""
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows = db.query(StoreDailyMetric).filter(
        StoreDailyMetric.store_alias == alias,
        StoreDailyMetric.metric_date >= start,
        StoreDailyMetric.metric_date <= end,
    ).order_by(StoreDailyMetric.metric_date).all()
    return [r.to_dict() for r in rows]


def store_loss_summary(db: Session, alias: str) -> dict:
    """单店 issue/亏损 汇总。"""
    rows = db.query(StoreProductLossMetric).filter_by(store_alias=alias).all()
    by_type = {}
    total_loss = 0
    for r in rows:
        by_type[r.issue_type] = {
            "issue_count": r.issue_count,
            "estimated_loss_cny": r.estimated_loss_cny,
        }
        total_loss += r.estimated_loss_cny
    return {
        "store_alias": alias,
        "by_type": by_type,
        "total_estimated_loss_cny": total_loss,
    }


def admin_overview(db: Session) -> dict:
    """root 管理员: 所有店铺汇总。"""
    stores = db.query(StoreProfile).filter_by(enabled=True).all()
    per_store = []
    totals = {
        "revenue_cny": 0, "estimated_profit_cny": 0, "actual_profit_cny": 0,
        "purchase_cost_cny": 0, "logistics_cost_cny": 0, "platform_fees_cny": 0,
        "total_cost_cny": 0, "order_count": 0,
    }
    for s in stores:
        summary = store_daily_summary(db, s.store_alias, days=30)
        per_store.append({
            "store_alias": s.store_alias,
            "display_name": s.display_name or s.store_alias,
            "revenue_cny": summary["revenue_cny"],
            "estimated_profit_cny": summary["estimated_profit_cny"],
            "actual_profit_cny": summary["actual_profit_cny"],
            "profit_margin_pct": summary["profit_margin_pct"],
            "order_count": summary["order_count"],
            "overdue_count": summary["overdue"]["overdue_order_count"],
        })
        for k in ["revenue_cny", "estimated_profit_cny", "actual_profit_cny",
                  "purchase_cost_cny", "logistics_cost_cny", "platform_fees_cny",
                  "total_cost_cny", "order_count"]:
            totals[k] += summary.get(k, 0)
    return {
        "store_count": len(stores),
        "totals": totals,
        "per_store": per_store,
    }
