/**
 * @description 店铺相关的**界面常量**（别名本身不在这里）
 *
 * ⚠️ 2026-09-19 起别名不再写死在前端（ADR-0008）：
 * 可见店铺由 `GET /api/stores` 下发（它已经按登录用户的授权过滤过），
 * 页面用 `useStoreStore()` 取。这里只留两类东西：
 *   1. 界面档位（统计窗口天数、每页条数）—— 它们的兜底值；
 *   2. 后端列表还没拿到时的**兜底别名**，避免首屏空转。
 *
 * 为什么不再写死别名：写死等于在前端维护第二份店铺白名单。
 * 后端加一个店、或某个账号被收回某个店的授权，前端不改就会显示一个点不开的入口。
 *
 * 后端契约（见 api/README.md 第 3 节）：
 *  - 别名白名单由后端 `OZON_STORES` 环境变量注册，前端**不能**凭空造别名；
 *  - 别名格式 `^[a-z0-9_-]{1,64}$`；
 *  - 未知别名 → 404「店铺不存在」，已知但无授权 → 403「无权访问该店铺」。
 */

/** 店铺别名（后端下发，运行时才知道有哪些） */
export type StoreAlias = string;

/**
 * 后端店铺列表拿到之前的兜底别名。
 * 只在首屏用一次；拿到列表后一律以后端为准（列表为空则显示空态而不是这个值）。
 */
export const FALLBACK_STORE_ALIAS = "store_alpha";

/** 看板默认统计窗口（天），后端 days 取值 1–365 */
export const DEFAULT_DASHBOARD_DAYS = 14;

/** 看板可选的统计窗口 */
export const DASHBOARD_DAY_OPTIONS = [7, 14, 30, 90] as const;

/** 表格可选的每页条数（后端 `/api/stores` 也会下发一份，以它为准） */
export const PAGE_SIZE_OPTIONS = [10, 20, 50, 100] as const;

/** 订单明细与逐 SKU 明细的默认每页条数 */
export const DEFAULT_PAGE_SIZE = 20;
