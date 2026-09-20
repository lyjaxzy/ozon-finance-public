import backend from "@/api/backendRequest";
import type {
  ResCostEvents,
  ResCostImportApply,
  ResCostImportPreview,
  ResCostList,
  ResCostMigrate,
  ResMissingCosts
} from "@/api/interfaces/backend";

/**
 * @description 采购成本管理（ADR-0009）
 *
 * 真实接口（`api/routers/costs.py`）：
 *   GET  /api/costs?keyword=&limit=&offset=
 *   GET  /api/costs/events?seller_sku=&limit=&offset=
 *   GET  /api/costs/missing?alias=&days=&limit=
 *   POST /api/costs/import/preview   （multipart：alias / days / file）
 *   POST /api/costs/import/apply     （multipart：同上）
 *   POST /api/costs/migrate-legacy   （无 body）
 *
 * 关键约定：
 *  - **金额一律字符串**（`"12.20"`），`null` 是「缺失」不是 0；本模块不做任何加减，
 *    影响面（受影响订单数、成本差额）全部由后端算好下发；
 *  - **预览绝不落库**：`import/preview` 只读成本库 + 读订单，返回差异与影响面；
 *    只有 `import/apply` 才写。页面必须做到「没预览过不让确认」；
 *  - 写操作只有 `root` / `finance` 能过（服务端 `COST_WRITE_ROLES`），
 *    `store_operator` 会拿到 403 `{"detail":"无权修改采购成本（只有 root / finance 角色可以）"}`；
 *  - 台账分页上限 100（`OZON_MAX_COST_ROWS`），越界后端 422 —— 这里先夹一次，
 *    避免把「前端传错参数」显示成「加载失败」。
 */

/** 能写成本库的角色（与后端 `api/costs.py::COST_WRITE_ROLES` 对齐，只用于隐藏按钮） */
export const COST_WRITE_ROLES = ["root", "finance"] as const;

/** 台账允许的每页条数上限：后端 Query(le=config.MAX_COST_ROWS_IN_RESPONSE)，当前 100 */
const MAX_COST_ROWS = 100;

/** 台账查询（keyword 同时匹配货号与 OZON 数字 SKU；空关键字表示不过滤） */
export interface CostListQuery {
  keyword?: string;
  limit?: number;
  offset?: number;
}

/** 成本台账（服务端分页 + 搜索） */
export const getCostListApi = async (query: CostListQuery = {}): Promise<ResCostList> => {
  const params: Record<string, string | number> = {
    limit: Math.min(Math.max(Math.trunc(query.limit ?? 50) || 1, 1), MAX_COST_ROWS),
    offset: Math.max(0, Math.trunc(query.offset ?? 0))
  };
  const keyword = (query.keyword ?? "").trim();
  if (keyword) params.keyword = keyword;
  return backend.get<unknown, ResCostList>("/costs", { params });
};

/** 变更留痕（按 id 倒序，最新的在前） */
export const getCostEventsApi = async (query: { seller_sku?: string; limit?: number; offset?: number } = {}) => {
  const params: Record<string, string | number> = {
    limit: Math.min(Math.max(Math.trunc(query.limit ?? 50) || 1, 1), MAX_COST_ROWS),
    offset: Math.max(0, Math.trunc(query.offset ?? 0))
  };
  if (query.seller_sku) params.seller_sku = query.seller_sku;
  return backend.get<unknown, ResCostEvents>("/costs/events", { params });
};

/**
 * 缺成本清单。
 *
 * ⚠️ 实测当前 14 天窗口是 115 个货号 / 115 个有价 / 缺 0 ——
 * **这是正确结果，不是故障**。清单为空说明窗口内不需要补价，
 * 页面上的空态必须这么说（写成「暂无数据」会让人以为接口坏了）。
 */
export const getMissingCostsApi = async (alias: string, days: number, limit = 200): Promise<ResMissingCosts> => {
  const safeDays = Math.min(Math.max(Math.trunc(days) || 1, 1), 365);
  return backend.get<unknown, ResMissingCosts>("/costs/missing", {
    params: { alias, days: safeDays, limit: Math.min(Math.max(Math.trunc(limit) || 1, 1), 1000) }
  });
};

/**
 * 把「别名 / 天数 / 文件」装成 multipart 表单。
 *
 * 为什么显式设 `Content-Type: multipart/form-data`：
 * `backend` 实例默认头是 `application/json`，浏览器不会替我们改它，
 * 后端就会按 JSON 解析而读不到 `file` 字段。显式声明后
 * axios 的 xhr 适配器会把该头**删掉**交给浏览器自己带（含 boundary），
 * 因此这里**不能**自己拼 boundary（手拼必然和后端对不上）。
 */
const buildImportForm = (alias: string, days: number, file: File): FormData => {
  const form = new FormData();
  form.append("alias", alias);
  form.append("days", String(days));
  // 第三个参数显式给文件名：后端按扩展名判断 .xlsx/.xlsm/.csv，缺名字会被 422 拒掉
  form.append("file", file, file.name);
  return form;
};

const MULTIPART_HEADERS = { "Content-Type": "multipart/form-data" } as const;

/** 导入预览（**不落库**）。文件类型不对 / 一行可用数据都没有 → 422 + detail */
export const previewCostImportApi = async (alias: string, days: number, file: File): Promise<ResCostImportPreview> => {
  return backend.post<unknown, ResCostImportPreview>("/costs/import/preview", buildImportForm(alias, days, file), {
    headers: MULTIPART_HEADERS
  });
};

/**
 * 确认导入（落库 + 留痕）。
 *
 * ⚠️ 只要有**一行**非法数据，后端整批 422 且什么都不写，
 * `detail` 会说明第几行什么原因 —— 页面要把 detail 原样展示出来。
 */
export const applyCostImportApi = async (alias: string, days: number, file: File): Promise<ResCostImportApply> => {
  return backend.post<unknown, ResCostImportApply>("/costs/import/apply", buildImportForm(alias, days, file), {
    headers: MULTIPART_HEADERS
  });
};

/**
 * 从原产品成本库迁移（幂等）。
 *
 * 旧库 `<DATA_ROOT>\data\desktop\purchase_costs.db` 只读打开，
 * 迁移是**复制**不是改；再点一次的结果全是 `unchanged`。
 */
export const migrateLegacyCostsApi = async (): Promise<ResCostMigrate> => {
  return backend.post<unknown, ResCostMigrate>("/costs/migrate-legacy", undefined);
};

/** 成本来源策略的中文标签（后端下发 book_first / book_authoritative 两个枚举值） */
export const COST_POLICY_LABELS: Record<string, string> = {
  book_first: "库优先（店铺库有成本就用库里的，为空才用成本库兜底）",
  book_authoritative: "成本库为权威（订单成本一律按成本库单价 × 数量合成）"
};
