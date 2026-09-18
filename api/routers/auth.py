# -*- coding: utf-8 -*-
"""认证接口。

    POST /api/auth/login  → {access_token, user}
    GET  /api/auth/me     → {user, stores[]}

登录失败一律 401 且口令错误与用户不存在**不给不同提示** ——
区分两者等于提供了一个用户名枚举接口。
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import config
from ..deps import Runtime, get_current_user, get_runtime
from ..schemas import (LoginRequest, LoginResponse, MeResponse, StoreOut,
                       UserOut)
from ..tokens import create_access_token

router = APIRouter(prefix='/api/auth', tags=['auth'])


@router.post('/login', response_model=LoginResponse)
def login(payload: LoginRequest, runtime: Runtime = Depends(get_runtime)) -> LoginResponse:
    user = runtime.user_store.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail='用户名或口令错误')
    token = create_access_token(user.username, user.role)
    return LoginResponse(
        access_token=token,
        expires_in=config.JWT_TTL_SECONDS,
        user=UserOut(**user.to_public()),
    )


@router.get('/me', response_model=MeResponse)
def me(user=Depends(get_current_user),
       runtime: Runtime = Depends(get_runtime)) -> MeResponse:
    """当前用户 + 该用户**可见**的店铺。

    这里下发的 stores 只是给前端画入口用的；真正拦人的是
    /api/dashboard/store/{alias} 里的服务端校验。
    """
    aliases = runtime.visible_aliases(user)
    return MeResponse(
        user=UserOut(**user.to_public()),
        stores=[StoreOut(**s) for s in runtime.registry.public_list(aliases)],
    )
