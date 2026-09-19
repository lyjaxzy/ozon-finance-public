<template>
  <div class="dashboard">
    <!-- 请求成功：正常看板。判断条件是 `boardData`（单店或合计）而不是 overview ——
         多店合计模式下 overview 一定是 null，用它会整页空白 -->
    <template v-if="boardData">
      <header class="page-head card">
        <div class="head-main">
          <div class="head-title">
            <el-icon class="head-icon"><Shop /></el-icon>
            <span class="shop-name">{{ headTitle }}</span>
            <el-tag v-if="isAggregate" class="alias-tag" size="small" type="warning" effect="plain">
              {{ selectedAliases.length }} 个店铺合计
            </el-tag>
            <el-tag v-else class="alias-tag" size="small" effect="plain">别名 {{ currentAlias }}</el-tag>
          </div>
          <div class="head-meta">
            <span class="meta-item">
              <el-icon><Clock /></el-icon>
              数据截止：{{ formatCutoff(boardData?.data_cutoff) }}
            </span>
            <el-divider direction="vertical" />
            <span class="meta-item">
              <el-icon><Calendar /></el-icon>
              统计区间：{{ boardPeriod.start }} ~ {{ boardPeriod.end }}（{{ boardPeriod.days }} 天）
            </span>
            <template v-if="isAggregate">
              <el-divider direction="vertical" />
              <span class="meta-item">
                <el-icon><Grid /></el-icon>
                共同窗口右端（各店最早结算日）：{{ boardPeriod.end }}
              </span>
            </template>
          </div>
        </div>
        <div class="head-actions">
          <el-select
            v-model="pickedAliases"
            class="store-select"
            multiple
            collapse-tags
            collapse-tags-tooltip
            :max-collapse-tags="2"
            placeholder="选择店铺（可多选做合计）"
            @change="onStorePick"
          >
            <el-option
              v-for="item in storeOptions"
              :key="item.alias"
              :label="item.display_name"
              :value="item.alias"
              :disabled="!item.available"
            >
              <span class="option-main">{{ item.display_name }}</span>
              <span class="option-side">
                <el-tag v-if="!item.available" size="small" type="danger" effect="plain">库不可用</el-tag>
                <template v-else-if="item.window_end">结算至 {{ item.window_end }}</template>
                <el-tag v-else size="small" type="info" effect="plain">无已结算</el-tag>
              </span>
            </el-option>
          </el-select>
          <el-select v-model="days" class="days-select" @change="onDaysChange">
            <el-option v-for="item in DASHBOARD_DAY_OPTIONS" :key="item" :label="`近 ${item} 天`" :value="item" />
          </el-select>
          <el-button type="primary" :icon="Refresh" @click="loadData">刷新</el-button>
        </div>
      </header>

      <!-- 合计口径说明：多店合计时把「合计里少了什么」直接摆在最上面 -->
      <el-alert
        v-if="isAggregate"
        class="mb16"
        type="info"
        :closable="false"
        show-icon
      >
        <template #title>
          多店合计：{{ selectedAliases.join("、") }}
        </template>
        <template #default>
          <p class="aggregate-note">
            合计的窗口右端取<strong>各店里最早的那个已结算日（{{ boardPeriod.end }}）</strong>
            —— 若按各店自己的最新数据分头相加，落后的那家店会在尾部几天"贡献 0"，
            看起来像那几天没生意。逐店明细见下方表格。
          </p>
          <ul v-if="aggregateWarnings.length" class="aggregate-warnings">
            <li v-for="(text, index) in aggregateWarnings" :key="index">{{ text }}</li>
          </ul>
        </template>
      </el-alert>

      <!-- 1. 顶部指标卡 -->
      <el-row :gutter="16" class="mb16">
        <el-col v-for="item in metricCards" :key="item.key" :xs="24" :sm="12" :lg="6">
          <div
            class="card metric-card"
            :class="{ 'metric-clickable': item.drilldown }"
            :role="item.drilldown ? 'button' : undefined"
            :tabindex="item.drilldown ? 0 : undefined"
            :title="item.title"
            @click="openDrilldown(item)"
            @keyup.enter="openDrilldown(item)"
          >
            <div class="metric-label">
              <el-icon class="metric-icon" :style="{ color: item.color }"><component :is="item.icon" /></el-icon>
              <span>{{ item.label }}</span>
              <!-- 可点击提示：文字 + 箭头，不靠 emoji -->
              <span v-if="item.drilldown" class="metric-hint">
                查看明细
                <el-icon class="hint-arrow"><ArrowRight /></el-icon>
              </span>
            </div>
            <div class="metric-value" :style="{ color: item.color }">
              <span class="metric-number">{{ item.value }}</span>
              <span v-if="item.unit" class="metric-unit">{{ item.unit }}</span>
            </div>
            <div class="metric-foot">{{ item.foot }}</div>
          </div>
        </el-col>
      </el-row>

      <!-- 2/3. 趋势图 + 利润构成 -->
      <el-row :gutter="16" class="mb16 chart-row">
        <el-col :xs="24" :lg="16">
          <div class="card chart-card">
            <div class="card-head">
              <h3 class="card-title">实际利润 vs 预估利润（近 {{ boardPeriod.days }} 天）</h3>
              <span class="card-tip">单位：¥ · 逐单精算汇总，不补零日期</span>
            </div>
            <div class="chart-body">
              <ECharts v-if="trend.length" :option="trendOption" height="320" />
              <el-empty v-else description="所选区间暂无趋势数据" :image-size="96" />
            </div>
          </div>
        </el-col>
        <el-col :xs="24" :lg="8">
          <div class="card chart-card">
            <div class="card-head">
              <h3 class="card-title">利润构成</h3>
              <span class="card-tip">后端已折算为 ¥</span>
            </div>
            <div class="chart-body">
              <ECharts v-if="composition.length" :option="breakdownOption" height="320" />
              <el-empty v-else description="暂无利润构成数据" :image-size="96" />
            </div>
          </div>
        </el-col>
      </el-row>

      <!-- 4. 逐店明细（仅多店合计时显示）：各店的合计必须在同一个窗口下 -->
      <div v-if="isAggregate" class="card mb16">
        <div class="card-head">
          <h3 class="card-title">逐店明细（同一共同窗口）</h3>
          <span class="card-tip">
            各家之和 == 上方合计；「窗口右端」是各店自己的最后结算日，与合计窗口不一致时合计里不含它多出来的那几天
          </span>
        </div>
        <el-table :data="aggregateStores" stripe class="order-table">
          <el-table-column label="店铺" min-width="200" fixed>
            <template #default="{ row }">
              <div class="store-cell">
                <span class="store-name">{{ row.display_name }}</span>
                <el-tag size="small" effect="plain">{{ row.alias }}</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="窗口右端" min-width="120" align="center">
            <template #default="{ row }">
              <span :class="row.window_end === boardPeriod.end ? '' : 'lagging'">
                {{ row.window_end ?? "--" }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="订单数" min-width="100" align="right">
            <template #default="{ row }">{{ row.totals.total_order_count }}</template>
          </el-table-column>
          <el-table-column label="完整单" min-width="100" align="right">
            <template #default="{ row }">{{ row.totals.complete_order_count }}</template>
          </el-table-column>
          <el-table-column label="实际利润 (¥)" min-width="140" align="right">
            <template #default="{ row }">
              <span :class="isNegative(row.totals.actual_profit_cny) ? 'profit-negative' : 'profit-positive'">
                {{ formatMoney(row.totals.actual_profit_cny) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="预估利润 (¥)" min-width="140" align="right">
            <template #default="{ row }">{{ formatMoney(row.totals.estimated_profit_cny) }}</template>
          </el-table-column>
          <el-table-column label="完成率" min-width="110" align="right">
            <template #default="{ row }">{{ formatPercent(row.totals.completion_rate) }}</template>
          </el-table-column>
          <el-table-column label="逾期" min-width="90" align="right">
            <template #default="{ row }">{{ row.totals.overdue_count ?? "--" }}</template>
          </el-table-column>
          <el-table-column label="操作" min-width="120" align="center">
            <template #default="{ row }">
              <el-button link type="primary" @click="openSingleStore(row.alias)">单店看板</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 5. 订单明细表（多店合计时不下发订单明细：它是单店口径） -->
      <div v-else ref="ordersCardRef" class="card">
        <div class="card-head">
          <h3 class="card-title">订单明细</h3>
          <span class="card-tip">
            本窗口共 {{ totals?.total_order_count ?? 0 }} 单，服务端分页 · 合计口径以上方指标卡为准
          </span>
        </div>
        <!-- 表格上方也放一个紧凑翻页条：进到这张卡片就能立刻翻页，
             不用先滚过 20 行（也顺带解决「以为没有翻页按钮」） -->
        <TablePager
          compact
          :page="page"
          :page-count="pageCount"
          :total="totals?.total_order_count ?? 0"
          :page-size="pageSize"
          :page-sizes="pageSizeOptions"
          :loading="loading"
          unit="单"
          @update:page="page = $event"
          @change="onPageChange"
        />
        <el-table
          v-loading="loading"
          element-loading-text="正在取订单明细…"
          :data="orders"
          stripe
          class="order-table"
          :max-height="520"
          :header-cell-style="{ textAlign: 'right' }"
        >
          <el-table-column prop="posting_number" label="订单号" min-width="150" align="left" fixed />
          <el-table-column label="结算日期" min-width="120" align="right">
            <template #default="{ row }">{{ row.settlement_date ?? "--" }}</template>
          </el-table-column>
          <el-table-column label="直接净额 (₽)" min-width="130" align="right">
            <template #default="{ row }">{{ formatMoney(row.direct_net_rub) }}</template>
          </el-table-column>
          <el-table-column label="汇率 (₽/¥)" min-width="110" align="right">
            <template #default="{ row }">{{ formatRate(row.exchange_rate_rub_per_cny) }}</template>
          </el-table-column>
          <el-table-column label="采购成本 (¥)" min-width="130" align="right">
            <template #default="{ row }">{{ formatMoney(row.purchase_cost_cny) }}</template>
          </el-table-column>
          <el-table-column label="实际利润 (¥)" min-width="130" align="right">
            <template #default="{ row }">
              <span :class="isNegative(row.actual_profit_cny) ? 'profit-negative' : 'profit-positive'">
                {{ formatMoney(row.actual_profit_cny) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="核算状态" min-width="240" align="left">
            <template #default="{ row }">
              <el-tag
                :type="row.complete ? ORDER_STATUS_MAP.complete.tagType : ORDER_STATUS_MAP.incomplete.tagType"
                size="small"
                effect="light"
              >
                {{ row.complete ? ORDER_STATUS_MAP.complete.label : ORDER_STATUS_MAP.incomplete.label }}
              </el-tag>
              <el-tooltip v-if="row.unknown_reason" :content="row.unknown_reason" placement="top">
                <span class="reason-text">{{ row.unknown_reason }}</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="这一页没有订单" :image-size="96" />
          </template>
        </el-table>
        <!-- 表格下方：完整翻页条（含每页条数与跳页）。表格已限高内部滚动，
             所以它始终紧跟在表格后面，不会被 20 行数据顶到很远的地方 -->
        <TablePager
          :page="page"
          :page-count="pageCount"
          :total="totals?.total_order_count ?? 0"
          :page-size="pageSize"
          :page-sizes="pageSizeOptions"
          :loading="loading"
          unit="单"
          @update:page="page = $event"
          @update:page-size="pageSize = $event"
          @change="onPageChange"
          @size-change="onPageSizeChange"
        />
      </div>
    </template>

    <!-- 加载中：骨架屏（首次进入时连骨架都没有，用 v-loading 兜底） -->
    <div v-else-if="loading" v-loading="true" element-loading-text="正在加载看板数据…" class="dashboard-loading">
      <el-skeleton :rows="6" animated />
    </div>

    <!-- 请求失败 / 无权访问：明确的错误面板 + 重试，不白屏 -->
    <div v-else class="card error-panel">
      <el-icon class="error-icon"><Warning /></el-icon>
      <h3 class="error-title">{{ errorTitle }}</h3>
      <p class="error-desc">{{ errorDesc }}</p>
      <div class="error-actions">
        <el-button v-if="canRetry" type="primary" :icon="Refresh" @click="loadData">重试</el-button>
        <el-button @click="goLogin">去登录</el-button>
      </div>
      <p class="error-tip">
        接口：<code>GET /api/dashboard/{{ isAggregate ? "aggregate?stores=" + selectedAliases.join(",") : "store/" + currentAlias }}?days={{ days }}</code>
        → 127.0.0.1:8849（经 vite dev 代理）
      </p>
    </div>

    <!-- 逐 SKU 利润下钻（点「实际利润合计」/「预估利润合计」指标卡打开） -->
    <!-- 只在单店模式下可用：逐 SKU 归属依赖单店库的分组键，多店合计没有这个口径 -->
    <SkuDetailDrawer
      v-model="drilldownOpen"
      v-model:mode="drilldownMode"
      :alias="currentAlias"
      :days="days"
      :page-sizes="pageSizeOptions"
    />
  </div>
</template>

<script setup lang="ts" name="dashboard">
import {
  ArrowLeft,
  ArrowRight,
  Calendar,
  Clock,
  DataAnalysis,
  Grid,
  Refresh,
  Shop,
  TrendCharts,
  Wallet,
  Warning
} from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { ElMessage } from "element-plus";
import { computed, markRaw, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { handleUnauthorized, type BackendError } from "@/api/backendRequest";
import type { ResAggregate, ResDashboard, ResOrderRow } from "@/api/interfaces/backend";
import { getStoreDashboardApi, ORDER_STATUS_MAP, type SkuProfitMode } from "@/api/modules/dashboard";
import { getStoreAggregateApi } from "@/api/modules/stores";
import ECharts from "@/components/ECharts/index.vue";
import type { ECOption } from "@/components/ECharts/config";
import { LOGIN_URL } from "@/config";
import {
  DASHBOARD_DAY_OPTIONS,
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS
} from "@/config/store";
import { useStoreStore } from "@/stores/modules/store";

import SkuDetailDrawer from "./components/SkuDetailDrawer.vue";
import TablePager from "./components/TablePager.vue";

/**
 * 店铺选择来自 **URL query**（`?stores=a,b`），不在组件里另存一份状态。
 *
 * 为什么用 URL 而不是本地状态（ADR-0008）：
 *  - 店铺管理页勾选完「查看所选合计」要跳过来，URL 是天然的交接点；
 *  - 刷新、复制链接、浏览器前进后退都能复现同一屏，不会有"看着是这个店、其实查的是那个店"。
 */
const route = useRoute();
const router = useRouter();
const storeStore = useStoreStore();

const parseStores = (raw: unknown): string[] => {
  const text = Array.isArray(raw) ? raw.join(",") : typeof raw === "string" ? raw : "";
  return text
    .split(",")
    .map(item => item.trim())
    .filter((item, index, all) => item && all.indexOf(item) === index);
};

/** URL 里点名的店铺；还没拿到后端列表时可能就是原始值 */
const requestedAliases = ref<string[]>(parseStores(route.query.stores));
/** 过滤掉无权/库不可用的别名之后的实际选择 */
const selectedAliases = computed(() => {
  const resolved = storeStore.resolveAliases(requestedAliases.value);
  return resolved.length ? resolved : [];
});
const isAggregate = computed(() => selectedAliases.value.length > 1);
const currentAlias = computed(() => selectedAliases.value[0] ?? "");
const headTitle = computed(() =>
  isAggregate.value ? "多店合计" : storeStore.displayName(currentAlias.value)
);

/** 下拉里可选的店铺：不可用的也列出来（禁用 + 说明原因），而不是悄悄藏掉 */
const storeOptions = computed(() => storeStore.stores);

/** 下拉的 v-model：直接读写 URL 里的那组别名 */
const pickedAliases = computed<string[]>({
  get: () => requestedAliases.value,
  set: value => {
    requestedAliases.value = value;
  }
});

const onStorePick = () => {
  const stores = selectedAliases.value;
  router.replace({
    query: { ...route.query, stores: stores.length ? stores.join(",") : undefined, page: undefined }
  });
  page.value = 1;
  loadData();
};

/** 统计窗口（天）。后端只接受 days，不接受任意日期区间，见 dashboard.ts 注释 */
const days = ref<number>(14);

const loading = ref(false);
/** 单店模式的响应 */
const overview = ref<ResDashboard | null>(null);
/** 多店合计的响应（与 overview 互斥） */
const aggregate = ref<ResAggregate | null>(null);
/** 请求失败的结构化错误；非空即渲染错误面板 */
const loadError = ref<BackendError | null>(null);

/** 订单明细的服务端分页状态 */
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const pageSizeOptions = computed(() =>
  storeStore.pageSizeOptions.length ? storeStore.pageSizeOptions : [...PAGE_SIZE_OPTIONS]
);
const pageCount = computed(() => {
  const total = totals.value?.total_order_count ?? 0;
  return Math.max(1, Math.ceil(total / pageSize.value));
});

/** 当前展示的响应（单店或合计），两者的公共字段形状一致 */
const boardData = computed<ResDashboard | ResAggregate | null>(() =>
  isAggregate.value ? aggregate.value : overview.value
);
const boardPeriod = computed(() => boardData.value?.period ?? { start: "--", end: "--", days: days.value });
const aggregateStores = computed(() => aggregate.value?.stores ?? []);
const aggregateWarnings = computed(() => aggregate.value?.warnings ?? []);

/** 数据已就绪（只有成功拿到响应才为 true，失败不会伪装成空数据） */
const errorStatus = computed(() => loadError.value?.status);
/** 403 重试也没用（授权问题不是抖动），所以不给重试按钮 */
const canRetry = computed(() => errorStatus.value !== 403);
const errorTitle = computed(() => {
  const status = errorStatus.value;
  if (status === 403) return "无权访问该店铺";
  if (status === 404) return "店铺不存在";
  if (status === 401) return "登录已失效";
  if (status === 503) return "店铺库不可用";
  if (status === 422) return "请求参数不合法";
  if (status !== undefined) return `看板数据加载失败（HTTP ${status}）`;
  return "无法连接后端服务";
});
const errorDesc = computed(() => {
  const message = loadError.value?.message ?? "未知错误";
  if (errorStatus.value === 403) {
    return `${message}。当前账号没有 ${selectedAliases.value.join("、") || currentAlias.value} 的授权 —— 这不是「这段时间没有订单」，请换有权限的账号，或联系管理员开通。`;
  }
  if (errorStatus.value === 404) {
    return `${message}。可用别名由后端 OZON_STORES 白名单决定，前端不能凭空造别名。`;
  }
  return message;
});

// 趋势/构成/合计：单店与合计两个响应里字段同名同形，这里统一取当前模式的那一份
const trend = computed(() => boardData.value?.trend ?? []);
const composition = computed(() => boardData.value?.composition ?? []);
/** 订单明细只有单店模式有（合计不下发订单明细，它是单店口径） */
const orders = computed<ResOrderRow[]>(() => overview.value?.orders ?? []);
const totals = computed(() => boardData.value?.totals ?? null);

/* ------------------------------ 数字格式化 ------------------------------ */
// ⚠️ 金额一律是字符串（后端为避免 JS 浮点误差刻意下发字符串）。
// 这里只做「显示用」的格式化，**不做任何加减乘除**；合计口径全部来自后端。
const moneyFormatter = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** 把后端金额字符串转成「显示用」数字；缺失返回 null（缺失是 null，不是 "0.00"） */
const toNumber = (value: unknown): number | null => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

/** ¥ 金额：千分位 + 2 位小数。字段名沿用后端金额字符串 */
const formatMoney = (value: unknown) => {
  const num = toNumber(value);
  return num === null ? "--" : `¥${moneyFormatter.format(num)}`;
};

/** 汇率：4 位小数更贴近实际报价精度 */
const formatRate = (value: unknown) => {
  const num = toNumber(value);
  return num === null ? "--" : num.toFixed(4);
};

/** 百分数：后端给的是字符串小数（"1.0000"），转成百分比展示 */
const formatPercent = (value: unknown) => {
  const num = toNumber(value);
  return num === null ? "--" : `${(num * 100).toFixed(2)}%`;
};

/** 是否为负数（仅用于着色，不参与计算） */
const isNegative = (value: unknown) => {
  const num = toNumber(value);
  return num !== null && num < 0;
};

/** data_cutoff 是北京时间 ISO 串，截到分钟足够 */
const formatCutoff = (value: string | null | undefined) => (value ? dayjs(value).format("YYYY-MM-DD HH:mm") : "--");

/* ------------------------------ 指标卡 ------------------------------ */
/**
 * 指标卡的 `drilldown` 字段决定它能不能点开逐 SKU 明细。
 *
 * 为什么**只在单店模式下**可点：逐 SKU 归属靠的是单店库里的分组键
 * （ADR-0006），多店合计没有这个口径 —— 跨库按货号聚合前还得先确认
 * 「同一货号在不同店的采购成本是不是同一份」。所以合计模式下不给这个入口，
 * 并在卡片下方直说原因，而不是让用户点进去看到一个单店的明细。
 */
const metricCards = computed(() => {
  const data = totals.value;
  const aggregateMode = isAggregate.value;
  const footSuffix = aggregateMode ? "（多店合计：逐 SKU 下钻请切到单店）" : "";
  return [
    {
      key: "actual_profit_cny",
      label: "实际利润合计",
      // 合计直接取后端字符串，绝不在前端累加明细
      value: data ? moneyFormatter.format(toNumber(data.actual_profit_cny) ?? 0) : "--",
      unit: "CNY",
      color: "var(--el-color-primary)",
      icon: markRaw(Wallet),
      drilldown: aggregateMode ? null : ("actual" as SkuProfitMode),
      title: aggregateMode ? "多店合计不支持逐 SKU 下钻（下钻是单店口径）" : "点击查看实际利润的逐 SKU 明细",
      foot: `完整核算 ${data?.complete_order_count ?? 0} / 共 ${data?.total_order_count ?? 0} 单（全窗口口径，非明细行数）${footSuffix}`
    },
    {
      key: "estimated_profit_cny",
      label: "预估利润合计",
      value: data ? moneyFormatter.format(toNumber(data.estimated_profit_cny) ?? 0) : "--",
      unit: "CNY",
      color: "var(--el-color-success)",
      icon: markRaw(TrendCharts),
      drilldown: aggregateMode ? null : ("estimated" as SkuProfitMode),
      title: aggregateMode ? "多店合计不支持逐 SKU 下钻（下钻是单店口径）" : "点击查看预估利润的逐 SKU 明细",
      foot: "收入 − 采购成本 − 物流费 − 预估平台佣金（后端现算）"
    },
    {
      key: "completion_rate",
      label: "核算完成率",
      value: data ? formatPercent(data.completion_rate) : "--",
      unit: "",
      color: "var(--el-color-warning)",
      icon: markRaw(DataAnalysis),
      drilldown: null,
      title: aggregateMode ? "Σ完整单 / Σ总单（不是各店完成率的平均）" : "完整核算订单数 / 总订单数",
      foot: aggregateMode ? "Σ完整单数 / Σ总单数 —— 不是各店完成率的平均" : "actual_complete=1 的订单数 / 总订单数"
    },
    {
      key: "overdue_count",
      label: "待发货逾期单数",
      value: data && data.overdue_count !== null ? String(data.overdue_count) : "--",
      unit: "单",
      color: "var(--el-color-danger)",
      icon: markRaw(Warning),
      drilldown: null,
      title: "当前逾期单数，与统计区间无关；任一店给不出时整体为空",
      foot: aggregateMode ? "各店相加；任一店给不出则为空（不当成 0）" : "截至数据截止时刻的当前逾期数，与统计区间无关"
    }
  ];
});

/* ------------------------------ 逐 SKU 下钻 ------------------------------ */
/** 抽屉是否打开，以及用哪个利润口径打开（两个指标卡共用同一个抽屉） */
const drilldownOpen = ref(false);
const drilldownMode = ref<SkuProfitMode>("estimated");

const openDrilldown = (item: { drilldown: SkuProfitMode | null; label: string }) => {
  if (!item.drilldown) return;
  drilldownMode.value = item.drilldown;
  drilldownOpen.value = true;
};

/* ------------------------------ 图表配置 ------------------------------ */
/** 读取运行时 CSS 变量，保证图表文字/网格跟随亮暗主题 */
const cssVar = (name: string, fallback: string) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
};

/**
 * ⚠️ ECharts 的 canvas 绘制不认识 CSS 变量字符串（直接传 var(...) 会画成黑色），
 * 因此这里统一把主题变量解析成具体色值后再交给图表。
 * 代价：主题换色后需要重新加载页面才能让图表跟随，图表文字/网格仍走实时读取。
 */
const palette = ref<string[]>([]);
const resolvePalette = () => {
  palette.value = [
    cssVar("--el-color-primary", "#009688"),
    cssVar("--el-color-success", "#67c23a"),
    cssVar("--el-color-warning", "#e6a23c"),
    cssVar("--el-color-danger", "#f56c6c")
  ];
};

const axisColor = () => cssVar("--el-text-color-secondary", "#909399");
const splitColor = () => cssVar("--el-border-color-lighter", "#ebeef5");
const areaBorderColor = () => cssVar("--el-bg-color-overlay", "#ffffff");

const trendOption = computed<ECOption>(() => ({
  color: [palette.value[0], palette.value[1]],
  tooltip: {
    trigger: "axis",
    valueFormatter: (value: unknown) => `¥${moneyFormatter.format(Number(value))}`
  },
  legend: { data: ["实际利润", "预估利润"], top: 0, textStyle: { color: axisColor() } },
  grid: { left: 8, right: 20, top: 44, bottom: 8, containLabel: true },
  xAxis: {
    type: "category",
    boundaryGap: false,
    data: trend.value.map(item => item.date.slice(5)),
    axisLine: { lineStyle: { color: splitColor() } },
    axisLabel: { color: axisColor() }
  },
  yAxis: {
    type: "value",
    name: "利润 (¥)",
    nameTextStyle: { color: axisColor() },
    axisLabel: { color: axisColor() },
    splitLine: { lineStyle: { color: splitColor(), type: "dashed" } }
  },
  series: [
    {
      name: "实际利润",
      type: "line",
      smooth: true,
      showSymbol: true,
      symbolSize: 6,
      areaStyle: { opacity: 0.08 },
      // 折线图坐标必须是 number，这里仅做绘图用转换；不做任何金额加减
      data: trend.value.map(item => toNumber(item.actual_profit_cny))
    },
    {
      name: "预估利润",
      type: "line",
      smooth: true,
      showSymbol: true,
      symbolSize: 6,
      areaStyle: { opacity: 0.08 },
      data: trend.value.map(item => toNumber(item.estimated_profit_cny))
    }
  ]
}));

const breakdownOption = computed<ECOption>(() => {
  /** ⚠️ 这里的占比只用于**图形展示**，不是财务口径；财务合计一律用 totals */
  const total = composition.value.reduce((sum, item) => sum + (toNumber(item.value_cny) ?? 0), 0);
  const percentMap = new Map(
    composition.value.map(item => [item.label, total ? (((toNumber(item.value_cny) ?? 0) / total) * 100).toFixed(2) : "0.00"])
  );

  return {
    color: [palette.value[0], palette.value[1], palette.value[2], palette.value[3]],
    tooltip: {
      trigger: "item",
      formatter: (params: any) => `${params.name}：¥${moneyFormatter.format(params.value)}（${params.percent}%）`
    },
    legend: {
      bottom: 0,
      icon: "circle",
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: axisColor(), fontSize: 12 },
      // 占比直接写进图例文案，避免小扇形内部的标签被裁切
      formatter: (name: string) => `${name} ${percentMap.get(name) ?? "0.00"}%`
    },
    series: [
      {
        name: "利润构成",
        type: "pie",
        radius: ["48%", "68%"],
        center: ["50%", "44%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: areaBorderColor(), borderWidth: 2 },
        label: { show: false },
        labelLine: { show: false },
        data: composition.value.map(item => ({ name: item.label, value: toNumber(item.value_cny) ?? 0 }))
      }
    ]
  };
});

/* ------------------------------ 数据加载 ------------------------------ */
const loadData = async () => {
  loading.value = true;
  loadError.value = null;
  try {
    // 图表色板需要从当前主题变量解析，放在数据渲染前执行
    resolvePalette();
    // 店铺列表只取一次（页面之间共享），它决定「别名 → 展示名」与可选项
    await storeStore.load();
    await ensureAliases();

    if (!selectedAliases.value.length) {
      // 一个可看的店都没有：这不是「空数据」，是没得看 —— 走错误面板而不是空白页
      throw {
        message: storeStore.error?.message ?? "当前账号没有任何可查看的店铺（或所有店铺库都不可用）",
        status: storeStore.error?.status,
        isNetworkError: storeStore.error?.isNetworkError ?? false
      } as BackendError;
    }

    if (isAggregate.value) {
      // 多店合计：**由后端算**。前端不做跨店累加（会变成第二套口径）
      aggregate.value = await getStoreAggregateApi(selectedAliases.value, days.value);
      overview.value = null;
    } else {
      // 单店：订单明细走服务端分页，合计仍是整个窗口的口径
      overview.value = await getStoreDashboardApi(currentAlias.value, days.value, {
        offset: (page.value - 1) * pageSize.value,
        limit: pageSize.value
      });
      aggregate.value = null;
    }
  } catch (error) {
    const e = error as BackendError;
    overview.value = null;
    aggregate.value = null;
    loadError.value = e;
    // 401：令牌失效 —— 清掉本地令牌并跳回登录页，不显示空数据
    if (e?.status === 401) {
      handleUnauthorized();
      ElMessage.error("登录已失效，请重新登录");
      return;
    }
    // 403 / 404 / 503 / 网络失败：留在当前页，展示明确错误 + 重试
    ElMessage.error(e?.message ?? "看板数据加载失败");
  } finally {
    loading.value = false;
  }
};

/**
 * 把 URL 里的店铺与后端下发的可见店铺对齐。
 *
 * 两件事：
 *  1. 没有任何选择时选第一个可用的店（不写死别名）；
 *  2. URL 里有无权/不可用的别名时**把它从 URL 里去掉**，并提示一次 ——
 *     否则页面会一直吃 403，而用户不知道自己为什么进不去。
 */
const ensureAliases = async () => {
  const wanted = requestedAliases.value;
  const resolved = storeStore.resolveAliases(wanted);
  if (wanted.length && resolved.length !== wanted.length) {
    const dropped = wanted.filter(a => !resolved.includes(a));
    ElMessage.warning(`已忽略不可用的店铺：${dropped.join("、")}`);
  }
  const next = resolved.length ? resolved : storeStore.defaultAliases();
  requestedAliases.value = next;
  if (next.join(",") !== wanted.join(",")) {
    await router.replace({ query: { ...route.query, stores: next.join(",") } });
  }
};

const onDaysChange = () => {
  page.value = 1;
  loadData();
};

const onPageSizeChange = () => {
  page.value = 1;
  loadData();
};

/** 订单明细卡片的 DOM 引用：翻页后把它的顶部滚回视野 */
const ordersCardRef = ref<HTMLElement | null>(null);

/** 翻页后把订单卡片顶部滚回视野（表格已限高内部滚动，这里只滚一次页面） */
const scrollOrdersToTop = () => ordersCardRef.value?.scrollIntoView({ block: "start", behavior: "smooth" });

/**
 * 页码变化（`TablePager` 已经把新页码写回 `page`）→ 只负责取数。
 *
 * ⚠️ 不要在这里判断「目标页是否等于当前页」：Element Plus 会先 emit
 * `update:current-page` 再 emit `current-change`，那时两者已经相等，
 * 判断的结果是「永远不动」—— 这正是「点了没反应」的经典写法。
 */
const onPageChange = async () => {
  await loadData();
  scrollOrdersToTop();
};

/** 从逐店明细跳该店的单店看板（合计模式下的出口） */
const openSingleStore = (alias: string) => {
  page.value = 1;
  router.replace({ query: { ...route.query, stores: alias, page: undefined } });
  requestedAliases.value = [alias];
  loadData();
};

/**
 * 从店铺管理页的「逐 SKU 明细」跳过来：`?stores=x&drilldown=estimated`。
 * 只在单店模式下自动打开抽屉 —— 合计模式没有逐 SKU 口径（ADR-0008 §六），
 * 那种情况下忽略这个参数，而不是打开一个单店的明细冒充合计。
 */
const maybeOpenDrilldown = () => {
  const wanted = route.query.drilldown;
  if (wanted !== "estimated" && wanted !== "actual") return;
  if (isAggregate.value || !currentAlias.value) return;
  drilldownMode.value = wanted;
  drilldownOpen.value = true;
};

const goLogin = () => router.replace(LOGIN_URL);

/** URL 变化（浏览器前进后退、店铺管理页跳过来）→ 重新取数 */
watch(
  () => route.query.stores,
  value => {
    const next = parseStores(value);
    if (next.join(",") === requestedAliases.value.join(",")) return;
    requestedAliases.value = next;
    page.value = 1;
    loadData();
  }
);

onMounted(async () => {
  await loadData();
  maybeOpenDrilldown();
});
</script>

<style scoped lang="scss">
/* 看板专用语义变量，颜色全部沿用 Element Plus 主题变量，避免散落硬编码 hex */
.dashboard {
  --board-gap: 16px;
  --board-radius: 8px;

  .card {
    padding: 16px;
    background-color: var(--el-bg-color-overlay);
    border: 1px solid var(--el-border-color-lighter);
    border-radius: var(--board-radius);
  }
}

/* 顶部店铺信息 */
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

  .alias-tag {
    margin-left: 8px;
    font-weight: 400;
  }

  .head-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
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
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .store-select {
    width: 320px;
  }

  .days-select {
    width: 120px;
  }
}

/* 店铺下拉：一行里左边店名、右边结算日/不可用标记 */
.option-main {
  float: left;
}

.option-side {
  float: right;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

/* 多店合计的说明块 */
.aggregate-note {
  margin: 0;
  line-height: 1.7;
}

.aggregate-warnings {
  padding-left: 18px;
  margin: 8px 0 0;

  li {
    line-height: 1.7;
  }
}

/* 逐店明细里的店名格 */
.store-cell {
  display: flex;
  gap: 8px;
  align-items: center;
}

/* 拖后腿的那家店：窗口右端与共同窗口不一致，标红提醒 */
.lagging {
  color: var(--el-color-danger);
  font-weight: 600;
}

/* 翻页条的样式统一在 components/TablePager.vue 里（看板与逐 SKU 抽屉共用） */

/* 首次加载：骨架屏 */
.dashboard-loading {
  padding: 24px;
  background-color: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--board-radius);
}

/* 错误面板：不白屏，给出原因 + 可执行的下一步 */
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
    color: var(--el-text-color-primary);
  }

  .error-desc {
    max-width: 620px;
    margin: 0;
    line-height: 1.6;
    color: var(--el-text-color-secondary);
  }

  .error-actions {
    display: flex;
    gap: 8px;
  }

  .error-tip {
    margin: 0;
    font-size: 12px;
    color: var(--el-text-color-placeholder);

    code {
      padding: 2px 6px;
      background-color: var(--el-fill-color-light);
      border-radius: 4px;
    }
  }
}

/* 指标卡 */
.metric-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 132px;

  .metric-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 14px;
    color: var(--el-text-color-secondary);
  }

  .metric-icon {
    font-size: 16px;
  }

  /* 「可点击查看明细」的提示：hover 才显形，静态时不抢视线 */
  .metric-hint {
    display: inline-flex;
    gap: 2px;
    align-items: center;
    margin-left: auto;
    font-size: 12px;
    color: var(--el-color-primary);
    opacity: 0;
    transition: opacity 0.15s ease;
  }

  .hint-arrow {
    font-size: 12px;
  }

  .metric-value {
    display: flex;
    align-items: baseline;
    gap: 6px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .metric-number {
    font-size: 26px;
    line-height: 1.2;
  }

  .metric-unit {
    font-size: 13px;
    font-weight: 400;
    color: var(--el-text-color-secondary);
  }

  .metric-foot {
    margin-top: auto;
    font-size: 12px;
    color: var(--el-text-color-placeholder);
  }
}

/* 可下钻的指标卡：鼠标手型 + hover 抬升 + 顶部描边，三重提示「这里能点」 */
.metric-clickable {
  cursor: pointer;
  transition:
    border-color 0.15s ease,
    box-shadow 0.15s ease,
    transform 0.15s ease;

  &:hover,
  &:focus-visible {
    border-color: var(--el-color-primary-light-5);
    box-shadow: 0 2px 12px rgb(0 0 0 / 8%);
    transform: translateY(-1px);
    outline: none;

    .metric-hint {
      opacity: 1;
    }
  }
}

/* 图表卡：lg 断点下两列等高 */
.chart-row {
  align-items: stretch;

  :deep(.el-col) {
    display: flex;
  }

  .chart-card {
    display: flex;
    flex: 1;
    flex-direction: column;
  }
}

.card-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--board-gap);
}

.card-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.card-tip {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.chart-body {
  flex: 1;
  min-height: 320px;
}

/* 明细表 */
.order-table {
  font-variant-numeric: tabular-nums;

  .reason-text {
    display: inline-block;
    max-width: 150px;
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

/* 移动端：指标卡与图表卡改为单列堆叠，统一补下间距（16/24px 节奏） */
@media (max-width: 768px) {
  .metric-card {
    margin-bottom: var(--board-gap);
  }

  .chart-row {
    :deep(.el-col) {
      margin-bottom: var(--board-gap);

      &:last-child {
        margin-bottom: 0;
      }
    }

    .chart-card {
      margin-bottom: 0;
    }
  }
}
</style>
