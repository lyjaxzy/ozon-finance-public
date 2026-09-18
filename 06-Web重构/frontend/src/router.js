import { createRouter, createWebHistory } from 'vue-router'
import Login from './views/Login.vue'
import Layout from './views/Layout.vue'
import AdminDashboard from './views/AdminDashboard.vue'
import StoreDashboard from './views/StoreDashboard.vue'
import UserManagement from './views/UserManagement.vue'
import StoreManagement from './views/StoreManagement.vue'

const routes = [
  { path: '/login', name: 'login', component: Login, meta: { public: true } },
  {
    path: '/',
    component: Layout,
    children: [
      { path: '', redirect: '/admin' },
      { path: 'admin', name: 'admin', component: AdminDashboard, meta: { roles: ['root'] } },
      { path: 'users', name: 'users', component: UserManagement, meta: { roles: ['root'] } },
      { path: 'stores', name: 'stores', component: StoreManagement, meta: { roles: ['root'] } },
      { path: 'store/:alias', name: 'store', component: StoreDashboard, meta: { roles: ['root', 'store_operator', 'finance'] } },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 登录守卫
router.beforeEach((to) => {
  const token = localStorage.getItem('token')
  const user = JSON.parse(localStorage.getItem('user') || 'null')
  if (to.meta.public) {
    return true
  }
  if (!token) {
    return { name: 'login' }
  }
  // 角色校验
  if (to.meta.roles && user && !to.meta.roles.includes(user.role)) {
    // 无权限则跳到其能看的第一页
    if (user.role === 'root') return { name: 'admin' }
    if (user.stores && user.stores.length) return { name: 'store', params: { alias: user.stores[0] } }
    return { name: 'login' }
  }
  return true
})

export default router
