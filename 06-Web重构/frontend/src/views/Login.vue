<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <template #header>
        <div class="card-title">OZON 云端看板</div>
      </template>
      <el-form :model="form" @submit.prevent="login">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" size="large">
            <template #prefix><el-icon><User /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码" size="large" show-password @keyup.enter="login">
            <template #prefix><el-icon><Lock /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" style="width:100%" :loading="loading" @click="login">登 录</el-button>
        </el-form-item>
        <el-alert v-if="error" :title="error" type="error" :closable="false" />
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { authApi } from '../api'

const router = useRouter()
const form = ref({ username: '', password: '' })
const loading = ref(false)
const error = ref('')

async function login() {
  if (!form.value.username || !form.value.password) {
    error.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const data = await authApi.login(form.value.username, form.value.password)
    localStorage.setItem('token', data.access_token)
    localStorage.setItem('user', JSON.stringify(data.user))
    // 按角色跳转
    if (data.user.role === 'root') router.push('/admin')
    else if (data.user.stores && data.user.stores.length) router.push('/store/' + data.user.stores[0])
    else router.push('/login')
  } catch (e) {
    error.value = e.response?.data?.detail || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap { height: 100vh; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #001529, #003a70); }
.login-card { width: 400px; }
.card-title { text-align: center; font-size: 20px; font-weight: 700; }
</style>
