# -*- coding: utf-8 -*-
"""口令校验 —— 标准库实现，刻意不引入 passlib/bcrypt。

`config/data/users.json` 里存的是 `pbkdf2_sha256$迭代次数$盐$摘要`，
**不存明文口令**。这样即使配置文件被误提交，也不会直接泄露口令。

诚实声明：这套东西是「够用的占位」，不是完整方案 ——
没有口令强度校验、没有失败锁定、没有轮换。等真正做用户表时，
这些都应该换成正式方案（PRD §9.2 尚未细化到这一层）。
"""
import hashlib
import hmac

ALGORITHM = 'pbkdf2_sha256'
DEFAULT_ITERATIONS = 200000


def hash_password(password: str, salt: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """按 `pbkdf2_sha256$迭代次数$盐$摘要` 生成口令串。"""
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 salt.encode('utf-8'), iterations)
    return '%s$%d$%s$%s' % (ALGORITHM, iterations, salt, digest.hex())


def verify_password(password: str, stored: str) -> bool:
    """常量时间比对。任何格式异常一律判为不通过，不抛异常。

    刻意返回 False 而不是 raise：登录接口不该因为配置写错而返回 500，
    那会把「口令错误」和「服务故障」混在一起，也让探测者能区分两者。
    """
    if not password or not stored:
        return False
    parts = str(stored).split('$')
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
    except ValueError:
        return False
    candidate = hash_password(password, parts[2], iterations)
    return hmac.compare_digest(candidate, stored)
