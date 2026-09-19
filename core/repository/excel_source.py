# -*- coding: utf-8 -*-
"""Excel/CSV 导入数据源 —— `ProfitDataSource` 的第三个实现。

背景与依据见 `docs/adr/0005-Excel导入数据源模板选择.md`：
OZON 后台能导出的两份文件喂的是不同的东西，**不能互相替代**。

| 导出文件 | 喂什么 | 用途 |
|---|---|---|
| `Отчет по начислениям_*.xlsx`（应计报表） | 应计流水（卢布） | §7.1 实际利润、利润构成 |
| `postings.csv`（发货单导出） | 发货单（订单） | §7.2 预估利润、§7.6 逾期判定、订单明细 |

本文件里**没有任何利润口径**：利润一律由 `core/domain/profit.py` 算。
导入器只做三件事 —— 解析、归类、取数。

## 实测过的关键事实（不是推测）

1. **应计报表第 1 行是区间标题**（`Период: ...`），**第 2 行才是表头**。
   这里刻意**不写死「表头在第 2 行」**，而是扫描前若干行找 `ID начисления`；
   找不到锚点直接报错，绝不静默读成空表。
2. **`postings.csv` 是分号分隔、UTF-8 BOM、中文表头**，一个发货单可能有多行
   （多商品）—— 金额与数量按发货单汇总，状态/截止时间取该单任一行。
3. **`ID начисления` 有两种形态**：
   * 条目单号 `FAKE-6948C859`（= `发货号码`，绝大多数应计挂这里）
   * **基单号** `FAKE-8B7013B3`（= `订单号`，**手续费/收单费挂这里**）

   > 这是本文件最容易写错的地方。实测：严格按 `ID начисления` 分组求和，
   > 与生产库 `posting_profit_facts.direct_net_rub` 的**逐单吻合率只有 7.6%**；
   > 把基单号的应计归属到它唯一的条目单之后，吻合率升到 **98.9%**（1410/1426）。
   > 原因就是 OZON 把 `Эквайринг`（收单费）记在基单号上，
   > 与 `core/README.md` 里「手续费挂在基单号上」是同一条结论。
   >
   > 一个基单号对应**多个**条目单时无法归属 —— 这时**不猜、不摊分**
   > （实测把金额平分给各条目单会把吻合率从 98.9% 拉到 94.3%），
   > 应计保留在基单号名下单独成一单，并在 `attribution` 里标成 `base_ambiguous`。
4. **应计报表没有结算汇率、没有订单状态、也没有发货截止时间。**   * 汇率：`postings.csv` 的 `已由买家支付 / 发货的金额` 隐含出汇率，实测与
     生产库 `settlement_snapshots.exchange_rate_rub_per_cny` **在 93.8% 的订单上完全相等**，
     但另外 6.2% 会正好差一个整数倍（多商品单的买家实付不完整）。
     所以它**默认关闭**，要用必须显式打开 `exchange_rate_mode='implied_buyer_payment'`。
   * 采购成本：两份文件里都没有。不填时 §7.1 会如实判 `missing_purchase_cost`
     —— 这是**正确的行为**，不是缺陷。要么由调用方注入
     `purchase_cost_by_offer`（货号 → 采购成本 CNY）。
   * 订单状态与发货截止时间只有 `postings.csv` 有 → 逾期判定走它；
     少了 `postings.csv` 时 `overdue_count` 返回 `available=False`（**不返回 0 冒充**）。
5. **有 3 行的 `ID начисления` 是空的**（四份报表合计，金额非 0）。
   如果按「锚点列非空」过滤数据行，这 3 行会被悄悄丢掉 —— 那正是本模块
   反复强调要避免的事（实测：丢掉它们会让取值统计少 3 个组合）。
   现在它们被标成 `attribution='no_accrual_id'` 留在库里、计入
   `accrual_type_census()`、可用 `unidentified_accruals()` 单独查，
   只是不参与逐单/窗口汇总（连单号都没有，归不到任何订单上）。

## 只读约束

* 源文件一律**只读打开**（openpyxl `read_only=True`；CSV 只读文本读）。
* 解析结果落 SQLite 临时库，以 `mode=ro` 打开使用 —— 与 `sqlite_source.py`
  一样没有写入路径；缓存文件写在系统临时目录，**不碰导出目录**。

## 性能

不「把 5 万行全常驻内存再反复线性扫描」：流式解析 → 分批落 SQLite → 建索引
（`posting_number` / `accrual_id` / `category`）→ 后续全走索引查询。
解析结果按「文件路径 + 大小 + mtime + 解析器版本」做指纹缓存
（实测 4 份报表共 52,573 行：冷解析 **48.6 秒**，命中缓存重开 **0.01 秒**）。
"""
import csv
import glob
import hashlib
import io
import os
import sqlite3
import tempfile
import warnings
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..domain.profit import (Operation, Posting, SettlementSnapshot, evaluate_actual,
                             evaluate_estimated, quantize_cny, rate_is_valid,
                             sum_amounts, to_decimal)
from .base import (DailyAmounts, DataCutoff, OverdueInfo, PeriodAmounts,
                   PeriodOrderRow, SkuDetail, UnattributedAmounts)
from .sku_attribution import attribute_order_lines

#: 数据截止时间统一按国内时间展示（与 sqlite_source 一致）
CN_TZ = timezone(timedelta(hours=8))

# ── 默认路径（与 docs/关于OZON导出模板.md 一致：两份都放同一个目录）──
DEFAULT_EXPORT_DIR = r'D:\Downloads'
DEFAULT_ACCRUAL_GLOB = 'Отчет по начислениям_*.xlsx'
DEFAULT_POSTINGS_CSV = 'postings.csv'

#: 解析器版本。任何影响落库内容的改动都要 +1，否则旧缓存会被复用。
PARSER_VERSION = 1

#: 应计报表的工作表名（找不到时退回第一个工作表）
ACCRUAL_SHEET = 'Начисления'
#: 表头识别锚点（第 1 行是区间标题，所以必须扫描而不是写死行号）
HEADER_ANCHOR = 'ID начисления'
#: 扫描表头时最多往下看几行
HEADER_SCAN_ROWS = 10
#: 流式写入的分批大小
BATCH = 4000


# ══════════════════════════════════════════════════════════════════
# 一、应计类型 → 八大类别 的映射表
# ══════════════════════════════════════════════════════════════════
#
# 八大类别的定义见 core/README.md 与 core/domain/profit.py 的注释：
#   SALE / COMMISSION / LOGISTICS / RETURN / ADVERTISING / ACQUIRING / AGENCY / OTHER
#
# ── 映射依据（重要）──────────────────────────────────────────────
# 生产库 `finance_transactions.operation_category` 就是旧系统对**同一批 OZON 业务**
# 的类别判定，它是本表的实证来源。实测对应关系（已逐条核对）：
#
#   ACQUIRING   <- MarketplaceRedistributionOfAcquiringOperation        （Эквайринг）
#   AGENCY      <- OperationMarketplaceAgencyFeeAggregator3PLGlobal     （Агентское вознаграждение Ozon）
#   LOGISTICS   <- MarketplaceInternFreightForwardSrvForTheOrgOfInternTransportationOperation
#                                                                       （Транспортно-экспедиционная услуга…）
#   LOGISTICS   <- MarketplaceRedistributionOfDeliveryServicesOperation （Логистика / Услуги международной доставки）
#   SALE        <- OperationAgentDeliveredToCustomer                    （Выручка）
#   ADVERTISING <- OperationMarketplaceCostPerClick                     （Оплата за клик）
#   OTHER       <- CustomerReviews                                      （Подписка Управление отзывами）
#   OTHER       <- StarsMembership                                      （Звёздные товары）
#   OTHER       <- OperationPromotionWithCostPerOrder                   （Продвижение с оплатой за заказ）
#   OTHER       <- DefectFineCancellation                               （Превышение индекса ошибок: отмена）
#   OTHER       <- DisposalOfGoods                                      （Утилизация товара）
#   OTHER       <- OperationMarketplaceWithHoldingForUndeliverableGoods （Удержание за недовложение товара）
#   OTHER       <- SetOffNonResident                                    （Взаимозачет требований между Договорами）
#   RETURN      <- OperationItemReturn / *ReturnFlowLogistic            （Обратная логистика / Возврат выручки）
#   RETURN      <- OperationMarketplaceServicePartialCompensationToClient
#                                                                       （Перечисления частичных компенсаций покупателям）
#
# **凡生产库能直接观测到的，一律服从生产库**（口径一致优先于语义直觉）。
# 只有两处刻意与生产库不同，都写在下面注释里，并在报告里标成「待业务确认」。

CATEGORY_SALE = 'SALE'
CATEGORY_COMMISSION = 'COMMISSION'
CATEGORY_LOGISTICS = 'LOGISTICS'
CATEGORY_RETURN = 'RETURN'
CATEGORY_ADVERTISING = 'ADVERTISING'
CATEGORY_ACQUIRING = 'ACQUIRING'
CATEGORY_AGENCY = 'AGENCY'
CATEGORY_OTHER = 'OTHER'

#: 八大类别的权威顺序（报表/测试按这个顺序输出）
PROFIT_CATEGORIES = (
    CATEGORY_SALE, CATEGORY_COMMISSION, CATEGORY_LOGISTICS, CATEGORY_RETURN,
    CATEGORY_ADVERTISING, CATEGORY_ACQUIRING, CATEGORY_AGENCY, CATEGORY_OTHER,
)

#: (Группа услуг, Тип начисления) → 类别。**精确组合键优先。**
#:
#: 只有两个类型需要按分组判成不同类别：
#:   * `Программы партнёров`、`Баллы за скидки` 在 `Продажи` 下是销售加项，
#:     在 `Возвраты` 下是同一批加项的冲回（退货）。
#:     （实测：库里把这两条连同「Выручка」「Вознаграждение за продажу」
#:     一起合并成一条 SALE 操作 —— 353.33+3.53+131.14−58.56 = 429.44。）
CATEGORY_BY_GROUP_TYPE: Dict[Tuple[str, str], str] = {
    ('Продажи', 'Программы партнёров'): CATEGORY_SALE,
    ('Возвраты', 'Программы партнёров'): CATEGORY_RETURN,
    ('Продажи', 'Баллы за скидки'): CATEGORY_SALE,
    ('Возвраты', 'Баллы за скидки'): CATEGORY_RETURN,
}

#: `Тип начисления` → 类别。覆盖 2026-06/07/08/09 四份应计报表里出现过的
#: **全部 27 个取值**（其中 25 个与分组无关，另外 2 个见 CATEGORY_BY_GROUP_TYPE）。
CATEGORY_BY_TYPE: Dict[str, str] = {
    # ── 销售侧
    'Выручка': CATEGORY_SALE,
    # ── 佣金：**刻意与生产库不同**。库里把佣金并进 SALE，于是看不出佣金占比；
    #    而 ADR-0005 明确要求「带应计类型分组 → 可直接拆出利润构成」，
    #    我们的八分类里本来就有 COMMISSION。所以这里按明细拆出来。
    #    ⚠️ 待业务确认：佣金是否应与销售额合并成一条。
    'Вознаграждение за продажу': CATEGORY_COMMISSION,
    #    退货时的佣金冲回。归 COMMISSION 而不是 RETURN，理由是它冲的是佣金科目。
    #    ⚠️ 待业务确认（按 RETURN 记也说得通）。
    'Возврат вознаграждения': CATEGORY_COMMISSION,
    # ── 物流 / 代理
    'Транспортно-экспедиционная услуга по организации международной перевозки':
        CATEGORY_LOGISTICS,
    'Услуги международной доставки': CATEGORY_LOGISTICS,
    'Логистика': CATEGORY_LOGISTICS,
    'Доставка до места выдачи': CATEGORY_LOGISTICS,
    'Доставка до места выдачи силами Ozon': CATEGORY_LOGISTICS,
    #    ⚠️ 待业务确认：Выдача товара（交付货物）语义上是末端交付动作，归 LOGISTICS；
    #    全样本仅 257 行，也可能属于平台服务费（OTHER）。
    'Выдача товара': CATEGORY_LOGISTICS,
    'Агентское вознаграждение Ozon': CATEGORY_AGENCY,
    # ── 收单
    'Эквайринг': CATEGORY_ACQUIRING,
    # ── 退货
    'Возврат выручки': CATEGORY_RETURN,
    'Обратная логистика': CATEGORY_RETURN,
    #    ⚠️ 待业务确认：退货处理服务费，归 RETURN（与退货同源）；也可能是 OTHER。
    'Обработка возвратов, отмен и невыкупов партнёрами': CATEGORY_RETURN,
    'Перечисления частичных компенсаций покупателям': CATEGORY_RETURN,
    # ── 广告：生产库只把「按点击付费」判成 ADVERTISING，其余推广类判成 OTHER。
    #    这里**服从生产库**，不按分组 `Продвижение и реклама` 一刀切 ——
    #    否则「评论管理订阅」也会被算成广告费，与库里口径对不上。
    'Оплата за клик': CATEGORY_ADVERTISING,
    'Подписка Управление отзывами': CATEGORY_OTHER,
    'Звёздные товары': CATEGORY_OTHER,
    'Продвижение с оплатой за заказ': CATEGORY_OTHER,
    #    ⚠️ 待业务确认：全样本 1 行。与 CustomerReviews 同类，暂归 OTHER。
    'Ускоренный сбор отзывов': CATEGORY_OTHER,
    # ── 其他（罚款 / 处置 / 冲抵 / 补偿）
    'Превышение индекса ошибок: отмена': CATEGORY_OTHER,
    'Утилизация товара': CATEGORY_OTHER,
    'Взаимозачет требований между Договорами': CATEGORY_OTHER,
    'Удержание за недовложение товара': CATEGORY_OTHER,
    #    ⚠️ 待业务确认：OZON 责任造成的物流丢件赔偿。八分类里没有「赔偿」科目，
    #    暂归 OTHER；若业务认为应冲减物流成本则应归 LOGISTICS。
    'Потеря по вине Ozon в логистике': CATEGORY_OTHER,
}


def classify_accrual(group_ru: Optional[str], type_ru: Optional[str]) -> Tuple[str, bool]:
    """把 (服务分组, 应计类型) 映射成八大类别之一。

    返回 `(类别, 是否识别)`。

    **未识别的取值一律落到 `OTHER` 并返回 `matched=False`** —— 由调用方记进
    `unmapped_types` 表。刻意**不做分组级兜底猜测**：
    「`Услуги партнёров` 下没见过的新类型」猜成 LOGISTICS 会静默算错利润构成，
    而记成 `OTHER` 至少能被报表发现。
    """
    g = (group_ru or '').strip()
    t = (type_ru or '').strip()
    if not t:
        return CATEGORY_OTHER, False
    hit = CATEGORY_BY_GROUP_TYPE.get((g, t))
    if hit is not None:
        return hit, True
    hit = CATEGORY_BY_TYPE.get(t)
    if hit is not None:
        return hit, True
    return CATEGORY_OTHER, False


# ══════════════════════════════════════════════════════════════════
# 二、postings.csv 的状态映射（中文 → OZON 状态码）
# ══════════════════════════════════════════════════════════════════
#
# 库里的 `postings.status` 存的是 OZON 状态码（awaiting_packaging / delivered / …），
# 而 CSV 导出的是**中文**。为了让逾期口径与库里一致，这里统一映射成状态码；
# 原始中文另存 `status_raw`，可通过 `raw_status()` 取回。
STATUS_CN_TO_CODE: Dict[str, str] = {
    '待备货': 'awaiting_packaging',
    '等待发运': 'awaiting_deliver',
    '运输中': 'delivering',
    # ⚠️ 推断：已到取货点但买家未取，OZON 侧仍算 delivering 而不是 delivered。
    #    对逾期判定无影响（两者都不在逾期集合里）。
    '已到达取货点，待取件': 'delivering',
    '已签收': 'delivered',
    '已取消': 'cancelled',
}

#: §7.6 逾期判定：只有这两个状态才谈得上「逾期未发运」
OVERDUE_STATUSES = frozenset({'awaiting_packaging', 'awaiting_deliver'})

#: postings.csv 里必须存在的列。缺列 → 报错而不是静默少读。
REQUIRED_POSTING_COLUMNS = (
    '发货号码', '订单号', '状态', '不逾期的发运日期', '发货的金额',
    '已由买家支付', '数量', 'SKU', '货号',
)


# ══════════════════════════════════════════════════════════════════
# 三、单元格解析工具
# ══════════════════════════════════════════════════════════════════
def require_openpyxl():
    """延迟导入 openpyxl：core 的其它部分不该因为缺这个库就跑不起来。"""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            '读取应计报表需要 openpyxl（anaconda 自带）。请执行：pip install openpyxl\n'
            '原始错误：%s' % exc)
    return openpyxl


def _text(value) -> Optional[str]:
    """单元格 → 去空白的字符串；空值返回 None（**不返回空串**）。"""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return str(value).strip() or None


def _amount_text(value) -> Optional[str]:
    """金额单元格 → **字符串**。

    金额一律以字符串落库、由 `core.domain.profit.to_decimal` 转 Decimal ——
    与生产库 `finance_transactions.amount_rub` 是 TEXT 的做法一致，
    避免 SQLite 的 SUM 把金额变成 REAL 而引入二进制浮点误差（PRD §14.2）。
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        # repr 给出最短往返表示（Excel 里存的是 IEEE double）。
        # 实测四份报表的金额最多 2 位小数，这里不会引入舍入噪声。
        return repr(value)
    return str(value).strip() or None


def _int_or_none(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(Decimal(s))
    except Exception:  # noqa: BLE001 - 任何解析失败都按「没有值」处理
        return None


def _date_text(value) -> Optional[str]:
    """日期单元格 → `YYYY-MM-DD`（协议里日期一律按字符串比较）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    s = str(value).strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s[:10] if len(s) >= 10 else s


def _dt_text(value) -> Optional[str]:
    """日期时间单元格 → `YYYY-MM-DD HH:MM:SS`（发货截止时间要按串比较）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    s = str(value).strip()
    return s or None


def iter_accrual_rows(path: str) -> Iterable[dict]:
    """逐行产出应计报表的原始字典（惰性、流式、只读）。

    表头靠**扫描锚点列**确定，而不是写死第 2 行 —— 这样既要处理
    「第 1 行是 `Период: ...` 区间标题」，也不会在 OZON 换模板时静默读空。

    ⚠️ 刻意**不按 `ID начисления` 非空来过滤数据行**：
    实测四份报表里有 **3 行 `ID начисления` 是空的**（金额不为 0）。
    按锚点判空会把它们悄悄丢掉 —— 那正是本模块反复强调要避免的事。
    这些行会被标成 `no_accrual_id` 留在库里、计入统计、单独可查，
    只是不参与逐单汇总（连单号都没有，没法归到哪个订单上）。
    """
    openpyxl = require_openpyxl()
    # OZON 导出的 xlsx 没有默认样式表，openpyxl 每次都会打一条 UserWarning。
    # 这条警告对使用者没有信息量，压掉 —— 但只压这一条，别把别的警告一起吞了。
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Workbook contains no default style')
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[ACCRUAL_SHEET] if ACCRUAL_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        header = None
        scanned = 0
        for row in ws.iter_rows(values_only=True):
            if row is None or all(v is None for v in row):
                continue
            cells = [_text(v) for v in row]
            if header is None:
                scanned += 1
                if HEADER_ANCHOR in cells:
                    header = cells
                elif scanned >= HEADER_SCAN_ROWS:
                    raise ValueError(
                        '应计报表前 %d 行都找不到表头锚点 «%s»：%s\n'
                        '模板可能变了 —— 拒绝静默读成空表。'
                        % (HEADER_SCAN_ROWS, HEADER_ANCHOR, path))
                continue
            yield dict(zip(header, row))
        if header is None:
            raise ValueError('应计报表是空的，或前 %d 行都没有表头锚点 «%s»：%s'
                             % (HEADER_SCAN_ROWS, HEADER_ANCHOR, path))
    finally:
        wb.close()


def _accrual_tuple(rec: dict, seq: int, tag: str) -> Tuple:
    """原始字典 → 落库 tuple。

    字段顺序与 `_INSERT_ACCRUAL` 一致，只差 `category` / `category_known` 两项
    （那两项要由映射表算出来，见 `_build`）：前 6 项 + 类别 2 项 + 后 10 项 = 19。
    """
    return (
        'ACC-%s-%06d' % (tag, seq),                                    # operation_id
        seq,                                                           # row_no
        _text(rec.get('ID начисления')),                               # accrual_id
        _date_text(rec.get('Дата начисления')),                        # accrual_date
        _text(rec.get('Группа услуг')),                                # group_ru
        _text(rec.get('Тип начисления')),                              # type_ru
        _text(rec.get('Артикул')),                                     # offer_id
        _text(rec.get('SKU')),                                         # sku
        _text(rec.get('Название товара')),                             # product_name
        _int_or_none(rec.get('Количество')),                           # quantity
        _amount_text(rec.get('Цена продавца')),                        # seller_price
        _date_text(rec.get('Дата принятия заказа в обработку или оказания услуги')),
        _text(rec.get('Платформа продажи')),                           # platform
        _text(rec.get('Схема работы')),                                # work_scheme
        _amount_text(rec.get('Вознаграждение Ozon, %')),               # commission_pct
        _amount_text(rec.get('Сумма итого, руб.')),                    # amount_rub（TEXT！）
    )


def _file_tag(path: str) -> str:
    """文件名 → 稳定短标识，用于拼 operation_id。"""
    return hashlib.sha1(os.path.basename(path).encode('utf-8')).hexdigest()[:8]


def read_postings_csv(path: str) -> Tuple[List[tuple], List[tuple]]:
    """读 postings.csv → (发货单聚合行, 商品明细行)。

    一个发货单可能有多行（多商品）：金额、数量按发货单汇总；
    状态/截止时间/订单号取该单任意一行（实测同单内一致）。
    """
    with io.open(path, encoding='utf-8-sig', newline='') as fh:
        sample = fh.readline()
        fh.seek(0)
        delim = ';' if sample.count(';') >= sample.count(',') else ','
        reader = csv.DictReader(fh, delimiter=delim)
        cols = list(reader.fieldnames or ())
        missing = [c for c in REQUIRED_POSTING_COLUMNS if c not in cols]
        if missing:
            raise ValueError(
                'postings.csv 缺少必需列：%s\n实际列：%s\n'
                '请从 OZON 后台 → 订单 → 发货单 重新导出（保持默认表头与分号分隔）。'
                % ('、'.join(missing), '、'.join(cols)))
        items: List[tuple] = []
        agg: Dict[str, dict] = {}
        for row in reader:
            pn = (row.get('发货号码') or '').strip()
            if not pn:
                continue
            order_key = (row.get('订单号') or '').strip() or None
            status_raw = (row.get('状态') or '').strip() or None
            revenue = _amount_text((row.get('发货的金额') or '').strip() or None)
            paid = _amount_text((row.get('已由买家支付') or '').strip() or None)
            qty = _int_or_none((row.get('数量') or '').strip() or None)
            offer = (row.get('货号') or '').strip() or None
            processing = _dt_text((row.get('正在处理中') or '').strip() or None)

            items.append((pn, order_key, (row.get('SKU') or '').strip() or None, offer,
                          qty, revenue, paid,
                          (row.get('商品名称') or '').strip() or None))
            a = agg.get(pn)
            if a is None:
                a = agg[pn] = {
                    'posting_number': pn, 'order_key': order_key,
                    'status_raw': status_raw,
                    'status': STATUS_CN_TO_CODE.get(status_raw or ''),
                    'shipment_deadline': _dt_text((row.get('不逾期的发运日期') or '').strip() or None),
                    'revenue_cny': None, 'buyer_paid_rub': None,
                    'currency': (row.get('货件的货币代码') or '').strip() or None,
                    'item_count': 0, 'quantity': 0,
                    'first_processing_at': processing, 'offer_ids': [],
                    # 推导隐含汇率用的分子分母：只累加**两侧都非空**的行
                    'rate_paid_sum': None, 'rate_value_sum': None, 'rate_rows': 0,
                }
            a['item_count'] += 1
            a['quantity'] += qty or 0
            if offer and offer not in a['offer_ids']:
                a['offer_ids'].append(offer)
            if revenue is not None:
                a['revenue_cny'] = str(Decimal(a['revenue_cny'] or '0') + Decimal(revenue))
            if paid is not None:
                a['buyer_paid_rub'] = str(Decimal(a['buyer_paid_rub'] or '0') + Decimal(paid))
            if paid is not None and revenue is not None and Decimal(revenue) > 0:
                a['rate_paid_sum'] = str(Decimal(a['rate_paid_sum'] or '0') + Decimal(paid))
                a['rate_value_sum'] = str(Decimal(a['rate_value_sum'] or '0') + Decimal(revenue))
                a['rate_rows'] += 1
            if processing and (a['first_processing_at'] is None
                               or processing < a['first_processing_at']):
                a['first_processing_at'] = processing
        out = []
        for a in agg.values():
            a['offer_ids'] = ','.join(a['offer_ids']) or None
            out.append(tuple(a[k] for k in POSTING_FIELDS))
        return out, items


#: `postings` 表的列顺序（与 `_INSERT_POSTING` 一致）
POSTING_FIELDS = (
    'posting_number', 'order_key', 'status_raw', 'status', 'shipment_deadline',
    'revenue_cny', 'buyer_paid_rub', 'currency', 'item_count', 'quantity',
    'first_processing_at', 'offer_ids', 'rate_paid_sum', 'rate_value_sum', 'rate_rows',
)

_INSERT_ACCRUAL = """
INSERT INTO accruals (
    operation_id, row_no, accrual_id, accrual_date, group_ru, type_ru, category,
    category_known, offer_id, sku, product_name, quantity, seller_price, accepted_at,
    platform, work_scheme, commission_pct, amount_rub, source_file
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_INSERT_POSTING = """
INSERT INTO postings (
    posting_number, order_key, status_raw, status, shipment_deadline, revenue_cny,
    buyer_paid_rub, currency, item_count, quantity, first_processing_at, offer_ids,
    rate_paid_sum, rate_value_sum, rate_rows
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_INSERT_ITEM = """
INSERT INTO posting_items (
    posting_number, order_key, sku, offer_id, quantity, revenue_cny, buyer_paid_rub,
    product_name
) VALUES (?,?,?,?,?,?,?,?)
"""

_SCHEMA = """
CREATE TABLE accruals (
    operation_id   TEXT PRIMARY KEY,
    row_no         INTEGER NOT NULL,
    -- ⚠️ 刻意允许 NULL：实测报表里有 3 行 ID начисления 是空的。
    --    这些行必须留下来并可查（标成 no_accrual_id），不许静默丢弃。
    accrual_id     TEXT,
    accrual_date   TEXT,
    group_ru       TEXT,
    type_ru        TEXT,
    category       TEXT NOT NULL,
    category_known INTEGER NOT NULL,
    offer_id       TEXT,
    sku            TEXT,
    product_name   TEXT,
    quantity       INTEGER,
    seller_price   TEXT,
    accepted_at    TEXT,
    platform       TEXT,
    work_scheme    TEXT,
    commission_pct TEXT,
    amount_rub     TEXT,
    source_file    TEXT,
    posting_number TEXT,
    attribution    TEXT
);
CREATE TABLE postings (
    posting_number      TEXT PRIMARY KEY,
    order_key           TEXT,
    status_raw          TEXT,
    status              TEXT,
    shipment_deadline   TEXT,
    revenue_cny         TEXT,
    buyer_paid_rub      TEXT,
    currency            TEXT,
    item_count          INTEGER,
    quantity            INTEGER,
    first_processing_at TEXT,
    offer_ids           TEXT,
    rate_paid_sum       TEXT,
    rate_value_sum      TEXT,
    rate_rows           INTEGER
);
CREATE TABLE posting_items (
    posting_number TEXT, order_key TEXT, sku TEXT, offer_id TEXT,
    quantity INTEGER, revenue_cny TEXT, buyer_paid_rub TEXT, product_name TEXT
);
CREATE TABLE order_items (
    order_key TEXT NOT NULL, posting_number TEXT NOT NULL,
    PRIMARY KEY (order_key, posting_number)
);
CREATE TABLE unmapped_types (
    group_ru TEXT NOT NULL, type_ru TEXT NOT NULL, rows INTEGER NOT NULL,
    sample_accrual_id TEXT, PRIMARY KEY (group_ru, type_ru)
);
CREATE TABLE source_files (
    path TEXT PRIMARY KEY, kind TEXT, size INTEGER, mtime_ns INTEGER, rows INTEGER
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""

_INDEXES = """
CREATE INDEX ix_acc_posting ON accruals(posting_number);
CREATE INDEX ix_acc_accrual_id ON accruals(accrual_id);
CREATE INDEX ix_acc_category ON accruals(category);
CREATE INDEX ix_acc_known ON accruals(category_known);
CREATE INDEX ix_items_posting ON posting_items(posting_number);
"""


def _fingerprint(files: Sequence[str], extra_category_map: Optional[dict] = None) -> str:
    """内容指纹：解析器版本 + 文件路径 + 大小 + mtime + **类别映射扩展**。

    类别映射会改变落库的 `category` 列，所以必须进指纹 ——
    否则同一个目录下换了 `extra_category_map` 会命中旧缓存，改了等于没改。
    """
    h = hashlib.sha1()
    h.update(('v%d' % PARSER_VERSION).encode('ascii'))
    for p in sorted(files):
        st = os.stat(p)
        h.update(os.path.abspath(p).encode('utf-8'))
        h.update(str(st.st_size).encode('ascii'))
        h.update(str(st.st_mtime_ns).encode('ascii'))
    if extra_category_map:
        for key in sorted(extra_category_map, key=lambda k: (str(k[0]), str(k[1]))):
            h.update(('%s\x1f%s\x1f%s\x1e' % (key[0], key[1],
                                              extra_category_map[key])).encode('utf-8'))
    return h.hexdigest()


class ExcelSource:
    """从 OZON 的两份导出文件读数据（只读）。

    典型用法::

        with ExcelSource() as src:                     # 默认 D:\\Downloads
            snap = src.settlement_snapshot('FAKE-6948C859')
            print(snap.direct_net_rub)

    `alias` 只用于把 `store_alias` 原样回给聚合查询 —— core 内部不依赖它。
    """

    def __init__(self,
                 accrual_dir: Optional[str] = DEFAULT_EXPORT_DIR,
                 accrual_files: Optional[Sequence[str]] = None,
                 postings_csv: Optional[str] = None,
                 alias: Optional[str] = None,
                 require_postings: bool = True,
                 exchange_rate_mode: str = 'none',
                 purchase_cost_by_offer: Optional[Dict[str, object]] = None,
                 extra_category_map: Optional[Dict[Tuple[str, str], str]] = None,
                 include_unmatched_accruals: bool = True,
                 cache: bool = True,
                 cache_dir: Optional[str] = None,
                 as_of: Optional[datetime] = None):
        """
        :param accrual_dir: 应计报表所在目录（默认 `D:\\Downloads`）
        :param accrual_files: 显式指定应计报表文件；给了它就不扫目录
        :param postings_csv: 发货单 CSV 路径；默认取应计报表同目录下的 `postings.csv`
        :param require_postings: 缺 postings.csv 时是否报错。False 时降级：
            只拿财务流水，`overdue_count` 与订单状态不可用。
        :param exchange_rate_mode:
            * `'none'`（默认）—— 不给汇率，§7.1 会如实判 `missing_exchange_rate`；
              应计报表里确实没有结算汇率，这是**正确行为**。
            * `'implied_buyer_payment'` —— 用 `已由买家支付 / 发货的金额` 推。
              实测与生产库结算汇率在 **93.8%** 的订单上完全相等，
              但 6.2% 会正好差一个整数倍（多商品单买家实付不全）。
              **这是临时方案，会算错 6.2% 的订单**，务必业务确认后再开。
        :param purchase_cost_by_offer: 货号 → 采购成本（CNY）。两份导出都没有这个字段，
            想让 §7.1 算得出来就必须从外部注入（例如采购成本模板）。
        :param extra_category_map: 追加/覆盖 (分组, 类型) → 类别 的映射。
        :param include_unmatched_accruals: 归属不到任何发货单的应计是否单独成单。
            默认 True —— **不静默丢弃**。
        :param cache: 是否使用磁盘缓存（按内容指纹复用解析结果）。
        :param as_of: 逾期判定的「现在」。默认取 postings.csv 的修改时间 ——
            导入器是静态快照，用系统当前时间会让逾期数随日子漂移。
        """
        if exchange_rate_mode not in ('none', 'implied_buyer_payment'):
            raise ValueError('exchange_rate_mode 只支持 none / implied_buyer_payment，'
                             '收到 %r' % (exchange_rate_mode,))

        self.notes: List[str] = []
        self.alias = alias or 'excel'
        self.exchange_rate_mode = exchange_rate_mode
        self.include_unmatched_accruals = bool(include_unmatched_accruals)
        self._extra_map = dict(extra_category_map or {})
        self._purchase_cost: Dict[str, Decimal] = {}
        for k, v in (purchase_cost_by_offer or {}).items():
            d = to_decimal(v)
            if d is not None:
                self._purchase_cost[str(k).strip()] = d

        files = self._resolve_files(accrual_dir, accrual_files, postings_csv,
                                    require_postings)
        self.accrual_files: Tuple[str, ...] = tuple(files['accruals'])
        self.postings_csv: Optional[str] = files['postings']
        self.export_dir = os.path.dirname(self.accrual_files[0])
        self.as_of = as_of or self._default_as_of()
        self.cache_path: Optional[str] = None
        self._conn = self._open_db(cache, cache_dir)

    # ── 生命周期 ─────────────────────────────────────────────────
    def close(self):
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - 关连接失败不该影响已经算好的结果
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 元信息 ───────────────────────────────────────────────────
    @property
    def source_kind(self) -> str:
        return 'excel'

    def raw_status(self, posting_number: str) -> Optional[str]:
        """发货单的**原始中文状态**（`Posting.status` 下发的是 OZON 状态码）。"""
        r = self._conn.execute('SELECT status_raw FROM postings WHERE posting_number=?',
                               (posting_number,)).fetchone()
        return None if r is None else r[0]

    def unmapped_accrual_types(self) -> Sequence[dict]:
        """未识别的 (分组, 类型) 清单 —— 这些行全部已落 `OTHER`，这里供报表发现。"""
        return [dict(r) for r in self._conn.execute(
            'SELECT group_ru, type_ru, rows, sample_accrual_id FROM unmapped_types '
            'ORDER BY rows DESC')]

    def accrual_type_census(self) -> Sequence[dict]:
        """(分组, 类型) → 行数 / 类别 / 金额合计（卢布）。

        金额在 Python 侧用 Decimal 累加 —— SQL 的 `SUM` 会把 TEXT 变 REAL，
        尾差会进到金额里（与 `sqlite_source.py` 的取舍一致）。
        """
        buckets: Dict[tuple, dict] = {}
        for r in self._conn.execute(
                'SELECT group_ru, type_ru, category, category_known, amount_rub FROM accruals'):
            key = (r['group_ru'], r['type_ru'], r['category'], r['category_known'])
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {'group_ru': r['group_ru'], 'type_ru': r['type_ru'],
                                    'category': r['category'],
                                    'category_known': bool(r['category_known']),
                                    'rows': 0, 'amounts': []}
            b['rows'] += 1
            b['amounts'].append(to_decimal(r['amount_rub']))
        out = []
        for b in buckets.values():
            amounts = b.pop('amounts')
            b['amount_rub'] = sum_amounts(amounts)
            out.append(b)
        out.sort(key=lambda x: (-x['rows'], x['group_ru'] or '', x['type_ru'] or ''))
        return out

    def attribution_stats(self) -> dict:
        """归属统计 —— 报告里用来证明「没有静默丢弃」。"""
        stats = {}
        for r in self._conn.execute(
                'SELECT attribution, count(*) AS n, count(DISTINCT posting_number) AS pns '
                'FROM accruals GROUP BY attribution'):
            stats[r['attribution'] or 'unknown'] = {'rows': r['n'], 'postings': r['pns']}
        stats['_accrual_rows_total'] = self._one('SELECT count(*) FROM accruals')
        stats['_postings_csv_rows'] = self._one('SELECT count(*) FROM postings')
        stats['_distinct_posting_keys'] = self._one(
            'SELECT count(DISTINCT posting_number) FROM accruals')
        return stats

    def unidentified_accruals(self) -> dict:
        """`ID начисления` 为空、因而无法归到任何订单上的应计（实测 3 行）。

        它们**留在库里、计入取值统计**，只是不参与逐单/窗口汇总 ——
        这里把行数与金额显式报出来，别让这部分钱无声消失。
        """
        rows = self._conn.execute(
            "SELECT operation_id, accrual_date, group_ru, type_ru, category, amount_rub "
            "FROM accruals WHERE attribution='no_accrual_id' ORDER BY row_no").fetchall()
        return {
            'rows': len(rows),
            'amount_rub': sum_amounts([to_decimal(r['amount_rub']) for r in rows]),
            'detail': [dict(r) for r in rows],
        }

    def _one(self, sql, args=()):
        r = self._conn.execute(sql, args).fetchone()
        return None if r is None else r[0]

    # ── 文件定位与解析 ───────────────────────────────────────────
    def _resolve_files(self, accrual_dir, accrual_files, postings_csv, require_postings):
        accruals = [p for p in (accrual_files or []) if p]
        if not accruals:
            accrual_dir = accrual_dir or DEFAULT_EXPORT_DIR
            pattern = os.path.join(accrual_dir, DEFAULT_ACCRUAL_GLOB)
            accruals = sorted(glob.glob(pattern))
            if not accruals:
                raise FileNotFoundError(
                    '找不到应计报表（%s）。\n'
                    '  期望路径：%s\n'
                    '  请到 OZON 后台 → 财务 → 应计报表，按需要的日期区间导出，\n'
                    '  放进 %s 目录，文件名形如 '
                    '«Отчет по начислениям_01.09.2026-11.09.2026.xlsx»。\n'
                    '  详见 docs/关于OZON导出模板.md。'
                    % (DEFAULT_ACCRUAL_GLOB, pattern, accrual_dir))
        missing = [p for p in accruals if not os.path.isfile(p)]
        if missing:
            raise FileNotFoundError('应计报表不存在：%s' % '、'.join(missing))

        csv_path = postings_csv
        if csv_path is None:
            base = os.path.dirname(accruals[0]) if accruals else (
                accrual_dir or DEFAULT_EXPORT_DIR)
            csv_path = os.path.join(base, DEFAULT_POSTINGS_CSV)
        if not os.path.isfile(csv_path):
            if require_postings:
                raise FileNotFoundError(
                    '找不到发货单导出 postings.csv。\n'
                    '  期望路径：%s\n'
                    '  请到 OZON 后台 → 订单 → 发货单，导出 CSV，另存为 %s\n'
                    '  （分号分隔、UTF-8 BOM 的中文表头，保持默认即可）。\n'
                    '  这份文件提供订单状态、发货截止时间与人民币销售额；\n'
                    '  没有它就算不出逾期，也判不了预估利润的适用范围。\n'
                    '  详见 docs/关于OZON导出模板.md。'
                    % (csv_path, DEFAULT_POSTINGS_CSV))
            self.notes.append(
                '缺少 postings.csv（%s）：订单状态、发货截止时间与人民币销售额不可用，'
                'overdue_count 返回 available=False。' % csv_path)
            csv_path = None
        return {'accruals': accruals, 'postings': csv_path}

    def _default_as_of(self) -> datetime:
        """静态快照的「现在」：postings.csv 的修改时间；没有就用应计报表的。"""
        ref = self.postings_csv or (self.accrual_files[0] if self.accrual_files else None)
        if ref and os.path.isfile(ref):
            return datetime.fromtimestamp(os.path.getmtime(ref), tz=CN_TZ)
        return datetime.now(tz=CN_TZ)

    def _open_db(self, cache: bool, cache_dir: Optional[str]) -> sqlite3.Connection:
        """打开解析结果库：优先复用磁盘缓存，否则重建。"""
        if cache:
            files = list(self.accrual_files)
            if self.postings_csv:
                files.append(self.postings_csv)
            try:
                key = _fingerprint(files, self._extra_map)
            except OSError:
                key = None
            if key:
                root = cache_dir or os.path.join(tempfile.gettempdir(),
                                                 'ozon_finance_excel_cache')
                path = os.path.join(root, 'excel_%s.db' % key)
                tmp = '%s.tmp.%d' % (path, os.getpid())
                try:
                    os.makedirs(root, exist_ok=True)
                    if not os.path.isfile(path):
                        conn = self._build(tmp, files)
                        conn.close()
                        os.replace(tmp, path)
                    conn = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'),
                                           uri=True)
                    conn.row_factory = sqlite3.Row
                    if conn.execute('SELECT count(*) FROM accruals').fetchone()[0] > 0:
                        self.cache_path = path
                        return conn
                    conn.close()
                except (sqlite3.Error, OSError, ValueError) as exc:
                    # 缓存故障不该变成请求故障：清掉半成品，退回内存库重建
                    self.notes.append('磁盘缓存不可用（%s），改用内存库重建。' % exc)
                    for p in (tmp, path):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
        files = list(self.accrual_files)
        if self.postings_csv:
            files.append(self.postings_csv)
        return self._build(':memory:', files)

    def _build(self, target: str, files: Sequence[str]) -> sqlite3.Connection:
        """解析所有文件并落库。`target` 可以是 `:memory:` 或一个文件路径。

        失败时把连接关掉再抛 —— 否则「缓存建到一半就出错」会留下未关闭的
        文件句柄（Windows 上还会让 `os.remove` 失败）。
        """
        conn = sqlite3.connect(target)
        try:
            return self._build_into(conn)
        except BaseException:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    def _build_into(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.execute('INSERT INTO meta(k, v) VALUES (?, ?)',
                     ('parser_version', str(PARSER_VERSION)))
        conn.execute('INSERT INTO meta(k, v) VALUES (?, ?)',
                     ('built_at', datetime.now(tz=CN_TZ).isoformat()))

        total = 0
        unmapped: Dict[tuple, dict] = {}
        for path in self.accrual_files:
            tag = _file_tag(path)
            seq = 0
            payload = []

            def flush(rows):
                if rows:
                    conn.executemany(_INSERT_ACCRUAL, rows)

            for rec in iter_accrual_rows(path):
                seq += 1
                base = _accrual_tuple(rec, seq, tag)
                category, known = self._classify(base[4], base[5])
                if not known:
                    key = (base[4], base[5])
                    u = unmapped.get(key)
                    if u is None:
                        u = unmapped[key] = {'rows': 0, 'sample': base[2]}
                    u['rows'] += 1
                payload.append(base[:6] + (category, 1 if known else 0) + base[6:]
                               + (os.path.basename(path),))
                if len(payload) >= BATCH:
                    flush(payload)
                    payload = []
            flush(payload)
            total += seq
            conn.execute('INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?)',
                         (os.path.abspath(path), 'accrual', os.path.getsize(path),
                          os.stat(path).st_mtime_ns, seq))
        for (g, t), u in unmapped.items():
            conn.execute('INSERT INTO unmapped_types VALUES (?,?,?,?)',
                         (g, t, u['rows'], u['sample']))

        if self.postings_csv:
            agg, items = read_postings_csv(self.postings_csv)
            conn.executemany(_INSERT_POSTING, agg)
            conn.executemany(_INSERT_ITEM, items)
            conn.execute('INSERT OR IGNORE INTO order_items(order_key, posting_number) '
                         'SELECT DISTINCT order_key, posting_number FROM postings '
                         'WHERE order_key IS NOT NULL')
            conn.execute('INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?)',
                         (os.path.abspath(self.postings_csv), 'postings',
                          os.path.getsize(self.postings_csv),
                          os.stat(self.postings_csv).st_mtime_ns, len(agg)))

        self._attribute(conn)
        conn.executescript(_INDEXES)
        conn.execute('INSERT INTO meta(k, v) VALUES (?, ?)', ('accrual_rows', str(total)))
        conn.commit()
        return conn

    @staticmethod
    def _attribute(conn: sqlite3.Connection):
        """把应计归属到发货单号（写入 `posting_number` / `attribution`）。

        五类归属：
          * `exact`          —— `ID начисления` 就是发货单号
          * `base_unique`    —— 是基单号，且只对应一个发货单号 → 归属过去
          * `base_ambiguous` —— 基单号对应多个发货单号 → **不摊分**，留在基单号名下
          * `orphan`         —— 两份文件里都查不到发件单 → 留在自己的编号名下
          * `no_accrual_id`  —— 报表里 `ID начисления` 本身就是空的（实测 3 行）
                                → 留在库里、可查、计入统计，但**不参与逐单汇总**
                                （连单号都没有，归不到任何订单上）

        后三类只有在 `include_unmatched_accruals=False` 时才排除出
        `list_posting_numbers()`；默认全部保留，**不静默丢弃**。
        """
        conn.execute("UPDATE accruals SET posting_number = accrual_id, attribution = 'exact' "
                     "WHERE accrual_id IN (SELECT posting_number FROM postings)")
        conn.execute("""
            UPDATE accruals
               SET posting_number = (SELECT oi.posting_number FROM order_items oi
                                      WHERE oi.order_key = accruals.accrual_id),
                   attribution = 'base_unique'
             WHERE posting_number IS NULL
               AND accrual_id IS NOT NULL
               AND (SELECT count(*) FROM order_items oi
                     WHERE oi.order_key = accruals.accrual_id) = 1
        """)
        conn.execute("""
            UPDATE accruals
               SET posting_number = accrual_id,
                   attribution = CASE
                       WHEN EXISTS (SELECT 1 FROM order_items oi
                                     WHERE oi.order_key = accruals.accrual_id)
                       THEN 'base_ambiguous' ELSE 'orphan' END
             WHERE posting_number IS NULL AND accrual_id IS NOT NULL
        """)
        conn.execute("UPDATE accruals SET attribution = 'no_accrual_id' "
                     "WHERE accrual_id IS NULL AND posting_number IS NULL")

    def _classify(self, group_ru, type_ru):
        if self._extra_map:
            hit = self._extra_map.get(((group_ru or '').strip(), (type_ru or '').strip()))
            if hit is not None:
                return hit, True
        return classify_accrual(group_ru, type_ru)

    # ── ProfitDataSource：逐单输入 ───────────────────────────────
    def list_posting_numbers(self) -> Sequence[str]:
        # posting_number IS NOT NULL 是必须的：`ID начисления` 为空的行会留在
        # accruals 里（attribution='no_accrual_id'），它们的 posting_number 是 NULL，
        # 不能当成一个「单号」下发 —— 否则调用方会拿到一个 None 形式的订单。
        if self.include_unmatched_accruals:
            if self.postings_csv:
                sql = ('SELECT posting_number FROM postings '
                       'UNION SELECT DISTINCT posting_number FROM accruals '
                       'WHERE posting_number IS NOT NULL '
                       'ORDER BY posting_number')
            else:
                sql = ('SELECT DISTINCT posting_number FROM accruals '
                       'WHERE posting_number IS NOT NULL ORDER BY posting_number')
        else:
            if not self.postings_csv:
                sql = ("SELECT DISTINCT posting_number FROM accruals "
                       "WHERE posting_number IS NOT NULL "
                       "AND attribution IN ('exact','base_unique') ORDER BY posting_number")
            else:
                sql = 'SELECT posting_number FROM postings ORDER BY posting_number'
        return [r[0] for r in self._conn.execute(sql)]

    def settlement_snapshot(self, posting_number: str) -> Optional[SettlementSnapshot]:
        rows = self._conn.execute(
            'SELECT operation_id, amount_rub, accrual_date, category FROM accruals '
            'WHERE posting_number=? ORDER BY row_no', (posting_number,)).fetchall()
        if not rows:
            return None
        direct_net = sum_amounts([to_decimal(r['amount_rub']) for r in rows])
        sales = sum_amounts([to_decimal(r['amount_rub']) for r in rows
                             if r['category'] == CATEGORY_SALE])
        dates = [r['accrual_date'] for r in rows if r['accrual_date']]
        return SettlementSnapshot(
            posting_number=posting_number,
            settlement_date=max(dates) if dates else None,
            # 应计报表没有「结算状态」字段：有应计即视为已结算 ——
            # 只有已结算的业务才会出现在应计报表里，这不是杜撰。
            state='locked',
            direct_net_rub=direct_net,
            settled_sales_rub=sales,
            exchange_rate_rub_per_cny=self._exchange_rate(posting_number),
            purchase_cost_cny=self._purchase_cost_for(posting_number),
            operation_ids=tuple(r['operation_id'] for r in rows),
            unknown_reason=None,
        )

    def operations(self, posting_number: str) -> Sequence[Operation]:
        return self.operations_for(posting_number, None)

    def operations_for(self, posting_number: str,
                       ids: Optional[Sequence[str]]) -> Sequence[Operation]:
        """按 operation_id 集合取流水。

        与 `sqlite_source.py` 一样必须支持**任意 id 集合**：基单号上的应计被归属到
        条目单之后，id 与 posting_number 不再一一对应，只按 posting_number 过滤会漏。
        """
        if ids is None:
            rows = self._conn.execute(
                'SELECT operation_id, amount_rub, category, accrual_date FROM accruals '
                'WHERE posting_number=? ORDER BY row_no', (posting_number,)).fetchall()
        else:
            ids = [str(i) for i in ids]
            if not ids:
                return ()
            rows = []
            # SQLite 的绑定变量上限是 999，分批查（900 一批）
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                rows.extend(self._conn.execute(
                    'SELECT operation_id, amount_rub, category, accrual_date FROM accruals '
                    'WHERE operation_id IN (%s)' % ','.join('?' * len(chunk)),
                    tuple(chunk)).fetchall())
        return tuple(Operation(operation_id=r['operation_id'],
                               amount_rub=to_decimal(r['amount_rub']),
                               operation_category=r['category'],
                               occurred_at=r['accrual_date'])
                     for r in rows)

    def linked_operation_ids(self, posting_number: str) -> Sequence[str]:
        """Excel 源没有独立的事实表，权威操作集就是归属到该单的应计集合。"""
        return tuple(r[0] for r in self._conn.execute(
            'SELECT operation_id FROM accruals WHERE posting_number=? ORDER BY row_no',
            (posting_number,)))

    def posting(self, posting_number: str) -> Optional[Posting]:
        r = self._conn.execute('SELECT * FROM postings WHERE posting_number=?',
                               (posting_number,)).fetchone()
        if r is None:
            return None
        return Posting(
            posting_number=posting_number,
            status=r['status'],
            revenue_cny=to_decimal(r['revenue_cny']),
            # 采购成本两份导出里都没有，只能由调用方按货号注入。
            purchase_cost_cny=self._purchase_cost_for(posting_number),
            # `运费` 列实测 9075 行全空；`实际计费` 也全空。
            # 给 None 而不是 0：让 §7.2 如实报 missing_purchase_cost，
            # 而不是拿 0 顶替物流费/平台费算出一个偏高的预估利润。
            logistics_cost_cny=None,
            estimated_platform_fee_cny=None,
        )

    # ── 参照：Excel 源没有「现有系统存下的结果」────────────────
    def reference_actual(self, posting_number: str) -> dict:
        """Excel 源没有旧系统存下的结果 → 空 dict（**不伪造基准**）。

        回归对账必须用 `SqliteSource`；Excel 导入器的口径一致性检查见
        `core/tools/verify_excel_vs_sqlite.py`（拿生产库当真值比对）。
        """
        return {}

    def reference_estimated(self, posting_number: str) -> Optional[str]:
        return None

    def reference_completion(self) -> dict:
        return {}

    # ── 只读聚合 ─────────────────────────────────────────────────
    def data_cutoff(self, store_alias: str) -> DataCutoff:
        """导入文件的时间边界。

        `cutoff`：应计数据里的最后一天 + 各导出文件的 mtime，取最大者；
        它回答「这份导入是什么时候做的」，**只用于展示**。

        `window_end`：最后一天应计日。这一条才是窗口右端 —— 文件 mtime
        是「下载/导入动作」的时间，与数据覆盖到哪天无关：今天导入一份只
        覆盖到 09-11 的报表，若拿 mtime 当窗口右端，`days=7` 的窗口
        （今天往前 7 天）里一行业务数据都没有。与 sqlite 源同一个道理。
        """
        cands = {}
        max_day = None
        r = self._conn.execute('SELECT max(accrual_date) FROM accruals').fetchone()
        if r is not None and r[0]:
            cands['accruals.max(Дата начисления)'] = r[0]
            max_day = _parse_day(r[0])
        for row in self._conn.execute('SELECT path, mtime_ns FROM source_files'):
            cands['file mtime: %s' % os.path.basename(row['path'])] = datetime.fromtimestamp(
                row['mtime_ns'] / 1e9, tz=CN_TZ).isoformat()
        parsed = {}
        for k, v in cands.items():
            dt = _parse_dt(v)
            if dt is not None:
                parsed[k] = dt
        if not parsed:
            return DataCutoff(None, 'none', cands, max_day)
        source = max(parsed, key=lambda k: parsed[k])
        return DataCutoff(parsed[source], source, cands, max_day)

    def _window_heads(self, start_date: str, end_date: str,
                      limit: Optional[int], offset: int):
        """窗口内的 (发货单号, 结算日)。

        结算日 = 该单**最后一条应计的日期**。与协议一致：窗口由结算日界定，
        且一个订单只落在一个窗口里（用首次应计日会让同一单在不同窗口重复出现）。
        """
        sql = """
            SELECT posting_number, max(accrual_date) AS settlement_date
              FROM accruals
             WHERE accrual_date IS NOT NULL AND posting_number IS NOT NULL
             GROUP BY posting_number
            HAVING settlement_date BETWEEN ? AND ?
             ORDER BY settlement_date DESC, posting_number DESC
        """
        args: List = [start_date, end_date]
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            args += [int(limit), int(offset)]
        return self._conn.execute(sql, tuple(args)).fetchall()

    def orders_for_period(self, store_alias: str, start_date: str, end_date: str,
                          limit: Optional[int] = None,
                          offset: int = 0) -> Sequence[PeriodOrderRow]:
        """窗口内的订单（原始数据，按结算日倒序）。

        实现方式刻意是**批量取数**而不是逐单 N+1：一次取窗口内全部应计、
        一次取全部发货单、一次取全部商品明细，然后在 Python 里组装。
        （逐单 4 条 SQL 在 1400 单上要跑 5600 次往返，看板会肉眼可见地卡。）
        """
        heads = self._window_heads(start_date, end_date, limit, offset)
        if not heads:
            return ()
        pns = [r['posting_number'] for r in heads]
        settlement = {r['posting_number']: r['settlement_date'] for r in heads}

        ops_by_pn: Dict[str, List[sqlite3.Row]] = {}
        for chunk in _chunks(pns, 900):
            for r in self._conn.execute(
                    'SELECT posting_number, operation_id, amount_rub, category, accrual_date '
                    'FROM accruals WHERE posting_number IN (%s) ORDER BY row_no'
                    % ','.join('?' * len(chunk)), tuple(chunk)):
                ops_by_pn.setdefault(r['posting_number'], []).append(r)

        postings_by_pn = {}
        for chunk in _chunks(pns, 900):
            for r in self._conn.execute(
                    'SELECT * FROM postings WHERE posting_number IN (%s)'
                    % ','.join('?' * len(chunk)), tuple(chunk)):
                postings_by_pn[r['posting_number']] = r

        offers_by_pn: Dict[str, List[tuple]] = {}
        for chunk in _chunks(pns, 900):
            for r in self._conn.execute(
                    'SELECT posting_number, offer_id, quantity FROM posting_items '
                    'WHERE posting_number IN (%s)' % ','.join('?' * len(chunk)),
                    tuple(chunk)):
                offers_by_pn.setdefault(r['posting_number'], []).append(
                    (r['offer_id'], r['quantity']))

        out = []
        for pn in pns:
            rows = ops_by_pn.get(pn, [])
            if not rows:
                continue
            direct_net = sum_amounts([to_decimal(r['amount_rub']) for r in rows])
            sales = sum_amounts([to_decimal(r['amount_rub']) for r in rows
                                 if r['category'] == CATEGORY_SALE])
            snap = SettlementSnapshot(
                posting_number=pn,
                settlement_date=settlement.get(pn),
                state='locked',
                direct_net_rub=direct_net,
                settled_sales_rub=sales,
                exchange_rate_rub_per_cny=self._rate_from_row(postings_by_pn.get(pn)),
                purchase_cost_cny=self._cost_from_items(offers_by_pn.get(pn)),
                operation_ids=tuple(r['operation_id'] for r in rows),
                unknown_reason=None,
            )
            pr = postings_by_pn.get(pn)
            posting = None if pr is None else Posting(
                posting_number=pn, status=pr['status'],
                revenue_cny=to_decimal(pr['revenue_cny']),
                purchase_cost_cny=self._cost_from_items(offers_by_pn.get(pn)),
                logistics_cost_cny=None, estimated_platform_fee_cny=None)
            out.append(PeriodOrderRow(
                posting_number=pn,
                settlement_date=settlement.get(pn),
                snapshot=snap,
                posting=posting,
                expected_operation_ids=snap.operation_ids,
                operations=tuple(Operation(operation_id=r['operation_id'],
                                           amount_rub=to_decimal(r['amount_rub']),
                                           operation_category=r['category'],
                                           occurred_at=r['accrual_date'])
                                 for r in rows),
            ))
        return tuple(out)

    def amounts_for_period(self, store_alias: str, start_date: str,
                           end_date: str) -> PeriodAmounts:
        """窗口合计。

        这里**不自己写口径**：逐单取数之后一律交给
        `core.domain.profit.evaluate_actual` / `evaluate_estimated`，
        与 `orders_for_period` 派生的看板数字必然自洽
        （`api/tests/test_api.py` 对 SqliteSource 正是这么把关的）。

        因为应计报表里没有结算汇率、也没有采购成本，默认配置下
        `actual_profit_cny` / `estimated_profit_cny` 会是 `None` ——
        **不写 0 冒充**（PRD §7.1）。口径约定与 `base.py` 一致：
        金额只累加**判定为完整**的订单，缺失值跳过而不是用 0 顶替。

        代价是这个「廉价路径」在 Excel 源上并不廉价（≈1.8 秒 / 1400 单）：
        它复用了 `orders_for_period` 的取数与领域层判定。刻意如此 ——
        看板的 `totals` 与 `orders` 必须出自同一批数，
        再写一份「只为快」的聚合口径必然与逐单路径漂移
        （`api/README.md` 记录的 44% 那次事故就是这么来的）。
        """
        rows = self.orders_for_period(store_alias, start_date, end_date)
        actual, estimated, costs = [], [], []
        complete = 0
        for r in rows:
            res = evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
            if res.complete:
                complete += 1
                actual.append(res.actual_profit_cny)
                costs.append(r.snapshot.purchase_cost_cny)
            if r.posting is not None:
                est = evaluate_estimated(r.posting)
                if est.complete:
                    estimated.append(est.estimated_profit_cny)
        direct_cny = []
        for r in rows:
            rate = r.snapshot.exchange_rate_rub_per_cny if r.snapshot else None
            if rate is not None and rate_is_valid(rate) and r.snapshot.direct_net_rub is not None:
                direct_cny.append(quantize_cny(r.snapshot.direct_net_rub / rate))
        return PeriodAmounts(
            total_order_count=len(rows),
            complete_order_count=complete,
            actual_profit_cny=sum_amounts(actual) if actual else None,
            estimated_profit_cny=sum_amounts(estimated) if estimated else None,
            direct_net_cny=sum_amounts(direct_cny) if direct_cny else None,
            purchase_cost_cny=sum_amounts(costs) if costs else None,
            platform_fee_cny=None,
            logistics_cost_cny=None,
        )

    def daily_amounts(self, store_alias: str, start_date: str,
                      end_date: str) -> Sequence[DailyAmounts]:
        """按天汇总。只返回**有数据的日期**，不补 0 日期行。

        口径与 `orders_for_period` 完全一致（同一天 = 结算日），
        金额同样经领域层算 —— 否则「按天加起来 ≠ 总计」。
        """
        bucket: Dict[str, dict] = {}
        for r in self.orders_for_period(store_alias, start_date, end_date):
            b = bucket.setdefault(r.settlement_date or '',
                                  {'n': 0, 'c': 0, 'a': [], 'e': []})
            b['n'] += 1
            res = evaluate_actual(r.snapshot, r.operations, r.expected_operation_ids)
            if res.complete:
                b['c'] += 1
                b['a'].append(res.actual_profit_cny)
            if r.posting is not None:
                est = evaluate_estimated(r.posting)
                if est.complete:
                    b['e'].append(est.estimated_profit_cny)
        return tuple(
            DailyAmounts(date=d,
                         actual_profit_cny=sum_amounts(v['a']) if v['a'] else None,
                         estimated_profit_cny=sum_amounts(v['e']) if v['e'] else None,
                         order_count=v['n'], complete_order_count=v['c'])
            for d, v in sorted(bucket.items()) if d)

    def sku_detail_for_period(self, store_alias: str, start_date: str,
                              end_date: str) -> SkuDetail:
        """窗口内逐 SKU（按货号）的取数与归属结果（Excel 导入器版本）。

        **应计报表与 postings.csv 都自带 SKU 分组键**，所以这里不需要任何分摊：

        * 收入：postings.csv 一行一个 SKU，`发货的金额` 就是**该行合计** ——
          它已经逐行落在 `posting_items.revenue_cny` 上（见 `read_postings_csv`），
          直接取用即可，等于数据自己给出的按行收入。
        * 采购成本：两份导出里都没有，只能由调用方按货号注入
          （`purchase_cost_by_offer`）。按 `数量 × 单件成本` 拆到行上，
          并要求逐行加总**恰好等于**整单成本（`_cost_from_items` 算的就是这个和）；
          对不上就整单进未归属桶。
        * 物流费 / 平台佣金：postings.csv 的 `运费` / `实际计费` 两列实测全空，
          `_posting_row` 里给的就是 None —— 没有数据就没有数据，不补 0。
        * 财务流水（§7.1）：应计报表的应计行按 `ID начисления` 归属到订单之后
          是**订单级**的，无法再按货号拆开；所以只有单货号订单能整单归属，
          多货号订单的实际利润进未归属桶。
        """
        rows = self.orders_for_period(store_alias, start_date, end_date)
        if not rows:
            return SkuDetail()

        pns = [r.posting_number for r in rows]
        items_by_pn = self._items_for_postings(pns)

        sku_rows: list = []
        unattributed: list = []
        actual_unattributed: list = []
        for row in rows:
            items = items_by_pn.get(row.posting_number, ())
            # postings.csv 的 `发货的金额` 本身就是行合计（`数量 × 最高价格`），
            # 已经逐行落在 posting_items.revenue_cny 上，直接取用、不再乘数量。
            line_revenues = {str(it[0]): it[4] for it in items if it[4] is not None}
            posting = row.posting
            snapshot = row.snapshot
            line_rows, records = attribute_order_lines(
                posting_number=row.posting_number,
                settlement_date=row.settlement_date,
                items=[it[:4] for it in items],
                revenue_cny=None if posting is None else posting.revenue_cny,
                purchase_cost_cny=None if snapshot is None else snapshot.purchase_cost_cny,
                unit_cost_by_offer=self._purchase_cost,
                line_revenues=line_revenues,
                logistics_cost_cny=None if posting is None else posting.logistics_cost_cny,
                platform_fee_cny=(
                    None if posting is None else posting.estimated_platform_fee_cny),
                snapshot=snapshot,
                operations=row.operations,
                expected_operation_ids=row.expected_operation_ids,
            )
            sku_rows.extend(line_rows)
            unattributed.extend(records)
            if not line_rows or not line_rows[0].actual_attributable:
                # 实际利润整单归不出去（多货号订单，或没有商品明细行）→ 未归属
                actual_unattributed.append(row)

        return SkuDetail(
            rows=tuple(sku_rows),
            unattributed=UnattributedAmounts(
                posting_count=len({rec.posting_number for rec in unattributed
                                   if rec.posting_number}),
                records=tuple(unattributed),
            ),
            actual_unattributed=tuple(actual_unattributed),
            order_count=len(rows),
        )

    def _items_for_postings(self, posting_numbers: Sequence[str]) -> dict:
        """一次取齐一批发货单的商品明细行。

        返回 5 元组 `(货号, SKU, 商品名, 数量, 按行收入)` —— **就是逐 SKU 的
        分组键**：postings.csv 一行一个 SKU，`发货的金额` 是该行合计，
        `read_postings_csv` 已经原样落进 `posting_items.revenue_cny`。
        （生产库的 `posting_items` 没有这一列，那边改用
        `postings.raw_json.products[].price` 求单价。）
        """
        out: dict = {}
        pns = list(posting_numbers)
        for i in range(0, len(pns), 900):
            chunk = pns[i:i + 900]
            for r in self._conn.execute(
                    'SELECT posting_number, offer_id, sku, product_name, quantity, '
                    'revenue_cny FROM posting_items WHERE posting_number IN (%s) '
                    'ORDER BY posting_number, offer_id' % ','.join('?' * len(chunk)),
                    tuple(chunk)):
                out.setdefault(r['posting_number'], []).append(
                    (r['offer_id'], r['sku'], r['product_name'], r['quantity'],
                     to_decimal(r['revenue_cny'])))
        return {pn: tuple(v) for pn, v in out.items()}

    def overdue_count(self, store_alias: str) -> OverdueInfo:
        """§7.6 逾期单数：`状态 ∈ {待备货, 等待发运}` 且 `as_of > 不逾期的发运日期`。

        `as_of` 默认取 postings.csv 的修改时间 —— 导入器是**静态快照**，
        用系统当前时间会让同一个文件的逾期数随日子一直涨，那不是数据在变。
        返回里如实带出 `as_of` 与判定口径，别让调用方以为这是实时值。
        """
        if not self.postings_csv:
            return OverdueInfo(store_alias, None, 'postings.csv', available=False,
                               note='缺少 postings.csv：没有订单状态与发货截止时间，'
                                    '逾期口径不可用（**不返回 0 冒充**）')
        stamp = self.as_of.strftime('%Y-%m-%d %H:%M:%S')
        n = self._conn.execute("""
            SELECT count(*) FROM postings
             WHERE status IN (%s)
               AND shipment_deadline IS NOT NULL
               AND shipment_deadline <> ''
               AND shipment_deadline < ?
        """ % ','.join('?' * len(OVERDUE_STATUSES)),
            tuple(sorted(OVERDUE_STATUSES)) + (stamp,)).fetchone()[0]
        return OverdueInfo(
            store_alias, int(n), 'postings.csv(status, 不逾期的发运日期)',
            as_of=self.as_of.isoformat(),
            note='静态快照口径：判定时刻取 postings.csv 的修改时间（%s）；'
                 '状态 %s 且截止时间早于该时刻即算逾期。'
                 % (stamp, '/'.join(sorted(OVERDUE_STATUSES))),
            available=True)

    # ── 内部 ─────────────────────────────────────────────────────
    def _exchange_rate(self, posting_number: str) -> Optional[Decimal]:
        if self.exchange_rate_mode != 'implied_buyer_payment':
            return None
        r = self._conn.execute(
            'SELECT rate_paid_sum, rate_value_sum, rate_rows FROM postings '
            'WHERE posting_number=?', (posting_number,)).fetchone()
        return self._rate_from_row(r)

    def _rate_from_row(self, r) -> Optional[Decimal]:
        """按当前 `exchange_rate_mode` 决定要不要给汇率。

        默认 `'none'` 一律返回 None —— 这是刻意让 §7.1 报 `missing_exchange_rate`
        而不是拿一个「看起来差不多」的推算值去算利润。
        """
        if self.exchange_rate_mode != 'implied_buyer_payment':
            return None
        if r is None or not r['rate_rows']:
            return None
        value = to_decimal(r['rate_value_sum'])
        paid = to_decimal(r['rate_paid_sum'])
        if value is None or paid is None or value <= 0 or paid <= 0:
            return None
        return paid / value

    def _purchase_cost_for(self, posting_number: str) -> Optional[Decimal]:
        rows = self._conn.execute(
            'SELECT offer_id, quantity FROM posting_items WHERE posting_number=?',
            (posting_number,)).fetchall()
        return self._cost_from_items([(r['offer_id'], r['quantity']) for r in rows])

    def _cost_from_items(self, items) -> Optional[Decimal]:
        """采购成本：两份导出里都没有，只能由调用方按货号注入。

        **必须按数量乘**：实测 `发货的金额` 是**行合计**而不是单价
        （`FAKE-BF83624B`：数量 2 × 最高价格 25.00 = 发货的金额 50.00），
        所以一个发货单的成本是 Σ(数量 × 单件成本)，不是 Σ 单件成本。

        缺任一货号的成本即返回 None —— **不用部分和冒充**，
        否则 §7.1 会算出一个偏高的利润（PRD §7.1 完整性规则）。
        """
        if not self._purchase_cost or not items:
            return None
        total = Decimal('0')
        seen = False
        for offer, qty in items:
            if not offer:
                continue
            seen = True
            cost = self._purchase_cost.get(str(offer).strip())
            if cost is None:
                return None
            total += cost * Decimal(max(1, int(qty or 1)))
        return total if seen else None


def _chunks(seq: Sequence, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _parse_dt(value) -> Optional[datetime]:
    """复用 sqlite_source 的时间解析，避免两处实现漂移。"""
    from .sqlite_source import _parse_dt as _impl
    return _impl(value)


def _parse_day(value) -> Optional[date]:
    """复用 sqlite_source 的日期解析，避免两处实现漂移。"""
    from .sqlite_source import _parse_day as _impl
    return _impl(value)
