<template>
  <div class="store-page">
    <!-- 店铺列表拿不到时不白屏：给出原因 + 重试 -->
    <div v-if="storeStore.error" class="card error-panel">
      <el-icon class="error-icon"><Warning /></el-icon>
      <h3 class="error-title">{{ errorTitle }}</h3>
      <p class="error-desc">{{ errorDesc }}</p>
      <div class="error-actions">
        <el-button v-if="storeStore.error.status !== 403" type="primary" :icon="Refresh" @click="reload">重试</el-button>
        <el-button @click="goLogin">去登录</el-button>
      </div>
      <p class="error-tip">接口：<code>GET /api/stores</code> → 127.0.0.1:8849（经 vite dev 代理）</p>
    </div>

    <!-- 一个店都没有：这是授权结果，不是故障 -->
    <div v-else-if="!storeStore.loading && !storeStore.stores.length" class="card">
      <el-empty description="当前账号没有任何可查看的店铺">
        <template #description>
          <p>当前账号没有任何可查看的店铺。</p>
          <p class="empty-hint">
            可见店铺由服务端授权决定（<code>api/data/users.json</code> 里的
            <code>store_aliases</code>），前端不能凭空造别名。
          </p>
        </template>
      </el-empty>
    </div>

    <template v-else>
      <header class="page-head card">
        <div class="head-main">
          <div class="head-title">
            <el-icon class="head-icon"><Shop /></el-icon>
            <span>店铺管理</span>
            <el-tag class="count-tag" size="small" effect="plain">{{ storeStore.stores.length }} 个可见店铺</el-tag>
          </div>
          <div class="head-meta">
            <span class="meta-item">
              <el-icon><InfoFilled /></el-icon>
              勾选多个店铺后点「查看所选店铺合计」，看板会给出这些店的合并口径
            </span>
          </div>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" :loading="storeStore.loading" @click="reload">刷新</el-button>
          <el-button type="primary" :disabled="!picked.length" :icon="DataAnalysis" @click="viewAggregate">
            查看所选店铺合计（{{ picked.length }}）
          </el-button>
        </div>
      </header>

      <!-- 只读边界：说清楚「新增店铺怎么做」，而不是给一个点了没用的按钮 -->
      <el-alert class="mb16" type="info" :closable="false" show-icon>
        <template #title>店铺注册表是服务端配置，界面只做查看与选择</template>
        <template #default>
          <p class="readonly-note">
            {{ storeStore.configHint }}
          </p>
          <p class="readonly-note">
            当前配置形如：
            <code>$env:OZON_STORES = "store_alpha=C:\...\store_alpha.db;store_beta=C:\...\store_beta.db"</code>
          </p>
        </template>
      </el-alert>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">可见店铺</h3>
          <span class="card-tip">
            「库可用」是后端真实打开一次库得到的结果；不可用时原因原样展示，不隐藏
          </span>
        </div>
        <el-table
          ref="tableRef"
          v-loading="storeStore.loading"
          element-loading-text="正在读取店铺列表…"
          :data="storeStore.stores"
          row-key="alias"
          stripe
          @selection-change="onSelectionChange"
        >
          <el-table-column type="selection" width="52" :selectable="row => row.available" />
          <el-table-column label="店铺" min-width="240">
            <template #default="{ row }">
              <div class="store-cell">
                <span class="store-name">{{ row.display_name }}</span>
                <el-tag size="small" effect="plain">{{ row.alias }}</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="库状态" min-width="140">
            <template #default="{ row }">
              <el-tag v-if="row.available" type="success" size="small" effect="light">可用</el-tag>
              <el-tooltip v-else :content="row.error ?? '不可用'" placement="top">
                <el-tag type="danger" size="small" effect="light" class="clickable-tag">库不可用</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="最后结算日" min-width="130" align="center">
            <template #default="{ row }">
              <span v-if="row.window_end">{{ row.window_end }}</span>
              <el-tag v-else size="small" type="info" effect="plain">无已结算</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="数据截止（写入时间）" min-width="180" align="center">
            <template #default="{ row }">{{ formatCutoff(row.data_cutoff) }}</template>
          </el-table-column>
          <el-table-column label="操作" min-width="180" align="center">
            <template #default="{ row }">
              <el-button link type="primary" :disabled="!row.available" @click="viewSingle(row.alias)">
                单店看板
              </el-button>
              <el-button link type="primary" :disabled="!row.available" @click="viewSingle(row.alias, true)">
                逐 SKU 明细
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="没有可见店铺" :image-size="96" />
          </template>
        </el-table>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts" name="storeManage">
import { DataAnalysis, InfoFilled, Refresh, Shop, Warning } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import type { ResStoreInfo } from "@/api/interfaces/backend";
import { handleUnauthorized } from "@/api/backendRequest";
import { LOGIN_URL } from "@/config";
import { useStoreStore } from "@/stores/modules/store";

/**
 * 店铺管理（ADR-0008）
 *
 * **只读页面**：能看有哪些店、能不能打开、数据到哪天，能勾选后跳到看板看合计。
 * 新增/下线店铺要改服务端 `OZON_STORES` 配置后重启 —— 本系统已声明的性质是
 * 「`api/` 下没有任何写路由」，不为一个管理界面破掉它。界面上把这条直说，
 * 不给一个点了没用的「新增店铺」按钮。
 */
const storeStore = useStoreStore();
const router = useRouter();

const picked = ref<string[]>([]);
const tableRef = ref();

const onSelectionChange = (rows: ResStoreInfo[]) => {
  picked.value = rows.map(row => row.alias);
};

const reload = () => {
  storeStore.load(true);
};

const formatCutoff = (value: string | null | undefined) =>
  value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "--";

const goLogin = () => router.replace(LOGIN_URL);

/** 跳看板：URL 是唯一的交接点（`?stores=a,b`），刷新与复制链接都能复现 */
const viewAggregate = () => {
  if (!picked.value.length) return;
  router.push({ path: "/dashboard", query: { stores: picked.value.join(",") } });
};

const viewSingle = (alias: string, drilldown = false) => {
  router.push({
    path: "/dashboard",
    query: { stores: alias, ...(drilldown ? { drilldown: "estimated" } : {}) }
  });
};

const errorTitle = computed(() => {
  const status = storeStore.error?.status;
  if (status === 401) return "登录已失效";
  if (status === 403) return "无权查看店铺列表";
  if (status !== undefined) return `店铺列表加载失败（HTTP ${status}）`;
  return "无法连接后端服务";
});
const errorDesc = computed(() => storeStore.error?.message ?? "未知错误");

onMounted(async () => {
  await storeStore.load();
  if (storeStore.error?.status === 401) handleUnauthorized();
});
</script>

<style scoped lang="scss">
.store-page {
  --board-gap: 16px;
  --board-radius: 8px;

  .card {
    padding: 16px;
    background-color: var(--el-bg-color-overlay);
    border: 1px solid var(--el-border-color-lighter);
    border-radius: var(--board-radius);
  }
}

.page-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--board-gap);
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--board-gap);

  .head-title {
    display: flex;
    align-items: center;
    font-size: 18px;
    font-weight: 700;
    color: var(--el-text-color-primary);
  }

  .head-icon {
    margin-right: 8px;
    font-size: 20px;
    color: var(--el-color-primary);
  }

  .count-tag {
    margin-left: 8px;
    font-weight: 400;
  }

  .head-meta {
    margin-top: 8px;
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .meta-item {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .head-actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }
}

.readonly-note {
  margin: 0 0 4px;
  line-height: 1.7;

  &:last-child {
    margin-bottom: 0;
  }
}

.card-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;

  .card-title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }

  .card-tip {
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
}

.store-cell {
  display: flex;
  gap: 8px;
  align-items: center;
}

.store-name {
  font-weight: 600;
}

.clickable-tag {
  cursor: help;
}

.empty-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* 错误面板：与看板保持一致的处理方式（不白屏、给出下一步） */
.error-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: center;
  padding: 48px 24px;
  text-align: center;

  .error-icon {
    font-size: 40px;
    color: var(--el-color-danger);
  }

  .error-title {
    margin: 0;
    font-size: 18px;
  }

  .error-desc {
    max-width: 720px;
    margin: 0;
    color: var(--el-text-color-secondary);
  }

  .error-actions {
    display: flex;
    gap: 8px;
  }

  .error-tip {
    margin: 0;
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
}
</style>
