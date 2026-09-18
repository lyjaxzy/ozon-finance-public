# -*- coding: utf-8 -*-
"""OZON 云端看板 — FastAPI 应用入口。"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .routers import auth, dashboard, admin

app = FastAPI(title="OZON 云端看板", version="0.1.0")

# CORS: 开发时允许所有; 生产可收紧到前端域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(admin.router)


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "ozon-cloud-dashboard"}
