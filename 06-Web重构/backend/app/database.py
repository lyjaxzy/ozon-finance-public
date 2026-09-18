# -*- coding: utf-8 -*-
"""数据库连接与会话管理。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
import os

from .config import settings
from .models import Base

# SQLite 需要 check_same_thread=False 供多线程用; Postgres 无此参数。
_connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    """建表 + 生成默认 root 账号。"""
    Base.metadata.create_all(bind=engine)

    from datetime import datetime
    from werkzeug.security import generate_password_hash
    from .models import User

    db = SessionLocal()
    try:
        root = db.query(User).filter_by(username=settings.ROOT_USERNAME).first()
        if not root:
            root = User(
                username=settings.ROOT_USERNAME,
                password_hash=generate_password_hash(settings.ROOT_PASSWORD),
                display_name=settings.ROOT_DISPLAY_NAME,
                role="root",
                is_active=True,
            )
            db.add(root)
            db.commit()
    finally:
        db.close()


def get_db():
    """FastAPI 依赖: 每个请求一个 session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
