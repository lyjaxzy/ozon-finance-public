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

/** GET /api/auth/me */
export interface ResMe {
  user: ResUser;
  stores: string[];
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
