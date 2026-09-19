<template>
  <!--
    翻页条（看板订单明细与逐 SKU 抽屉共用）。

    为什么抽成组件而不是各写一份：两处的翻页行为必须完全一致，
    而「上一页 / 下一页」这种地方最容易出现一处修好、另一处还是旧写法。

    为什么按钮带文字：原先是 Element Plus 默认的两个 12px 图标箭头，
    实测被用户当成装饰（反馈「没有上一页/下一页按钮」）。分页控件必须一眼看得懂。

    两种形态：
      * compact —— 卡片头部那个：只放「第 X / Y 页」+ 两个按钮，够用且不抢视线；
      * 完整 —— 表格下方那个：再加每页条数、页码、跳页。
  -->
  <div class="table-pager" :class="{ 'is-compact': compact }">
    <div class="pager-left">
      <span class="pager-page">第 <b>{{ page }}</b> / {{ pageCount }} 页</span>
      <span v-if="!compact" class="pager-total">共 {{ total }} {{ unit }} · 每页 {{ pageSize }} {{ sizeUnit }}</span>
    </div>
    <div class="pager-right">
      <el-button :icon="ArrowLeft" :disabled="loading || page <= 1" @click="go(page - 1)">上一页</el-button>
      <el-button :disabled="loading || page >= pageCount" @click="go(page + 1)">
        下一页<el-icon class="el-icon--right"><ArrowRight /></el-icon>
      </el-button>
      <el-pagination
        v-if="!compact"
        v-model:current-page="currentPage"
        v-model:page-size="currentSize"
        :page-sizes="pageSizes"
        :total="total"
        background
        layout="sizes, pager, jumper"
        @current-change="onCurrentChange"
        @size-change="onSizeChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ArrowLeft, ArrowRight } from "@element-plus/icons-vue";
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    /** 当前页，从 1 开始 */
    page: number;
    /** 总页数（至少 1） */
    pageCount: number;
    /** 记录总数（全量口径，不随分页变化） */
    total: number;
    pageSize: number;
    pageSizes: number[];
    loading?: boolean;
    /** 计数单位，例如「单」「个」 */
    unit?: string;
    /** 每页条数的单位，例如「条」「个」 */
    sizeUnit?: string;
    /** 紧凑形态：只留页码与两个按钮（放表格上方用） */
    compact?: boolean;
  }>(),
  { loading: false, unit: "条", sizeUnit: "条", compact: false }
);

const emit = defineEmits<{
  (event: "update:page", value: number): void;
  (event: "update:pageSize", value: number): void;
  /** 页码变了（父组件据此取数） */
  (event: "change"): void;
  /** 每页条数变了（父组件据此取数） */
  (event: "size-change"): void;
}>();

/** 目标页永远夹在 [1, pageCount] 内 —— 越界请求不可能发出去 */
const clamp = (value: number) => Math.min(Math.max(Math.trunc(value) || 1, 1), props.pageCount);

const currentPage = computed({
  get: () => props.page,
  set: (value: number) => emit("update:page", clamp(value))
});

const currentSize = computed({
  get: () => props.pageSize,
  set: (value: number) => emit("update:pageSize", value)
});

const go = (target: number) => {
  const next = clamp(target);
  if (next === props.page) return;
  emit("update:page", next);
  emit("change");
};

/**
 * `el-pagination` 的页码 / 跳页触发。
 * ⚠️ Element Plus 会**先** emit `update:current-page`（v-model 写回父组件）
 * **再** emit `current-change`，所以这里不能再判断「目标页是否等于当前页」——
 * 那时它已经相等了，判断的结果是「永远不动」。
 */
const onCurrentChange = () => emit("change");

const onSizeChange = (value: number) => {
  emit("update:pageSize", value);
  emit("update:page", 1);
  emit("size-change");
};
</script>

<style scoped lang="scss">
.table-pager {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding-top: 10px;
  margin-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);

  /* 紧凑形态（表格上方）：去掉分隔线，右对齐 */
  &.is-compact {
    justify-content: flex-end;
    padding-top: 0;
    margin-top: 0;
    margin-bottom: 8px;
    border-top: none;
  }

  .pager-left {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: baseline;
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .pager-page {
    color: var(--el-text-color-primary);

    b {
      color: var(--el-color-primary);
    }
  }

  .pager-right {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
}
</style>
