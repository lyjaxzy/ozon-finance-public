# -*- coding: utf-8 -*-
"""独立验证 API：用 python 直接请求，避免 PowerShell 的编码干扰。"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:8849'


def req(path, token=None, body=None):
    url = BASE + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    r = urllib.request.Request(url, data=data, method='POST' if body else 'GET')
    if body:
        r.add_header('Content-Type', 'application/json')
    if token:
        r.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode('utf-8'))
        except Exception:
            return e.code, raw.decode('utf-8', 'replace')


def login(u, p):
    st, d = req('/api/auth/login', body={'username': u, 'password': p})
    return st, d.get('access_token') if isinstance(d, dict) else None, d


def main():
    py = sys.executable
    env = None
    # 启动服务
    proc = subprocess.Popen([py, '-m', 'uvicorn', 'api.app:app', '--host', '127.0.0.1',
                             '--port', '8849', '--log-level', 'warning'],
                            cwd=r'D:\xzy\ozon-finance')
    time.sleep(9)
    try:
        print('=' * 70)
        print('① 中文编码检查（display_name）')
        st, tok, d = login('admin', 'admin')
        print('   HTTP %s' % st)
        print('   user = %s' % json.dumps(d.get('user'), ensure_ascii=False))
        name = (d.get('user') or {}).get('display_name')
        ok = name == '系统管理员'
        print('   display_name == "系统管理员" ? %s' % ('✓ 是（编码正常）' if ok else '✗ 否 → 接口真有问题'))

        print()
        print('=' * 70)
        print('② 403 越权检查（用受限角色，而不是 root）')
        for user, pwd, role in (('operator01', 'operator01', 'store_operator'),
                                ('finance01', 'finance01', 'finance')):
            st2, tok2, _ = login(user, pwd)
            if not tok2:
                print('   %-12s 登录失败 HTTP %s' % (user, st2))
                continue
            s1, d1 = req('/api/dashboard/store/store_alpha', tok2)
            s2, d2 = req('/api/dashboard/store/store_beta', tok2)
            leak = [k for k in ('totals', 'orders', 'trend', 'composition') if k in d2] \
                if isinstance(d2, dict) else []
            print('   %-12s (%s)' % (user, role))
            print('      自己店铺 store_alpha -> HTTP %s %s' % (s1, '✓ 允许' if s1 == 200 else '✗ 被拒'))
            print('      越权 store_beta     -> HTTP %s %s' % (s2, '✓ 拒绝' if s2 == 403 else '✗ 竟然放行'))
            print('      403 响应体是否泄漏业务数据: %s' % ('✓ 无泄漏（只含 detail）' if not leak else '✗ 泄漏 ' + str(leak)))
            print('      403 响应体: %s' % json.dumps(d2, ensure_ascii=False))

        print()
        print('=' * 70)
        print('③ 未登录')
        su, du = req('/api/dashboard/store/store_alpha')
        print('   HTTP %s  %s' % (su, json.dumps(du, ensure_ascii=False)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        print()
        print('服务已停止')
    return 0


if __name__ == '__main__':
    sys.exit(main())
