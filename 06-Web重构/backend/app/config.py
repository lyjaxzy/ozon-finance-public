# -*- coding: utf-8 -*-
"""应用配置。通过环境变量注入(适合云端 PaaS)。"""
import os
from pathlib import Path


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return int(default)


class Settings:
    # 数据库连接。云端用 Postgres, 本地开发用 SQLite(自动换)。
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "sqlite:///./ozon_dashboard.db",
    )
    # JWT
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production-0000000000")
    JWT_ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_HOURS = _int("ACCESS_TOKEN_EXPIRE_HOURS", 24)

    # 默认 root 账号(首次部署自动创建)
    ROOT_USERNAME = os.environ.get("ROOT_USERNAME", "admin")
    ROOT_PASSWORD = os.environ.get("ROOT_PASSWORD", "admin123")
    ROOT_DISPLAY_NAME = os.environ.get("ROOT_DISPLAY_NAME", "系统管理员")

    # 上传/同步
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "./uploads")


settings = Settings()
