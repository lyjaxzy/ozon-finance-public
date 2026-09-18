# -*- coding: utf-8 -*-
"""认证与授权: JWT + 角色 + 店铺数据隔离。

角色:
  - root:            所有店铺全部数据
  - store_operator:  只能访问授权店铺的数据
  - finance:         只能访问授权店铺的财务数据

店铺隔离规则:
  - root 默认可见所有启用店铺
  - store_operator / finance 必须存在于 user_store_grants
"""
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash

from .config import settings
from .database import get_db
from .models import User, UserStoreGrant, StoreProfile

bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# 密码 & JWT
# ---------------------------------------------------------------------------

def verify_password(plain, hashed):
    return check_password_hash(hashed, plain)


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.ACCESS_TOKEN_EXPIRE_HOURS)).timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效或过期的令牌")


# ---------------------------------------------------------------------------
# 当前用户依赖
# ---------------------------------------------------------------------------

def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    payload = decode_access_token(creds.credentials)
    user = db.query(User).filter_by(id=int(payload["sub"]), is_active=True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不存在或已停用")
    return user


def require_role(*roles: str):
    """依赖工厂: 限制当前用户必须属于某个角色。"""
    def checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限执行此操作")
        return user
    return checker


# ---------------------------------------------------------------------------
# 店铺可见性(数据隔离核心)
# ---------------------------------------------------------------------------

def visible_store_aliases(user: User, db: Session) -> list[str]:
    """返回当前用户可见的所有店铺别名。

    - root: 所有启用店铺
    - else: 仅授权的店铺(且启用)
    """
    if user.role == "root":
        rows = db.query(StoreProfile).filter_by(enabled=True).all()
        return [r.store_alias for r in rows]

    grants = db.query(UserStoreGrant).filter_by(user_id=user.id).all()
    granted = {g.store_alias for g in grants}
    if not granted:
        return []
    rows = db.query(StoreProfile).filter_by(enabled=True).filter(
        StoreProfile.store_alias.in_(granted)
    ).all()
    return [r.store_alias for r in rows]


def can_access_store(user: User, db: Session, store_alias: str) -> bool:
    if user.role == "root":
        return True
    grant = db.query(UserStoreGrant).filter_by(user_id=user.id, store_alias=store_alias).first()
    return grant is not None
