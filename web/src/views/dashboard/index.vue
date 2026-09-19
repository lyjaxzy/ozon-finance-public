<template>
  <div class="dashboard">
    <!-- 请求成功：正常看板（v-if 同时承担 null 收窄，ready 与 overview 等价，避免重复判断） -->
    <template v-if="overview">
      <header class="page-head card">
        <div class="head-main">
          <div class="head-title">
            <el-icon class="head-icon"><Shop /></el-icon>
            <span class="shop-name">{{ storeDisplayName }}</span>
            <el-tag class="alias-tag" size="small" effect="plain">别名 {{ alias }}</el-tag>
          </div>
          <div class="head-meta">
            <span class="meta-item">
              <el-icon><Clock /></el-icon>
              数据截止：{{ formatCutoff(overview.data_cutoff) }}
            </span>
            <el-divider direction="vertical" />
            <span class="meta-item">
              <el-icon><Calendar /></el-icon>
              统计区间：{{ overview.period.start }} ~ {{ overview.period.end }}（{{ overview.period.days }} 天）
            </span>
          </div>
        </div>
        <div class="head-actions">
          <el-select v-model="days" class="days-select" @change="loadData">
            <el-option v-for="item in DASHBOARD_DAY_OPTIONS" :key="item" :label="`近 ${item} 天`" :value="item" />
          </el-select>
          <el-button type="primary" :icon="Refresh" @click="loadData">刷新</el-button>
        </div>
      </header>

      <!-- 1. 顶部指标卡 -->
      <el-row :gutter="16" class="mb16">
        <el-col v-for="item in metricCards" :key="item.key" :xs="24" :sm="12" :lg="6">
          <div
            class="card metric-card"
            :class="{ 'metric-clickable': item.drilldown }"
            :role="item.drilldown ? 'button' : undefined"
            :tabindex="item.drilldown ? 0 : undefined"
            :title="item.drilldown ? `点击查看${item.label}的逐 SKU 明细` : undefined"
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
              <h3 class="card-title">实际利润 vs 预估利润（近 {{ overview.period.days }} 天）</h3>
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

      <!-- 4. 订单明细表 -->
      <div class="card">
        <div class="card-head">
          <h3 class="card-title">订单明细</h3>
          <span class="card-tip">
            本窗口共 {{ overview.totals.total_order_count }} 单，明细下发上限 {{ orders.length }} 条
            （后端配置），合计口径以上方指标卡为准
          </span>
        </div>
        <el-table :data="orders" stripe class="order-table" :header-cell-style="{ textAlign: 'right' }">
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
        </el-table>
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
        接口：<code>GET /api/dashboard/store/{{ alias }}?days={{ days }}</code> → 127.0.0.1:8849（经 vite dev 代理）
      </p>
    </div>

    <!-- 逐 SKU 利润下钻（点「实际利润合计」/「预估利润合计」指标卡打开） -->
    <SkuDetailDrawer
      v-model="drilldownOpen"
      v-model:mode="drilldownMode"
      :alias="alias"
      :days="days"
    />
  </div>
</template>

<script setup lang="ts" name="dashboard">
import { ArrowRight, Calendar, Clock, DataAnalysis, Refresh, Shop, TrendCharts, Wallet, Warning } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { ElMessage } from "element-plus";
import { computed, markRaw, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { handleUnauthorized, type BackendError } from "@/api/backendRequest";
import type { ResDashboard, ResOrderRow } from "@/api/interfaces/backend";
import { getStoreDashboardApi, ORDER_STATUS_MAP, type SkuProfitMode } from "@/api/modules/dashboard";
import ECharts from "@/components/ECharts/index.vue";
import type { ECOption } from "@/components/ECharts/config";
import { LOGIN_URL } from "@/config";
import { DASHBOARD_DAY_OPTIONS, DEFAULT_STORE_ALIAS, STORE_DISPLAY_NAMES, type StoreAlias } from "@/config/store";

import SkuDetailDrawer from "./components/SkuDetailDrawer.vue";

/**
 * 店铺别名：集中配置在 `@/config/store.ts`，这里只消费。
 * 当前后端只注册了 store_alpha（真实数据）/ store_beta（库文件不存在），
 * 接多店时改配置文件即可，本页不需要动。
 */
const alias = ref<StoreAlias>(DEFAULT_STORE_ALIAS);
const storeDisplayName = computed(() => STORE_DISPLAY_NAMES[alias.value]);

/** 统计窗口（天）。后端只接受 days，不接受任意日期区间，见 dashboard.ts 注释 */
const days = ref<number>(14);

const router = useRouter();
const loading = ref(false);
/** 后端返回的看板数据（整体保存，页头也要用 period / data_cutoff） */
const overview = ref<ResDashboard | null>(null);
/** 请求失败的结构化错误；非空即渲染错误面板 */
const loadError = ref<BackendError | null>(null);

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
    return `${message}。当前账号没有 ${alias.value} 的授权 —— 这不是「这段时间没有订单」，请换有权限的账号，或联系管理员开通。`;
  }
  if (errorStatus.value === 404) {
    return `${message}。可用别名由后端 OZON_STORES 白名单决定，前端不能凭空造别名。`;
  }
  return message;
});

const trend = computed(() => overview.value?.trend ?? []);
const composition = computed(() => overview.value?.composition ?? []);
const orders = computed<ResOrderRow[]>(() => overview.value?.orders ?? []);
const totals = computed(() => overview.value?.totals ?? null);

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
 * 目前只给「实际利润合计」与「预估利润合计」两张卡开下钻 ——
 * 它们背后是同一份逐 SKU 数据（接口一次就把两个口径都返回了），
 * 所以多一张卡的成本只是切换默认排序与高亮口径。
 */
const metricCards = computed(() => {
  const data = totals.value;
  return [
    {
      key: "actual_profit_cny",
      label: "实际利润合计",
      // 合计直接取后端字符串，绝不在前端累加明细
      value: data ? moneyFormatter.format(toNumber(data.actual_profit_cny) ?? 0) : "--",
      unit: "CNY",
      color: "var(--el-color-primary)",
      icon: markRaw(Wallet),
      drilldown: "actual" as SkuProfitMode,
      foot: `完整核算 ${data?.complete_order_count ?? 0} / 共 ${data?.total_order_count ?? 0} 单（全窗口口径，非明细行数）`
    },
    {
      key: "estimated_profit_cny",
      label: "预估利润合计",
      value: data ? moneyFormatter.format(toNumber(data.estimated_profit_cny) ?? 0) : "--",
      unit: "CNY",
      color: "var(--el-color-success)",
      icon: markRaw(TrendCharts),
      drilldown: "estimated" as SkuProfitMode,
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
      foot: "actual_complete=1 的订单数 / 总订单数"
    },
    {
      key: "overdue_count",
      label: "待发货逾期单数",
      value: data ? String(data.overdue_count) : "--",
      unit: "单",
      color: "var(--el-color-danger)",
      icon: markRaw(Warning),
      drilldown: null,
      foot: "截至数据截止时刻的当前逾期数，与统计区间无关"
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
    overview.value = await getStoreDashboardApi(alias.value, days.value);
  } catch (error) {
    const e = error as BackendError;
    overview.value = null;
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

const goLogin = () => router.replace(LOGIN_URL);

onMounted(() => loadData());
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

  .days-select {
    width: 120px;
  }
}

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
