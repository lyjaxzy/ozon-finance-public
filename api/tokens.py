# -*- coding: utf-8 -*-
"""令牌签发与校验。

用 `PyJWT`（环境里已有，2.10.1），不自己造签名算法 ——
手写 token 最容易犯的错是「先比较内容再验签」，一旦写反就是任意伪造。

令牌里只放**身份**（用户名 / 角色），不放授权明细：
授权每次都从 UserStore 现取，这样改授权立即生效，不必等令牌过期。
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from . import config


class TokenError(Exception):
    """令牌缺失/过期/被篡改。调用方一律转成 401。"""


def create_access_token(username: str, role: str,
                        ttl_seconds: Optional[int] = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        'sub': username,
        'role': role,
        'iat': int(now.timestamp()),
        'exp': int((now + timedelta(
            seconds=config.JWT_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
        ).timestamp()),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析并验签。任何异常统一包成 TokenError，不把 PyJWT 的异常类型漏到接口层。"""
    if not token:
        raise TokenError('缺少令牌')
    try:
        payload = jwt.decode(token, config.JWT_SECRET,
                             algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise TokenError('令牌已过期')
    except jwt.InvalidTokenError as exc:
        raise TokenError('令牌无效: %s' % exc)
    if not payload.get('sub'):
        raise TokenError('令牌缺少 sub')
    return payload
