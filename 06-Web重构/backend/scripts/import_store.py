# -*- coding: utf-8 -*-
"""
把现有 OZON 店铺库(store_alpha.db)的原始表, 聚合成看板指标写入云端 Postgres。

用法(在 backend 目录下):
    python -m scripts.import_store --db "<DATA_ROOT>/data/stores/store_alpha.db" --alias store_alpha

说明:
    - store_daily_metrics   : 从 postings / posting_profit_facts 按日期聚合
    - store_overdue_metrics : 从 overdue_fast_current 取当前快照
    - store_product_loss    : 从 posting_profit_facts 统计亏损订单
只做聚合写入, 不改动原始库。
"""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import init_db, session_scope
from app.models import (
    StoreProfile,
    StoreDailyMetric,
    StoreOverdueMetric,
    StoreProductLossMetric,
)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def aggregate_daily(conn, alias):
    """从 postings + posting_profit_facts 按订单创建日期/结算日期聚合成每日指标。"""
    cur = conn.cursor()
    # 1) postings 提供 revenue_cny / sale_value_rub / platform_fees / api_logistics_cost / created_at
    postings = {}
    try:
        rows = cur.execute("""
            SELECT posting_number, created_at, revenue_cny, sale_value_rub,
                   platform_fees_cny, api_logistics_cost_cny
            FROM postings
        """).fetchall()
        for pn, created_at, rev, sale, fees, logi in rows:
            day = (created_at or "")[:10]
            if not day:
                continue
            postings[pn] = {
                "day": day,
                "revenue_cny": _f(rev),
                "sale_value_rub": _f(sale),
                "platform_fees_cny": _f(fees),
                "logistics_cost_cny": _f(logi),
                "purchase_cost_cny": 0.0,
                "estimated_profit_cny": 0.0,
                "actual_profit_cny": 0.0,
            }
    except Exception as e:
        print("  postings 读取失败(可忽略):", e)

    # 2) posting_profit_facts 提供采购成本/预估/实际利润 + 订单所属月份
    try:
        rows = cur.execute("""
            SELECT posting_number, order_month, purchase_cost_cny,
                   estimated_profit_cny, actual_profit_cny
            FROM posting_profit_facts
        """).fetchall()
        for pn, om, pc, ep, ap in rows:
            if pn not in postings:
                continue
            postings[pn]["purchase_cost_cny"] = _f(pc)
            postings[pn]["estimated_profit_cny"] = _f(ep)
            postings[pn]["actual_profit_cny"] = _f(ap)
    except Exception as e:
        print("  posting_profit_facts 读取失败(可忽略):", e)

    # 3) 按日期聚合
    daily = {}
    for pn, d in postings.items():
        day = d["day"]
        if day not in daily:
            daily[day] = {
                "revenue_cny": 0, "sale_value_rub": 0, "platform_fees_cny": 0,
                "logistics_cost_cny": 0, "purchase_cost_cny": 0,
                "estimated_profit_cny": 0, "actual_profit_cny": 0, "order_count": 0,
            }
        d_ = daily[day]
        for k in ["revenue_cny", "sale_value_rub", "platform_fees_cny",
                  "logistics_cost_cny", "purchase_cost_cny",
                  "estimated_profit_cny", "actual_profit_cny"]:
            d_[k] += d[k]
        d_["order_count"] += 1

    from datetime import date as _date
    return [{"store_alias": alias, "metric_date": _date.fromisoformat(day), **v} for day, v in sorted(daily.items())]


def read_overdue_current(conn, alias):
    """从 overdue_fast_current 读取当前逾期快照。"""
    from datetime import datetime as _dt
    cur = conn.cursor()
    try:
        rows = cur.execute("""
            SELECT scan_id, as_of, row_count, active_order_count
            FROM overdue_fast_current
            ORDER BY activated_at DESC LIMIT 1
        """).fetchall()
        if not rows:
            return None
        scan_id, as_of, row_count, active = rows[0]
        as_of_dt = None
        if as_of:
            try:
                # 兼容带时区后缀, 去掉 Z 并用 pydantic/sqlite 需要的 naive datetime
                s = str(as_of).replace('Z', '+00:00')
                as_of_dt = _dt.fromisoformat(s).replace(tzinfo=None)
            except Exception:
                as_of_dt = None
        return {
            "store_alias": alias,
            "scan_id": scan_id or "",
            "as_of": as_of_dt,
            "overdue_order_count": _i(row_count),
            "active_order_count": _i(active),
            "overdue_hours_sum": 0.0,
        }
    except Exception as e:
        print("  overdue_fast_current 读取失败:", e)
        return None


def read_loss(conn, alias):
    """从 posting_profit_facts 统计亏损订单。"""
    cur = conn.cursor()
    try:
        rows = cur.execute("""
            SELECT order_month, COUNT(*) AS cnt, SUM(actual_profit_cny) AS loss
            FROM posting_profit_facts
            WHERE actual_is_loss = 1
            GROUP BY order_month
        """).fetchall()
        return [{"store_alias": alias, "issue_type": "actual_loss",
                 "issue_count": _i(cnt), "estimated_loss_cny": _f(loss)}
                for om, cnt, loss in rows]
    except Exception as e:
        print("  posting_profit_facts 亏损统计失败:", e)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="店铺 SQLite 数据库路径")
    ap.add_argument("--alias", required=True, help="店铺别名")
    args = ap.parse_args()

    init_db()
    print(f"[import] 店铺库: {args.db}  alias: {args.alias}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    with session_scope() as db:
        # 注册店铺
        store = db.query(StoreProfile).filter_by(store_alias=args.alias).first()
        if not store:
            store = StoreProfile(store_alias=args.alias, display_name=args.alias,
                                 enabled=True, source_db_path=args.db)
            db.add(store)
            db.flush()
        else:
            store.display_name = store.display_name or args.alias
            store.source_db_path = args.db

        # 每日指标
        daily = aggregate_daily(conn, args.alias)
        for d in daily:
            row = db.query(StoreDailyMetric).filter_by(
                store_alias=args.alias, metric_date=d["metric_date"]).first()
            if row:
                for k, v in d.items():
                    if k not in ("store_alias", "metric_date"):
                        setattr(row, k, v)
            else:
                db.add(StoreDailyMetric(**d))
        print(f"  每日指标: {len(daily)} 天")

        # 逾期
        od = read_overdue_current(conn, args.alias)
        if od:
            existing = db.query(StoreOverdueMetric).filter_by(
                store_alias=args.alias).order_by(StoreOverdueMetric.id.desc()).first()
            if existing:
                for k, v in od.items():
                    setattr(existing, k, v)
            else:
                db.add(StoreOverdueMetric(**od))
            print("  逾期快照: 已写入")

        # 亏损
        loss = read_loss(conn, args.alias)
        for l in loss:
            row = db.query(StoreProductLossMetric).filter_by(
                store_alias=args.alias, issue_type=l["issue_type"]).first()
            if row:
                for k, v in l.items():
                    if k not in ("store_alias", "issue_type"):
                        setattr(row, k, v)
            else:
                db.add(StoreProductLossMetric(**l))
        print(f"  亏损汇总: {len(loss)} 期")

    conn.close()
    print("[import] 完成")


if __name__ == "__main__":
    main()
