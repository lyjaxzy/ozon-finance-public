# api —— OZON 财务系统只读后端 API

FastAPI 应用，**只读**。利润口径全部来自 `core/domain/profit.py`，
数据全部来自 `core/repository/`，本目录不自己写 SQL、也不自建数据模型。

> 这一条是本目录存在的全部理由。上一版失败的原因就是 Web 层自建了一套库和模型、
> 再离线导入快照，导致数据永远是旧的。所以本目录的红线是：
> **API 里不出现任何利润计算逻辑**，只做「参数校验 → 取仓储层数据 → 调领域层算 → 组装 JSON」。

---

## 1. 启动

```powershell
cd D:\xzy\ozon-finance

# 端口刻意用 8849，避开前端开发服务器的 8848
python -m uvicorn api.app:app --host 127.0.0.1 --port 8849 --log-level info
```

启动后：

* 接口文档（自动生成）：<http://127.0.0.1:8849/docs>
* 健康检查：<http://127.0.0.1:8849/api/health>

依赖：`fastapi` / `uvicorn` / `pyjwt` / `pydantic`（本机已装，**未新增任何依赖**）。
若换了机器需要装：

```powershell
python -m pip install fastapi uvicorn pyjwt
```

前端联调：`web/.env.development` 里的 `VITE_PROXY` 改成 `[["/api","http://127.0.0.1:8849"]]`。
后端刻意**没有**加 CORS 中间件 —— 走 vite 代理是同源请求，不需要；
加 `allow_origins=["*"]` 反而会把只读接口暴露给任意页面。

## 2. 测试

```powershell
cd D:\xzy\ozon-finance

# 新增的 API 测试（39 项）
python -m unittest discover -s api/tests -t .

# core 回归测试，必须仍是 Ran 14 tests, OK
python -m unittest core.tests.test_profit_golden

# 详细模式
python -m unittest discover -s api/tests -t . -v
```

Windows 下如果中文输出乱码，先设一次 `$env:PYTHONIOENCODING="utf-8"`。

测试分三层：

| 测试类 | 数据源 | 证明什么 |
|---|---|---|
| `AuthTest` / `AuthPermissionTest` | 内存夹具 | 401 / 403 / 授权与越权语义 |
| `DashboardContractTest` / `CanonicalFormulaTest` | 内存夹具（数字手算可核） | 响应形状、金额是字符串、与 `evaluate_actual` 逐个相同 |
| `RealDatabaseConsistencyTest` | **真实生产库（只读）** | 真的连到生产库、真的按 core 算；生产库不存在时自动跳过 |
| `FixtureSourceAggregateTest` | `core/tests/fixtures/golden_sample.json` | 夹具数据源的聚合查询与逐单路径一致 |

## 3. 接口清单

| 方法 | 路径 | 说明 | 需要登录 |
|---|---|---|---|
| POST | `/api/auth/login` | `{username, password}` → `{access_token, token_type, expires_in, user}` | 否 |
| GET | `/api/auth/me` | → `{user, stores[]}`，`stores` 只含**该用户可见**的店铺 | 是 |
| GET | `/api/dashboard/store/{alias}?days=14` | 单店看板 | 是 |
| GET | `/api/health` | → `{status, data_source_readonly, store_aliases, orders_in_response_limit}` | 否 |
| GET | `/api/health/store/{alias}` | 店铺库连通性自检（只回布尔量，不含业务数据） | 否 |

`days` 取值 1–365，默认 14，越界返回 422。

### 看板响应形状

**前端已按这个形状写好了，任何字段的增删改名都是破坏性变更。**

```json
{
  "store_alias": "store_alpha",
  "data_cutoff": "2026-09-11T01:16:21.605343+08:00",
  "period": {"start": "2026-08-28", "end": "2026-09-10", "days": 14},
  "totals": {
    "actual_profit_cny": "48215.95",
    "estimated_profit_cny": "46082.29",
    "completion_rate": "1.0000",
    "complete_order_count": 1978,
    "total_order_count": 1978,
    "overdue_count": 28
  },
  "trend": [
    {"date": "2026-08-28", "actual_profit_cny": "2909.19", "estimated_profit_cny": "2967.00"}
  ],
  "composition": [
    {"key": "direct_net", "label": "直接净额", "value_cny": "74321.59"},
    {"key": "purchase",   "label": "采购成本", "value_cny": "26105.73"},
    {"key": "platform",   "label": "平台费用", "value_cny": "0.00"},
    {"key": "logistics",  "label": "物流费用", "value_cny": "14853.25"}
  ],
  "orders": [
    {
      "posting_number": "FAKE-4F6B7A31",
      "settlement_date": "2026-09-10",
      "direct_net_rub": "403.27",
      "exchange_rate_rub_per_cny": "9.3095",
      "purchase_cost_cny": "18.13",
      "actual_profit_cny": "25.19",
      "complete": true,
      "unknown_reason": null
    }
  ]
}
```

金额与汇率**一律是字符串**（前端决定怎么显示），避免 JS 浮点误差。
字符串一律 2 位小数（汇率 4 位），缺失值是 `null`，**不是 `"0.00"`**。

`data_cutoff` 是库里最后一个可观测写入时间（UTC 存储 → 换算成北京时间下发）。
窗口右端取它的日期，而**不是**系统当天 —— 店铺库是定期同步的，
按当天取窗口会让最后几天永远为空，看板「看着正常但没有数」。

## 4. 认证方式

JWT（`PyJWT` 2.10.1，HS256），有效期 8 小时（`OZON_JWT_TTL_SECONDS` 可调）。
令牌里只放身份（`sub` / `role`），**不放授权明细** —— 授权每次都现查，
改授权立即生效，不必等令牌过期。

```
Authorization: Bearer <access_token>
```

`OZON_JWT_SECRET` 生产必须显式设置；不设就用开发默认值，接口能跑但绝不能上生产。

### 三个 mock 账号

用户存在 `api/data/users.json`（**还没有用户表**）。口令以
`pbkdf2_sha256$迭代次数$盐$摘要` 存哈希，不存明文。
明文口令与账号对应关系（仅本机开发用）：

| 用户名 | 口令 | 角色 | 角色标识 | 数据范围 |
|---|---|---|---|---|
| `admin` | `admin` | root 管理员 | `root` | 全部店铺 |
| `finance01` | `finance01` | 财务 | `finance` | 仅 `store_alpha` |
| `operator01` | `operator01` | 单店铺运营 | `store_operator` | 仅 `store_alpha` |

> ⚠️ 口令等于用户名，是**故意的**：这是本地只读 mock，方便手工联调。
> 换成真实用户表时必须同时换掉口令、加上口令强度校验与失败锁定。

## 5. 三档权限与数据隔离

数据隔离**只在服务端**做，前端隐藏入口不算隔离。

| 场景 | 返回 | 响应体 |
|---|---|---|
| 未登录 / 令牌无效 / 令牌对应用户已删除 | `401` | `{"detail": "请先登录"}` |
| 别名不在店铺白名单 | `404` | `{"detail": "店铺不存在"}` |
| 在白名单但该用户无授权 | `403` | `{"detail": "无权访问该店铺"}` |
| 通过 | `200` | 正常看板数据 |

**跨店必须 403，不得返回空数据伪装成功。**
「200 + 空数组」会让调用方把「没权限」当成「这段时间没订单」，问题被静默掩盖。
越权响应体里只有 `detail` 一个键，这一点有测试专门断言
（`test_unauthorized_store_is_403_not_empty_data` 逐字段检查有没有泄漏
`totals` / `orders` / `trend` / `composition` / `period` / `data_cutoff`）。

授权检查发生在**打开数据库之前**，所以跨店请求根本不会碰到数据。

root 不能删除/停用自己（PRD §9.2）：约束落在 `api/users.py` 的
`User.can_delete_self()`。本步骤是只读，还没有删除接口，
把这个方法放在模型上是为了将来做用户管理时不可能漏掉。

### 怎么让 403 真的测得出来

只有一个店铺别名时，跨店请求只会得到 404「店铺不存在」，403 分支永远走不到。
所以店铺白名单默认注册两个别名：

| 别名 | 库路径 | 授权给 |
|---|---|---|
| `store_alpha` | `<DATA_ROOT>\data\stores\store_alpha.db` | root / finance01 / operator01 |
| `store_beta` | 同上路径把 `store_alpha` 换成 `store_beta`（**该库当前不存在**） | 仅 root |

`store_beta` 的库文件不存在是**有意为之**：它只用来证明「运营/财务拿不到它」，
而 root 请求它会得到 503（服务端配置故障）而不是 403。要真正启用二店，
把库文件放到位即可。

白名单用环境变量覆盖：

```powershell
$env:OZON_STORES = "store_alpha=C:\path\a.db;store_beta=C:\path\b.db"
```

**路径永远不来自请求参数**，只来自这个白名单；别名本身还受
`^[a-z0-9_-]{1,64}$` 的正则约束，双重挡住路径穿越。

## 6. 口径与取数（照抄 core，不另立）

| 项 | 口径 | 实现位置 |
|---|---|---|
| §7.1 实际利润 | `Σ(权威操作集金额) / 结算汇率 − 采购成本`，保留 2 位小数 | `core/domain/profit.evaluate_actual` |
| 权威操作集 | `posting_profit_facts.linked_operation_ids_json`（**不是** `settlement_snapshots.operation_ids_json`，按后者只有 35% 吻合率） | `core/repository/sqlite_source` |
| §7.2 预估利润 | `收入 − 采购成本 − 物流费 − 预估平台佣金` | `core/domain/profit.evaluate_estimated` |
| §7.3 完成率 | `actual_complete=1 的订单数 / 总订单数` | `core/domain/profit.completion_rate` |

看板的 `totals` / `trend` / `orders` **出自同一批数**：
先 `orders_for_period` 取回整窗口，再对每单调 `evaluate_actual`，最后汇总。
这样「明细加起来 = 合计」永远成立，用户不会看到无法解释的差额。

汇总时缺失值一律**跳过**，不用 0 顶替；算不出实际利润的订单不进实际利润合计，
但它的 `unknown_reason` 会原样下发。

## 7. 仓储层新增的只读聚合方法

`ProfitDataSource` 原来只有逐单取数，看板需要聚合。新增（在 `core/repository/` 里，
保持同样的协议风格）：

| 方法 | 作用 | 路径 |
|---|---|---|
| `data_cutoff(alias)` | 最后一个可观测写入时间 | `base.DataCutoff` |
| `orders_for_period(alias, start, end, limit, offset)` | 窗口内的订单，**带齐喂给领域层的输入** | 看板权威路径 |
| `amounts_for_period(alias, start, end)` | 窗口合计（廉价路径，一次 SQL 扫描） | 对账/告警 |
| `daily_amounts(alias, start, end)` | 按天汇总（廉价路径） | 对账/告警 |
| `overdue_count(alias)` | 逾期单数 | → `base.OverdueInfo` |
| `direct_net_totals(alias, start, end)` | 直接净额逐单取数/汇率（要按每单汇率折算才能相加） | `amounts_for_period` 内部 |

时间窗口的约定（两种实现必须一致）：

* 订单属于哪个窗口，由**锁定结算快照的 `settlement_date`** 决定 ——
  它一旦锁定就不再变化，同一订单重复查询落点稳定。
* 只有 `state='locked'` 的快照算「已结算」；没有锁定快照的订单不属于任何窗口。
* 日期按 `YYYY-MM-DD` 字符串比较。

`FixtureSource` 能实现的都实现了（`data_cutoff` / `orders_for_period` /
`amounts_for_period` / `daily_amounts`），**`overdue_count` 明确抛
`NotImplementedError`**：夹具没冻结 `overdue_fast_*` 扫描数据，
而逾期是「当前态」不是历史事实。返回 0 会变成伪装成成功的错误，所以宁可抛错。

还有一条测试（`test_cheap_aggregate_methods_agree_with_order_level_path`）
专门盯着「廉价路径」与「逐单精算路径」的合计是否一致。
开发过程中它真的抓到一个 bug：`amounts_for_period` 原本按
`estimated_complete=1` 过滤，而这个标记是**全库**完整标记，
会把窗口内约 56% 的订单挡掉，导致按天预估合计只有总计的 44%。
两个数都「看着对」，但差一倍 —— 现在两条路径都必须等于逐单精算的结果。

## 8. 已知限制（诚实清单）

### 8.1 临时方案

1. **用户是 mock，不是真实用户表**。`api/data/users.json` 三个账号，
   口令等于用户名，没有口令强度校验、没有失败锁定、没有轮换。
   `UserStore` 的接口（`authenticate` / `get` / 角色常量）是照真实用户表设计的，
   换实现时只需替换 `UserStore.from_file`。
2. **单机内存 JWT**，没有刷新令牌、没有吊销名单（登出只能等过期）。
   令牌里只放身份不放授权，所以改授权立即生效。
3. **店铺元信息写死在配置里**（`config.STORE_DISPLAY_NAMES`），还没有店铺表。
4. **`orders` 数组上限 200 条**（`OZON_MAX_ORDERS` 可调）。
   响应形状是前端定死的，**不能**偷偷加分页字段，所以超出部分只体现在
   `totals` / `trend` 里。当前窗口实际 1978 单 → 明细返回 200 条。
   `orders_for_period` 已经支持 `limit` / `offset`，加正式分页时不用改仓储层。

### 8.2 没能实现 / 需要业务确认

1. **逾期数不按看板窗口过滤**。取自 `overdue_fast_current`（当前生效的那次扫描，
   本库 28 单）。这张表存的是**当前**在途逾期集合，不是历史每日快照，
   按窗口过滤只会把结果打成 0。所以 `totals.overdue_count` 是「截至数据截止时刻的
   当前逾期单数」，与 `period` 无关。
2. **逾期口径的时效与 `data_cutoff` 同源**：都来自 `overdue_fast_scans.updated_at`，
   所以它们永远一致，但也意味着**逾期扫描停了，截止时间就不动了**。
3. **`composition.platform` 恒为 `"0.00"`**：`postings.estimated_platform_fee_cny`
   在全库（10,903 单）**全部为空**，只有 547 单的 `platform_fees_cny` 有值。
   这里按前端展示需要折成 0.00，**不是真实平台费用**。
   平台费用目前已体现在 `direct_net` 里（它是平台扣费后的净额），
   所以不参与任何利润口径，折 0 不会污染利润数字。要单独展示这一项，
   需要先确认平台佣金的权威来源字段。
4. **§7.2 的预估利润用 `postings.estimated_profit_cny` 这类派生字段时会有历史脏值**：
   看板走的是 `evaluate_estimated` 现算，与库里的派生值在
   `KNOWN_STALE_ESTIMATED`（1 单，见 core/README D3）上不一致 —— 这是旧系统的
   数据一致性缺陷，不是本次实现引入的。
5. **`completion_rate` 在当前 14 天窗口恒为 1.0000**。
   不是写死，而是口径的必然结果：窗口只含**已锁定结算**的订单，
   而 `posting_profit_facts.actual_complete=1` 与「有锁定快照」在这套库上完全等价
   （4231 单没有锁定快照的，`actual_complete` 都是 0）。
   要看「未完成」的分布，得把窗口定义改成「按订单创建月」之类的另一套口径 ——
   那属于业务定义，本次没有擅自改。
6. **没有按 SKU / 商品维度的聚合**，也没有 §7.4 回款测算（core 也未实现）。
7. **`store_beta` 的库文件不存在**，root 访问它返回 503，不是 404 ——
   别名注册了但库没到位属于服务端配置故障，用 503 让运维看得见。

### 8.3 数据来源对照

| 字段 | 来源 |
|---|---|
| `orders[].direct_net_rub` / `exchange_rate_rub_per_cny` / `purchase_cost_cny` / `actual_profit_cny` / `complete` / `unknown_reason` | `evaluate_actual(...)`：快照 + **事实表 linked_operation_ids 对应的流水** |
| `orders[].settlement_date` | `settlement_snapshots.settlement_date`（`state='locked'`） |
| `totals.actual_profit_cny` / `complete_order_count` | 窗口内 `evaluate_actual` 结果汇总（只累加 `complete=True`） |
| `totals.total_order_count` | 窗口内有锁定结算快照的订单数（= 事实表行数，已核对相等） |
| `totals.estimated_profit_cny` | 逐单 `evaluate_estimated(posting)` 汇总 |
| `totals.completion_rate` | `completion_rate(actuals)` |
| `totals.overdue_count` | `overdue_fast_current` → `overdue_fast_scans.overdue_order_count` |
| `trend[].*` | 同一天内逐单精算结果汇总（**不补零日期行**） |
| `composition.direct_net` | `Σ(权威操作集金额 ÷ 该单汇率)`，**不乘区间平均汇率** |
| `composition.purchase` | `Σ` 各单 `evaluate_actual(...).purchase_cost_cny` |
| `composition.platform` | `postings.estimated_platform_fee_cny`（当前全为空 → 0.00） |
| `composition.logistics` | `Σ postings.logistics_cost_cny` |
| `data_cutoff` | `max`(快照 `updated_at`, 事实表 `updated_at`, 逾期扫描 `updated_at`) → 北京时间 |

### 8.4 只读保证

* `SqliteSource` 一律以 `mode=ro` 打开，构造时做写保护自检
  （尝试 `CREATE TABLE` 必须抛 `OperationalError`），失败即启动失败。
* `api/` 下没有任何写路由；SQL 全是 `SELECT`。
* 有一条测试（`test_source_is_readonly`）在真实库上再验一次。
