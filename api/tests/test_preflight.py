# -*- coding: utf-8 -*-
"""对外暴露自检的测试（`api/preflight.py`）。

这道闸的存在意义是**拒绝启动**：默认配置（口令=用户名、仓库里的 JWT 默认值、
用户表在仓库内）在本机无所谓，挂到公网就是把真实经营数据公开。
所以每条检查都要有测试钉住 —— 一个「看起来会拦、其实拦不住」的安全闸
比没有更危险。
"""
import io
import json
import os
import tempfile
import unittest

from api import preflight
from api.security import hash_password
from api.users import UserStore


class PreflightTestBase(unittest.TestCase):
    """造一个「完全合规」的基线，再逐条破坏它 —— 这样能证明每条检查真的在起作用。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ozon-preflight-')
        self.addCleanup(self._rmtree, self.tmp)
        self.users_path = os.path.join(self.tmp, 'users.json')
        self.dist = os.path.join(self.tmp, 'dist')
        os.makedirs(self.dist)
        with io.open(os.path.join(self.dist, 'index.html'), 'w', encoding='utf-8') as fh:
            fh.write('<div id="app"></div>')

    @staticmethod
    def _rmtree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def write_users(self, specs):
        users = []
        for username, password in specs:
            users.append({
                'username': username,
                'display_name': username,
                'role': 'root',
                'store_aliases': [],
                # 迭代数降到 1000：自检会对每个账号逐个试 ~21 个弱口令，
                # 20 万次迭代下这一组测试要跑 45 秒。哈希串自带迭代数，
                # 校验逻辑完全一致，只是测试快 40 倍。
                'password_hash': hash_password(password, username + '-salt', 1000),
            })
        with io.open(self.users_path, 'w', encoding='utf-8') as fh:
            json.dump({'users': users}, fh, ensure_ascii=False)
        return self.users_path

    def config(self, **over):
        """一个合规的 config 替身：强密钥 + 仓库外的用户表 + 有前端产物。"""
        class Cfg:
            JWT_SECRET = 'x' * 64
            USERS_PATH = self.users_path
            FRONTEND_INDEX = os.path.join(self.dist, 'index.html')
            DATA_SOURCE = 'excel'          # 跳过「店铺库存不存在」这条，与本组测试无关
            PUBLIC_MODE = True
        for key, value in over.items():
            setattr(Cfg, key, value)
        return Cfg


class PublicReadyCheckTest(PreflightTestBase):
    def test_compliant_config_has_no_problems(self):
        """基线：强密钥 + 仓库外用户表 + 强口令 + 有前端产物 → 没有问题。"""
        self.write_users([('alice', 'Zq7-strong-passphrase-2026')])
        self.assertEqual([], preflight.check_public_ready(self.config()))

    def test_default_jwt_secret_is_rejected(self):
        """仓库里的默认密钥必须被拦 —— 它公开在版本历史里，谁都能自己签令牌。"""
        self.write_users([('alice', 'Zq7-strong-passphrase-2026')])
        problems = preflight.check_public_ready(
            self.config(JWT_SECRET='dev-only-insecure-secret-change-me'))
        self.assertTrue(any('JWT 密钥还是默认值' in p for p in problems), problems)

    def test_short_jwt_secret_is_rejected(self):
        self.write_users([('alice', 'Zq7-strong-passphrase-2026')])
        problems = preflight.check_public_ready(self.config(JWT_SECRET='short-but-not-default'))
        self.assertTrue(any('太短' in p for p in problems), problems)

    def test_users_file_inside_repo_is_rejected(self):
        """仓库里的 users.json 口令=用户名，对外必须另存。"""
        inside = os.path.join(preflight.repo_root(), 'api', 'data', 'users.json')
        self.assertTrue(os.path.isfile(inside), '仓库里应该带着开发用户表')
        problems = preflight.check_public_ready(self.config(USERS_PATH=inside))
        self.assertTrue(any('用户表在仓库目录内' in p for p in problems), problems)

    def test_password_equal_to_username_is_rejected(self):
        """当前默认状态就是这样：admin/admin。这条测试就是那个场景的守卫。"""
        self.write_users([('admin', 'admin')])
        problems = preflight.check_public_ready(self.config())
        self.assertTrue(any('admin' in p and '弱口令' in p for p in problems), problems)

    def test_common_weak_password_is_rejected(self):
        self.write_users([('alice', '123456')])
        problems = preflight.check_public_ready(self.config())
        self.assertTrue(any('弱口令' in p for p in problems), problems)

    def test_missing_frontend_build_is_rejected(self):
        """没有构建产物就只能挂 Vite 开发服务器，那会暴露源码。"""
        self.write_users([('alice', 'Zq7-strong-passphrase-2026')])
        problems = preflight.check_public_ready(
            self.config(FRONTEND_INDEX=os.path.join(self.tmp, 'nope', 'index.html')))
        self.assertTrue(any('前端生产构建产物' in p for p in problems), problems)

    def test_missing_users_file_is_reported_once_and_stops(self):
        """用户表不存在：如实报告，并且不再继续查口令（否则会抛栈）。"""
        problems = preflight.check_public_ready(
            self.config(USERS_PATH=os.path.join(self.tmp, 'nope.json')))
        self.assertEqual(1, len(problems), problems)
        self.assertIn('用户表不存在', problems[0])


class EnforceTest(PreflightTestBase):
    def test_public_mode_off_never_blocks(self):
        """本机模式不该被拦：默认配置就是给本机用的。"""
        class Cfg:
            JWT_SECRET = 'dev-only-insecure-secret-change-me'
            USERS_PATH = self.users_path
            FRONTEND_INDEX = 'nope'
            DATA_SOURCE = 'excel'
            PUBLIC_MODE = False
        preflight.enforce_public_ready(Cfg)          # 不抛异常即通过

    def test_public_mode_on_raises_with_actionable_list(self):
        """对外模式不满足 → 抛异常，且错误信息里逐条写清怎么修。"""
        self.write_users([('admin', 'admin')])
        with self.assertRaises(RuntimeError) as ctx:
            preflight.enforce_public_ready(self.config())
        message = str(ctx.exception)
        self.assertIn('拒绝启动', message)
        self.assertIn('弱口令', message)
        self.assertIn('prepare_public.py', message, '错误信息要告诉人怎么修')

    def test_enforce_passes_on_compliant_config(self):
        self.write_users([('alice', 'Zq7-strong-passphrase-2026')])
        preflight.enforce_public_ready(self.config())


class PreparedCredentialsTest(unittest.TestCase):
    """`ops/public/prepare_public.py` 生成的口令必须能过自检、且互为不同。"""

    def test_generated_accounts_pass_the_gate(self):
        import sys
        sys.path.insert(0, os.path.join(preflight.repo_root(), 'ops', 'public'))
        import prepare_public as prep

        users, plain = prep.build_users()
        self.assertEqual(len(prep.ACCOUNTS), len(plain))
        # 每个账号口令不同、长度 20、且不是用户名
        self.assertEqual(len(plain), len(set(plain.values())), '口令不能重复')
        for name, password in plain.items():
            self.assertEqual(20, len(password))
            self.assertNotEqual(name, password)
        # 生成的哈希能验证、且过得了自检
        store = UserStore.from_plain_passwords([
            {'username': u['username'], 'role': u['role'],
             'display_name': u['display_name'], 'password': plain[u['username']],
             'store_aliases': u['store_aliases']} for u in users['users']])
        for name, password in plain.items():
            self.assertIsNotNone(store.authenticate(name, password))
            self.assertIsNone(store.authenticate(name, 'admin'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
