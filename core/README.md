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
├─ repository/sqlite_source.py   现有 SQLite 实现（只读）
├─ repository/fixture_source.py  夹具实现（CI 用）
├─ tools/freeze_golden.py     从生产库冻结黄金样本
└─ tests/test_profit_golden.py   回归测试
```

## 尚未实现（诚实清单）

- **§7.4 回款测算**：`posting_profit_facts` 里有相关字段,但本目录尚未实现,也未对账。
- **§7.2 广告费分摊**：PRD 提到「广告费分摊（如有）」,现有实现未体现在预估利润公式里,
  广告数据在店铺级的 `advertising_daily_facts` / `advertising_month_snapshots`,需业务确认是否应计入。
- **汇率取值优先级**（§7.5）：目前直接采用结算快照里的汇率,未实现「三级来源优先级」。
- **换 PostgreSQL**：接口已就位,但尚未写第二个实现。
