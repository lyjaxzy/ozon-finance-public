import backend from "@/api/backendRequest";
import type { ResAggregate, ResStoreList } from "@/api/interfaces/backend";

/**
 * @description 店铺列表与多店合计（ADR-0008）
 *
 * 真实接口：
 *   GET /api/stores
 *   GET /api/dashboard/aggregate?stores=a,b&days=14
 *
 * 关键约定：
 *  - 店铺列表**只含当前用户可见的**店铺，别名不再写死在前端（`@/config/store.ts`
 *    只剩下界面常量）；新增店铺改服务端 `OZON_STORES` 配置，后端没有写路由。
 *  - 合计必须由后端算：前端把几个单店响应加起来会变成第二套口径，
 *    而且金额是字符串，JS 浮点加法会有尾差。
 *  - 合计的窗口右端是各店里**最早**的那个已结算日，逐店 `period` 与顶层一致；
 *    `warnings` 里写明「哪家店的数据没进合计」，界面必须展示它。
 */

/** 拉取当前用户可见的店铺列表 */
export const getStoreListApi = async (): Promise<ResStoreList> => {
  return backend.get<unknown, ResStoreList>("/stores");
};

/**
 * 多店合计。`aliases` 里的顺序即展示顺序，重复项由后端去重。
 * 只传一个店时也可以调用（结果与单店接口一致，有测试把守）。
 */
export const getStoreAggregateApi = async (aliases: string[], days: number): Promise<ResAggregate> => {
  const safeDays = Math.min(Math.max(Math.trunc(days) || 1, 1), 365);
  const stores = aliases.map(a => String(a).trim()).filter(Boolean).join(",");
  return backend.get<unknown, ResAggregate>("/dashboard/aggregate", {
    params: { stores, days: safeDays }
  });
};
