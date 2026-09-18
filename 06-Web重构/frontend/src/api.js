import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 20000,
})

// 请求拦截: 自动附 token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截: 401 跳登录
api.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      if (location.pathname !== '/login') {
        location.href = '/login'
      }
    }
    return Promise.reject(err)
  }
)

export default api

export const authApi = {
  login: (username, password) => api.post('/auth/login', { username, password }),
  me: () => api.get('/auth/me'),
  changePassword: (old_password, new_password) =>
    api.post('/auth/change-password', { old_password, new_password }),
}

export const adminApi = {
  listUsers: () => api.get('/admin/users'),
  createUser: (payload) => api.post('/admin/users', payload),
  updateUser: (id, payload) => api.put(`/admin/users/${id}`, payload),
  deleteUser: (id) => api.delete(`/admin/users/${id}`),
  listStores: () => api.get('/admin/stores'),
  upsertStore: (payload) => api.post('/admin/stores', payload),
  listRoles: () => api.get('/admin/roles'),
}

export const dashboardApi = {
  overview: () => api.get('/dashboard/overview'),
  storeDetail: (alias, days = 30) => api.get(`/dashboard/store/${alias}?days=${days}`),
}
