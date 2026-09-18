<template>
  <div class="dashboard" v-loading="loading" element-loading-text="正在加载看板数据…">
    <!-- 页面头部：店铺名 + 数据截止时间 + 区间切换 -->
    <header class="page-head card">
      <div class="head-main">
        <div class="head-title">
          <el-icon class="head-icon"><Shop /></el-icon>
          <span class="shop-name">{{ overview?.shop_name ?? "—" }}</span>
        </div>
        <div class="head-meta">
          <span class="meta-item">
            <el-icon><Clock /></el-icon>
            数据截止时间：{{ overview?.data_updated_at ?? "—" }}
          </span>
          <el-divider direction="vertical" />
          <span class="meta-item">
            <el-icon><Calendar /></el-icon>
            统计区间：{{ query.startDate }} ~ {{ query.endDate }}（{{ trend.length }} 天）
          </span>
        </div>
      </div>
      <div class="head-actions">
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          unlink-panels
          range-separator="~"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          :clearable="false"
          @change="onRangeChange"
        />
        <el-button type="primary" :icon="Refresh" @click="loadData">刷新</el-button>
      </div>
    </header>

    <el-alert v-if="loadError" class="mb16" type="error" :title="loadError" show-icon :closable="false" />

    <!-- 1. 顶部指标卡 -->
    <el-row :gutter="16" class="mb16">
      <el-col v-for="item in metricCards" :key="item.key" :xs="24" :sm="12" :lg="6">
        <div class="card metric-card">
          <div class="metric-label">
            <el-icon class="metric-icon" :style="{ color: item.color }"><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
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
            <h3 class="card-title">实际利润 vs 预估利润（近 {{ trend.length }} 天）</h3>
            <span class="card-tip">单位：¥</span>
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
            <span class="card-tip">统一折算为 ¥</span>
          </div>
          <div class="chart-body">
            <ECharts v-if="profitRows.length" :option="breakdownOption" height="320" />
            <el-empty v-else description="暂无利润构成数据" :image-size="96" />
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- 4. 订单明细表 -->
    <div class="card">
      <div class="card-head">
        <h3 class="card-title">订单明细</h3>
        <span class="card-tip">共 {{ orders.length }} 条 · 金额右对齐，保留 2 位小数</span>
      </div>
      <el-table :data="orders" stripe class="order-table" :header-cell-style="{ textAlign: 'right' }">
        <el-table-column prop="posting_number" label="订单号" min-width="150" align="left" fixed />
        <el-table-column prop="settlement_date" label="结算日期" min-width="120" align="right" />
        <el-table-column label="直接净额 (₽)" min-width="130" align="right">
          <template #default="{ row }">{{ formatRub(row.direct_net_rub) }}</template>
        </el-table-column>
        <el-table-column label="汇率" min-width="100" align="right">
          <template #default="{ row }">{{ formatRate(row.exchange_rate) }}</template>
        </el-table-column>
        <el-table-column label="采购成本 (¥)" min-width="130" align="right">
          <template #default="{ row }">{{ formatMoney(row.purchase_cost_cny) }}</template>
        </el-table-column>
        <el-table-column label="实际利润 (¥)" min-width="130" align="right">
          <template #default="{ row }">
            <span :class="row.actual_profit_cny >= 0 ? 'profit-positive' : 'profit-negative'">
              {{ formatMoney(row.actual_profit_cny) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="核算状态" min-width="230" align="left">
          <template #default="{ row }">
            <el-tag :type="ORDER_STATUS_MAP[row.status as Dashboard.OrderStatus].tagType" size="small" effect="light">
              {{ ORDER_STATUS_MAP[row.status as Dashboard.OrderStatus].label }}
            </el-tag>
            <el-tooltip v-if="row.unknown_reason" :content="row.unknown_reason" placement="top">
              <span class="reason-text">{{ row.unknown_reason }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts" name="dashboard">
import { Calendar, Clock, DataAnalysis, Refresh, Shop, TrendCharts, Wallet, Warning } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { ElMessage } from "element-plus";
import { computed, markRaw, onMounted, reactive, ref } from "vue";

import type { Dashboard } from "@/api/interface";
import { mockDashboardApi, ORDER_STATUS_MAP } from "@/api/modules/dashboard";
import ECharts from "@/components/ECharts/index.vue";
import type { ECOption } from "@/components/ECharts/config";

/** 默认统计区间：近 14 天 */
const defaultRange: [string, string] = [
  dayjs().subtract(13, "day").format("YYYY-MM-DD"),
  dayjs().format("YYYY-MM-DD")
];

const dateRange = ref<[string, string]>([...defaultRange]);
const query = reactive<Required<Pick<Dashboard.ReqOverview, "startDate" | "endDate">>>({
  startDate: defaultRange[0],
  endDate: defaultRange[1]
});

const loading = ref(false);
const loadError = ref("");
const overview = ref<Dashboard.ResOverview | null>(null);
const trend = ref<Dashboard.ResTrendPoint[]>([]);
const profitRows = ref<Dashboard.ResProfitBreakdownItem[]>([]);
const orders = ref<Dashboard.ResOrderRow[]>([]);

/* ------------------------------ 数字格式化 ------------------------------ */
const moneyFormatter = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** ¥ 金额：千分位 + 2 位小数 */
const formatMoney = (value: number | null | undefined) =>
  value === null || value === undefined ? "--" : `¥${moneyFormatter.format(value)}`;

/** ₽ 金额：千分位 + 2 位小数 */
const formatRub = (value: number | null | undefined) =>
  value === null || value === undefined ? "--" : `₽${moneyFormatter.format(value)}`;

/** 汇率：4 位小数更贴近实际报价精度 */
const formatRate = (value: number | null | undefined) =>
  value === null || value === undefined ? "--" : value.toFixed(4);

const formatPercent = (value: number | null | undefined) =>
  value === null || value === undefined ? "--" : `${(value * 100).toFixed(2)}%`;

/* ------------------------------ 指标卡 ------------------------------ */
const metricCards = computed(() => {
  const data = overview.value;
  return [
    {
      key: "actual_profit_cny",
      label: "实际利润合计",
      value: data ? moneyFormatter.format(data.actual_profit_cny) : "--",
      unit: "CNY",
      color: "var(--el-color-primary)",
      icon: markRaw(Wallet),
      foot: `完整核算 ${data?.completed_order_count ?? 0} / 共 ${data?.total_order_count ?? 0} 单`
    },
    {
      key: "estimated_profit_cny",
      label: "预估利润合计",
      value: data ? moneyFormatter.format(data.estimated_profit_cny) : "--",
      unit: "CNY",
      color: "var(--el-color-success)",
      icon: markRaw(TrendCharts),
      foot: "含未完整核算订单的待补费用"
    },
    {
      key: "completion_rate",
      label: "核算完成率",
      value: data ? formatPercent(data.completion_rate) : "--",
      unit: "",
      color: "var(--el-color-warning)",
      icon: markRaw(DataAnalysis),
      foot: "完整核算订单数 / 总订单数"
    },
    {
      key: "overdue_count",
      label: "待发货逾期单数",
      value: data ? String(data.overdue_count) : "--",
      unit: "单",
      color: "var(--el-color-danger)",
      icon: markRaw(Warning),
      foot: "超出发货时效，需优先处理"
    }
  ];
});

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
      data: trend.value.map(item => item.actual_profit_cny)
    },
    {
      name: "预估利润",
      type: "line",
      smooth: true,
      showSymbol: true,
      symbolSize: 6,
      areaStyle: { opacity: 0.08 },
      data: trend.value.map(item => item.estimated_profit_cny)
    }
  ]
}));

const breakdownOption = computed<ECOption>(() => {
  const total = profitRows.value.reduce((sum, item) => sum + item.value_cny, 0);
  const percentMap = new Map(
    profitRows.value.map(item => [item.name, total ? ((item.value_cny / total) * 100).toFixed(2) : "0.00"])
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
        data: profitRows.value.map(item => ({ name: item.name, value: item.value_cny }))
      }
    ]
  };
});

/* ------------------------------ 数据加载 ------------------------------ */
const loadData = async () => {
  loading.value = true;
  loadError.value = "";
  try {
    // ⚠️ 当前为本地 mock 数据，接口就绪后无需改动本页逻辑
    const data = await mockDashboardApi({ startDate: query.startDate, endDate: query.endDate });
    // 图表色板需要从当前主题变量解析，放在数据渲染前执行
    resolvePalette();
    overview.value = data.overview;
    trend.value = data.trend;
    profitRows.value = data.profit_breakdown;
    orders.value = data.orders;
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : "看板数据加载失败，请稍后重试";
    ElMessage.error(loadError.value);
  } finally {
    loading.value = false;
  }
};

const onRangeChange = (value: [string, string] | null) => {
  if (!value?.[0] || !value?.[1]) return;
  query.startDate = value[0];
  query.endDate = value[1];
  loadData();
};

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
