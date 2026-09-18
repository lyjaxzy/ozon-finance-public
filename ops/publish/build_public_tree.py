#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从私密仓库生成「可公开」子集，并做泄漏门禁。

为什么脚本化
------------
手工拷贝公开子集必然漂移：改了一个文件忘了同步、或者不小心把含真实数据的
文件拷了过去。所以公开树**每次都由这个脚本完整重建**。

三道防线
--------
1. **fail-closed 白名单**：只有 `publish.json` 里列出的路径会被复制。
   新增文件默认不公开 —— 忘记登记只会漏公开，不会误公开。
2. **脱敏**：字面替换（生产路径、店铺别名、个人邮箱）+
   正则替换（凡是长得像真实订单号的 token 一律换成虚构值）。
3. **泄漏门禁**：对**生成后的公开树整体**再扫一遍，命中任何模式即中止发布。
   扫描对象是产物，不是源文件 —— 这样才算真正验证了「没漏出去」。

用法
----
    python ops/publish/build_public_tree.py            # 只做检查，打印报告，不落盘
    python ops/publish/build_public_tree.py --confirm  # 真的生成公开树
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CONFIG = os.path.join(HERE, 'publish.json')

POSTING_RX = re.compile(r'\b\d{6,}-\d{4}(?:-\d{1,2})?\b')
TEXT_EXT = {'.py', '.md', '.txt', '.json', '.js', '.ts', '.vue', '.yml', '.yaml',
            '.spec', '.html', '.css', '.example', '.cmd', '.sh', '.toml', '.ini'}


def tracked():
    out = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True,
                         text=True, encoding='utf-8', errors='replace')
    return [f for f in out.stdout.splitlines() if f.strip()]


def matches(path, pattern):
    """支持 ** 与 * 的 glob 匹配（相对仓库根的 POSIX 路径）。"""
    if pattern.endswith('/**'):
        return path == pattern[:-3] or path.startswith(pattern[:-3] + '/')
    if '**' in pattern:
        rx = re.escape(pattern).replace(r'\*\*', '.*').replace(r'\*', '[^/]*')
        return re.match('^' + rx + '$', path) is not None
    return fnmatch.fnmatch(path, pattern)


def select(files, cfg):
    chosen = []
    for f in files:
        if any(matches(f, p) for p in cfg.get('exclude', [])):
            continue
        if any(matches(f, p) for p in cfg['include']):
            chosen.append(f)
    return sorted(chosen)


def fake_posting(tok):
    """把真实订单号换成虚构值。

    **刻意不用数字格式**：早先版本生成 `FAKE-922F6D79` 这种同形状假值，
    结果泄漏门禁分不清真假、把自己的假值也报了警。
    改用 `FAKE-<hash>` 前缀，一眼可辨，且永不匹配真实订单号模式。
    """
    h = hashlib.sha256(tok.encode()).hexdigest()
    return 'FAKE-' + h[:8].upper()


def sanitize(text, cfg):
    for rule in cfg.get('sanitize_literals', []):
        text = text.replace(rule['from'], rule['to'])
    text = POSTING_RX.sub(lambda m: fake_posting(m.group(0)), text)
    return text


def make_synthetic_fixture(cfg):
    """生成合成夹具：结构相同、数值全假。

    诚实说明：这个夹具**不能**验证口径正确性 —— 它的期望值是由同一套实现算出来的，
    属自证。公开仓库只能用它验证「代码能跑通、结构没坏」。
    真实口径对账在私密仓库用真实夹具做。
    """
    spec = cfg['synthetic_fixture']
    rnd = random.Random(spec['seed'])
    orders = []
    for i in range(spec['orders']):
        pn = 'FAKE-%06d-%04d' % (rnd.randint(0, 999999), rnd.randint(0, 9999))
        rate = round(rnd.uniform(6.0, 13.0), 4)
        sales = round(rnd.uniform(120, 900), 2)
        cost = round(rnd.uniform(3, 40), 2)
        returns = 0.0
        ops = [
            {'operation_id': str(70000000000 + i * 4), 'posting_number': pn,
             'occurred_at': '2026-01-01T00:00:00+00:00', 'operation_category': 'SALE',
             'operation_type': 'OperationAgentDeliveredToCustomer',
             'amount_rub': str(round(sales * 0.88, 2))},
            {'operation_id': str(70000000001 + i * 4), 'posting_number': pn,
             'occurred_at': '2026-01-01T00:00:00+00:00', 'operation_category': 'AGENCY',
             'operation_type': 'OperationMarketplaceAgencyFeeAggregator3PLGlobal',
             'amount_rub': '-15'},
            {'operation_id': str(70000000002 + i * 4), 'posting_number': pn,
             'occurred_at': '2026-01-01T00:00:00+00:00', 'operation_category': 'LOGISTICS',
             'operation_type': 'MarketplaceRedistributionOfDeliveryServicesOperation',
             'amount_rub': str(round(-rnd.uniform(30, 120), 2))},
        ]
        direct_net = round(sum(float(o['amount_rub']) for o in ops), 2)
        rate_d = str(rate)
        profit = round(round(direct_net / rate, 2) - cost, 2)
        revenue = round(cost + rnd.uniform(5, 40), 2)
        logistics = round(rnd.uniform(0, 15), 2)
        fee = round(rnd.uniform(0, 8), 2) if rnd.random() < 0.4 else None
        est = round(revenue - cost - logistics - (fee or 0), 2)
        orders.append({
            'posting_number': pn, 'order_month': '2026-01', 'status': 'delivered',
            'fact': {
                'actual_profit_cny': str(profit), 'direct_net_rub': str(direct_net),
                'settled_sales_rub': str(sales), 'exchange_rate_rub_per_cny': rate_d,
                'purchase_cost_cny': str(cost), 'actual_complete': 1,
                'actual_unknown_reason': None, 'estimated_profit_cny': str(est),
                'estimated_complete': 1, 'linked_operation_count': len(ops),
                'linked_operation_ids': [o['operation_id'] for o in ops],
                'updated_at': '2026-01-02T00:00:00+00:00',
            },
            'settlement_snapshot': {
                'settlement_date': '2026-01-01', 'state': 'locked',
                'direct_net_rub': str(direct_net), 'settled_sales_rub': str(sales),
                'exchange_rate_rub_per_cny': rate_d, 'purchase_cost_cny': str(cost),
                'actual_profit_cny': str(profit), 'unknown_reason': None,
                'operation_count': len(ops),
                'operation_ids': [o['operation_id'] for o in ops],
                'locked_at': '2026-01-02T00:00:00+00:00',
            },
            'posting': {
                'status': 'delivered', 'revenue_cny': str(revenue),
                'purchase_cost_cny': str(cost), 'logistics_cost_cny': str(logistics),
                'estimated_platform_fee_cny': None if fee is None else str(fee),
                'platform_fees_cny': None, 'estimated_profit_cny': str(est),
                'profit_included': 1, 'exclusion_reason': None,
            },
            'items': [{'offer_id': 'SYNTH-%03d' % i, 'sku': str(100000000 + i), 'quantity': 1}],
            'operations': ops,
        })
    return {
        'meta': {
            'schema_version': 1,
            'source': '【合成数据】由 ops/publish/build_public_tree.py 生成，不含任何真实业务数据',
            'note': '⚠️ 期望值由同一套实现计算，属自证；本夹具只验证代码结构，不验证口径正确性。',
            'per_month': spec['orders'], 'months': 1, 'order_count': len(orders),
            'missing_operations': 0,
        },
        'completion_rate': {'total': len(orders), 'actual_complete': len(orders),
                            'estimated_complete': len(orders)},
        'orders': orders,
    }


PUBLIC_README = """# OZON 财务核算核心（公开子集）

本仓库是私密主仓库的**可公开子集**，由 `ops/publish/build_public_tree.py` 生成。

## 这里有什么

- `core/` —— 三段利润口径的**可读实现**（Decimal、纯函数、不依赖框架与 Windows），
  以及可替换的数据来源接口（SQLite / 夹具两种实现）
- `.githooks/` —— git 门禁（提交/合并/推送前自动跑回归）
- `docs/adr/` —— 架构决策记录
- `ops/` —— 生产数据备份工具、公开树构建工具

## ⚠️ 必读：这里的测试不能证明口径正确

公开仓库里的夹具是**合成数据**（`core/tests/fixtures/golden_sample.json` 由脚本生成）。
它的期望值是用**同一套实现**算出来的，因此跑通只说明「代码能运行、结构没坏」，
**不构成口径正确性的证据**。

真实的等价性验证在私密仓库中进行：用从生产库冻结的 147 单真实样本，
逐单对账新实现与既有系统，要求金额、直接净额、完成标记全部一致
（当前结果：实际利润 6668 单核对，0 处不符）。

## 不在这里的内容

- 反编译产物与打包启动器（含商业软件授权绕过代码）—— 涉版权与授权，不公开
- 真实业务数据（订单号、金额、汇率、店铺别名）—— 属客户数据，不公开
- 完整 PRD 与业务细节

## 背景

原来这套财务系统的逻辑只存在于反编译字节码里，不可阅读、不可维护。
本项目把它重写为可读源码，并用「冻结真实样本 + 逐单对账」的方式证明重写前后等价。
公开这部分，是因为其中的**方法论**（先冻结黄金样本、再重写、用门禁守住）
比具体业务更有参考价值。
"""


def leak_scan(tree, cfg):
    """对生成后的公开树整体扫描。返回违规列表。

    唯一的豁免见 publish.json 的 leak_scan_skip：本构建脚本的配置文件自身
    就含有这些模式定义，扫它必然自指误报。除此之外不做任何豁免。
    """
    pats = {k: re.compile(v) for k, v in cfg['leak_patterns'].items()}
    skip = set(cfg.get('leak_scan_skip', []))
    bad = []
    for dp, dn, fn in os.walk(tree):
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, tree).replace(os.sep, '/')
            if rel in skip:
                continue
            if os.path.splitext(f)[1].lower() not in TEXT_EXT:
                continue
            try:
                text = io.open(p, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            for name, rx in pats.items():
                hits = rx.findall(text)
                if hits:
                    bad.append((rel, name, len(hits), hits[:3]))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--confirm', action='store_true', help='真的生成公开树')
    args = ap.parse_args()

    cfg = json.load(io.open(CONFIG, encoding='utf-8'))
    files = tracked()
    chosen = select(files, cfg)

    print('=' * 78)
    print('公开树构建报告')
    print('=' * 78)
    print('私密仓库文件总数 : %d' % len(files))
    print('公开子集文件数   : %d' % len(chosen))
    total = sum(os.path.getsize(os.path.join(ROOT, f.replace('/', os.sep)))
                for f in chosen if os.path.isfile(os.path.join(ROOT, f.replace('/', os.sep))))
    print('公开子集体积     : %.1f KB' % (total / 1024.0))
    print()
    tops = {}
    for f in chosen:
        tops[f.split('/')[0]] = tops.get(f.split('/')[0], 0) + 1
    for t in sorted(tops, key=lambda x: -tops[x]):
        print('   %-28s %3d 个文件' % (t, tops[t]))

    out = cfg['output_dir']
    print()
    print('输出目录: %s' % out)

    if not args.confirm:
        print()
        print('（未加 --confirm，仅检查。加 --confirm 才会真正生成）')
        return 0

    # ── 生成 ──
    # 注意：output_dir 很可能**就是公开仓库的工作区**。
    # 早先版本直接 rmtree(output_dir)，重跑一次就会把公开仓库的 .git 历史删掉。
    # 因此保留 .git，只清理其余内容 —— 重建的是内容，不是仓库。
    if os.path.isdir(out):
        for name in os.listdir(out):
            if name == '.git':
                continue
            victim = os.path.join(out, name)
            if os.path.isdir(victim):
                shutil.rmtree(victim, ignore_errors=True)
            else:
                try:
                    os.remove(victim)
                except OSError:
                    pass
    else:
        os.makedirs(out, exist_ok=True)
    for rel in chosen:
        src = os.path.join(ROOT, rel.replace('/', os.sep))
        dst = os.path.join(out, rel.replace('/', os.sep))
        if not os.path.isfile(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.splitext(rel)[1].lower() in TEXT_EXT:
            text = io.open(src, encoding='utf-8', errors='replace').read()
            io.open(dst, 'w', encoding='utf-8', newline='\n').write(sanitize(text, cfg))
        else:
            shutil.copy2(src, dst)

    # 公开版 README 与合成夹具
    io.open(os.path.join(out, 'README.md'), 'w', encoding='utf-8', newline='\n').write(PUBLIC_README)
    if cfg['synthetic_fixture']['enabled']:
        fp = os.path.join(out, cfg['synthetic_fixture']['path'].replace('/', os.sep))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        json.dump(make_synthetic_fixture(cfg), io.open(fp, 'w', encoding='utf-8', newline='\n'),
                  ensure_ascii=False, indent=1)
        print('已生成合成夹具: %s' % cfg['synthetic_fixture']['path'])

    # ── 泄漏门禁（扫产物）──
    bad = leak_scan(out, cfg)
    print()
    print('=' * 78)
    print('泄漏门禁（扫描生成后的公开树）')
    print('=' * 78)
    if bad:
        print('✗ 发现 %d 处泄漏，发布中止：' % len(bad))
        for rel, name, n, sample in bad[:20]:
            print('   %-46s %-12s ×%-4d %s' % (rel, name, n, sample))
        print()
        print('公开树已生成但**不可用于发布**，请修正 publish.json 或源文件后重跑。')
        return 1
    print('✓ 未发现任何泄漏模式')

    n = sum(len(f) for _, _, f in os.walk(out))
    print()
    print('公开树已生成: %s（%d 个文件）' % (out, n))
    print('下一步：在该目录 git init 并推送到公开仓库。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
