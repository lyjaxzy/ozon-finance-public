<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="brand">OZON 看板</div>
      <el-menu :default-active="menuActive" router background-color="#001529" text-color="#fff" active-text-color="#409EFF">
        <el-menu-item v-if="isRoot" index="/admin">
          <el-icon><DataBoard /></el-icon><span>总览(全部店铺)</span>
        </el-menu-item>
        <el-menu-item v-if="isRoot" index="/stores">
          <el-icon><Shop /></el-icon><span>店铺管理</span>
        </el-menu-item>
        <el-menu-item v-if="isRoot" index="/users">
          <el-icon><UserFilled /></el-icon><span>账号管理</span>
        </el-menu-item>
        <el-menu-item v-for="s in myStores" :key="s" :index="'/store/'+s">
          <el-icon><Shop /></el-icon><span>{{ s }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span class="title">{{ pageTitle }}</span>
        <div class="right">
          <el-tag size="small" type="success">{{ roleLabel }}</el-tag>
          <span class="who">{{ user?.display_name || user?.username }}</span>
          <el-button link @click="pwdDialog = true">修改密码</el-button>
          <el-button link @click="logout">退出登录</el-button>
        </div>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>

    <el-dialog v-model="pwdDialog" title="修改密码" width="420px">
      <el-form label-width="90px">
        <el-form-item label="原密码">
          <el-input v-model="pwd.old_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="pwd.new_password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdDialog = false">取消</el-button>
        <el-button type="primary" :loading="pwdSaving" @click="changePwd">确定</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authApi } from '../api'

const route = useRoute()
const router = useRouter()
const user = ref(JSON.parse(localStorage.getItem('user') || 'null'))

const isRoot = computed(() => user.value?.role === 'root')
const roleLabel = computed(() => {
  const map = { root: 'root 管理员', store_operator: '店铺运营', finance: '财务' }
  return map[user.value?.role] || user.value?.role
})
const myStores = computed(() => (isRoot.value ? [] : (user.value?.stores || [])))
const menuActive = computed(() => route.path)
const pageTitle = computed(() => {
  if (route.path === '/admin') return '总览 · 全部店铺'
  if (route.path === '/users') return '账号管理'
  if (route.path === '/stores') return '店铺管理'
  if (route.params.alias) return `店铺 · ${route.params.alias}`
  return 'OZON 云端看板'
})

const pwdDialog = ref(false)
const pwdSaving = ref(false)
const pwd = ref({ old_password: '', new_password: '' })

async function changePwd() {
  pwdSaving.value = true
  try {
    await authApi.changePassword(pwd.value.old_password, pwd.value.new_password)
    ElMessage.success('密码已修改')
    pwdDialog.value = false
    pwd.value = { old_password: '', new_password: '' }
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '修改失败')
  } finally {
    pwdSaving.value = false
  }
}

function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
  router.push('/login')
}
</script>

<style scoped>
.layout { height: 100vh; }
.aside { background: #001529; }
.brand { color: #fff; font-size: 18px; font-weight: 700; text-align: center; padding: 18px 0; }
.header { background: #fff; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #eee; }
.title { font-size: 18px; font-weight: 600; }
.right { display: flex; align-items: center; gap: 10px; }
.who { color: #666; font-size: 13px; }
.main { background: #f5f7fa; }
</style>
