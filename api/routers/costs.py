# -*- coding: utf-8 -*-
"""成本管理接口（ADR-0009）。

    GET  /api/costs                       成本台账（分页 + 搜索）
    GET  /api/costs/events                变更留痕
    GET  /api/costs/missing?alias=&days=  缺成本清单
    POST /api/costs/import/preview        预览（**不落库**）
    POST /api/costs/import/apply          确认导入（落库 + 留痕）
    POST /api/costs/migrate-legacy        从原产品成本库迁移（旧库只读）

## ⚠️ 这是本项目第一批**写操作**，边界必须说清楚

`api/app.py` 原先声明「`api/` 下没有任何写路由」。ADR-0009 修改了这条声明，
新的边界是：

| 允许写 | 不允许写 |
|---|---|
| **成本库**（自有文件 `cost_book.db`） | **生产店铺库**（永远 `mode=ro`，有测试断言它的内容哈希不变） |
| 将来的自有平台库（阶段 C/D） | 旧产品的任何产物（迁移是**复制**，不是改） |

写权限只给 `root` / `finance`：成本是财务口径的输入，运营角色不该改。
"""
import os
import tempfile
from dataclasses import replace
from typing import Annotated, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Path, Query,
                     UploadFile)

from .. import config
from ..costs import (COST_WRITE_ROLES, apply_import, build_change_events,
                     build_cost_list, build_import_preview, build_missing_costs,
                     file_sha256)
from ..dashboard import _period_end
from ..deps import Runtime, get_current_user, get_runtime, resolve_store
from core.repository.cost_book import parse_cost_file

router = APIRouter(prefix='/api/costs', tags=['costs'])

_ALIAS_PATTERN = r'^[a-z0-9_-]{1,64}$'


def _require_write(user) -> None:
    if getattr(user, 'role', None) not in COST_WRITE_ROLES:
        raise HTTPException(
            status_code=403,
            detail='无权修改采购成本（只有 %s 角色可以）' % ' / '.join(COST_WRITE_ROLES))


def _window(source, alias: str, days: int):
    """成本相关接口统一走「与看板逐字一致」的窗口（ADR-0007 的 window_end）。"""
    cutoff = source.data_cutoff(alias)
    end_date = _period_end(cutoff, None)
    from datetime import date, timedelta
    start_date = (date.fromisoformat(end_date) - timedelta(days=days - 1)).isoformat()
    return start_date, end_date


@router.get('')
def list_costs(
    keyword: Annotated[Optional[str], Query(max_length=64, description='按货号/SKU 搜索')] = None,
    limit: Annotated[int, Query(ge=1, le=config.MAX_COST_ROWS_IN_RESPONSE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    try:
        with runtime.open_cost_book(readonly=True) as book:
            return build_cost_list(book, keyword=keyword, limit=limit, offset=offset)
    except FileNotFoundError as exc:
        # 成本库还不存在：这不是错误，是「还没导入过」——返回空台账并把路径告诉他
        return {'book_path': runtime.cost_book_path, 'total': 0, 'returned': 0,
                'limit': limit, 'offset': offset, 'keyword': keyword, 'rows': [],
                'change_event_total': 0, 'last_change': None,
                'note': '成本库还不存在（%s），请先导入或从原产品成本库迁移。' % exc}


@router.get('/events')
def list_cost_events(
    seller_sku: Annotated[Optional[str], Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=config.MAX_COST_ROWS_IN_RESPONSE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    try:
        with runtime.open_cost_book(readonly=True) as book:
            return build_change_events(book, seller_sku=seller_sku,
                                       limit=limit, offset=offset)
    except FileNotFoundError:
        return {'total': 0, 'rows': [], 'limit': limit, 'offset': offset}


@router.get('/missing')
def missing_costs(
    alias: Annotated[str, Query(pattern=_ALIAS_PATTERN, description='店铺别名')],
    days: Annotated[int, Query(ge=1, le=config.MAX_DAYS)] = config.DEFAULT_DAYS,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    store = resolve_store(alias, user, runtime)
    try:
        with runtime.open_source(store) as source:
            start_date, end_date = _window(source, store.alias, days)
            try:
                with runtime.open_cost_book(readonly=True) as book:
                    return build_missing_costs(source, book, store.alias,
                                               start_date, end_date, days, limit=limit)
            except FileNotFoundError:
                raise HTTPException(
                    status_code=503,
                    detail='成本库不存在（%s）。请先导入采购成本或从原产品成本库迁移。'
                           % runtime.cost_book_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail='店铺库不可用: %s' % exc)
    except NotImplementedError as exc:
        # 数据源实现不了 SKU 明细（本接口要靠它算缺成本清单）：如实报 501，
        # 而不是 500/空清单 —— 空清单会被当成「没有缺成本」，那是**相反的结论**。
        raise HTTPException(status_code=501, detail=str(exc))
    except AttributeError as exc:
        # 数据源连 `sku_detail_for_period` 都没有（协议是 Protocol，不做强制）：
        # 同上，501 而不是 500 —— 「这个源做不到」不是「服务端内部错误」。
        raise HTTPException(
            status_code=501,
            detail='该数据源不支持逐 SKU 明细，无法计算缺成本清单：%s' % exc)


@router.post('/import/preview')
async def import_preview(
    alias: Annotated[str, Form(pattern=_ALIAS_PATTERN)],
    days: Annotated[int, Form(ge=1, le=config.MAX_DAYS)] = config.DEFAULT_DAYS,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    """**只预览，不落库**：返回差异 + 两种策略下的影响面。"""
    _require_write(user)
    store = resolve_store(alias, user, runtime)
    path, sha = await _save_upload(file)
    try:
        rows, invalid = parse_cost_file(path)
        if not rows and invalid:
            raise HTTPException(status_code=422,
                                detail='文件里没有可用的成本行：%s' % invalid[0][2])
        try:
            with runtime.open_source(store) as source:
                start_date, end_date = _window(source, store.alias, days)
                with runtime.open_cost_book(readonly=False) as book:   # 建表但不写数据
                    preview = build_import_preview(
                        book, rows, invalid, source, store.alias,
                        start_date, end_date, days,
                        file_name=file.filename, file_sha=sha)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc))
        except AttributeError as exc:
            raise HTTPException(
                status_code=501,
                detail='该数据源不支持逐 SKU 明细，无法计算导入影响面：%s' % exc)
        preview['parsed_rows'] = len(rows)
        return preview
    finally:
        _cleanup(path)


@router.post('/import/apply')
async def import_apply(
    alias: Annotated[str, Form(pattern=_ALIAS_PATTERN)],
    days: Annotated[int, Form(ge=1, le=config.MAX_DAYS)] = config.DEFAULT_DAYS,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    _require_write(user)
    store = resolve_store(alias, user, runtime)
    path, sha = await _save_upload(file)
    try:
        rows, invalid = parse_cost_file(path)
        if invalid:
            # 有非法行就整批拒绝：宁可让用户改文件，也不做「部分导入」那种说不清的事
            raise HTTPException(
                status_code=422,
                detail='文件里有 %d 行非法数据（第一处：第 %d 行 %s），整批未导入。'
                       % (len(invalid), invalid[0][0], invalid[0][2]))
        if not rows:
            raise HTTPException(status_code=422, detail='文件里没有成本行')
        # 台账的「来源」要记**用户的原文件名**，不是服务端落盘用的临时名
        # （`ozon_cost_ab12cd34.xlsx` 对审计毫无意义）。批次指纹另有 file_sha256。
        rows = [replace(r, source=file.filename) for r in rows]
        with runtime.open_cost_book(readonly=False) as book:
            return apply_import(book, rows, actor=user.username,
                                file_name=file.filename, file_sha=sha)
    finally:
        _cleanup(path)


@router.post('/migrate-legacy')
def migrate_legacy(
    user=Depends(get_current_user),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    """把原产品成本库（只读）里的成本搬到我们的成本库。

    幂等：再点一次只会有 `unchanged`。跳过的行逐条给出原因（不静默丢弃）。
    """
    _require_write(user)
    legacy = runtime.legacy_cost_book_path or config.COST_BOOK_LEGACY_PATH
    if not os.path.isfile(legacy):
        raise HTTPException(status_code=404, detail='找不到原产品成本库: %s' % legacy)
    before = os.path.getmtime(legacy)
    try:
        with runtime.open_cost_book(readonly=False) as book:
            result = book.migrate_from_legacy(legacy, actor=user.username)
            result['total_after'] = book.count()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return dict(result, legacy_path=legacy,
                legacy_mtime_unchanged=(os.path.getmtime(legacy) == before),
                legacy_readonly=True)


async def _save_upload(file: UploadFile):
    """把上传文件落到临时目录（不覆盖、按内容算 sha256），返回 (路径, sha256)。"""
    suffix = os.path.splitext(file.filename or '')[1].lower()
    if suffix not in ('.xlsx', '.xlsm', '.csv'):
        raise HTTPException(status_code=422,
                            detail='只接受 .xlsx / .csv 的成本表，收到 %r' % suffix)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail='上传的文件是空的')
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=422, detail='文件超过 20 MB，不接受')
    fd, path = tempfile.mkstemp(prefix='ozon_cost_', suffix=suffix)
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
    return path, file_sha256(path)


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
