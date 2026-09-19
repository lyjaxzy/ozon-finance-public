<template>
  <el-drawer
    v-model="visible"
    :title="modeMeta.title"
    :size="drawerSize"
    class="sku-drawer"
    @open="onOpen"
    @closed="onClosed"
  >
    <template #header>
      <div class="drawer-head">
        <h3 class="drawer-title">{{ modeMeta.title }}</h3>
        <p class="drawer-sub">
          {{ storeDisplayName }} · 统计区间 {{ periodText }}
          <span class="sub-dot">·</span>
          数据截止 {{ formatCutoff(data?.data_cutoff) }}
        </p>
      </div>
    </template>

    <!-- 加载态：首次进入还没有任何数据时给骨架屏 -->
    <div v-if="loading && !data" class="state-block">
      <el-skeleton :rows="8" animated />
    </div>

    <!-- 错误态：不白屏，给出原因 + 重试 -->
    <div v-else-if="error" class="state-block">
      <el-alert type="error" :closable="false" show-icon>
        <template #title>{{ errorTitle }}</template>
        <template #default>{{ errorDesc }}</template>
      </el-alert>
      <div class="state-actions">
        <el-button type="primary" :icon="Refresh" @click="load">重试</el-button>
      </div>
    </div>

    <template v-else-if="data">
      <!-- 口径摘要：缺成本 / 不完整 / 条数，一眼能看出这批数能不能直接用 -->
      <div class="summary-grid">
        <div v-for="item in summaryItems" :key="item.label" class="summary-item">
          <span class="summary-label">{{ item.label }}</span>
          <span class="summary-value" :class="item.tone">
            {{ item.value }}<span v-if="item.unit" class="summary-unit">{{ item.unit }}</span>
          </span>
        </div>
      </div>

      <!-- 缺成本告警：这是本项目存在的理由，必须在界面上直说 -->
      <el-alert
        v-if="data.totals.missing_cost_sku_count > 0"
        class="mb16"
        type="warning"
        :closable="false"
        show-icon
      >
        <template #title>
          有 {{ data.totals.missing_cost_sku_count }} 个 SKU 缺采购成本（共
          {{ data.totals.sku_count }} 个）
        </template>
        <template #default>
          缺失一律显示为「缺成本」，<strong>不会用 0 顶替</strong>，也不参与对应 SKU 的利润计算。
          请到采购成本库按货号录入单件成本后刷新。
        </template>
      </el-alert>

      <!-- 未归属：多货号订单里没有按货号分组键的费用，绝不摊分 -->
      <el-alert
        v-if="data.unattributed.posting_count > 0 || data.unattributed.actual_posting_count > 0"
        class="mb16"
        type="info"
        :closable="false"
        show-icon
      >
        <template #title>
          有 {{ data.unattributed.posting_count }} 笔金额无法归属到货号（实际利润另有
          {{ data.unattributed.actual_posting_count }} 单）
        </template>
        <template #default>
          <ul class="reason-list">
            <li v-for="(item, index) in data.unattributed.reasons" :key="index">
              {{ item.label }}<span class="reason-field">（{{ item.field }}）</span>：
              {{ item.posting_count }} 单
              <template v-if="item.amount_cny !== null">，{{ formatMoney(item.amount_cny) }}</template>
            </li>
          </ul>
          <p class="reason-foot">
            这些金额按「不摊分」处理，留在未归属里 —— 逐 SKU 合计 + 未归属 = 订单口径合计。
          </p>
        </template>
      </el-alert>

      <!-- 对账条：两条独立路径必须给出同一个合计 -->
      <div class="recon" :class="allMatch ? 'recon-ok' : 'recon-bad'">
        <div class="recon-head">
          <span class="recon-title">
            {{ allMatch ? "两条口径一致" : "两条口径不一致（请勿直接使用这批数）" }}
          </span>
          <span class="recon-tip">逐 SKU 合计 + 未归属 = 订单口径合计</span>
        </div>
        <div class="recon-body">
          <span v-for="item in reconItems" :key="item.label" class="recon-cell">
            <span class="recon-label">{{ item.label }}</span>
            <span class="recon-value">{{ item.value }}</span>
          </span>
        </div>
      </div>

      <!-- 工具条：搜索 + 利润口径切换 + 刷新 -->
      <div class="toolbar">
        <el-input
          v-model="keyword"
          class="search-input"
          placeholder="搜索 货号 / SKU / 商品名"
          clearable
          :prefix-icon="Search"
          @input="onKeywordInput"
          @clear="onKeywordInput('')"
        />
        <el-radio-group v-model="activeMode" size="default">
          <el-radio-button value="estimated">预估利润</el-radio-button>
          <el-radio-button value="actual">实际利润</el-radio-button>
        </el-radio-group>
        <span class="toolbar-tip">
          合计为全窗口口径（不受搜索与排序影响）；本页
          {{ data.totals.returned_count }} / 共 {{ data.totals.matched_sku_count }} 个 SKU
          <template v-if="data.totals.truncated">（还有下一页）</template>
        </span>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </div>

      <!-- 明细表：空态与加载态都要有 -->
      <el-table
        v-loading="loading"
        element-loading-text="正在取逐 SKU 明细…"
        :data="data.rows"
        stripe
        class="sku-table"
        :default-sort="{ prop: sortProp, order: sortOrder }"
        @sort-change="onSortChange"
      >
        <el-table-column prop="offer_id" label="货号" min-width="140" fixed show-overflow-tooltip />
        <el-table-column prop="sku" label="SKU" min-width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.sku ?? "--" }}</template>
        </el-table-column>
        <el-table-column prop="product_name" label="商品名" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.product_name ?? "--" }}</template>
        </el-table-column>
        <el-table-column prop="quantity" label="数量" width="90" align="right" sortable="custom" />
        <el-table-column prop="order_count" label="订单数" width="90" align="right" sortable="custom" />
        <el-table-column prop="revenue_cny" label="收入 (¥)" min-width="120" align="right" sortable="custom">
          <template #default="{ row }">{{ formatMoney(row.revenue_cny) }}</template>
        </el-table-column>
        <el-table-column
          prop="purchase_cost_cny"
          label="采购成本 (¥)"
          min-width="140"
          align="right"
          sortable="custom"
        >
          <template #default="{ row }">
            <!-- 缺成本：明确的标记，**不是 0**。tooltip 说清是「没录入」还是「归不到货号」 -->
            <el-tooltip
              v-if="row.purchase_cost_cny === null"
              :content="costReasonText(row)"
              placement="top"
            >
              <el-tag type="danger" size="small" effect="light">缺成本</el-tag>
            </el-tooltip>
            <span v-else>{{ formatMoney(row.purchase_cost_cny) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="estimated_profit_cny" label="预估利润 (¥)" min-width="140" align="right" sortable="custom">
          <template #default="{ row }">
            <span v-if="row.estimated_profit_cny === null" class="cell-missing">
              <el-tooltip :content="row.unknown_reason_label ?? row.unknown_reason ?? '不可算'" placement="top">
                <span>不可算</span>
              </el-tooltip>
            </span>
            <span v-else :class="isNegative(row.estimated_profit_cny) ? 'profit-negative' : 'profit-positive'">
              {{ formatMoney(row.estimated_profit_cny) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="actual_profit_cny" label="实际利润 (¥)" min-width="140" align="right" sortable="custom">
          <template #default="{ row }">
            <span v-if="row.actual_profit_cny === null" class="cell-missing">
              <el-tooltip :content="row.unknown_reason_label ?? row.unknown_reason ?? '不可算'" placement="top">
                <span>不可算</span>
              </el-tooltip>
            </span>
            <span v-else :class="isNegative(row.actual_profit_cny) ? 'profit-negative' : 'profit-positive'">
              {{ formatMoney(row.actual_profit_cny) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="180">
          <template #default="{ row }">
            <el-tag :type="row.complete ? 'success' : 'warning'" size="small" effect="light">
              {{ row.complete ? "完整" : "不完整" }}
            </el-tag>
            <el-tooltip v-if="row.unknown_reason" :content="row.unknown_reason_label ?? row.unknown_reason" placement="top">
              <span class="reason-text">{{ row.unknown_reason_label ?? row.unknown_reason }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="keyword ? '没有匹配的 SKU，换个关键词试试' : '所选区间没有 SKU 明细'" :image-size="96" />
        </template>
      </el-table>

      <!-- 服务端分页：翻页只改下发的行，合计与对账数字始终是全窗口口径 -->
      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="pageSizes"
          :total="data.totals.matched_sku_count"
          background
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="load"
          @size-change="onPageSizeChange"
        />
      </div>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { Refresh, Search } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { ElMessage } from "element-plus";
import { computed, ref, watch } from "vue";

import { handleUnauthorized, type BackendError } from "@/api/backendRequest";
import type { ResSkuDetail } from "@/api/interfaces/backend";
import { getStoreSkuDetailApi, SKU_PROFIT_MODE_MAP, type SkuProfitMode } from "@/api/modules/dashboard";
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS } from "@/config/store";
import { useStoreStore } from "@/stores/modules/store";

/**
 * 逐 SKU 利润下钻抽屉。
 *
 * 设计要点（对应 ADR-0006 / ADR-0008）：
 *  - **金额一律来自后端的字符串**，本组件不做任何加减；
 *  - `null` 一律渲染成明确的「缺成本 / 不可算」标记 + 原因 tooltip，**绝不显示成 0**；
 *  - 合计与对账数字都用后端下发的**全窗口口径**，它们不随搜索/排序/分页变化，
 *    所以「表里看到的行」和「上面的合计」对不上是正常的 —— 界面上写明这一点；
 *  - 分页是**服务端分页**（`limit` / `offset`），每页 10/20/50/100。
 */
const props = defineProps<{
  modelValue: boolean;
  alias: string;
  days: number;
  mode: SkuProfitMode;
  /** 每页档位；由看板页从后端 `/api/stores` 取，取不到就用本地兜底 */
  pageSizes?: number[];
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: boolean): void;
  (event: "update:mode", value: SkuProfitMode): void;
}>();

const storeStore = useStoreStore();
const pageSizes = computed(() => (props.pageSizes?.length ? props.pageSizes : [...PAGE_SIZE_OPTIONS]));

const visible = computed({
  get: () => props.modelValue,
  set: value => emit("update:modelValue", value)
});

/** 抽屉里可以自己切利润口径：同一个接口就已经把两个口径都返回了 */
const activeMode = computed({
  get: () => props.mode,
  set: value => emit("update:mode", value)
});

const modeMeta = computed(() => SKU_PROFIT_MODE_MAP[props.mode]);

const data = ref<ResSkuDetail | null>(null);
const loading = ref(false);
const error = ref<BackendError | null>(null);
const keyword = ref("");
const sortKey = ref("estimated_profit_cny");
const sortDesc = ref(true);

/** 服务端分页状态：页码从 1 开始，`offset = (page-1) * pageSize` */
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);

const onPageSizeChange = () => {
  page.value = 1;
  load();
};

/** 抽屉宽度：窄屏占满，宽屏留出看板的可见区域 */
const drawerSize = computed(() => (window.innerWidth < 1200 ? "100%" : "78%"));

/* ------------------------------ 格式化 ------------------------------ */
const moneyFormatter = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const toNumber = (value: unknown): number | null => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

/** ¥ 金额：千分位 + 2 位小数。缺失返回「--」，**不是 0** */
const formatMoney = (value: unknown) => {
  const num = toNumber(value);
  return num === null ? "--" : `¥${moneyFormatter.format(num)}`;
};

const isNegative = (value: unknown) => {
  const num = toNumber(value);
  return num !== null && num < 0;
};

/**
 * 「缺成本」的原因文案。
 *
 * 采购成本为 null 有两种成因，界面上必须说清是哪一种 ——
 * 两种的修复动作不同：一种是**没录入**（去成本库补货号），
 * 另一种是**这笔成本归不到这个货号**（多货号订单没有按行的成本键）。
 */
const costReasonText = (row: { unknown_reason: string | null; unknown_reason_label: string | null }) => {
  if (row.unknown_reason === "missing_purchase_cost") {
    return "采购成本库没有这个货号的单件成本 —— 请录入后刷新";
  }
  if (row.unknown_reason) {
    return `采购成本不可知：${row.unknown_reason_label ?? row.unknown_reason}（缺失时不给 0）`;
  }
  return "采购成本不可知（缺失时不给 0）";
};

const formatCutoff = (value: string | null | undefined) => (value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "--");

// 展示名来自后端下发的店铺列表（前端不再维护一份别名 → 店名 的映射）
const storeDisplayName = computed(() => storeStore.displayName(props.alias));

const periodText = computed(() => {
  const period = data.value?.period;
  if (!period) return `${props.days} 天`;
  return `${period.start} ~ ${period.end}（${period.days} 天）`;
});

/* ------------------------------ 摘要 ------------------------------ */
const summaryItems = computed(() => {
  const totals = data.value?.totals;
  if (!totals) return [];
  const profitField = modeMeta.value.profitField;
  const profitTotal = profitField === "estimated_profit_cny"
    ? totals.estimated_profit_cny
    : totals.actual_profit_cny;
  return [
    { label: "SKU 数", value: String(totals.sku_count), unit: "个", tone: "" },
    {
      label: "缺成本 SKU",
      value: String(totals.missing_cost_sku_count),
      unit: "个",
      tone: totals.missing_cost_sku_count > 0 ? "tone-warn" : ""
    },
    {
      label: "不完整 SKU",
      value: String(totals.incomplete_sku_count),
      unit: "个",
      tone: totals.incomplete_sku_count > 0 ? "tone-warn" : ""
    },
    { label: "涉及订单", value: String(totals.order_count), unit: "单", tone: "" },
    { label: `${modeMeta.value.label}合计`, value: formatMoney(profitTotal), unit: "", tone: "tone-profit" }
  ];
});

/* ------------------------------ 对账 ------------------------------ */
const allMatch = computed(() => {
  const matches = data.value?.reconciliation?.matches;
  if (!matches) return true;
  return Object.values(matches).every(Boolean);
});

const reconItems = computed(() => {
  const recon = data.value?.reconciliation;
  const un = data.value?.unattributed;
  if (!recon) return [];
  const rows = [
    { label: "收入", field: "revenue_cny" },
    { label: "采购成本", field: "purchase_cost_cny" },
    { label: "预估利润", field: "estimated_profit_cny" },
    { label: "实际利润", field: "actual_profit_cny" }
  ];
  return rows.map(item => {
    const skuValue = recon.sku_level[item.field as keyof typeof recon.sku_level] as string | null;
    const orderValue = recon.order_level[item.field as keyof typeof recon.order_level] as string | null;
    const unValue = (un?.[item.field as keyof typeof un] ?? null) as string | null;
    const skuNum = toNumber(skuValue) ?? 0;
    const unNum = toNumber(unValue) ?? 0;
    const orderNum = toNumber(orderValue) ?? 0;
    const ok = Math.abs(skuNum + unNum - orderNum) < 0.005;
    return {
      label: item.label,
      value: `${formatMoney(skuValue)} + ${formatMoney(unValue)} ${ok ? "=" : "≠"} ${formatMoney(orderValue)}`
    };
  });
});

/* ------------------------------ 排序映射 ------------------------------ */
const SORT_PROP_TO_KEY: Record<string, string> = {
  quantity: "quantity",
  order_count: "order_count",
  revenue_cny: "revenue_cny",
  purchase_cost_cny: "purchase_cost_cny",
  estimated_profit_cny: "estimated_profit_cny",
  actual_profit_cny: "actual_profit_cny",
  offer_id: "offer_id"
};

const sortProp = computed(() => {
  const entry = Object.entries(SORT_PROP_TO_KEY).find(([, key]) => key === sortKey.value);
  return entry ? entry[0] : "estimated_profit_cny";
});
const sortOrder = computed<"ascending" | "descending">(() => (sortDesc.value ? "descending" : "ascending"));

const onSortChange = ({ prop, order }: { prop: string; order: string | null }) => {
  if (!order) {
    // 取消排序时回到「按当前口径的利润倒序」—— 比回到默认主键更符合直觉
    sortKey.value = modeMeta.value.profitField;
    sortDesc.value = true;
  } else {
    sortKey.value = SORT_PROP_TO_KEY[prop] ?? modeMeta.value.profitField;
    sortDesc.value = order === "descending";
  }
  load();
};

/* ------------------------------ 数据加载 ------------------------------ */
const errorTitle = computed(() => {
  const status = error.value?.status;
  if (status === 403) return "无权访问该店铺";
  if (status === 404) return "店铺不存在";
  if (status === 401) return "登录已失效";
  if (status === 501) return "该数据源不支持逐 SKU 下钻";
  if (status === 503) return "店铺库不可用";
  if (status !== undefined) return `逐 SKU 明细加载失败（HTTP ${status}）`;
  return "无法连接后端服务";
});
const errorDesc = computed(() => error.value?.message ?? "未知错误");

const load = async () => {
  loading.value = true;
  error.value = null;
  try {
    data.value = await getStoreSkuDetailApi(props.alias, {
      days: props.days,
      sort: sortKey.value,
      desc: sortDesc.value,
      q: keyword.value.trim() || undefined,
      // 服务端分页：只取当前页。合计/对账不受它影响（后端全窗口口径）
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value
    });
  } catch (err) {
    const e = err as BackendError;
    error.value = e;
    data.value = null;
    if (e?.status === 401) {
      handleUnauthorized();
      ElMessage.error("登录已失效，请重新登录");
    }
  } finally {
    loading.value = false;
  }
};

/** 搜索防抖：不装 lodash，一个定时器足够 */
let searchTimer: ReturnType<typeof setTimeout> | null = null;
const onKeywordInput = (value: string) => {
  keyword.value = value ?? "";
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    // 换了搜索词就必须回到第 1 页，否则可能停在一个不存在的页码上
    page.value = 1;
    load();
  }, 400);
};

const onOpen = () => {
  keyword.value = "";
  page.value = 1;
  sortKey.value = modeMeta.value.profitField;
  sortDesc.value = true;
  // 抽屉标题里要显示店铺名：列表可能还没加载（比如直接刷新在抽屉打开状态）
  if (!storeStore.loaded) storeStore.load();
  load();
};

/** 关闭时清空数据：下次打开一定重新取，不会拿旧窗口的数给新窗口用 */
const onClosed = () => {
  data.value = null;
  error.value = null;
};

/** 切换利润口径：重新按新主键排序取数（合计不变，行顺序变） */
watch(
  () => props.mode,
  mode => {
    if (!visible.value) return;
    sortKey.value = SKU_PROFIT_MODE_MAP[mode].profitField;
    sortDesc.value = true;
    page.value = 1;
    load();
  }
);

/** 看板的统计天数变了，抽屉里的窗口必须跟着变（页码也要回到第 1 页） */
watch(
  () => props.days,
  () => {
    if (visible.value) {
      page.value = 1;
      load();
    }
  }
);

/** 换了店铺：搜索词、页码、排序都回到初始状态，不能把上一个店的条件带过来 */
watch(
  () => props.alias,
  () => {
    if (!visible.value) return;
    keyword.value = "";
    page.value = 1;
    sortKey.value = modeMeta.value.profitField;
    sortDesc.value = true;
    load();
  }
);
</script>

<style scoped lang="scss">
.sku-drawer {
  :deep(.el-drawer__header) {
    padding: 16px 24px;
    margin-bottom: 0;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  :deep(.el-drawer__body) {
    padding: 24px;
  }
}

.drawer-head {
  display: flex;
  flex-direction: column;
  gap: 4px;

  .drawer-title {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }

  .drawer-sub {
    margin: 0;
    font-size: 12px;
    color: var(--el-text-color-secondary);

    .sub-dot {
      margin: 0 6px;
    }
  }
}

.state-block {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.state-actions {
  display: flex;
  justify-content: center;
}

/* 摘要：4/5 列自适应，节奏 16px */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 16px;
  padding: 16px;
  margin-bottom: 16px;
  background-color: var(--el-fill-color-lighter);
  border-radius: 8px;
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.summary-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.summary-value {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-primary);

  &.tone-warn {
    color: var(--el-color-warning);
  }

  &.tone-profit {
    color: var(--el-color-success);
  }
}

.summary-unit {
  margin-left: 4px;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.mb16 {
  margin-bottom: 16px;
}

.reason-list {
  padding-left: 18px;
  margin: 4px 0 0;

  li {
    line-height: 1.7;
  }

  .reason-field {
    color: var(--el-text-color-secondary);
  }
}

.reason-foot {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
}

/* 对账条 */
.recon {
  padding: 12px 16px;
  margin-bottom: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-left-width: 4px;
  border-radius: 8px;

  &.recon-ok {
    border-left-color: var(--el-color-success);
  }

  &.recon-bad {
    border-left-color: var(--el-color-danger);
    background-color: var(--el-color-danger-light-9);
  }
}

.recon-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 8px;

  .recon-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }

  .recon-tip {
    font-size: 12px;
    color: var(--el-text-color-placeholder);
  }
}

.recon-body {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
}

.recon-cell {
  display: inline-flex;
  gap: 6px;
  align-items: baseline;
  font-size: 12px;
  font-variant-numeric: tabular-nums;

  .recon-label {
    color: var(--el-text-color-secondary);
  }

  .recon-value {
    color: var(--el-text-color-primary);
  }
}

/* 工具条 */
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 16px;

  .search-input {
    width: 260px;
  }

  .toolbar-tip {
    flex: 1;
    min-width: 200px;
    font-size: 12px;
    color: var(--el-text-color-placeholder);
  }
}

.sku-table {
  font-variant-numeric: tabular-nums;

  :deep(.el-table__header th) {
    text-align: right;
  }

  :deep(.el-table__header th:first-child),
  :deep(.el-table__header th:nth-child(2)),
  :deep(.el-table__header th:nth-child(3)) {
    text-align: left;
  }

  .cell-missing {
    color: var(--el-color-warning);
    cursor: help;
  }

  .reason-text {
    display: inline-block;
    max-width: 130px;
    margin-left: 8px;
    overflow: hidden;
    color: var(--el-text-color-secondary);
    text-overflow: ellipsis;
    white-space: nowrap;
    vertical-align: middle;
    cursor: help;
  }
}

.profit-positive {
  color: var(--el-color-success);
}

.profit-negative {
  color: var(--el-color-danger);
}

/* 分页条：与上方表格留一点间距，右对齐 */
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
