<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="4" v-for="c in cards" :key="c.label">
        <el-card shadow="hover">
          <div class="stat-label">{{ c.label }}</div>
          <div class="stat-value">{{ c.value }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top:16px">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header><b>每日趋势</b></template>
          <div ref="trendRef" style="height:320px"></div>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never">
          <template #header><b>逾期快照</b></template>
          <el-descriptions :column="1" border v-if="summary.overdue">
            <el-descriptions-item label="活动订单">{{ summary.overdue.active_order_count }}</el-descriptions-item>
            <el-descriptions-item label="逾期订单">{{ summary.overdue.overdue_order_count }}</el-descriptions-item>
            <el-descriptions-item label="快照时间">{{ summary.overdue.as_of || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
        <el-card shadow="never" style="margin-top:16px">
          <template #header><b>亏损/issue 汇总</b></template>
          <el-table :data="lossRows" border empty-text="暂无数据">
            <el-table-column prop="issue_type" label="类型" />
            <el-table-column prop="issue_count" label="单数" />
            <el-table-column prop="estimated_loss_cny" label="预估亏损(CNY)" :formatter="fmtMoney" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { dashboardApi } from '../api'
import * as echarts from 'echarts'

const route = useRoute()
const loading = ref(false)
const summary = ref({})
const lossRows = ref([])
let trendChart

const alias = computed(() => route.params.alias)

const cards = computed(() => {
  const s = summary.value || {}
  const o = s.overdue || {}
  return [
    { label: '营收(近30天)', value: fmtMoneyPlain(s.revenue_cny) },
    { label: '预估利润(近30天)', value: fmtMoneyPlain(s.estimated_profit_cny) },
    { label: '利润率', value: (s.profit_margin_pct ?? 0).toFixed(2) + '%' },
    { label: '订单数', value: (s.order_count ?? 0).toLocaleString() },
    { label: '逾期单', value: (o.overdue_order_count ?? 0).toLocaleString() },
    { label: '总成本', value: fmtMoneyPlain(s.total_cost_cny) },
  ]
})

function fmtMoney(row, col, val) { return fmtMoneyPlain(val) }
function fmtMoneyPlain(v) { return (v ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }

async function load() {
  if (!alias.value) return
  loading.value = true
  try {
    const data = await dashboardApi.storeDetail(alias.value, 30)
    summary.value = data.summary || {}
    lossRows.value = data.loss ? Object.entries(data.loss.by_type || {}).map(([issue_type, v]) => ({ issue_type, ...v })) : []
    await nextTick()
    renderTrend(data.trend || [])
  } finally {
    loading.value = false
  }
}

function renderTrend(trend) {
  if (!trendRef.value) return
  if (!trendChart) trendChart = echarts.init(trendRef.value)
  trendChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['营收', '预估利润'] },
    grid: { left: 70, right: 20, top: 40, bottom: 40 },
    xAxis: { type: 'category', data: trend.map(t => t.metric_date) },
    yAxis: { type: 'value' },
    series: [
      { name: '营收', type: 'line', smooth: true, data: trend.map(t => t.revenue_cny) },
      { name: '预估利润', type: 'line', smooth: true, data: trend.map(t => t.estimated_profit_cny) },
    ],
  })
}

const trendRef = ref(null)
onMounted(load)
// 路由参数变化时(如切换店铺)重新加载
watch(alias, () => { load() })
</script>

<style scoped>
.stat-label { color: #8c8c8c; font-size: 13px; }
.stat-value { font-size: 22px; font-weight: 700; margin-top: 6px; }
</style>
