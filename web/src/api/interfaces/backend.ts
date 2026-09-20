/**
 * @description OZON 真实后端（api/，FastAPI）的响应类型
 *
 * 与脚手架自带的 `@/api/interface` 区别：
 *  - `@/api/interface` 是 geeker 脚手架的 `{code,msg,data}` 包装格式（历史遗留，已不使用）；
 *  - 本文件描述的是**真实后端原样返回的 JSON**，没有任何包装。
 *
 * 契约来源：`api/README.md` 第 3 节「看板响应形状」。
 * ⚠️ 所有金额与汇率都是**字符串**（后端为避免 JS 浮点误差刻意下发字符串），
 *    前端只做格式化显示，**绝不用浮点做加减**。
 */

import type { StoreAlias } from "@/config/store";

/** 登录响应：POST /api/auth/login */
export interface ResLogin {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: ResUser;
}

/** 用户信息（令牌里只有身份，授权明细每次现查） */
export interface ResUser {
  username: string;
  display_name: string;
  /** root / finance / store_operator */
  role: string;
  /** 该用户可见的店铺别名；**空数组代表不限制（root 全部店铺）** */
  store_aliases: string[];
}

/** /api/auth/me 与 /api/auth/login 里下发的店铺条目（只有别名与展示名） */
export interface ResMeStore {
  alias: string;
  display_name: string;
}

/**
 * GET /api/auth/me
 *
 * ⚠️ `stores` 是**对象数组**（`{alias, display_name}`，与 `api/schemas.py::StoreOut`
 * 一致），不是别名字符串数组 —— 实测 `{"user":{...},"stores":[{"alias":"store_alpha",...}]}`。
 * 前端**不要**在这里取店铺列表（那要用 `GET /api/stores`，
 * 它多下发布局可用性与最近结算日）；`/auth/me` 的用处是拿 `user.role`。
 */
export interface ResMe {
  user: ResUser;
  stores: ResMeStore[];
}

/** 统计窗口 */
export interface ResPeriod {
  /** YYYY-MM-DD */
  start: string;
  /** YYYY-MM-DD */
  end: string;
  days: number;
}

/** 顶部指标（金额是字符串，计数是数字） */
export interface ResTotals {
  /** 实际利润合计（CNY），字符串 2 位小数 */
  actual_profit_cny: string | null;
  /** 预估利润合计（CNY），字符串 2 位小数 */
  estimated_profit_cny: string | null;
  /** 核算完成率，字符串小数，如 "0.6667" */
  completion_rate: string;
  complete_order_count: number;
  total_order_count: number;
  /**
   * 逾期单数。
   * ⚠️ 它是「截至 data_cutoff 的当前逾期单数」，**与 period 窗口无关**（见 api/README 8.2-1）。
   */
  overdue_count: number;
}

/** 趋势图单日数据点 */
export interface ResTrendPoint {
  date: string;
  actual_profit_cny: string | null;
  estimated_profit_cny: string | null;
}

/** 利润构成项 */
export interface ResCompositionItem {
  /** direct_net / purchase / platform / logistics */
  key: string;
  label: string;
  value_cny: string | null;
}

/** 订单明细行 */
export interface ResOrderRow {
  posting_number: string;
  settlement_date: string | null;
  /** 直接净额（₽），字符串 */
  direct_net_rub: string | null;
  /** 卢布对人民币汇率，字符串 4 位小数（字段名与旧 mock 的 exchange_rate 不同） */
  exchange_rate_rub_per_cny: string | null;
  /** 采购成本（¥），字符串；缺失是 null 而不是 "0.00" */
  purchase_cost_cny: string | null;
  /** 实际利润（¥），字符串；缺失是 null */
  actual_profit_cny: string | null;
  /** 核算是否完整（取代旧 mock 的 status: complete/incomplete） */
  complete: boolean;
  /** 核算不完整的原因，complete 为 true 时是 null */
  unknown_reason: string | null;
}

/** GET /api/dashboard/store/{alias}?days=n */
export interface ResDashboard {
  store_alias: StoreAlias | string;
  /** 库里最后一个可观测写入时间，北京时间 ISO 串 */
  data_cutoff: string;
  period: ResPeriod;
  totals: ResTotals;
  /** ⚠️ 明细有上限（后端 OZON_MAX_ORDERS，当前 200 条），totals/trend 才是全量口径 */
  orders: ResOrderRow[];
  trend: ResTrendPoint[];
  composition: ResCompositionItem[];
}

/** GET /api/health */
export interface ResHealth {
  status: string;
  data_source_readonly: boolean;
  store_aliases: string[];
  orders_in_response_limit: number;
}

/* ──────────────────────────────────────────────────────────────────
 * 店铺列表与多店合计（ADR-0008）
 *
 * 契约来源：
 *   GET /api/stores                                  → 当前用户可见的店铺
 *   GET /api/dashboard/aggregate?stores=a,b&days=14   → 多店合计
 *
 * ⚠️ 三条铁律：
 *  1. **前端不做任何跨店累加**：金额是字符串，JS 的浮点加法会引入尾差；
 *     合计一律取后端下发的字符串。
 *  2. 合计的窗口右端是**各店里最早的那个已结算日**（共同窗口），
 *     所以逐店的 `period` 与顶层 `period` 是同一个区间 —— 不能各算各的。
 *  3. `overdue_count` 等字段为 `null` 表示**算不出来**，不许当成 0。
 * ────────────────────────────────────────────────────────────────── */

/** 一个店铺的可用性与最新数据时间 */
export interface ResStoreInfo {
  alias: string;
  display_name: string;
  /** 库能不能以只读方式打开；false 时看 `error` */
  available: boolean;
  /** available=false 时的原因（服务端配置故障要如实透出） */
  error: string | null;
  /** 最近一次写入时间（北京时间 ISO），只用于展示 */
  data_cutoff: string | null;
  /** 最后一个已结算日 YYYY-MM-DD；null 表示一单都没结算 */
  window_end: string | null;
}

/** GET /api/stores */
export interface ResStoreList {
  stores: ResStoreInfo[];
  total: number;
  /** 允许的每页条数档位（后端唯一定义，前端不各写一份） */
  page_size_options: number[];
  max_aggregate_stores: number;
  /** 「新增店铺该怎么做」的说明文案，直接展示给用户 */
  config_hint: string;
}

/** 合计里的一家店（它自己的合计，窗口与顶层一致） */
export interface ResAggregateStore {
  alias: string;
  display_name: string;
  data_cutoff: string | null;
  /** 该店自己的最后结算日；与顶层 window_end 不同时说明它拖后腿了 */
  window_end: string | null;
  period: ResPeriod;
  totals: ResTotals;
}

/** GET /api/dashboard/aggregate?stores=a,b */
export interface ResAggregate {
  store_aliases: string[];
  stores: ResAggregateStore[];
  /** 各店最晚的一次写入（展示用） */
  data_cutoff: string | null;
  /** 共同窗口右端 = 各店已结算日里最早的那个（口径用） */
  window_end: string | null;
  period: ResPeriod;
  totals: ResTotals;
  trend: ResTrendPoint[];
  composition: ResCompositionItem[];
  /** 「合计里少了什么」的明文说明 —— 合计最会骗人的地方就是它不说话 */
  warnings: string[];
}

/* ──────────────────────────────────────────────────────────────────
 * 逐 SKU 利润下钻（ADR-0006）
 *
 * 契约来源：`api/routers/sku_detail.py`
 *   GET /api/dashboard/store/{alias}/sku-detail?days=&limit=&offset=&sort=&desc=&q=
 *
 * ⚠️ 两条铁律（与后端一致）：
 *  1. `purchase_cost_cny` 等金额为 `null` 表示**缺失**，前端必须显示成「缺成本」
 *     之类的明确标记，**绝不当成 0**，也不许用 0 参与任何前端计算；
 *  2. 所有金额/合计都以后端下发的字符串为准，前端只做格式化。
 * ────────────────────────────────────────────────────────────────── */

/** 逐 SKU 明细行 */
export interface ResSkuRow {
  /** 货号（分组键，采购成本也按它录入） */
  offer_id: string;
  sku: string | null;
  product_name: string | null;
  quantity: number;
  order_count: number;
  revenue_cny: string | null;
  /** 采购成本；**缺失是 null 而不是 "0.00"** */
  purchase_cost_cny: string | null;
  logistics_cny: string | null;
  platform_fee_cny: string | null;
  estimated_profit_cny: string | null;
  actual_profit_cny: string | null;
  estimated_complete: boolean;
  actual_complete: boolean;
  complete: boolean;
  /** 主缺失原因（枚举取值与 core/domain/profit.py 一致） */
  unknown_reason: string | null;
  /** 上者的中文标签（后端映射，前端不再维护一份） */
  unknown_reason_label: string | null;
  /** 该 SKU 取不到值的字段名列表 */
  missing_fields: string[];
  /** 该 SKU 名下有多少单不完整 */
  incomplete_order_count: number;
}

/** 一笔归不出去的金额（含原因） */
export interface ResSkuUnattributedReason {
  reason: string;
  label: string;
  field: string;
  posting_count: number;
  amount_cny: string | null;
}

/** 未归属区块：**归不到货号的钱都在这里，绝不分摊、绝不丢弃** */
export interface ResSkuUnattributed {
  posting_count: number;
  revenue_cny: string | null;
  purchase_cost_cny: string | null;
  logistics_cny: string | null;
  platform_fee_cny: string | null;
  estimated_profit_cny: string | null;
  actual_profit_cny: string | null;
  actual_posting_count: number;
  reasons: ResSkuUnattributedReason[];
}

/** 对账的一方（逐 SKU 口径或订单口径，字段同形） */
export interface ResSkuLevelTotals {
  order_count: number;
  revenue_cny: string | null;
  purchase_cost_cny: string | null;
  logistics_cny: string | null;
  platform_fee_cny: string | null;
  estimated_profit_cny: string | null;
  actual_profit_cny: string | null;
}

/** 两条口径的对账结果 */
export interface ResSkuReconciliation {
  sku_level: ResSkuLevelTotals;
  order_level: ResSkuLevelTotals;
  /** 逐字段是否一致：逐 SKU 合计 + 未归属 == 订单口径合计 */
  matches: Record<string, boolean>;
  note: string;
}

/** 逐 SKU 下钻的合计（**全窗口口径，不受搜索/分页影响**） */
export interface ResSkuTotals {
  sku_count: number;
  matched_sku_count: number;
  returned_count: number;
  truncated: boolean;
  order_count: number;
  missing_cost_sku_count: number;
  incomplete_sku_count: number;
  estimated_incomplete_sku_count: number;
  actual_incomplete_sku_count: number;
  quantity: number;
  revenue_cny: string | null;
  purchase_cost_cny: string | null;
  logistics_cny: string | null;
  platform_fee_cny: string | null;
  /** 逐 SKU 口径合计（缺失项按 0 参与，与指标卡同口径） */
  estimated_profit_cny: string | null;
  actual_profit_cny: string | null;
  /** 只累加完整 SKU 的展示值 */
  estimated_profit_complete_only_cny: string | null;
  actual_profit_complete_only_cny: string | null;
}

/** GET /api/dashboard/store/{alias}/sku-detail */
export interface ResSkuDetail {
  store_alias: string;
  data_cutoff: string | null;
  period: ResPeriod;
  query: {
    sort: string;
    desc: boolean;
    q: string | null;
    limit: number;
    offset: number;
    sort_keys: string[];
  };
  totals: ResSkuTotals;
  unattributed: ResSkuUnattributed;
  reconciliation: ResSkuReconciliation;
  rows: ResSkuRow[];
}

/* ──────────────────────────────────────────────────────────────────
 * 采购成本库与成本管理（ADR-0009）
 *
 * 契约来源：`api/routers/costs.py` + `api/costs.py`
 *   GET  /api/costs?keyword=&limit=&offset=        台账（服务端分页 + 搜索）
 *   GET  /api/costs/events?seller_sku=&limit=&offset=  变更留痕
 *   GET  /api/costs/missing?alias=&days=&limit=    缺成本清单
 *   POST /api/costs/import/preview                 multipart 预览（**不落库**）
 *   POST /api/costs/import/apply                   multipart 落库
 *   POST /api/costs/migrate-legacy                 从原产品成本库迁移（幂等）
 *
 * ⚠️ 四条铁律：
 *  1. **金额一律是字符串**，前端只做格式化显示，绝不用浮点做加减；
 *  2. `null` 表示**缺失**（缺成本），不是 0 —— 显示成「--」，不许当 0 参与计算；
 *  3. 「缺成本清单为空」= 窗口内每个货号都有单价（**正确结果**），不是加载失败；
 *  4. 写操作只有 `root` / `finance` 能过 —— 权限隔离在服务端，
 *     前端按角色隐藏按钮只是少给一个点了会 403 的入口。
 * ────────────────────────────────────────────────────────────────── */

/** 台账里的一行成本（`GET /api/costs` 的 `rows[]`） */
export interface ResCostRow {
  cost_id: string;
  /** 货号（= 订单里的 offer_id，成本按它归属） */
  seller_sku: string;
  /** OZON 数字 SKU；缺失是 null */
  platform_sku: string | null;
  /** 单件成本（CNY），字符串 */
  unit_cost_cny: string;
  /** 生效日期 YYYY-MM-DD：订单按自己的结算日回溯取最近一条 */
  effective_from: string;
  /** 本次恒为 shared（店铺级覆盖是预留字段，ADR-0009 规则 3） */
  scope: string;
  /** 预留：本次恒为 null */
  store_alias: string | null;
  /** confirmed / pending；只有 confirmed 参与核算 */
  status: string;
  source: string | null;
  note: string | null;
  created_at: string;
  updated_at: string;
}

/** 成本变更留痕的一行（`sku_cost_change_events`，只追加不改） */
export interface ResCostChangeEvent {
  id: number;
  cost_id: string | null;
  seller_sku: string;
  effective_from: string;
  /** insert / update / migrated_insert / migrated_update / … */
  action: string;
  old_json: string | null;
  new_json: string | null;
  source: string | null;
  /** 「这批是哪次导入」的文件指纹 */
  file_sha256: string | null;
  actor_id: string | null;
  occurred_at: string;
}

/** GET /api/costs */
export interface ResCostList {
  /** 成本库文件路径（服务端配置，前端只展示，不接受参数指定） */
  book_path: string;
  /** 命中关键词的全量条数（不随分页变化） */
  total: number;
  returned: number;
  limit: number;
  offset: number;
  keyword: string | null;
  rows: ResCostRow[];
  change_event_total: number;
  last_change: ResCostChangeEvent | null;
  /** 成本库还不存在时后端额外下发的说明（此时 total=0、rows=[]） */
  note?: string;
}

/** GET /api/costs/events */
export interface ResCostEvents {
  total: number;
  rows: ResCostChangeEvent[];
  limit: number;
  offset: number;
}

/** 缺成本清单的一行：窗口内有订单、但成本库没有单价的货号 */
export interface ResMissingCostRow {
  seller_sku: string;
  sku: string | null;
  product_name: string | null;
  quantity: number;
  order_count: number;
  /** 订单里已有的归属成本；缺失是 null */
  attributed_cost_cny: string | null;
  /** 后端给出的中文原因（如「成本库里没有这个货号的单价」） */
  reason: string;
}

/** GET /api/costs/missing */
export interface ResMissingCosts {
  store_alias: string;
  period: ResPeriod;
  /** 窗口内出现过的**货号数**（去重，与看板「缺成本 SKU 数」同口径） */
  offer_in_window: number;
  offer_with_price: number;
  missing_count: number;
  missing: ResMissingCostRow[];
  /** 清单被 limit 截断（此时 missing.length < missing_count） */
  truncated: boolean;
  price_source: string;
  book_offer_count: number;
}

/** 预览里的新增/更新行（`unit_cost_cny` 是字符串，更新行多一个旧单价） */
export interface ResCostPreviewRow {
  seller_sku: string;
  platform_sku: string | null;
  unit_cost_cny: string;
  effective_from: string;
  status: string;
  note: string;
  source: string;
  /** 只有「更新」行有：被覆盖掉的旧单价 */
  old_unit_cost_cny?: string;
}

/** 预览里的非法行（行号 + 原始值 + 原因，原样展示给用户改文件） */
export interface ResCostInvalidRow {
  /** Excel/CSV 里的行号（1 基，含表头） */
  line: number;
  value: string;
  reason: string;
}

/** 导入预览体（`preview` 与 `preview_at_apply` 同形） */
export interface ResCostImportPreviewBody {
  total: number;
  created_count: number;
  updated_count: number;
  unchanged_count: number;
  invalid_count: number;
  created: ResCostPreviewRow[];
  updated: ResCostPreviewRow[];
  invalid: ResCostInvalidRow[];
  /** 明细被截断到 50 行（计数仍是全量） */
  truncated: boolean;
  /** 没有任何非法行。**只有它为 true 才允许点「确认导入」** */
  ok: boolean;
}

/** 某一种成本注入策略下的影响面 */
export interface ResCostImpactStrategy {
  affected_orders: number;
  /** 采购成本合计变化（字符串，可负）；缺失是 null */
  delta_purchase_cost_cny: string | null;
  /** 后端写好的口径说明，原样展示（不要改写） */
  note: string;
}

/** 两种策略的影响面（ADR-0009 §四：切「成本库权威」前要有据可依） */
export interface ResCostImpact {
  window: ResPeriod;
  /** 窗口内命中本次导入货号的货号数 */
  matched_skus: number;
  book_first: ResCostImpactStrategy;
  book_authoritative: ResCostImpactStrategy;
  /** 当前生效的策略（book_first / book_authoritative） */
  current_policy: string;
}

/** POST /api/costs/import/preview —— **只预览不落库** */
export interface ResCostImportPreview {
  file: { name: string | null; sha256: string | null };
  book_path: string;
  preview: ResCostImportPreviewBody;
  impact: ResCostImpact;
  parsed_rows: number;
}

/** POST /api/costs/import/apply */
export interface ResCostImportApply {
  applied: { created: number; updated: number; unchanged: number };
  /** 落库前重算的预览（用于核对「预览和实际一致」） */
  preview_at_apply: ResCostImportPreviewBody;
  book_path: string;
  total_after: number;
  actor: string;
  file: { name: string | null; sha256: string | null };
}

/** 迁移时被跳过的旧库行（逐条给原因，不静默丢弃） */
export interface ResCostSkippedRow {
  cost_id: string | null;
  seller_sku: string | null;
  platform_sku: string | null;
  unit_cost_cny: string | null;
  created_at: string | null;
  reason: string;
}

/** POST /api/costs/migrate-legacy —— 幂等，可重复点 */
export interface ResCostMigrate {
  created: number;
  updated: number;
  unchanged: number;
  scanned: number;
  skipped: number;
  skipped_detail: ResCostSkippedRow[];
  /** 跳过明细被截断到 50 行（skipped 计数仍是全量） */
  skipped_truncated: boolean;
  total_after: number;
  legacy_path: string;
  /** 迁移前后旧库 mtime 是否没变（证明我们只读它） */
  legacy_mtime_unchanged: boolean;
  legacy_readonly: boolean;
}
