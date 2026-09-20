# -*- coding: utf-8 -*-
"""生成「对外提供服务」所需的安全配置（ADR-0010 / api/preflight.py 的配套）。

做三件事，**都写到仓库之外**（`<DATA_ROOT>Platform\\config\\`）：

1. 生成生产用户表 `users.json`：保留同样的三个角色，但口令换成
   **20 位随机串**（每个账号不同），只在标准输出打印一次；
2. 生成 `platform.env`：强 `OZON_JWT_SECRET`（64 位十六进制）+ 对外模式开关 +
   用户表路径 + 成本库路径 + 店铺白名单；
3. 打印「怎么启动」与「三种公网入口方案」的可复制命令。

用法：

    python ops/public/prepare_public.py                # 生成（已存在则拒绝覆盖）
    python ops/public/prepare_public.py --force        # 覆盖重生成（会换掉所有口令）
    python ops/public/prepare_public.py --check        # 只做自检，不写文件

**为什么不把口令写进仓库**：仓库会进版本历史、也会同步到公开仓库。
口令与密钥只存在于本机数据根，`api/preflight.py` 会强制这一点。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from api import config                                    # noqa: E402
from api.security import hash_password                    # noqa: E402

CONFIG_DIR = r'<DATA_ROOT>Platform\config'
USERS_FILE = os.path.join(CONFIG_DIR, 'users.json')
ENV_FILE = os.path.join(CONFIG_DIR, 'platform.env')

#: 账号模板：角色与可见店铺照旧，只有口令是新的
ACCOUNTS = [
    {'username': 'admin', 'display_name': '系统管理员', 'role': 'root',
     'store_aliases': []},
    {'username': 'finance01', 'display_name': '财务 01', 'role': 'finance',
     'store_aliases': ['store_alpha', 'store_beta']},
    {'username': 'operator01', 'display_name': '运营 01', 'role': 'store_operator',
     'store_aliases': ['store_alpha']},
]

PASSWORD_ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def gen_password(length: int = 20) -> str:
    """可人工抄写的强口令（去掉了容易看错的 0/O/1/l/I）。"""
    return ''.join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def build_users() -> tuple[dict, dict]:
    plain = {}
    users = []
    for spec in ACCOUNTS:
        password = gen_password()
        plain[spec['username']] = password
        users.append({
            'username': spec['username'],
            'display_name': spec['display_name'],
            'role': spec['role'],
            'store_aliases': spec['store_aliases'],
            'password_hash': hash_password(password, spec['username'] + '-salt'),
        })
    return {'users': users}, plain


def build_env(secret: str) -> str:
    return '\n'.join([
        '# 对外提供服务时的环境变量（由 ops/public/prepare_public.py 生成）',
        '#',
        '# 用法（PowerShell）：',
        '#   Get-Content <DATA_ROOT>Platform\\config\\platform.env |',
        '#     Where-Object { $_ -and -not $_.StartsWith("#") } | ForEach-Object {',
        '#       $k, $v = $_ -split "=", 2; Set-Item -Path "Env:$k" -Value $v }',
        '#   python -m uvicorn api.app:app --host 127.0.0.1 --port 8849',
        '',
        'OZON_PUBLIC_MODE=1',
        'OZON_JWT_SECRET=%s' % secret,
        'OZON_USERS_PATH=%s' % USERS_FILE,
        'OZON_COST_BOOK=%s' % os.path.join(os.path.dirname(CONFIG_DIR), 'cost_book.db'),
        'OZON_PUBLIC_BASE_URL=https://<你的公网域名>',
        '',
    ])


def cmd_check() -> int:
    from api.preflight import check_public_ready
    problems = check_public_ready(config)
    if not problems:
        print('✓ 当前环境满足对外暴露的前提（OZON_PUBLIC_MODE 下可启动）')
        return 0
    print('✗ 还不满足，逐条：')
    for i, p in enumerate(problems, 1):
        print('  %d) %s' % (i, p))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description='生成对外提供服务所需的安全配置')
    ap.add_argument('--force', action='store_true', help='覆盖已存在的文件（会换掉所有口令）')
    ap.add_argument('--check', action='store_true', help='只自检，不写文件')
    args = ap.parse_args()

    if args.check:
        return cmd_check()

    os.makedirs(CONFIG_DIR, exist_ok=True)
    existing = [p for p in (USERS_FILE, ENV_FILE) if os.path.isfile(p)]
    if existing and not args.force:
        print('这些文件已存在，拒绝覆盖（用 --force 重生成，会换掉所有口令）：')
        for p in existing:
            print('   ', p)
        return 1

    users, plain = build_users()
    secret = secrets.token_hex(32)              # 64 位十六进制
    with open(USERS_FILE, 'w', encoding='utf-8') as fh:
        json.dump(users, fh, ensure_ascii=False, indent=2)
    with open(ENV_FILE, 'w', encoding='utf-8') as fh:
        fh.write(build_env(secret))

    print('=' * 72)
    print('已生成（都在仓库之外，不会进版本历史）：')
    print('   用户表：', USERS_FILE)
    print('   环境变量：', ENV_FILE)
    print()
    print('★ 口令只显示这一次，请立刻抄走（系统里只存哈希，丢了只能重生成）：')
    for name, password in plain.items():
        print('   %-12s %s' % (name, password))
    print()
    print('★ 启动前先加载环境变量，再起服务：')
    print('   Get-Content "%s" | Where-Object { $_ -and -not $_.StartsWith("#") } |' % ENV_FILE)
    print('     ForEach-Object { $k, $v = $_ -split "=", 2; Set-Item -Path "Env:$k" -Value $v }')
    print('   python -m uvicorn api.app:app --host 127.0.0.1 --port 8849')
    print('   → 页面与 /api 由同一个进程同源提供，对外只需暴露 8849 这一个端口')
    print('=' * 72)
    return 0


if __name__ == '__main__':
    sys.exit(main())
