<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="6" v-for="c in cards" :key="c.label">
        <el-card shadow="hover">
          <div class="stat-label">{{ c.label }}</div>
          <div class="stat-value">{{ c.value }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card style="margin-top:16px" shadow="never">
      <template #header><b>各店铺经营概览(近30天)</b></template>
      <el-table :data="parsed.per_store" v-loading="loading" border stripe>
        <el-table-column prop="store_alias" label="店铺" />
        <el-table-column prop="revenue_cny" label="营收(CNY)" :formatter="fmtMoney" />
        <el-table-column prop="estimated_profit_cny" label="预估利润(CNY)" :formatter="fmtMoney" />
        <el-table-column prop="profit_margin_pct" label="利润率" :formatter="fmtPct" />
        <el-table-column prop="order_count" label="订单数" />
        <el-table-column prop="overdue_count" label="逾期单" />
        <el-table-column label="操作">
          <template #default="{ row }">
            <el-button link type="primary" @click="$router.push('/store/'+row.store_alias)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card style="margin-top:16px" shadow="never">
      <template #header><b>营收趋势</b></template>
      <div ref="chartRef" style="height:320px"></div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { dashboardApi } from '../api'
import * as echarts from 'echarts'

const loading = ref(false)
const raw = ref({ per_store: [], totals: {}, store_count: 0 })
let chart

const parsed = computed(() => raw.value)

const cards = computed(() => {
  const t = raw.value.totals || {}
  return [
    { label: '店铺数', value: raw.value.store_count },
    { label: '营收(近30天)', value: fmtMoneyPlain(t.revenue_cny) },
    { label: '预估利润(近30天)', value: fmtMoneyPlain(t.estimated_profit_cny) },
    { label: '订单数(近30天)', value: (t.order_count || 0).toLocaleString() },
  ]
})

function fmtMoney(row, col, val) { return fmtMoneyPlain(val) }
function fmtPct(row, col, val) { return (val ?? 0).toFixed(2) + '%' }
function fmtMoneyPlain(v) { return (v ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }

async function load() {
  loading.value = true
  try {
    const data = await dashboardApi.overview()
    raw.value = data
    await nextTick()
    renderChart(data.per_store)
  } finally {
    loading.value = false
  }
}

function renderChart(stores) {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['营收', '预估利润'] },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: stores.map(s => s.store_alias) },
    yAxis: { type: 'value' },
    series: [
      { name: '营收', type: 'bar', data: stores.map(s => s.revenue_cny) },
      { name: '预估利润', type: 'bar', data: stores.map(s => s.estimated_profit_cny) },
    ],
  })
}

const chartRef = ref(null)
onMounted(load)
</script>

<style scoped>
.stat-label { color: #8c8c8c; font-size: 13px; }
.stat-value { font-size: 26px; font-weight: 700; margin-top: 6px; }
</style>
