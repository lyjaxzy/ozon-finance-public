/**
 * @description 店铺别名配置 —— 前端唯一改名的入口
 *
 * ⚠️ 为什么单独放一个文件：
 * 页面里出现 `store_alpha` 是可以接受的（当前生产库只有这一个店的真实数据），
 * 但它**必须只在一个地方**出现，将来后端下发多店列表时只改这里。
 *
 * 后端契约（见 api/README.md 第 5 节）：
 *  - 别名白名单由后端 `OZON_STORES` 环境变量注册，前端**不能**凭空造别名；
 *  - 别名受 `^[a-z0-9_-]{1,64}$` 约束；
 *  - 未知别名 → 404「店铺不存在」，已知但无授权 → 403「无权访问该店铺」。
 */

/** 可用的店铺别名（当前后端注册了 store_alpha / store_beta，只有 store_alpha 的库存在） */
export const STORE_ALIASES = ["store_alpha", "store_beta"] as const;

/** 店铺别名联合类型 */
export type StoreAlias = (typeof STORE_ALIASES)[number];

/**
 * 默认展示的店铺。
 * 当前只有 store_alpha 有真实数据，所以默认就是它；
 * store_beta 的库文件不存在（root 请求会得到 503），不要把它设为默认。
 */
export const DEFAULT_STORE_ALIAS: StoreAlias = "store_alpha";

/** 店铺展示名（后端目前把店铺元信息写死在 api/config.STORE_DISPLAY_NAMES 里，前端只做兜底显示） */
export const STORE_DISPLAY_NAMES: Record<StoreAlias, string> = {
  store_alpha: "OZON 俄罗斯站 · 主力店（store_alpha）",
  store_beta: "OZON 俄罗斯站 · 二店（store_beta，暂无数据）"
};

/** 看板默认统计窗口（天），后端 days 取值 1–365 */
export const DEFAULT_DASHBOARD_DAYS = 14;

/** 看板可选的统计窗口 */
export const DASHBOARD_DAY_OPTIONS = [7, 14, 30, 90] as const;
