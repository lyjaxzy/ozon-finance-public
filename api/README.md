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
| GET | `/api/stores` | **可见店铺列表** + 库可用性 + 最近结算日（ADR-0008） | 是 |
| GET | `/api/dashboard/store/{alias}?days=14` | 单店看板（`orders_offset` / `orders_limit` 做服务端分页） | 是 |
| GET | `/api/dashboard/aggregate?stores=a,b&days=14` | **多店合计**（ADR-0008） | 是 |
| GET | `/api/dashboard/store/{alias}/sku-detail?days=14` | **逐 SKU 利润下钻**（ADR-0006，`limit` / `offset` 分页） | 是 |
| GET | `/api/health` | → `{status, data_source_readonly, store_aliases, orders_in_response_limit}` | 否 |
| GET | `/api/health/store/{alias}` | 店铺库连通性自检（只回布尔量，不含业务数据） | 否 |

`days` 取值 1–365，默认 14，越界返回 422。

分页参数（都是**可选**的，不传就是原来的行为）：

| 接口 | 参数 | 默认 / 上限 | 影响什么 |
|---|---|---|---|
| 单店看板 | `orders_limit` | 默认 200，上限 200（越界 422） | 只决定 `orders` 数组下发哪一段 |
| 单店看板 | `orders_offset` | 默认 0 | 同上 |
| 逐 SKU 明细 | `limit` / `offset` | 默认 500 / 0，上限 500 | 只决定 `rows` 下发哪一段 |

**分页永远不影响合计**：`totals` / `trend` / `composition`（以及逐 SKU 的
`totals` / `reconciliation`）都按**整个窗口**算。所以「这一页的明细加起来 ≠ 合计」
是正常的，界面上必须写明这一点。

### 多店合计接口（ADR-0008）

```
GET /api/dashboard/aggregate?stores=store_alpha,store_beta&days=14
```

```json
{
  "store_aliases": ["store_alpha", "store_beta"],
  "stores": [
    {
      "alias": "store_alpha",
      "display_name": "OZON 俄罗斯站 · 主力店",
      "data_cutoff": "2026-09-11T00:46:47.314183+08:00",
      "window_end": "2026-09-10",
      "period": {"start": "2026-08-28", "end": "2026-09-10", "days": 14},
      "totals": { "...": "与单店接口同形" }
    },
    { "alias": "store_beta", "...": "..." }
  ],
  "data_cutoff": "2026-09-11T00:47:33.012103+08:00",
  "window_end": "2026-09-10",
  "period": {"start": "2026-08-28", "end": "2026-09-10", "days": 14},
  "totals": {
    "actual_profit_cny": "49437.26",
    "estimated_profit_cny": "47494.96",
    "completion_rate": "1.0000",
    "complete_order_count": 2023,
    "total_order_count": 2023,
    "overdue_count": 20
  },
  "trend": [ "..." ],
  "composition": [ "..." ],
  "warnings": []
}
```

四条必须知道的规则：

1. **共同窗口**：`window_end` = 各店 `window_end` 里**最早**的那个，逐店的
   `period` 与顶层 `period` 是**同一个区间**。理由见 ADR-0008 §二：
   按各店自己的最新数据分头相加，落后的那家店会在尾部几天「贡献 0」，
   看起来像那几天没生意（那正是 ADR-0007 那个事故的形状）。
2. **逐店之和 == 顶层合计**（逐字段）。逐店 `totals` 就是在共同窗口下算的，
   所以这条等式可以当场核对；`api/tests/test_multi_store.py` 把它写成了断言。
3. **完成率是 `Σ完整单 / Σ总单`**，不是各店完成率的平均。定义只有一处：
   `core/domain/profit.py::completion_rate_from_counts`。
   实测两家店单量差 40 倍（1978 : 45），按店等权会明显失真。
4. **任一店给不出某项就给 `null`**，并在 `warnings` 里点名是哪家店。
   `overdue_count` 是最常见的一个（某店的数据源不支持逾期扫描）。

权限：任一店不通过就**整体** 403/404，不返回「少了一家店」的合计
（PRD §9.2 要禁掉的正是这种「伪装成成功的错误」）。`stores` 上限 10 个店，越界 422。

### 店铺列表接口

```
GET /api/stores
```

```json
{
  "stores": [
    {"alias": "store_alpha", "display_name": "OZON 俄罗斯站 · 主力店",
     "available": true, "error": null,
     "data_cutoff": "2026-09-11T00:46:47.314183+08:00", "window_end": "2026-09-10"}
  ],
  "total": 1,
  "page_size_options": [10, 20, 50, 100],
  "max_aggregate_stores": 10,
  "config_hint": "新增/下线店铺要改服务端 OZON_STORES 环境变量后重启…"
}
```

* `stores` 只含**该用户可见**的店铺（root 全部，其余按 `users.json` 的授权过滤）。
* `available` 是**真的打开一次库**得到的结果；打不开时 `error` 里写明原因，
  不是静默跳过 —— placeholder 店铺在界面上会显示成「库不可用」并给出原因。
* **库路径不下发**：它是服务端配置，前端只需要别名。
* 这个接口**没有写操作**：新增/下线店铺改 `OZON_STORES` 后重启。
  本项目的性质是「`api/` 下没有任何写路由」，不为一个管理界面破掉它。

### 看板响应形状

**前端已按这个形状写好了，任何字段的增删改名都是破坏性变更。**

```json
{
  "store_alias": "store_alpha",
  "data_cutoff": "2026-09-11T00:46:47.314183+08:00",
  "period": {"start": "2026-08-28", "end": "2026-09-10", "days": 14},
  "totals": {
    "actual_profit_cny": "48215.95",
    "estimated_profit_cny": "46082.29",
    "completion_rate": "1.0000",
    "complete_order_count": 1978,
    "total_order_count": 1978,
    "overdue_count": 16
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

响应体里有**两个时间边界，它们不是一回事**（详见 ADR-0007）：

| 字段 | 是什么 | 怎么来的 |
|---|---|---|
| `data_cutoff` | 「这份数据什么时候同步进来的」 | 财务表 `updated_at` 的最大值（UTC 存储 → 换算成北京时间下发） |
| `period.end` | 窗口右端：「窗口排到哪天」 | 锁定快照里最大的 `settlement_date` |

窗口右端取的是 **`period.end`（最后一个已结算日）**，不是 `data_cutoff`，
也**不是**系统当天：

* 用系统当天 → 店铺库是定期同步的，最后几天永远为空，看板「看着正常但没有数」；
* 用 `data_cutoff` → 写入时间会被同步动作刷新到数据之后（**真实踩过的坑**：
  财务同步停在 09-10，逾期扫描在 09-19 跑过，`days=7` 的窗口
  09-13~09-19 里一天财务数据都没有，接口返回 0 单 / `actual_profit_cny: null`）。

上面这个例子里两者正好差一天（`data_cutoff` 是北京 09-11 凌晨的那次同步，
数据覆盖到 09-10），这是正常的：它们回答的是两个不同的问题。

### 逐 SKU 下钻响应形状（ADR-0006）

**这是独立接口、独立响应形状**：现有看板那份被前端依赖，加字段进去会一起坏。

```
GET /api/dashboard/store/{alias}/sku-detail?days=14&limit=500&offset=0
    &sort=estimated_profit_cny&desc=true&q=货号或SKU或商品名
```

```json
{
  "store_alias": "store_alpha",
  "data_cutoff": "2026-09-11T00:46:47.314183+08:00",
  "period": {"start": "2026-08-28", "end": "2026-09-10", "days": 14},
  "query": {"sort": "estimated_profit_cny", "desc": true, "q": null,
            "limit": 500, "offset": 0, "sort_keys": ["..."]},
  "totals": {
    "sku_count": 115, "matched_sku_count": 115, "returned_count": 115, "truncated": false,
    "order_count": 1978, "missing_cost_sku_count": 3, "incomplete_sku_count": 7,
    "quantity": 2127,
    "revenue_cny": "87041.27", "purchase_cost_cny": "26082.88",
    "logistics_cny": "14831.53", "platform_fee_cny": null,
    "estimated_profit_cny": "46126.86", "actual_profit_cny": "48085.27",
    "estimated_profit_complete_only_cny": "45882.38",
    "actual_profit_complete_only_cny": "47205.61"
  },
  "unattributed": {
    "posting_count": 3, "actual_posting_count": 5,
    "revenue_cny": null, "purchase_cost_cny": "22.85",
    "logistics_cny": "21.72", "platform_fee_cny": null,
    "estimated_profit_cny": "-44.57", "actual_profit_cny": "130.68",
    "reasons": [{"reason": "cost_not_attributable", "label": "按行成本与订单成本对不上，不摊分",
                 "field": "purchase_cost_cny", "posting_count": 2, "amount_cny": "22.85"},
                {"reason": "multi_item_posting", "label": "多货号订单，该费用无按货号的分组键",
                 "field": "logistics_cny", "posting_count": 2, "amount_cny": "21.72"}]
  },
  "reconciliation": {
    "sku_level": {"order_count": 1978, "revenue_cny": "87041.27", "...": "..."},
    "order_level": {"order_count": 1978, "revenue_cny": "87041.27", "...": "..."},
    "matches": {"revenue_cny": true, "purchase_cost_cny": true, "logistics_cny": true,
                "platform_fee_cny": true, "estimated_profit_cny": true,
                "actual_profit_cny": true, "order_count": true},
    "note": "逐 SKU 合计 + 未归属 = 订单口径合计。"
  },
  "rows": [
    {
      "offer_id": "csx-6.11-45", "sku": "3307891350", "product_name": "Плетеная корзина ...",
      "quantity": 1655, "order_count": 1538,
      "revenue_cny": "70638.00", "purchase_cost_cny": "21481.90",
      "logistics_cny": null, "platform_fee_cny": null,
      "estimated_profit_cny": "36820.43", "actual_profit_cny": "40371.99",
      "estimated_complete": true, "actual_complete": true, "complete": true,
      "unknown_reason": null, "unknown_reason_label": null,
      "missing_fields": ["logistics_cny", "platform_fee_cny"], "incomplete_order_count": 0
    }
  ]
}
```

读这份响应的四条要点：

1. **`purchase_cost_cny` 是 `null` 就是「缺成本」**，不是 0 —— 前端必须显示明确标记，
   而且该 SKU 的 `estimated_profit_cny` / `actual_profit_cny` 也会是 `null`。
2. **`totals` / `unattributed` / `reconciliation` 都是全窗口口径**，
   不随 `q` / `sort` / `limit` / `offset` 变化。搜索后「表里的行加起来 ≠ 上面的合计」是正常的。
3. **`unattributed` 是「归不到货号的金额」**，一分钱都不摊、也不丢：
   逐 SKU 合计 + 未归属 = 订单口径合计（`reconciliation.matches` 就是这个等式的结果）。
4. `sort` 只接受白名单（`estimated_profit_cny` / `actual_profit_cny` / `purchase_cost_cny` /
   `revenue_cny` / `quantity` / `order_count` / `offer_id`），越界 422；
   `limit` 上限 500。数据源不支持下钻（夹具）时返回 **501**，不返回空表。

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
| `store_beta` | 同上路径把 `store_alpha` 换成 `store_beta`（**实测该库真实存在**，见下方更正） | 仅 root |

`store_beta` 的库**真实存在**（7.3 MB / 45 单，2026-09-18 实测，见文末「更正记录」）。
它同时承担两件事：证明「运营/财务拿不到它」（他们只被授权 `store_alpha`，
访问它得到 **403**），以及证明 root 能看到第二个店铺（得到真实数据，不是 404/503）。

> 历史说明（已被更正）：本文档曾称该库不存在、root 访问返回 503。
> 若要新增一个**尚未就位**的店铺别名，正确行为是 503 而不是 404 ——
> 别名注册了但库没到位属于服务端配置故障，用 503 让运维看得见。

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
| `data_cutoff(alias)` | 两个时间边界：`cutoff`（写入时间，展示用）与 `window_end`（最后一个已结算日，**窗口右端**） | `base.DataCutoff` |
| `orders_for_period(alias, start, end, limit, offset)` | 窗口内的订单，**带齐喂给领域层的输入** | 看板权威路径 |
| `amounts_for_period(alias, start, end)` | 窗口合计（廉价路径，一次 SQL 扫描） | 对账/告警 |
| `daily_amounts(alias, start, end)` | 按天汇总（廉价路径） | 对账/告警 |
| `overdue_count(alias)` | 逾期单数 | → `base.OverdueInfo` |
| `direct_net_totals(alias, start, end)` | 直接净额逐单取数/汇率（要按每单汇率折算才能相加） | `amounts_for_period` 内部 |
| `sku_detail_for_period(alias, start, end)` | 逐 SKU 明细行 + 归不出去的金额（ADR-0006） | 下钻接口 |

时间窗口的约定（两种实现必须一致）：

* 订单属于哪个窗口，由**锁定结算快照的 `settlement_date`** 决定 ——
  它一旦锁定就不再变化，同一订单重复查询落点稳定。
* **窗口右端 = 锁定快照里最大的 `settlement_date`**（`DataCutoff.window_end`），
  不能用任何 `updated_at`：写入时间来自同步动作，可以整体刷新到数据之后，
  而窗口查询的过滤条件恰恰是结算日落在区间内 —— 右端越过去就必然是空窗。
  这条口径与上面那条必须写在同一个地方，Postgres 迁移时不要只搬一半（ADR-0007）。
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
3. **店铺元信息写死在配置里**（`config.STORE_DISPLAY_NAMES` + `OZON_STORES`），
   还没有店铺表。`GET /api/stores` 把这些配置**读出来**下发（含可用性探测），
   但新增/下线店铺仍然要改配置后重启 —— 没有写路由，也没打算加。
4. **订单明细分页是服务端分页，但服务端仍按整窗口取数**（ADR-0008 §四）。
   分页参数（`orders_offset` / `orders_limit`）只裁剪 `orders` 数组，
   合计/趋势/构成的取数范围不变 —— 它们必须覆盖整个窗口，这是锁定口径。
   所以分页省下的是**响应体积与浏览器 DOM**（200 行 → ≤100 行），
   **不是后端的取数开销**。
   后端优化的诱惑是「合计走廉价聚合路径，只对当前页跑领域层」，**刻意不做**：
   廉价路径的预估利润取库里存下来的派生值、逐单路径是现算，两者在已知脏值
   （D3，1 单）上不同 —— 一旦按分页状态切换数据路径，同一张指标卡会在翻页时
   显示不同数字，这是最难查的一类 bug。

### 8.2 没能实现 / 需要业务确认

1. **逾期数不按看板窗口过滤**。取自 `overdue_fast_current`（当前生效的那次扫描）。
   这张表存的是**当前**在途逾期集合，不是历史每日快照，
   按窗口过滤只会把结果打成 0。所以 `totals.overdue_count` 是「截至最近一次
   逾期扫描的当前逾期单数」，与 `period` 无关。
2. **逾期口径与窗口右端来自两个互不相干的表**（2026-09-19 修正）：
   `overdue_count` 来自 `overdue_fast_scans`（运营扫描，本库最后更新 09-19），
   窗口右端来自 `settlement_snapshots.settlement_date`（财务结算，最后 09-10）。
   **两者故意不同步** —— 曾经让 `data_cutoff` 取所有表 `updated_at` 的最大值，
   于是「昨天扫过逾期」会把窗口右端推到财务数据之后，`days=7` 直接返回 0 单。
   现在 `overdue_fast_scans.updated_at` 只记录在 `DataCutoff.candidates` 里
   供排查，不参与任何判断（ADR-0007）。
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
6. ~~**没有按 SKU / 商品维度的聚合**~~ —— 已实现，见
   `GET /api/dashboard/store/{alias}/sku-detail`（ADR-0006）。
   仍未实现的是 **§7.4 回款测算**（core 也没有）。
7. ~~**`store_beta` 的库文件不存在**~~ —— **此条已作废，见文末「更正记录」**。
   该库真实存在，root 访问它返回真实数据（45 单）；
   「注册了但库没到位」应返回 503 而不是 404（配置故障要让运维看见）。
8. **多店合计不做店间抵消，也不做多店 SKU 明细**（ADR-0008 §六）：
   我们没有内部交易，没有可抵消项；逐 SKU 归属依赖单店库的分组键，
   跨库按货号聚合前得先确认「同一货号在不同店的采购成本是不是同一份」（业务确认）。
   因此**合计模式下前端禁用逐 SKU 下钻**，界面上直说原因。

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
| `period.end` | 锁定快照里最大的 `settlement_date`（= `DataCutoff.window_end`），**不是** `data_cutoff` |
| `data_cutoff` | `max`(快照 `updated_at`, 事实表 `updated_at`, 流水表 `updated_at`) → 北京时间；**只用于展示**，见 ADR-0007 |

### 8.4 只读保证

* `SqliteSource` 一律以 `mode=ro` 打开，构造时做写保护自检
  （尝试 `CREATE TABLE` 必须抛 `OperationalError`），失败即启动失败。
* `api/` 下没有任何写路由；SQL 全是 `SELECT`。
* 有一条测试（`test_source_is_readonly`）在真实库上再验一次。

---

## 更正记录

### 2026-09-18：`store_beta` 库**确实存在**，此前文档写错了

本文档原先称「`store_beta` 库文件不存在，注册该别名只为让 403 分支可复现」。
**这是错的。** 实测：

```
<DATA_ROOT>\data\stores\store_beta.db   7.3 MB，真实存在
```

影响：

- root 访问 `/api/dashboard/store/store_beta` 返回的是**真实数据**（45 单），
  不是文档原先描述的 503。这是正确行为 —— root 本就该能访问全部店铺。
- **403 分支仍然可复现，但要用受限角色**（`finance01` / `operator01`），
  它们只能访问 `store_alpha`，访问 `store_beta` 得到 403。
  这一点在测试里已经这么写了，不受影响。
- 白名单注册 `store_beta` 本身没有问题，无需改动。

教训：文档里的环境断言也要实测。这条是前端联调时发现的（对方登录 root
访问 `store_beta` 拿到的是真实数据，与文档描述不符才报上来）。
