<template>
  <div>
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <b>店铺管理</b>
          <el-button type="primary" :icon="Plus" @click="openCreate">新增店铺</el-button>
        </div>
      </template>

      <el-table :data="stores" v-loading="loading" border stripe>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="store_alias" label="店铺别名" width="150" />
        <el-table-column prop="display_name" label="显示名" width="160" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="metric_days" label="已导入天数" width="110" />
        <el-table-column prop="source_db_path" label="数据来源" show-overflow-tooltip />
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="success" @click="$router.push('/store/'+row.store_alias)">看板</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialog" :title="editing ? '编辑店铺' : '新增店铺'" width="560px">
      <el-form :model="form" label-width="110px">
        <el-form-item label="店铺别名">
          <el-input v-model="form.store_alias" :disabled="!!editing" placeholder="如 store_alpha(小写字母数字-_)" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="数据来源路径">
          <el-input v-model="form.source_db_path" placeholder="店铺 SQLite 路径(便于溯源)" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { adminApi } from '../api'

const stores = ref([])
const loading = ref(false)
const saving = ref(false)
const dialog = ref(false)
const editing = ref(null)
const form = ref({ store_alias: '', display_name: '', enabled: true, source_db_path: '' })

async function load() {
  loading.value = true
  try {
    stores.value = await adminApi.listStores()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  form.value = { store_alias: '', display_name: '', enabled: true, source_db_path: '' }
  dialog.value = true
}

function openEdit(row) {
  editing.value = row
  form.value = { ...row }
  dialog.value = true
}

async function save() {
  if (!form.value.store_alias) { ElMessage.warning('请填写店铺别名'); return }
  saving.value = true
  try {
    await adminApi.upsertStore({
      store_alias: form.value.store_alias,
      display_name: form.value.display_name,
      enabled: form.value.enabled,
      source_db_path: form.value.source_db_path,
    })
    ElMessage.success('已保存')
    dialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
</style>
