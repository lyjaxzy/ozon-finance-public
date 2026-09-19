<template>
  <el-form ref="loginFormRef" :model="loginForm" :rules="loginRules" size="large">
    <el-form-item prop="username">
      <el-input v-model="loginForm.username" placeholder="用户名：admin（root）/ finance01 / operator01">
        <template #prefix>
          <el-icon class="el-input__icon">
            <user />
          </el-icon>
        </template>
      </el-input>
    </el-form-item>
    <el-form-item prop="password">
      <el-input
        v-model="loginForm.password"
        type="password"
        placeholder="口令：admin（与用户名相同）"
        show-password
        autocomplete="new-password"
      >
        <template #prefix>
          <el-icon class="el-input__icon">
            <lock />
          </el-icon>
        </template>
      </el-input>
    </el-form-item>
  </el-form>
  <div class="login-btn">
    <el-button :icon="CircleClose" round size="large" @click="resetForm(loginFormRef)"> 重置 </el-button>
    <el-button :icon="UserFilled" round size="large" type="primary" :loading="loading" @click="login(loginFormRef)">
      登录
    </el-button>
  </div>
</template>

<script setup lang="ts">
import { CircleClose, UserFilled } from "@element-plus/icons-vue";
import type { ElForm } from "element-plus";
import { ElMessage, ElNotification } from "element-plus";
import { onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import type { BackendError } from "@/api/backendRequest";
import { isBackendError, loginApi } from "@/api/modules/login";
import { HOME_URL } from "@/config";
import { initDynamicRouter } from "@/routers/modules/dynamicRouter";
import { useKeepAliveStore } from "@/stores/modules/keepAlive";
import { useTabsStore } from "@/stores/modules/tabs";
import { useUserStore } from "@/stores/modules/user";

const router = useRouter();
const userStore = useUserStore();
const tabsStore = useTabsStore();
const keepAliveStore = useKeepAliveStore();

type FormInstance = InstanceType<typeof ElForm>;
const loginFormRef = ref<FormInstance>();
const loginRules = reactive({
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入口令", trigger: "blur" }]
});

const loading = ref(false);
const loginForm = reactive({
  username: "",
  password: ""
});

/**
 * @description 登录 —— 真实后端 `POST /api/auth/login`
 *
 * ⚠️ 口令**不做 md5**：后端 users.json 里存的是 pbkdf2(明文口令)，
 * 而明文口令等于用户名（admin/admin）。先前端哈希会永远登录失败。
 * 口令在浏览器里也只是走本机 vite 代理，不会再叠加一层客户端哈希来假装安全。
 */
const login = (formEl: FormInstance | undefined) => {
  if (!formEl) return;
  formEl.validate(async valid => {
    if (!valid) return;
    loading.value = true;
    try {
      // 1.执行登录接口（后端成功返回 {access_token, token_type, expires_in, user}）
      const res = await loginApi({ username: loginForm.username, password: loginForm.password });
      userStore.setToken(res.access_token);

      // 2.添加动态路由
      await initDynamicRouter();

      // 3.清空 tabs、keepAlive 数据
      tabsStore.setTabs([]);
      keepAliveStore.setKeepAliveName([]);

      // 4.跳转到首页（单店财务看板）
      router.push(HOME_URL);
      ElNotification({
        title: "登录成功",
        message: `欢迎回来，${res.user?.display_name ?? loginForm.username}（${res.user?.role ?? "unknown"}）`,
        type: "success",
        duration: 3000
      });
    } catch (error) {
      // 登录失败要把后端的 detail 原样提示出来（如「用户名或口令错误」），不要吞掉
      const backendError = error as BackendError;
      const message = isBackendError(error)
        ? backendError.message
        : error instanceof Error
          ? error.message
          : "登录失败，请稍后重试";
      ElMessage.error(message);
    } finally {
      loading.value = false;
    }
  });
};

// resetForm
const resetForm = (formEl: FormInstance | undefined) => {
  if (!formEl) return;
  formEl.resetFields();
};

onMounted(() => {
  // 监听 enter 事件（调用登录）
  document.onkeydown = (e: KeyboardEvent) => {
    if (e.code === "Enter" || e.code === "enter" || e.code === "NumpadEnter") {
      if (loading.value) return;
      login(loginFormRef.value);
    }
  };
});

onBeforeUnmount(() => {
  document.onkeydown = null;
});
</script>

<style scoped lang="scss">
@use "../index";
</style>
