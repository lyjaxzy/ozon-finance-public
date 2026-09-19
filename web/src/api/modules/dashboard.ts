import backend from "@/api/backendRequest";
import type { ResDashboard, ResSkuDetail } from "@/api/interfaces/backend";
import { DEFAULT_STORE_ALIAS, type StoreAlias } from "@/config/store";

/**
 * @description 单店财务看板的真实数据源
 *
 * 真实接口（api/README.md 第 3 节）：
 *   GET /api/dashboard/store/{alias}?days=14
 *
 * 关键约定：
 *  - **所有金额/汇率都是字符串**，前端只做格式化，绝不用浮点做加减；
 *  - `totals.completion_rate` 是字符串小数（如 "1.0000"），展示时转百分比；
 *  - `orders` 有上限（后端 OZON_MAX_ORDERS，当前 200 条），
 *    全量口径在 `totals` / `trend` / `composition` 里，页面必须写明这一点，
 *    否则「明细加起来 ≠ 合计」会被当成 bug；
 *  - 窗口是 `days` 而不是任意日期区间：后端窗口右端取 `data_cutoff` 的日期
 *    （不是系统当天），所以前端不该自己算日期区间，只能传天数。
 */

/** 请求某店铺某窗口的看板 */
export const getStoreDashboardApi = async (alias: string, days: number): Promise<ResDashboard> => {
  const safeDays = Math.min(Math.max(Math.trunc(days) || 1, 1), 365);
  return backend.get<unknown, ResDashboard>(`/dashboard/store/${alias}`, { params: { days: safeDays } });
};

/** 默认店铺（集中配置在 @/config/store，接多店时只改那一处） */
export const DEFAULT_ALIAS: StoreAlias = DEFAULT_STORE_ALIAS;

/** 订单核算状态枚举（真实字段是布尔 `complete`，不是旧 mock 的字符串 status） */
export const ORDER_STATUS_MAP: Record<"complete" | "incomplete", { label: string; tagType: "success" | "warning" }> = {
  complete: { label: "完整", tagType: "success" },
  incomplete: { label: "不完整", tagType: "warning" }
};

/* ────────────────────────── 逐 SKU 利润下钻 ────────────────────────── */

/**
 * @description 逐 SKU 利润明细（ADR-0006）
 *
 * 真实接口：`GET /api/dashboard/store/{alias}/sku-detail?days=&limit=&offset=&sort=&desc=&q=`
 *
 * 与看板接口的关系：**它是独立接口**，因为看板那份响应形状被前端依赖，不能加字段；
 * 而且逐 SKU 归属要额外跑一遍取数与对账，不该由每次看板刷新买单。
 *
 * 关键约定：
 *  - 窗口口径与看板**逐字一致**（同为 data_cutoff 决定的 days 天），否则合计会对不上；
 *  - 金额一律字符串；`purchase_cost_cny === null` 表示**缺成本**，必须显示标记而不是 0；
 *  - `totals` / `reconciliation` 是**全窗口**口径，搜索与分页都不影响它们；
 *  - `unattributed` 里是归不到货号的金额（含原因）—— 后端不做任何分摊。
 */
export interface SkuDetailQuery {
  days: number;
  sort?: string;
  desc?: boolean;
  q?: string;
  limit?: number;
  offset?: number;
}

/** 拉取某店铺某窗口的逐 SKU 利润明细 */
export const getStoreSkuDetailApi = async (alias: string, query: SkuDetailQuery): Promise<ResSkuDetail> => {
  const safeDays = Math.min(Math.max(Math.trunc(query.days) || 1, 1), 365);
  const params: Record<string, string | number | boolean> = { days: safeDays };
  if (query.sort) params.sort = query.sort;
  if (query.desc !== undefined) params.desc = query.desc;
  if (query.q) params.q = query.q;
  if (query.limit !== undefined) params.limit = Math.min(Math.max(Math.trunc(query.limit) || 1, 1), 500);
  if (query.offset) params.offset = query.offset;
  return backend.get<unknown, ResSkuDetail>(`/dashboard/store/${alias}/sku-detail`, { params });
};

/** 下钻面板：两个指标卡共用同一个接口，只是排序与高亮列不同 */
export type SkuProfitMode = "estimated" | "actual";

export const SKU_PROFIT_MODE_MAP: Record<SkuProfitMode, { label: string; profitField: string; title: string }> = {
  estimated: { label: "预估利润", profitField: "estimated_profit_cny", title: "逐 SKU 预估利润明细" },
  actual: { label: "实际利润", profitField: "actual_profit_cny", title: "逐 SKU 实际利润明细" }
};
