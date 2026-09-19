import { defineStore } from "pinia";
import { computed, ref } from "vue";

import type { BackendError } from "@/api/backendRequest";
import type { ResStoreInfo, ResStoreList } from "@/api/interfaces/backend";
import { getStoreListApi } from "@/api/modules/stores";
import { FALLBACK_STORE_ALIAS, PAGE_SIZE_OPTIONS } from "@/config/store";

/**
 * @description 可见店铺列表（ADR-0008）
 *
 * 为什么单独一个 store 而不是每个页面自己请求：
 *  - 看板页与店铺管理页都要它，而且**两页之间跳转不该各请求一次**；
 *  - 「当前选中的店铺」在店铺管理页勾选、在看板页展示，需要一个共同的地方落。
 *
 * ⚠️ 列表**只做缓存**，不伪造：请求失败时 `error` 非空、`stores` 为空，
 * 页面据此显示错误态，而不是拿兜底别名假装成功。
 */
export const useStoreStore = defineStore("ozon-stores", () => {
  const stores = ref<ResStoreInfo[]>([]);
  const pageSizeOptions = ref<number[]>([...PAGE_SIZE_OPTIONS]);
  const maxAggregateStores = ref(10);
  const configHint = ref("");
  const loading = ref(false);
  const error = ref<BackendError | null>(null);
  /** 是否已经成功取过一次（避免每次进页面都重新请求） */
  const loaded = ref(false);

  /** 库可用的店铺 —— 界面上的「能点开」的入口只从这里来 */
  const availableStores = computed(() => stores.value.filter(s => s.available));

  const aliasOf = (alias: string) => stores.value.find(s => s.alias === alias) ?? null;

  const displayName = (alias: string) => aliasOf(alias)?.display_name ?? alias;

  /**
   * 取列表。`force=true` 时跳过缓存（用户点「刷新」时用）。
   * 失败时**清空** stores —— 保留上一次的列表会让人以为店铺还在。
   */
  const load = async (force = false) => {
    if (loading.value) return;
    if (loaded.value && !force) return;
    loading.value = true;
    error.value = null;
    try {
      const data: ResStoreList = await getStoreListApi();
      stores.value = data.stores;
      pageSizeOptions.value = data.page_size_options?.length ? data.page_size_options : [...PAGE_SIZE_OPTIONS];
      maxAggregateStores.value = data.max_aggregate_stores ?? 10;
      configHint.value = data.config_hint ?? "";
      loaded.value = true;
    } catch (err) {
      error.value = err as BackendError;
      stores.value = [];
      loaded.value = false;
    } finally {
      loading.value = false;
    }
  };

  /**
   * 把一组别名收敛成「当前真的可以看」的那些。
   * 用于 URL 里带了无权/不存在别名时清掉它，而不是让页面去吃 403。
   */
  const resolveAliases = (wanted: string[]) => wanted.filter(alias => aliasOf(alias)?.available);

  /** 兜底：列表还没拿到时先给一个默认店铺，拿到后由调用方按 resolveAliases 收敛 */
  const defaultAliases = () => (availableStores.value.length ? [availableStores.value[0].alias] : [FALLBACK_STORE_ALIAS]);

  return {
    stores,
    pageSizeOptions,
    maxAggregateStores,
    configHint,
    loading,
    error,
    loaded,
    availableStores,
    aliasOf,
    displayName,
    load,
    resolveAliases,
    defaultAliases
  };
});
