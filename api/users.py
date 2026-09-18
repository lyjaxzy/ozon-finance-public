# -*- coding: utf-8 -*-
"""用户与授权模型（内存 + 本地 JSON）。

PRD §9.2 的三档角色：
    root            全部店铺
    store_operator  仅被授权店铺
    finance         仅被授权店铺

**当前是 mock 用户，不是真实用户表。** 这一点在 README 的「已知限制」里
单独列了一条 —— 任何按 `users.json` 做的权限结论都只在本机开发环境成立。

数据隔离**只在服务端**做：`users.json` 里的 store_aliases 是唯一授权来源，
请求参数里的别名先过白名单、再过授权，两者都通过才去取数。
"""
import json
import os
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Sequence, Tuple

from .security import DEFAULT_ITERATIONS, verify_password

# ── 角色 ─────────────────────────────────────────────────────────
ROLE_ROOT = 'root'
ROLE_STORE_OPERATOR = 'store_operator'
ROLE_FINANCE = 'finance'

#: 能看全部店铺的角色。只有 root。
GLOBAL_ROLES = frozenset({ROLE_ROOT})
ALL_ROLES = frozenset({ROLE_ROOT, ROLE_STORE_OPERATOR, ROLE_FINANCE})


@dataclass(frozen=True)
class User:
    """一个用户。`store_aliases` 为空且角色是 root 时表示全部店铺。"""

    username: str
    display_name: str
    role: str
    store_aliases: FrozenSet[str] = field(default_factory=frozenset)
    password_hash: str = ''
    allowed_actions: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def is_root(self) -> bool:
        return self.role == ROLE_ROOT

    def can_access_store(self, alias: str) -> bool:
        """授权判定。root 通吃；其余角色只认白名单。"""
        if self.is_root:
            return True
        return alias in self.store_aliases

    def can_delete_self(self) -> bool:
        """PRD §9.2 的硬约束：root 不能停用/删除自己。

        本步骤是只读接口，还没有删除动作；把这个约束放在模型上，
        等真的做用户管理时，只要走这个方法就不可能漏掉。
        """
        return not self.is_root

    def to_public(self) -> dict:
        """下发到前端的用户对象。**不含 password_hash**。"""
        return {
            'username': self.username,
            'display_name': self.display_name,
            'role': self.role,
            'store_aliases': sorted(self.store_aliases),
        }


class UserStore:
    """用户集合。默认从 JSON 文件加载，也可在测试里直接内存构造。"""

    def __init__(self, users: Sequence[User]):
        self._by_name = {u.username: u for u in users}

    # ── 构造 ──
    @classmethod
    def from_file(cls, path: str) -> 'UserStore':
        if not os.path.isfile(path):
            raise FileNotFoundError('找不到用户文件: %s' % path)
        with open(path, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        users = []
        for item in raw.get('users', []):
            role = item.get('role')
            if role not in ALL_ROLES:
                # 角色写错必须当场炸掉，不能让一个非法角色退化成「无权限」而静默通过
                raise ValueError('用户 %s 的角色非法: %r' % (item.get('username'), role))
            users.append(User(
                username=item['username'],
                display_name=item.get('display_name') or item['username'],
                role=role,
                store_aliases=frozenset(item.get('store_aliases') or ()),
                password_hash=item.get('password_hash') or '',
                allowed_actions=frozenset(item.get('allowed_actions') or ()),
            ))
        return cls(users)

    @classmethod
    def from_plain_passwords(cls, specs: Sequence[dict],
                             iterations: int = DEFAULT_ITERATIONS) -> 'UserStore':
        """给**测试**用：直接传明文口令，内部现算哈希。

        真实用户口令只以哈希形式存在于 `data/users.json`，
        没有任何生产路径会调用这个方法 —— 它存在的意义是让测试
        不必把哈希硬编码进测试文件。

        `iterations` 可调：测试里 20 万次 PBKDF2 会让整套测试慢到几十秒，
        降到 1000 次只影响测试速度，不影响生产强度。
        """
        from .security import hash_password
        users = []
        for s in specs:
            users.append(User(
                username=s['username'],
                display_name=s.get('display_name') or s['username'],
                role=s.get('role') or ROLE_STORE_OPERATOR,
                store_aliases=frozenset(s.get('store_aliases') or ()),
                password_hash=hash_password(s['password'], s['username'] + '-salt',
                                            iterations),
            ))
        return cls(users)

    # ── 查询 ──
    def get(self, username: str) -> Optional[User]:
        return self._by_name.get(username)

    def authenticate(self, username: str, password: str) -> Optional[User]:
        """校验用户名口令。失败一律返回 None，不区分「用户不存在」和「口令错误」。"""
        user = self._by_name.get(username)
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def all(self) -> Tuple[User, ...]:
        return tuple(self._by_name.values())
