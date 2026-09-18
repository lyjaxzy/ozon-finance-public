# -*- coding: utf-8 -*-
"""管理接口(仅 root): 用户管理 / 角色分配 / 店铺授权 / 店铺管理。"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from ..database import get_db
from ..models import User, UserStoreGrant, StoreProfile, StoreDailyMetric
from ..auth import require_role, get_current_user

router = APIRouter(prefix="/api/admin", tags=["admin"])

VALID_ROLES = {"root", "store_operator", "finance"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6)
    display_name: str = ""
    role: str = "store_operator"
    stores: list[str] = []


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None
    stores: list[str] | None = None


class StoreUpsert(BaseModel):
    store_alias: str = Field(..., min_length=2, max_length=32)
    display_name: str = ""
    enabled: bool = True
    source_db_path: str = ""


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------

def _user_with_stores(db: Session, user: User) -> dict:
    grants = db.query(UserStoreGrant).filter_by(user_id=user.id).all()
    return user.to_dict(stores=[g.store_alias for g in grants])


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_role("root"))):
    users = db.query(User).order_by(User.id).all()
    return [_user_with_stores(db, u) for u in users]


@router.post("/users")
def create_user(payload: UserCreate, db: Session = Depends(get_db),
                _: User = Depends(require_role("root"))):
    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色: {payload.role}")
    if db.query(User).filter_by(username=payload.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(
        username=payload.username,
        password_hash=generate_password_hash(payload.password),
        display_name=payload.display_name or payload.username,
        role=payload.role,
        is_active=True,
    )
    db.add(user)
    db.flush()

    for alias in payload.stores:
        db.add(UserStoreGrant(user_id=user.id, store_alias=alias))
    db.commit()
    return _user_with_stores(db, user)


@router.put("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db),
                current: User = Depends(require_role("root"))):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if payload.role is not None:
        if payload.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"无效角色: {payload.role}")
        # 不允许把最后一个 root 降级
        if user.role == "root" and payload.role != "root":
            root_count = db.query(User).filter_by(role="root", is_active=True).count()
            if root_count <= 1:
                raise HTTPException(status_code=400, detail="不能降级最后一个 root 管理员")
        user.role = payload.role
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.is_active is not None:
        if user.id == current.id and not payload.is_active:
            raise HTTPException(status_code=400, detail="不能停用自己的账号")
        user.is_active = payload.is_active
    if payload.password:
        user.password_hash = generate_password_hash(payload.password)

    if payload.stores is not None:
        db.query(UserStoreGrant).filter_by(user_id=user.id).delete()
        for alias in payload.stores:
            db.add(UserStoreGrant(user_id=user.id, store_alias=alias))

    db.commit()
    return _user_with_stores(db, user)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db),
                current: User = Depends(require_role("root"))):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == current.id:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    if user.role == "root":
        root_count = db.query(User).filter_by(role="root", is_active=True).count()
        if root_count <= 1:
            raise HTTPException(status_code=400, detail="不能删除最后一个 root 管理员")
    db.query(UserStoreGrant).filter_by(user_id=user.id).delete()
    db.delete(user)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 店铺管理
# ---------------------------------------------------------------------------

@router.get("/stores")
def list_stores(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    stores = db.query(StoreProfile).order_by(StoreProfile.id).all()
    result = []
    for s in stores:
        metric_count = db.query(StoreDailyMetric).filter_by(store_alias=s.store_alias).count()
        d = s.to_dict()
        d["metric_days"] = metric_count
        result.append(d)
    return result


@router.post("/stores")
def upsert_store(payload: StoreUpsert, db: Session = Depends(get_db),
                 _: User = Depends(require_role("root"))):
    store = db.query(StoreProfile).filter_by(store_alias=payload.store_alias).first()
    if store:
        store.display_name = payload.display_name or store.display_name
        store.enabled = payload.enabled
        store.source_db_path = payload.source_db_path or store.source_db_path
    else:
        store = StoreProfile(
            store_alias=payload.store_alias,
            display_name=payload.display_name or payload.store_alias,
            enabled=payload.enabled,
            source_db_path=payload.source_db_path,
        )
        db.add(store)
    db.commit()
    return store.to_dict()


@router.get("/roles")
def list_roles(_: User = Depends(require_role("root"))):
    return [
        {"value": "root", "label": "root 管理员", "desc": "可见全部店铺全部指标"},
        {"value": "store_operator", "label": "店铺运营", "desc": "仅可见被授权的店铺"},
        {"value": "finance", "label": "财务", "desc": "仅可见被授权店铺的财务数据"},
    ]
