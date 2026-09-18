<template>
  <div>
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <b>账号管理</b>
          <el-button type="primary" :icon="Plus" @click="openCreate">新建账号</el-button>
        </div>
      </template>

      <el-table :data="users" v-loading="loading" border stripe>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="username" label="用户名" width="140" />
        <el-table-column prop="display_name" label="显示名" width="140" />
        <el-table-column label="角色" width="130">
          <template #default="{ row }">
            <el-tag :type="roleTag(row.role)">{{ roleLabel(row.role) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="可见店铺">
          <template #default="{ row }">
            <template v-if="row.role === 'root'">
              <el-tag size="small" type="info">全部店铺</el-tag>
            </template>
            <template v-else>
              <el-tag v-for="s in row.stores" :key="s" size="small" style="margin-right:4px">{{ s }}</el-tag>
              <span v-if="!row.stores || !row.stores.length" style="color:#bbb">未授权</span>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'danger'" size="small">
              {{ row.is_active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="warning" @click="resetPwd(row)">改密</el-button>
            <el-button link type="danger" @click="removeUser(row)" :disabled="row.id === me.id">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="dialog" :title="editing ? '编辑账号' : '新建账号'" width="560px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="用户名">
          <el-input v-model="form.username" :disabled="editing" placeholder="3-32 位字母数字" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" />
        </el-form-item>
        <el-form-item label="密码" v-if="!editing">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width:100%">
            <el-option v-for="r in roles" :key="r.value" :label="r.label" :value="r.value">
              <span>{{ r.label }}</span>
              <span style="float:right;color:#999;font-size:12px">{{ r.desc }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="可见店铺" v-if="form.role !== 'root'">
          <el-select v-model="form.stores" multiple style="width:100%" placeholder="选择授权店铺">
            <el-option v-for="s in stores" :key="s.store_alias" :label="s.display_name || s.store_alias" :value="s.store_alias" />
          </el-select>
        </el-form-item>
        <el-form-item label="账号状态" v-if="editing">
          <el-switch v-model="form.is_active" active-text="启用" inactive-text="停用" />
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
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { adminApi } from '../api'

const me = JSON.parse(localStorage.getItem('user') || '{}')
const users = ref([])
const stores = ref([])
const roles = ref([])
const loading = ref(false)
const saving = ref(false)
const dialog = ref(false)
const editing = ref(null)
const form = ref({ username: '', display_name: '', password: '', role: 'store_operator', stores: [], is_active: true })

const roleLabelMap = { root: 'root 管理员', store_operator: '店铺运营', finance: '财务' }
function roleLabel(r) { return roleLabelMap[r] || r }
function roleTag(r) { return r === 'root' ? 'danger' : (r === 'finance' ? 'warning' : 'primary') }

async function load() {
  loading.value = true
  try {
    const [u, s, r] = await Promise.all([adminApi.listUsers(), adminApi.listStores(), adminApi.listRoles()])
    users.value = u
    stores.value = s
    roles.value = r
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  form.value = { username: '', display_name: '', password: '', role: 'store_operator', stores: [], is_active: true }
  dialog.value = true
}

function openEdit(row) {
  editing.value = row
  form.value = {
    username: row.username,
    display_name: row.display_name,
    password: '',
    role: row.role,
    stores: [...(row.stores || [])],
    is_active: row.is_active,
  }
  dialog.value = true
}

async function save() {
  saving.value = true
  try {
    if (editing.value) {
      const payload = {
        display_name: form.value.display_name,
        role: form.value.role,
        is_active: form.value.is_active,
        stores: form.value.role === 'root' ? [] : form.value.stores,
      }
      await adminApi.updateUser(editing.value.id, payload)
      ElMessage.success('已保存')
    } else {
      if (!form.value.username || !form.value.password) {
        ElMessage.warning('请填写用户名和密码'); return
      }
      await adminApi.createUser({
        username: form.value.username,
        password: form.value.password,
        display_name: form.value.display_name,
        role: form.value.role,
        stores: form.value.role === 'root' ? [] : form.value.stores,
      })
      ElMessage.success('已创建')
    }
    dialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '操作失败')
  } finally {
    saving.value = false
  }
}

async function resetPwd(row) {
  try {
    const { value } = await ElMessageBox.prompt(`为 ${row.username} 设置新密码`, '修改密码', {
      inputType: 'password',
      inputValidator: (v) => (v && v.length >= 6) || '至少 6 位',
    })
    await adminApi.updateUser(row.id, { password: value })
    ElMessage.success('密码已修改')
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.response?.data?.detail || '操作失败')
  }
}

async function removeUser(row) {
  try {
    await ElMessageBox.confirm(`确定删除账号 ${row.username}?`, '删除确认', { type: 'warning' })
    await adminApi.deleteUser(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.response?.data?.detail || '删除失败')
  }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
</style>
