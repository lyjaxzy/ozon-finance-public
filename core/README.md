# core —— 财务领域核心（可读、可测、可替换）

本目录是财务计算的**唯一权威实现**,用来取代原先只存在于反编译字节码里的逻辑
（PRD §12.2 S1）。阶段一 T-01 的落地。

## 为什么有这个东西

现有系统的财务核心是 PyInstaller 打包的字节码,函数体不完整、不可阅读、不可维护。
在它上面直接做前后端分离,等于把黑盒搬到 API 后面 —— 这是上一次「套 Web 壳」失败的根因之一。

所以顺序是:**先把口径变成可读、可测的代码,再做 API,最后做前端。**

## 设计约束（刻意为之）

| 约束 | 原因 |
|---|---|
| 不依赖 FastAPI / 任何 Web 框架 | 领域逻辑要能被单测、CLI、定时任务、API 共同复用 |
| 不依赖 Windows（无 DPAPI / taskkill / 计划任务） | 为将来的跨平台留路（PRD S2）|
| 金额一律 `Decimal`,禁止 float | PRD §14.2：金额字段不得用 FLOAT |
| 通过 `repository/base.py` 取数 | 「DB 可替换」的落点：换库只换实现,业务代码不动 |
| 缺失值写 `unknown_reason`,不用 0 顶替 | PRD §7.1 完整性规则 |

## 三段口径（已用生产库全量验证）

### §7.1 实际利润

```
actual_profit_cny = quantize(Σ(权威操作集金额) / 结算汇率, 0.01) − 采购成本
```

**权威操作集 = `posting_profit_facts.linked_operation_ids_json`。**

> ⚠️ 这是本目录最重要的一条结论,也是最容易被写错的地方。
> 直觉上会按 `posting_number` 去 JOIN `finance_transactions` 求和 —— **实测只有 35.1% 吻合**。
> 两个原因:
> 1. **手续费挂在基单号上**。订单 `FAKE-11D63D3B` 的 ACQUIRING 手续费 `-4.87`
>    记在 `FAKE-015C8A22`（无条目后缀）,按单号 JOIN 会漏掉。
> 2. **结算快照会重新锁定、退货在结算后才到达**。快照的 `operation_ids` 与事实表的
>    `linked_operation_ids` 在约 1.8% 的订单上互有出入,且**两个方向都有**（各有各的缺失）。

按 `linked_operation_ids` 求和 → **全库 6668 个完整订单,金额与直接净额 0 处不符**。

### §7.2 预估利润

```
estimated_profit_cny = 收入 − 采购成本 − 物流费 − coalesce(预估平台佣金, 0)
```

全库 10,899 单中 **10,898 单吻合**。唯一例外见下方 D3。

### §7.3 核算完成率

```
核算完成率 = actual_complete=1 的订单数 / 总订单数
```

当前基准（2026-09-10）：总 10,899,实际完整 6,668（61.2%）,预估完整 3,116（28.6%）。

## 已确认的三处口径分歧

这三处**不是实现缺陷,是旧系统内部不可复现的选择**。全部写进了回归测试的白名单,
精确到条件,不是放宽阈值。详见 `docs/adr/0003-利润口径分歧.md`。

| 编号 | 分歧 | 规模 | 处理 |
|---|---|---|---|
| **D1** | 快照 `operation_ids` ≠ 事实表 `linked_operation_ids` | 80/10,899 | 断言：数字不符时必须确实源于操作集不同 |
| **D2** | `no_settled_sale` / `no_direct_settlement` / `platform_subsidy_one_ruble_promotion` 的细分依据在旧实现内部,输入侧不可观测；且补贴与无效汇率的汇率区间在 0.0238 处重叠 | 649/10,899 | 断言：无结算记录时原因字段允许分歧 |
| **D3** | `postings.estimated_profit_cny` 是过期派生值（订单重新同步过,该值没刷新） | 1/10,899 | 精确白名单 `KNOWN_STALE_ESTIMATED` |

**D2 需要业务确认**（对应 PRD T-05）：新版应该定义自己的原因分类,
还是必须逐字复刻旧分类?建议定义新版,因为旧分类依赖不可观测的中间量。

## 回归测试

```powershell
# 全量（夹具 + 真实库,真实库不存在时自动跳过）
python -m unittest core.tests.test_profit_golden

# 只跑夹具（任何机器可跑,无需生产库）
python -m unittest core.tests.test_profit_golden.GoldenFixtureTest

# 全部 core 测试（口径 + Excel 导入 + 窗口边界）
python -m unittest discover -s core/tests -t .
```

测试是**双轨**的：

- `GoldenFixtureTest` —— 跑 `tests/fixtures/golden_sample.json`（147 单 / 7 个月 / 328 KB,已入库）。
  夹具里同时冻结了「输入」和「现有系统算出的结果」,所以换任何机器都能复现对账。
- `LiveDatabaseTest` —— 直接对 `<DATA_ROOT>\data\stores\store_alpha.db`
  跑同一套断言（只读,有写保护自检）。生产库在哪台机器,就在哪台机器上跑全量。

### 重新冻结夹具

口径没变、但生产库新增了数据时：

```powershell
python core/tools/freeze_golden.py                  # 每月抽 25 单
python core/tools/freeze_golden.py --per-month 60   # 抽得更多
```

`freeze_golden.py` 以 `mode=ro` 打开生产库,**绝无写入路径**。

## 目录结构

```
core/
├─ domain/profit.py           三段口径的实现（纯函数,可单测）
├─ repository/base.py         数据来源接口 —— 「DB 可替换」的落点
│                             （逐单输入 + 只读聚合，后者供看板使用）
├─ repository/sqlite_source.py   现有 SQLite 实现（只读）
├─ repository/fixture_source.py  夹具实现（CI 用）
├─ repository/excel_source.py    Excel/CSV 导入实现（见下节）
├─ tools/freeze_golden.py     从生产库冻结黄金样本
├─ tools/verify_excel_vs_sqlite.py  Excel 导入器与生产库的口径对账
└─ tests/
   ├─ test_profit_golden.py          三段口径的逐单回归（夹具 + 真实库）
   ├─ test_excel_source.py           Excel 导入器
   └─ test_sqlite_source_cutoff.py   窗口右端 / 数据截止两个边界（ADR-0007）
```

## Excel/CSV 导入数据源（2026-09 新增，ADR-0005）

`repository/excel_source.py` 是 `ProfitDataSource` 的**第三个实现**，
喂它的是 OZON 后台的两份导出（放同一个目录，默认 `D:\Downloads`）：

| 文件 | 喂什么 |
|---|---|
| `Отчет по начислениям_*.xlsx`（应计报表） | 应计流水（卢布）→ §7.1、利润构成 |
| `postings.csv`（发货单导出） | 订单状态 / 发货截止时间 / 人民币销售额 → §7.2、§7.6 |

```powershell
# 与生产库逐单对账（只读；没有生产库时该脚本无法运行，那是对账必须有真值）
python core/tools/verify_excel_vs_sqlite.py --census
```

三条**实测**得出的关键结论（都不是推测，`verify_excel_vs_sqlite.py` 可复现）：

1. **`ID начисления` 有两种形态**：条目单号 `…-0033-1`（= `发货号码`）与
   **基单号** `…-0033`（= `订单号`，**收单费/手续费挂这里**）。
   严格按 `ID начисления` 分组求和，逐单吻合率只有 **7.6%**；
   把基单号的应计归属到它唯一的条目单之后 → **98.9%**。
   基单号对应多个条目单时**不摊分**（摊分会把吻合率拉回 94.3%），
   应计留在基单号名下并在 `attribution` 里标成 `base_ambiguous`。
2. **报表里有 3 行 `ID начисления` 是空的**（净额 +35,009.09 RUB，
   其中一笔是「合同间债权抵销」这种本来就无订单号的科目）。
   这些行留在库里、计入取值统计、可用 `unidentified_accruals()` 单独查，
   只是不参与逐单汇总 —— **一行都不静默丢弃**。
3. **应计报表没有结算汇率，两份文件都没有采购成本。**
   所以默认配置下 §7.1 会如实判 `missing_exchange_rate` / `missing_purchase_cost`，
   这是**正确行为**而不是缺陷。两个可选补法（都要显式打开）：
   * `exchange_rate_mode='implied_buyer_payment'` —— 用
     `已由买家支付 / 发货的金额` 推汇率。实测与生产库结算汇率在 **93.8%**
     的订单上完全相等，但 6.2% 会正好差一个整数倍（多商品单买家实付不全）。
     ⚠️ 临时方案，**须业务确认**。
   * `purchase_cost_by_offer={'货号': 成本}` —— 采购成本从外部注入；
     多商品单缺任一货号即返回 `None`（不用部分和冒充）。

`Группа услуг` / `Тип начисления` → 八大类别的映射表是代码里的常量
（`CATEGORY_BY_GROUP_TYPE` / `CATEGORY_BY_TYPE`），依据是**生产库
`finance_transactions.operation_category` 对同一批业务的既有判定**；
**未识别的取值一律落 `OTHER` 并记进 `unmapped_types` 表**，不做分组级兜底猜测。
2026-06/07/08/09 四份报表里的 29 个组合已全覆盖（`test_excel_source.py` 用
`OBSERVED_COMBOS` 当护栏）。

性能：流式解析 → 分批落 SQLite 临时库 → 建索引；
解析结果按「文件内容指纹」缓存在系统临时目录
（实测 4 份报表共 52,573 行：冷解析 **48.6 秒**，命中缓存重开 **0.01 秒**，
窗口取数（1400+ 单）1.8 秒；`cache=False` 可关）。

## 只读聚合查询（看板用，2026-09 新增）

原来的 `ProfitDataSource` 只有逐单取数。看板需要「近 N 天合计 / 按天汇总 /
订单列表 / 逾期单数」，这些查询**加在 repository 层，不是加在 API 层** ——
一旦把 SQL 写进 API，就又会退化成「Web 层自建一套模型」的老路。

| 方法 | 返回 | 用途 |
|---|---|---|
| `data_cutoff(alias)` | `DataCutoff` | 两个时间边界：`cutoff`（写入时间，展示用）+ `window_end`（**窗口右端** = 最后一个已结算日） |
| `orders_for_period(alias, start, end, limit, offset)` | `Sequence[PeriodOrderRow]` | **看板权威路径**：一次取齐窗口内每个订单喂给 `evaluate_actual`/`evaluate_estimated` 的全部输入 |
| `amounts_for_period(alias, start, end)` | `PeriodAmounts` | 廉价路径：一次 SQL 扫描出窗口合计（对账/告警） |
| `daily_amounts(alias, start, end)` | `Sequence[DailyAmounts]` | 廉价路径：按天汇总 |
| `overdue_count(alias)` | `OverdueInfo` | 逾期单数 |
| `direct_net_totals(alias, start, end)` | `dict` | 直接净额逐单取数与汇率（各单汇率不同，必须先折算再相加） |
| `sku_detail_for_period(alias, start, end)` | `SkuDetail` | 逐 SKU 明细行 + 归不出去的金额（ADR-0006） |

约定：

* **窗口由锁定结算快照的 `settlement_date` 界定**。它一旦锁定就不再变化，
  所以同一订单重复查询的落点稳定；按订单创建时间会让同一单在不同窗口里跳。
* **窗口右端必须取自数据，不能取自写入时间**：`DataCutoff.window_end` =
  `max(settlement_date) WHERE state='locked'`。`cutoff`（`updated_at` 最大值）
  会被同步动作刷新到数据之后，而窗口查询的过滤条件就是结算日落在区间内 ——
  右端越过去，`days=7` 就会返回 0 单、`actual_profit_cny` 变成 `null`
  （2026-09-19 真实发生过：逾期扫描把右端从 09-10 推到 09-19，见 ADR-0007）。
  运营类表的时间戳（`overdue_fast_scans`）只进 `DataCutoff.candidates` 供排查。
* 没有锁定快照的订单不属于任何窗口（既不算实际利润，也不算预估利润）。
* 日期按 `YYYY-MM-DD` 字符串比较。
* 聚合方法**不含利润口径**。`PeriodAmounts.actual_profit_cny` 只累加库里
  `actual_complete=1` 的订单 —— 该列的语义与 `evaluate_actual` 的完整性规则一致，
  由 `test_profit_golden.py` 逐单核对过（全库 6668 单，0 处不符）。
* 廉价路径走 SQL 的 `CAST(... AS REAL)` 聚合，会有浮点噪声
  （实测 `46082.29` 会算成 `46082.2900000000005`），所以统一用
  `quantize_cny` 收敛到分再返回。它与逐单精算路径的一致性由
  `api/tests/test_api.py::test_cheap_aggregate_methods_agree_with_order_level_path`
  把关 —— 这条测试在开发中真的抓到一个 bug：按 `estimated_complete=1` 过滤会把
  窗口内约 56% 的订单挡掉，让按天合计只有总计的 44%。
* `FixtureSource` 的 `overdue_count` **抛 `NotImplementedError`**：
  夹具没冻结 `overdue_fast_*` 扫描数据，而逾期是「当前态」不是历史事实。
  返回 0 会变成伪装成成功的错误，所以宁可抛错。

新增的 `core/domain/profit.sum_amounts(values)` 是唯一的求和定义
（跳过 `None`，**不当作 0**），仓储层与 API 层都经它汇总，
避免一处跳过、一处补 0 的分歧。它只是求和工具，不是利润口径。

## 逐 SKU 下钻（2026-09 新增，ADR-0006）

看板原本只能看合计，看不到「哪个货号在赚钱」。而**采购成本是按货号录入的**
（OZON 两份导出里唯独没有成本），所以成本的最小单位就是利润该有的展示单位。

新增的协议方法 `sku_detail_for_period(alias, start, end)` 返回 `SkuDetail`：
窗口内「一个订单 × 一个货号」的明细行 + **归不出去的金额**（带原因）+ 无法归属实际利润的订单。

三条口径约定：

* **只按数据自带的分组键分组求和，不做任何比例分摊。** 应计报表有
  `Артикул` / `SKU` / `Количество` / `Сумма итого, руб.`；`postings.csv` 一行一个 SKU，
  `发货的金额` 就是该行合计。实测 11,002 单里 10,953 单（99.55%）只有一个货号 ——
  整单归属那一个货号，本来就不需要分摊。
* **归不出去就如实报。** 多货号订单的物流费/平台佣金/财务流水在库里是**订单级单值**，
  没有按行的分组键 → 整笔进 `SkuDetail.unattributed` 并写原因（`multi_item_posting` 等），
  界面上单独成区。**不摊分、不补 0、不静默丢弃。**
* **缺成本一律 `null`。** `core/domain/profit._all_or_none()`：一个货号名下只要有任一行
  缺这一项，这一项的合计就是 `None` —— 不用部分和冒充。

一致性（由 `api/tests/test_sku_detail.py` 把守，真库上逐字段精确到分）：

```
Σ(逐 SKU) + 未归属 == 订单口径合计        # 收入/成本/物流/佣金/预估利润/实际利润
```

逐 SKU 的利润计算**没有新公式**：`core/domain/profit.evaluate_sku_profit()` 只是把每一行
喂给 `evaluate_estimated` / `evaluate_actual`，再按货号分组求和。

`FixtureSource` 实现不了这个方法（夹具没冻结 `posting_items`），**明确抛
`NotImplementedError`** 而不是返回空表 —— 空表会被当成「这个窗口没有 SKU」。

## 尚未实现（诚实清单）

- **§7.4 回款测算**：`posting_profit_facts` 里有相关字段,但本目录尚未实现,也未对账。
- **§7.2 广告费分摊**：PRD 提到「广告费分摊（如有）」,现有实现未体现在预估利润公式里,
  广告数据在店铺级的 `advertising_daily_facts` / `advertising_month_snapshots`,需业务确认是否应计入。
- **汇率取值优先级**（§7.5）：目前直接采用结算快照里的汇率,未实现「三级来源优先级」。
- **换 PostgreSQL**：接口已就位,但尚未写第二个实现。
- **平台费用单独口径**：`postings.estimated_platform_fee_cny` 全库为空，
  `platform_fees_cny` 只有 547/10903 单有值。看板的「平台费用」构成项目前只能折 0，
  待业务确认权威来源字段（详见 `api/README.md` §8.2）。
- **真实成本库没有数据**：`sku_cost_cache`（sku → 单件成本）是逐 SKU 下钻的权威来源，
  但本机是 0 行。目前由「单货号订单的整单成本 ÷ 数量」反推一个单件成本兜底
  （实测 253 个货号取值唯一、0 冲突），属**临时方案**，详见 ADR-0006 §六。
- **多货号订单的费用仍归不到货号**：实测 14 天窗口 1,978 单里 3 单属此类（0.15%）。
  要真正归属需要平台给出**按商品的运费/佣金明细**，两份导出都没有。
