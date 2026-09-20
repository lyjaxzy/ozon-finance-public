#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OZON 生产数据备份与恢复演练。

为什么需要它
------------
`<DATA_ROOT>` 里的店铺库是唯一的真实业务资产。
它原先只有一份 **2026-09-07 的手工快照**,而且：
  * 陈旧（缺约 1,983 条流水）
  * 与数据同盘同根（`backups/` 就在数据根里，被连锅端）
  * 从未验证可恢复（backup_runs 表 0 行）

设计要点
--------
1. **在线备份**：用 SQLite 官方 `Connection.backup()`，不是裸拷贝文件。
   应用正在运行时裸拷贝可能拿到撕裂的页；官方 API 保证一致性快照。
2. **写到数据根之外**：默认 `D:\\OzonFinanceBackup`，避免同根被一起删除。
3. **每份快照带清单**：文件哈希 + 表行数 + integrity_check，可独立验证。
4. **保留策略**：最近 7 份每日 + 4 份每周，自动清理。
5. **恢复演练**：真把备份还原到临时目录跑一遍，并比对行数与哈希。
6. **只读对待生产库**：绝不写入生产数据库（包括不写 backup_runs 表，
   那是应用自己的记账，避免两套系统互相干扰）。

已知局限（必须说清楚）
----------------------
本机 C: 与 D: 是**同一块物理 NVMe 盘**（磁盘0）。因此本方案防的是
「误删、库损坏、逻辑错误」，**防不了磁盘物理损坏**。
真正的离盘副本需要外接硬盘 / NAS / 网盘，属待办。

用法
----
    python ops/backup/ozon_backup.py run        # 备份 + 校验 + 清理过期
    python ops/backup/ozon_backup.py list       # 列出所有快照
    python ops/backup/ozon_backup.py verify     # 校验最新快照
    python ops/backup/ozon_backup.py verify <id>
    python ops/backup/ozon_backup.py drill      # 恢复演练（还原并比对）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys

DATA_ROOT = r'<DATA_ROOT>'
#: 我们自己的数据根（ADR-0009）。旧产品**不会**写这里，所以必须单独收进来 ——
#: 否则成本台账（3424 条真实成本，人工录入成果）会完全没有备份。
PLATFORM_ROOT = r'<DATA_ROOT>Platform'
BACKUP_ROOT = r'D:\OzonFinanceBackup\ozon'
LOG_DIR = r'D:\OzonFinanceBackup\logs'
DRILL_DIR = r'D:\OzonFinanceBackup\_drill'

KEEP_DAILY = 7
KEEP_WEEKLY = 4

# 需要备份的内容：(相对 DATA_ROOT 的目录, 文件后缀) —— 递归
SOURCES = [
    (os.path.join('data', 'stores'), '.db'),
    (os.path.join('data', 'desktop'), '.db'),
    (os.path.join('data',), '.db'),          # 只取该层文件：multi_store.db
    ('config', '.json'),
    ('config', '.env'),
    (os.path.join('config', 'stores'), '.env'),
]
TOP_LEVEL_ONLY = {os.path.join('data',)}     # 这些目录不递归

#: 我们自有数据根下要备份的内容：(相对 PLATFORM_ROOT 的目录, 后缀)。
#: 备份键会加 `platform/` 前缀，与旧根的文件不会重名。
PLATFORM_SOURCES = [('', '.db')]

# 每个库要记录行数的表（用于跨快照/跨恢复比对）
COUNT_TABLES = ['postings', 'posting_items', 'finance_transactions',
                'posting_profit_facts', 'settlement_snapshots',
                # 自有成本库（ADR-0009）：台账与变更留痕都要能跨快照比对
                'sku_costs', 'sku_cost_change_events']


# ────────────────────────────── 工具 ──────────────────────────────
def log(msg, fh=None):
    line = '[%s] %s' % (dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    if fh:
        fh.write(line + '\n')
        fh.flush()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with io.open(path, 'rb') as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def snapshot_id(now=None):
    return (now or dt.datetime.now()).strftime('%Y%m%dT%H%M%SZ')


def connect_ro(path):
    return sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)


def source_path_for(key: str) -> str:
    """快照内的键 → 当前生产文件的绝对路径。

    `platform/` 前缀的键来自**我们自己的数据根**（ADR-0009 的成本库）；
    其余仍按旧数据根解析。少了这一步，平台库会被拿去旧根里找、
    拿到 None 并报一条「备份=3424 生产=None」的假告警 ——
    假告警会训练人忽略真告警。
    """
    key = key.replace('/', os.sep)
    if key.startswith('platform' + os.sep):
        return os.path.join(PLATFORM_ROOT, key[len('platform' + os.sep):])
    return os.path.join(DATA_ROOT, key)


def sqlite_facts(path):
    """返回 {integrity, tables:{name:rows}, error}。只读。"""
    out = {'integrity': None, 'tables': {}, 'error': None}
    try:
        c = connect_ro(path)
    except sqlite3.Error as exc:
        out['error'] = 'open failed: %s' % exc
        return out
    try:
        out['integrity'] = c.execute('PRAGMA integrity_check').fetchone()[0]
        for t in COUNT_TABLES:
            try:
                out['tables'][t] = c.execute('SELECT count(*) FROM %s' % t).fetchone()[0]
            except sqlite3.Error:
                pass
    except sqlite3.Error as exc:
        out['error'] = str(exc)
    finally:
        c.close()
    return out


def collect_sources():
    """列出待备份文件，返回 [(绝对路径, 快照内的相对键)]。

    两个根一起收：
      * 旧产品的 `<DATA_ROOT>`（只读，键是它下面的相对路径）；
      * 我们自己的 `<DATA_ROOT>Platform`（ADR-0009，键加 `platform/` 前缀）。
    成本台账在我们自己的根里，**漏掉它 = 人工录入的成本没有备份**。
    """
    found = []
    for rel, ext in SOURCES:
        d = os.path.join(DATA_ROOT, rel)
        if not os.path.isdir(d):
            continue
        if rel in TOP_LEVEL_ONLY:
            names = [n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n))]
        else:
            names = []
            for dp, _dn, fn in os.walk(d):
                names.extend(os.path.join(dp, f) for f in fn)
        for n in names:
            ap = n if os.path.isabs(n) else os.path.join(d, n)
            if not os.path.isfile(ap):
                continue
            if ext and not ap.lower().endswith(ext):
                continue
            # 排除数据根内的旧备份目录，避免自我复制
            if os.sep + 'backups' + os.sep in ap:
                continue
            found.append((ap, os.path.relpath(ap, DATA_ROOT)))

    for rel, ext in PLATFORM_SOURCES:
        d = os.path.join(PLATFORM_ROOT, rel) if rel else PLATFORM_ROOT
        if not os.path.isdir(d):
            continue
        for dp, _dn, fn in os.walk(d):
            for f in fn:
                ap = os.path.join(dp, f)
                if ext and not ap.lower().endswith(ext):
                    continue
                key = os.path.join('platform', os.path.relpath(ap, PLATFORM_ROOT))
                found.append((ap, key))

    return sorted(set(found), key=lambda x: x[1])


def online_backup(src_path, dst_path):
    """用 SQLite 官方 API 做一致性在线备份。"""
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    src = sqlite3.connect('file:%s?mode=ro' % src_path.replace('\\', '/'), uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


# ────────────────────────────── 备份 ──────────────────────────────
def cmd_run(args):
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    logpath = os.path.join(LOG_DIR, 'backup-%s.log' % dt.date.today().isoformat())
    with io.open(logpath, 'a', encoding='utf-8') as lf:
        # 最小间隔保护：让「定时」与「登录时补跑」两个触发器不会重复备份。
        # 机器夜间关机时，登录补跑会接手；已经备过就跳过。
        newest = load_snapshots()
        if newest:
            try:
                last = dt.datetime.strptime(newest[0][0], '%Y%m%dT%H%M%SZ')
                age_h = (dt.datetime.now() - last).total_seconds() / 3600.0
                if age_h < args.min_interval_hours:
                    log('距上次快照仅 %.1f 小时（阈值 %.0f 小时），本次跳过。'
                        % (age_h, args.min_interval_hours), lf)
                    return 0
            except ValueError:
                pass

        sid = snapshot_id()
        dest = os.path.join(BACKUP_ROOT, sid)
        log('===== 备份开始 %s =====' % sid, lf)
        srcs = collect_sources()
        if not srcs:
            log('✗ 找不到任何待备份文件，中止', lf)
            return 1
        log('待备份 %d 个文件' % len(srcs), lf)

        entries = []
        failed = []
        for ap, rel in srcs:
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            is_db = ap.lower().endswith(('.db', '.sqlite3'))
            try:
                if is_db:
                    online_backup(ap, target)
                else:
                    shutil.copy2(ap, target)
            except Exception as exc:
                log('  ✗ %s 备份失败: %r' % (rel, exc), lf)
                failed.append(rel)
                continue
            size = os.path.getsize(target)
            entry = {'path': rel, 'bytes': size, 'sha256': sha256_file(target)}
            if is_db:
                entry['sqlite'] = sqlite_facts(target)
            entries.append(entry)
            log('  ✓ %-46s %10s 字节' % (rel, format(size, ',')), lf)

        if failed:
            log('✗ 有 %d 个文件备份失败，快照不完整: %s' % (len(failed), failed), lf)
            return 1

        # 与生产库比对行数（证明快照确实是当下的）
        drift = []
        for e in entries:
            if 'sqlite' not in e:
                continue
            live = sqlite_facts(source_path_for(e['path']))
            for t, n in (e['sqlite'].get('tables') or {}).items():
                if live['tables'].get(t) != n:
                    drift.append('%s.%s 备份=%s 生产=%s' % (e['path'], t, n, live['tables'].get(t)))
        if drift:
            log('⚠ 备份与生产库行数存在差异（备份期间有写入，属正常）:', lf)
            for d in drift[:10]:
                log('   ' + d, lf)

        manifest = {
            'snapshot': sid,
            'created_at': dt.datetime.now().isoformat(timespec='seconds'),
            'data_root': DATA_ROOT,
            'file_count': len(entries),
            'total_bytes': sum(e['bytes'] for e in entries),
            'files': entries,
            'note': '由 ops/backup/ozon_backup.py 生成。哈希与行数用于恢复后校验。',
        }
        with io.open(os.path.join(dest, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1, sort_keys=False)

        log('快照完成: %s  共 %.1f MB' % (dest, manifest['total_bytes'] / 1048576.0), lf)
        removed = apply_retention(lf)
        log('保留策略清理: %d 个过期快照' % removed, lf)
        log('===== 备份结束 =====', lf)
    return 0


def load_snapshots():
    if not os.path.isdir(BACKUP_ROOT):
        return []
    snaps = []
    for n in sorted(os.listdir(BACKUP_ROOT)):
        p = os.path.join(BACKUP_ROOT, n)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, 'manifest.json')):
            snaps.append((n, p))
    return sorted(snaps, reverse=True)


def apply_retention(lf=None):
    """保留最近 KEEP_DAILY 份 + 最近 KEEP_WEEKLY 个自然周各一份。"""
    snaps = load_snapshots()
    if len(snaps) <= KEEP_DAILY:
        return 0
    keep = {n for n, _ in snaps[:KEEP_DAILY]}
    seen_weeks = []
    for n, _ in snaps:
        try:
            d = dt.datetime.strptime(n, '%Y%m%dT%H%M%SZ')
        except ValueError:
            continue
        wk = d.isocalendar()[:2]
        if wk not in seen_weeks:
            seen_weeks.append(wk)
            if len(seen_weeks) <= KEEP_WEEKLY:
                keep.add(n)
    removed = 0
    for n, p in snaps:
        if n in keep:
            continue
        try:
            shutil.rmtree(p)
            removed += 1
            if lf:
                log('  已删除过期快照 %s' % n, lf)
        except OSError as exc:
            if lf:
                log('  ! 删除 %s 失败: %r' % (n, exc), lf)
    return removed


# ────────────────────────────── 校验 ──────────────────────────────
def verify_snapshot(path, lf=None):
    """校验一份快照：哈希 + SQLite 完整性。返回 (ok, 报告行列表)。"""
    mpath = os.path.join(path, 'manifest.json')
    with io.open(mpath, encoding='utf-8') as fh:
        man = json.load(fh)
    lines = []
    bad = 0
    for e in man['files']:
        fp = os.path.join(path, e['path'])
        if not os.path.isfile(fp):
            lines.append('✗ 缺失 %s' % e['path'])
            bad += 1
            continue
        got = sha256_file(fp)
        if got != e['sha256']:
            lines.append('✗ 哈希不符 %s' % e['path'])
            bad += 1
            continue
        if 'sqlite' in e:
            f = sqlite_facts(fp)
            if f['integrity'] != 'ok':
                lines.append('✗ 完整性失败 %s: %s' % (e['path'], f['integrity']))
                bad += 1
                continue
            drift = [t for t, n in e['sqlite']['tables'].items() if f['tables'].get(t) != n]
            if drift:
                lines.append('✗ 行数不符 %s: %s' % (e['path'], drift))
                bad += 1
                continue
        lines.append('✓ %s' % e['path'])
    return bad == 0, lines


def cmd_verify(args):
    snaps = load_snapshots()
    if not snaps:
        print('没有任何快照')
        return 1
    target = None
    if args.snapshot:
        for n, p in snaps:
            if n == args.snapshot:
                target = (n, p)
                break
        if not target:
            print('找不到快照 %s' % args.snapshot)
            return 1
    else:
        target = snaps[0]
    print('校验快照 %s' % target[0])
    ok, lines = verify_snapshot(target[1])
    for l in lines:
        print('  ' + l)
    print('结果: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def cmd_list(args):
    snaps = load_snapshots()
    if not snaps:
        print('没有任何快照（%s）' % BACKUP_ROOT)
        return 0
    print('共 %d 份快照（%s）' % (len(snaps), BACKUP_ROOT))
    for n, p in snaps:
        man = json.load(io.open(os.path.join(p, 'manifest.json'), encoding='utf-8'))
        print('  %-18s %2d 个文件  %8.1f MB  %s'
              % (n, man['file_count'], man['total_bytes'] / 1048576.0, man['created_at']))
    return 0


# ────────────────────────────── 恢复演练 ──────────────────────────────
def cmd_drill(args):
    """真把最新快照还原到临时目录，跑完整性并比对行数与哈希。"""
    snaps = load_snapshots()
    if not snaps:
        print('没有快照可演练')
        return 1
    sid, path = snaps[0]
    print('恢复演练：快照 %s' % sid)
    if os.path.isdir(DRILL_DIR):
        shutil.rmtree(DRILL_DIR)
    os.makedirs(DRILL_DIR, exist_ok=True)

    man = json.load(io.open(os.path.join(path, 'manifest.json'), encoding='utf-8'))
    dbs = [e for e in man['files'] if 'sqlite' in e]
    if not dbs:
        print('✗ 快照里没有数据库文件')
        return 1

    ok = True
    for e in dbs:
        src = os.path.join(path, e['path'])
        dst = os.path.join(DRILL_DIR, e['path'].replace(os.sep, '__'))
        shutil.copy2(src, dst)
        f = sqlite_facts(dst)
        same_rows = all(f['tables'].get(t) == n for t, n in e['sqlite']['tables'].items())
        same_hash = sha256_file(dst) == e['sha256']
        good = f['integrity'] == 'ok' and same_rows and same_hash
        ok = ok and good
        print('  %s %-42s 完整性=%-4s 行数一致=%-5s 哈希一致=%s'
              % ('✓' if good else '✗', e['path'], f['integrity'], same_rows, same_hash))
        if not good:
            print('      备份行数 %s' % e['sqlite']['tables'])
            print('      还原行数 %s' % f['tables'])

    shutil.rmtree(DRILL_DIR, ignore_errors=True)
    print('结果: %s' % ('PASS 备份可恢复' if ok else 'FAIL 备份不可恢复'))
    return 0 if ok else 1


# ────────────────────────────── 入口 ──────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description='OZON 生产数据备份与恢复演练')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run', help='备份 + 校验 + 清理过期')
    r.add_argument('--min-interval-hours', type=float, default=20.0,
                   help='距上次快照不足该小时数则跳过（默认 20，供定时+登录双触发器去重）')
    sub.add_parser('list', help='列出快照')
    v = sub.add_parser('verify', help='校验快照')
    v.add_argument('snapshot', nargs='?', default=None)
    sub.add_parser('drill', help='恢复演练')
    args = ap.parse_args(argv)
    return {'run': cmd_run, 'list': cmd_list,
            'verify': cmd_verify, 'drill': cmd_drill}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
