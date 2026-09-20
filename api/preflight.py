# -*- coding: utf-8 -*-
"""对外暴露前的 fail-closed 自检（`OZON_PUBLIC_MODE=1`）。

## 为什么需要这个文件

本系统的默认配置是**为「只在本机自己用」设计的**：

* 三个账号的口令等于用户名（`admin`/`admin`、`finance01`/`finance01`、`operator01`/`operator01`）；
* JWT 密钥有仓库里的默认值 `dev-only-insecure-secret-change-me`；
* 用户表就是仓库里的 `api/data/users.json`（口令哈希随仓库一起走）。

这三条在本机无所谓，**一旦挂到公网就等于把真实经营数据公开**。
所以对外模式不是「设置一个环境变量就完事」，而是**先过这道闸**：
不满足条件就**拒绝启动**，而不是打印警告后照常服务 ——
警告会被忽略，拒绝启动不会。

## 检查项

| # | 检查 | 为什么 |
|---|---|---|
| 1 | JWT 密钥不是默认值且 ≥ 32 字符 | 默认值公开在仓库里，任何人都能自己签一个合法令牌 |
| 2 | 用户表**不在仓库目录内** | 仓库里的 `users.json` 口令=用户名；生产用户表必须另存 |
| 3 | 每个账号的口令都**不是**用户名/常见弱口令 | 靠哈希逐个试常见弱口令，能抓到「改成 123456」这种偷懒 |
| 4 | 前端有生产构建产物（`web/dist`） | 否则对外只能挂 Vite **开发服务器** —— 它会暴露源码，且有任意文件读取的历史问题 |
| 5 | 数据源（店铺库 / 导出目录）确实存在 | 对外服务却在首页报 503 很难看，也让排查变慢 |

密钥与口令**从不打印**：只报「哪一项不合格」和「怎么修」。
"""
import os
from typing import List, Optional

from . import security

#: 已知弱口令：只要某个账号能被这些口令登进去，就不允许对外
WEAK_PASSWORDS = (
    'admin', 'password', 'passw0rd', '123456', '1234567', '12345678', '123456789',
    'qwerty', 'abc123', 'admin123', 'root', 'test', 'finance', 'operator',
    'ozon', 'ozon123', 'changeme', 'letmein', '111111', '000000',
)

MIN_SECRET_LENGTH = 32

#: 仓库里自带的默认密钥（`api/config.py`）。这里刻意再写一份字面量：
#: 万一有人改了 config 的默认值，这道闸不该跟着一起失去作用。
INSECURE_DEFAULT_SECRETS = (
    'dev-only-insecure-secret-change-me',
    '',
    'secret',
    'changeme',
)


def repo_root() -> str:
    """本仓库根目录（`api/` 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == \
            os.path.abspath(root)
    except ValueError:
        # 不同盘符：commonpath 会抛 ValueError，那就是「不在里面」
        return False


def check_public_ready(config, users_path: Optional[str] = None) -> List[str]:
    """返回**问题清单**（空列表 = 可以对外）。不抛异常，方便自检接口复用。"""
    problems: List[str] = []

    # 1. JWT 密钥
    secret = config.JWT_SECRET or ''
    if secret in INSECURE_DEFAULT_SECRETS:
        problems.append(
            'JWT 密钥还是默认值 —— 默认值公开在仓库里，任何人都能自己签一个合法令牌。'
            '设置 OZON_JWT_SECRET（≥ %d 位随机串）。' % MIN_SECRET_LENGTH)
    elif len(secret) < MIN_SECRET_LENGTH:
        problems.append('JWT 密钥太短（当前 %d 位，要求 ≥ %d 位）。'
                        % (len(secret), MIN_SECRET_LENGTH))

    # 2. 用户表位置
    path = users_path or config.USERS_PATH
    if _inside(path, repo_root()):
        problems.append(
            '用户表在仓库目录内（%s）—— 仓库里的 users.json 是开发用账号，口令等于用户名。'
            '把生产用户表放到仓库外并用 OZON_USERS_PATH 指定。' % path)
    if not os.path.isfile(path):
        problems.append('用户表不存在：%s' % path)
        return problems                       # 读不到用户就没法继续查口令

    # 3. 逐个账号试常见弱口令（靠哈希验证，不需要明文）
    try:
        from .users import UserStore
        store = UserStore.from_file(path)
    except Exception as exc:                  # noqa: BLE001 - 自检工具要把原因说清楚
        problems.append('用户表读不出来：%s' % exc)
        return problems

    for user in store.all():
        name = user.username
        stored = getattr(user, 'password_hash', None)
        if not stored:
            problems.append('账号 %s 没有口令哈希' % name)
            continue
        candidates = [name] + list(WEAK_PASSWORDS)
        for candidate in candidates:
            if security.verify_password(candidate, stored):
                problems.append(
                    '账号 %s 的口令是弱口令（能被「%s」登进去）' % (name, candidate))
                break

    # 4. 前端生产构建
    if not os.path.isfile(config.FRONTEND_INDEX):
        problems.append(
            '没有前端生产构建产物（%s）—— 对外不能挂 Vite 开发服务器：'
            '它会暴露源码，并有任意文件读取的历史问题。先在 web 下跑 pnpm build:pro。'
            % config.FRONTEND_INDEX)

    # 5. 数据源
    if config.DATA_SOURCE == 'sqlite':
        missing = config_store_missing()
        if missing:
            problems.append('这些店铺的库文件不存在：%s'
                            % '、'.join('%s(%s)' % (s.alias, s.db_path) for s in missing))
    return problems


def config_store_missing():
    """注册表里「库文件不存在」的店铺。放在这里是为了让自检能提前喊出来。"""
    from .deps import StoreRegistry
    registry = StoreRegistry.default()
    return [registry.get(alias) for alias in registry.aliases()
            if not os.path.isfile(registry.get(alias).db_path)]


def enforce_public_ready(config, users_path: Optional[str] = None) -> None:
    """不满足就**拒绝启动**。错误信息里逐条写「哪一项不合格 + 怎么修」。"""
    if not getattr(config, 'PUBLIC_MODE', False):
        return
    problems = check_public_ready(config, users_path=users_path)
    if problems:
        raise RuntimeError(
            'OZON_PUBLIC_MODE=1 但对外暴露的前提没满足（拒绝启动，逐条修完再启）：\n'
            + '\n'.join('  %d) %s' % (i + 1, p) for i, p in enumerate(problems))
            + '\n\n提示：跑 `python ops/public/prepare_public.py` 可以一次性生成'
              '强口令用户表与 JWT 密钥（放在仓库外）。')
