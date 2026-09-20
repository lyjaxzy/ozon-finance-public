# -*- coding: utf-8 -*-
"""采购成本库（ADR-0009）。

**这是本项目存在的理由**：OZON 的两份导出里唯独没有采购成本，成本只能由我方按货号录入。
本模块提供这块数据的**唯一来源**：台账 + 生效日期 + 变更留痕 + 导入 + 从原产品成本库迁移。

## 职责边界（三条，越界即错）

1. **这里没有任何利润公式**。成本只是 `evaluate_actual` / `evaluate_estimated` 的输入，
   利润一律由 `core/domain/profit.py` 算。
2. **写只落这个自有库**。生产店铺库永远 `mode=ro`（有测试断言导入后它的内容哈希不变）。
3. **不摊分**：多货号订单的成本归属沿用 ADR-0006 的规则，本模块只提供**单件成本**，
   不决定它落到哪个订单上。

## 三个必须记住的约定

* 成本是**单件 CNY**，字符串存 Decimal（与全项目金额一致，避免浮点）；
* `effective_from` 让「改价」不重写历史：某订单用哪个价，取决于它的**结算日**
  （取 `effective_from <= 该日` 里最近的一条）；
* 每次写入都往 `sku_cost_change_events` **追加**一条，旧的永不修改、永不删除。

## 表结构对齐原产品

`scope` / `store_alias` / `status` 这三个字段照抄原产品的 `purchase_costs`
（实测：3495 条全是 `shared` + `confirmed` + `store_alias IS NULL`），
这样「从原库迁移」是零转换的，将来要支持店铺级覆盖也不必改表。
"""
import csv
import hashlib
import io
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..domain.profit import to_decimal
from .sqlite_source import CN_TZ

#: 只认 `confirmed` 的成本参与核算（`pending` 是留给将来的审批态）
STATUS_CONFIRMED = 'confirmed'
STATUS_PENDING = 'pending'

SCHEMA = """
CREATE TABLE IF NOT EXISTS sku_costs (
    cost_id        TEXT PRIMARY KEY,
    seller_sku     TEXT NOT NULL,
    platform_sku   TEXT,
    unit_cost_cny  TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    scope          TEXT NOT NULL DEFAULT 'shared',
    store_alias    TEXT,
    status         TEXT NOT NULL DEFAULT 'confirmed',
    source         TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sku_costs_offer_effective
    ON sku_costs(seller_sku, effective_from);
CREATE INDEX IF NOT EXISTS ix_sku_costs_offer ON sku_costs(seller_sku);

CREATE TABLE IF NOT EXISTS sku_cost_change_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cost_id        TEXT,
    seller_sku     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    action         TEXT NOT NULL,
    old_json       TEXT,
    new_json       TEXT,
    source         TEXT,
    file_sha256    TEXT,
    actor_id       TEXT,
    occurred_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cost_events_sku ON sku_cost_change_events(seller_sku);
"""

#: 导入模板的列名别名（与原产品《采购成本模板.xlsx》一致：货号 | OZON 数字 SKU | 单价 | 备注）
COL_OFFER = ('seller_sku', '货号', 'offer_id', 'Offer ID', 'offer id', 'Артикул')
COL_PLATFORM_SKU = ('platform_sku', 'OZON 数字 SKU', 'OZON数字SKU', '数字 SKU', 'sku', 'SKU')
COL_COST = ('unit_cost_cny', '单价', '采购成本', '采购单价', '成本', '单价(CNY)', 'cost')
COL_NOTE = ('note', '备注', '说明')
COL_EFFECTIVE = ('effective_from', '生效日期', '生效日')


def _now() -> str:
    return datetime.now(CN_TZ).isoformat()


def _today() -> str:
    return datetime.now(CN_TZ).date().isoformat()


def _sha1(*parts: str) -> str:
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()


def _index_of(cells: Sequence[str], names: Sequence[str]) -> Optional[int]:
    lowered = [c.strip().lower() for c in cells]
    for name in names:
        if name.lower() in lowered:
            return lowered.index(name.lower())
    return None


@dataclass(frozen=True)
class CostRow:
    """一条成本（导入/迁移的统一输入形态）。"""

    seller_sku: str
    unit_cost_cny: Decimal
    effective_from: str
    platform_sku: Optional[str] = None
    note: Optional[str] = None
    source: Optional[str] = None
    status: str = STATUS_CONFIRMED

    def as_json(self) -> dict:
        return {
            'seller_sku': self.seller_sku,
            'platform_sku': self.platform_sku,
            'unit_cost_cny': format(self.unit_cost_cny, 'f'),
            'effective_from': self.effective_from,
            'status': self.status,
            'note': self.note or '',
            'source': self.source or '',
        }


@dataclass
class ImportPreview:
    """导入预览：**不落库**，只回答「会发生什么」。"""

    total: int = 0
    created: List[CostRow] = field(default_factory=list)
    updated: List[Tuple[CostRow, Decimal]] = field(default_factory=list)  # (新行, 旧单价)
    unchanged: List[CostRow] = field(default_factory=list)
    invalid: List[Tuple[int, str, str]] = field(default_factory=list)      # (行号, 原始值, 原因)

    @property
    def ok(self) -> bool:
        return not self.invalid

    def as_dict(self) -> dict:
        return {
            'total': self.total,
            'created_count': len(self.created),
            'updated_count': len(self.updated),
            'unchanged_count': len(self.unchanged),
            'invalid_count': len(self.invalid),
            'created': [r.as_json() for r in self.created[:50]],
            'updated': [dict(r.as_json(), old_unit_cost_cny=format(old, 'f'))
                        for r, old in self.updated[:50]],
            'invalid': [{'line': n, 'value': v, 'reason': why}
                        for n, v, why in self.invalid[:50]],
            'truncated': (len(self.created) > 50 or len(self.updated) > 50
                          or len(self.invalid) > 50),
        }


# ── 解析导入文件 ──────────────────────────────────────────────────
def parse_cost_file(path: str) -> Tuple[List[CostRow], List[Tuple[int, str, str]]]:
    """把 xlsx / csv 读成成本行。返回 (行, 非法行)。

    **一份实现两处用**：导入接口与 Excel 数据源的成本注入都走这里，
    避免「界面上导入的列名」与「程序读文件的列名」两套规则漂移。
    """
    rows = _read_table(path)
    if not rows:
        return [], [(0, '', '文件是空的')]
    header_index = oi = pi = ci = ni = ei = None
    for i, row in enumerate(rows[:10]):
        cells = [('' if c is None else str(c)) for c in row]
        o = _index_of(cells, COL_OFFER)
        c = _index_of(cells, COL_COST)
        if o is not None and c is not None:
            header_index, oi, ci = i, o, c
            pi = _index_of(cells, COL_PLATFORM_SKU)
            ni = _index_of(cells, COL_NOTE)
            ei = _index_of(cells, COL_EFFECTIVE)
            break
    if header_index is None:
        return [], [(0, '', '读不出表头：需要一列「货号」与一列「单价」（可用列名：%s / %s）'
                     % ('、'.join(COL_OFFER), '、'.join(COL_COST)))]

    out: List[CostRow] = []
    bad: List[Tuple[int, str, str]] = []
    seen: Dict[str, int] = {}
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        cells = [('' if c is None else str(c).strip()) for c in row]

        def cell(idx: Optional[int]) -> str:
            return cells[idx] if idx is not None and idx < len(cells) else ''

        offer = cell(oi)
        raw_cost = cell(ci)
        if not offer and not raw_cost:
            continue                      # 整行空白：跳过，不算错
        if not offer:
            bad.append((offset, raw_cost, '缺货号'))
            continue
        if not raw_cost:
            bad.append((offset, offer, '缺单价'))
            continue
        cost = to_decimal(raw_cost.replace(',', '')) if isinstance(raw_cost, str) else to_decimal(raw_cost)
        if cost is None:
            bad.append((offset, raw_cost, '单价解析不了'))
            continue
        if cost < 0:
            bad.append((offset, raw_cost, '单价为负'))
            continue
        if offer in seen:
            bad.append((offset, offer, '与第 %d 行重复（同一文件里同一货号只能出现一次）' % seen[offer]))
            continue
        seen[offer] = offset
        effective = cell(ei) or _today()
        if len(effective) >= 10:
            effective = effective[:10]
        try:
            date.fromisoformat(effective)
        except ValueError:
            bad.append((offset, effective, '生效日期不是 YYYY-MM-DD'))
            continue
        out.append(CostRow(
            seller_sku=offer,
            unit_cost_cny=cost,
            effective_from=effective,
            platform_sku=cell(pi) or None,
            note=cell(ni) or None,
            source=os.path.basename(path),
        ))
    return out, bad


def _read_table(path: str) -> List[list]:
    """把 xlsx / csv 读成「行 × 列」。"""
    if path.lower().endswith('.csv'):
        with io.open(path, encoding='utf-8-sig', newline='') as fh:
            sample = fh.readline()
            fh.seek(0)
            delim = ';' if sample.count(';') > sample.count(',') else ','
            return [list(row) for row in csv.reader(fh, delimiter=delim)]
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError('读 .xlsx 成本表需要 openpyxl：%s' % exc)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        return [[None if v is None else str(v) for v in row]
                for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


# ── 成本库 ────────────────────────────────────────────────────────
class CostBook:
    """自有成本库（SQLite）。读可只读打开；写只影响这个文件。"""

    def __init__(self, path: str, readonly: bool = False):
        self.path = path
        if readonly:
            if not os.path.isfile(path):
                raise FileNotFoundError('找不到成本库: %s' % path)
            uri = 'file:%s?mode=ro' % path.replace('\\', '/')
            self._conn = sqlite3.connect(uri, uri=True)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._conn = sqlite3.connect(path)
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._conn.row_factory = sqlite3.Row

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 读 ──
    def unit_cost(self, seller_sku: str, on_date: Optional[str] = None) -> Optional[Decimal]:
        """某货号在 `on_date`（默认今天）生效的单件成本。

        取 `effective_from <= on_date` 里最近的一条；没有就返回 None
        —— **不返回 0**（缺成本和零成本是两件事）。
        """
        day = (on_date or _today())[:10]
        row = self._conn.execute(
            "SELECT unit_cost_cny FROM sku_costs "
            " WHERE seller_sku = ? AND status = ? AND effective_from <= ? "
            " ORDER BY effective_from DESC LIMIT 1",
            (seller_sku, STATUS_CONFIRMED, day)).fetchone()
        return None if row is None else to_decimal(row['unit_cost_cny'])

    def price_map(self, on_date: Optional[str] = None) -> Dict[str, Decimal]:
        """所有货号在 `on_date` 生效的单价：{货号: 单价}。

        用于喂给取数层（Excel 源的 `purchase_cost_by_offer` 等）。
        一条 SQL 取每个货号的最新生效行，避免 N 次查询。
        """
        day = (on_date or _today())[:10]
        rows = self._conn.execute(
            "SELECT c.seller_sku, c.unit_cost_cny FROM sku_costs c "
            " JOIN (SELECT seller_sku, max(effective_from) AS d FROM sku_costs "
            "        WHERE status = ? AND effective_from <= ? GROUP BY seller_sku) m "
            "   ON m.seller_sku = c.seller_sku AND m.d = c.effective_from "
            " WHERE c.status = ?",
            (STATUS_CONFIRMED, day, STATUS_CONFIRMED)).fetchall()
        out: Dict[str, Decimal] = {}
        for r in rows:
            v = to_decimal(r['unit_cost_cny'])
            if v is not None:
                out[r['seller_sku']] = v
        return out

    def count(self) -> int:
        return self._conn.execute('SELECT count(*) FROM sku_costs').fetchone()[0]

    def list_costs(self, keyword: Optional[str] = None, limit: int = 50,
                   offset: int = 0) -> Tuple[List[dict], int]:
        """台账（分页）。`keyword` 同时匹配货号与 OZON 数字 SKU。"""
        where = ''
        args: list = []
        if keyword:
            where = " WHERE seller_sku LIKE ? OR platform_sku LIKE ? "
            like = '%' + keyword + '%'
            args = [like, like]
        total = self._conn.execute('SELECT count(*) FROM sku_costs' + where, args).fetchone()[0]
        rows = self._conn.execute(
            'SELECT * FROM sku_costs' + where +
            ' ORDER BY seller_sku, effective_from DESC LIMIT ? OFFSET ?',
            args + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def change_events(self, seller_sku: Optional[str] = None,
                      limit: int = 50, offset: int = 0) -> Tuple[List[dict], int]:
        where = ''
        args: list = []
        if seller_sku:
            where = ' WHERE seller_sku = ? '
            args = [seller_sku]
        total = self._conn.execute(
            'SELECT count(*) FROM sku_cost_change_events' + where, args).fetchone()[0]
        rows = self._conn.execute(
            'SELECT * FROM sku_cost_change_events' + where +
            ' ORDER BY id DESC LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    # ── 预览（不落库）──
    def preview(self, rows: Iterable[CostRow]) -> ImportPreview:
        out = ImportPreview()
        for row in rows:
            out.total += 1
            current = self.unit_cost(row.seller_sku, row.effective_from)
            if current is None:
                out.created.append(row)
            elif current == row.unit_cost_cny:
                out.unchanged.append(row)
            else:
                out.updated.append((row, current))
        return out

    # ── 写（导入 / 迁移）──
    def apply(self, rows: Sequence[CostRow], actor: Optional[str] = None,
              file_sha256: Optional[str] = None,
              action_prefix: str = '') -> dict:
        """把成本行写入台账 + 追加变更事件。返回 {created, updated, unchanged}。

        幂等：同样的行再导一次，`unchanged` 计数、不产生事件、不改 `updated_at`。
        """
        created = updated = unchanged = 0
        now = _now()
        for row in rows:
            current = self.unit_cost(row.seller_sku, row.effective_from)
            exists = self._conn.execute(
                'SELECT cost_id FROM sku_costs WHERE seller_sku = ? AND effective_from = ?',
                (row.seller_sku, row.effective_from)).fetchone()
            cost_id = (exists['cost_id'] if exists is not None
                       else _sha1(row.seller_sku, row.effective_from))
            if current == row.unit_cost_cny and exists is not None:
                unchanged += 1
                continue
            old_json = None
            if exists is not None:
                old = self._conn.execute('SELECT * FROM sku_costs WHERE cost_id = ?',
                                         (cost_id,)).fetchone()
                old_json = _row_json(old)
                self._conn.execute(
                    'UPDATE sku_costs SET unit_cost_cny = ?, platform_sku = ?, note = ?, '
                    ' status = ?, source = ?, scope = ?, updated_at = ? WHERE cost_id = ?',
                    (format(row.unit_cost_cny, 'f'), row.platform_sku, row.note or '',
                     row.status, row.source or '', 'shared', now, cost_id))
                updated += 1
                action = action_prefix + 'update'
            else:
                self._conn.execute(
                    'INSERT INTO sku_costs (cost_id, seller_sku, platform_sku, unit_cost_cny,'
                    ' effective_from, scope, store_alias, status, source, note,'
                    ' created_at, updated_at)'
                    ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (cost_id, row.seller_sku, row.platform_sku,
                     format(row.unit_cost_cny, 'f'), row.effective_from, 'shared', None,
                     row.status, row.source or '', row.note or '', now, now))
                created += 1
                action = action_prefix + 'insert'
            self._conn.execute(
                'INSERT INTO sku_cost_change_events (cost_id, seller_sku, effective_from,'
                ' action, old_json, new_json, source, file_sha256, actor_id, occurred_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                (cost_id, row.seller_sku, row.effective_from, action, old_json,
                 _row_json(self._conn.execute('SELECT * FROM sku_costs WHERE cost_id = ?',
                                              (cost_id,)).fetchone()),
                 row.source, file_sha256, actor, now))
        self._conn.commit()
        return {'created': created, 'updated': updated, 'unchanged': unchanged}

    # ── 删除（错录 / 测试数据）──
    def purge(self, seller_sku: str, reason: str, actor: Optional[str] = None) -> dict:
        """删掉某个货号的全部成本行，并**追加**一条 `purge` 事件留痕。

        为什么需要它：录错货号、把测试数据写进生产台账，都是必然会发生的事。
        没有这个口子，人就会去拿 SQL 直接擦库 —— 那样审计链就断了。
        事件里保留被删行的完整快照（`old_json`），删了什么一目了然。

        注意：删除**不**影响历史利润 —— 订单成本已经落在店铺库/快照里，
        这里删的只是「以后怎么算」的依据。
        """
        rows = self._conn.execute(
            'SELECT * FROM sku_costs WHERE seller_sku = ?', (seller_sku,)).fetchall()
        if not rows:
            return {'purged': 0, 'seller_sku': seller_sku}
        now = _now()
        for r in rows:
            self._conn.execute(
                'INSERT INTO sku_cost_change_events (cost_id, seller_sku, effective_from,'
                ' action, old_json, new_json, source, file_sha256, actor_id, occurred_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                (r['cost_id'], seller_sku, r['effective_from'], 'purge', _row_json(r),
                 None, reason, None, actor, now))
        self._conn.execute('DELETE FROM sku_costs WHERE seller_sku = ?', (seller_sku,))
        self._conn.commit()
        return {'purged': len(rows), 'seller_sku': seller_sku, 'reason': reason,
                'actor': actor, 'occurred_at': now}

    # ── 从原产品成本库迁移（只读源）──
    def migrate_from_legacy(self, legacy_path: str, actor: str = 'migration') -> dict:
        """把旧产品 `purchase_costs` 里的成本搬过来。**旧库只读打开。**

        生效日期取原记录的 `created_at` 日期（诚实的默认：什么时候录的就什么时候生效）。

        跳过的行**逐条给出原因**，不只给计数 —— 实测旧库里有 71 行
        `seller_sku` 为空（来自 2026-09-07 的一次脏导入），
        这些行没有货号就无法归属，既不能静默丢弃也不能瞎猜货号（例如拿 platform_sku 顶）。
        """
        if not os.path.isfile(legacy_path):
            raise FileNotFoundError('找不到原产品成本库: %s' % legacy_path)
        uri = 'file:%s?mode=ro' % legacy_path.replace('\\', '/')
        src = sqlite3.connect(uri, uri=True)
        src.row_factory = sqlite3.Row
        skipped: List[dict] = []
        try:
            tabs = {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'purchase_costs' not in tabs:
                raise ValueError('旧成本库里没有 purchase_costs 表: %s' % legacy_path)
            rows: List[CostRow] = []
            for r in src.execute('SELECT * FROM purchase_costs'):
                keys = r.keys()
                offer = (r['seller_sku'] or '').strip() if 'seller_sku' in keys else ''
                cost = to_decimal(r['unit_cost_cny']) if 'unit_cost_cny' in keys else None
                why = None
                if not offer:
                    why = '货号为空（无法归属到任何 SKU）'
                elif cost is None:
                    why = '单价缺失或解析不了：%r' % (r['unit_cost_cny'],)
                elif cost < 0:
                    why = '单价为负：%s' % format(cost, 'f')
                if why:
                    skipped.append({
                        'cost_id': (r['cost_id'] if 'cost_id' in keys else None),
                        'seller_sku': offer or None,
                        'platform_sku': ((r['platform_sku'] or None)
                                         if 'platform_sku' in keys else None),
                        'unit_cost_cny': ((str(r['unit_cost_cny'])
                                           if 'unit_cost_cny' in keys else None)),
                        'created_at': ((r['created_at'] or None)
                                       if 'created_at' in keys else None),
                        'reason': why,
                    })
                    continue
                created = (r['created_at'] or '') if 'created_at' in keys else ''
                effective = created[:10] if len(created) >= 10 else _today()
                rows.append(CostRow(
                    seller_sku=offer,
                    unit_cost_cny=cost,
                    effective_from=effective,
                    platform_sku=(r['platform_sku'] or None) if 'platform_sku' in keys else None,
                    note=(r['note'] or None) if 'note' in keys else None,
                    source='migrated:%s' % os.path.basename(legacy_path),
                ))
        finally:
            src.close()
        result = self.apply(rows, actor=actor, action_prefix='migrated_')
        result['scanned'] = len(rows) + len(skipped)
        result['skipped'] = len(skipped)
        result['skipped_detail'] = skipped[:50]
        result['skipped_truncated'] = len(skipped) > 50
        return result


def _row_json(row) -> Optional[str]:
    if row is None:
        return None
    import json
    return json.dumps({k: row[k] for k in row.keys()}, ensure_ascii=False, sort_keys=True)
