# -*- coding: utf-8 -*-
"""看板路由: root 总览 / 单店铺看板(按角色隔离)。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..auth import get_current_user, visible_store_aliases, can_access_store
from ..services.dashboard import (
    store_daily_summary, store_daily_trend, store_loss_summary, admin_overview,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _ensure_visible(db, user, store_alias):
    if not can_access_store(user, db, store_alias):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该店铺")


@router.get("/overview")
def overview(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """root 管理员总览所有店铺; 其他角色只返回其可见店铺。"""
    if user.role == "root":
        return admin_overview(db)

    stores = visible_store_aliases(user, db)
    per_store = []
    for alias in stores:
        s = store_daily_summary(db, alias, days=30)
        per_store.append({
            "store_alias": alias,
            "display_name": alias,
            "revenue_cny": s["revenue_cny"],
            "estimated_profit_cny": s["estimated_profit_cny"],
            "actual_profit_cny": s["actual_profit_cny"],
            "profit_margin_pct": s["profit_margin_pct"],
            "order_count": s["order_count"],
            "overdue_count": s["overdue"]["overdue_order_count"],
        })
    return {"store_count": len(stores), "per_store": per_store}


@router.get("/store/{store_alias}")
def store_detail(
    store_alias: str,
    days: int = 30,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单店铺看板: 汇总 + 趋势 + 亏损。仅 root/授权用户可访问。"""
    _ensure_visible(db, user, store_alias)
    return {
        "summary": store_daily_summary(db, store_alias, days=days),
        "trend": store_daily_trend(db, store_alias, days=days),
        "loss": store_loss_summary(db, store_alias),
    }
