import axios, { AxiosError, AxiosResponse, InternalAxiosRequestConfig } from "axios";

import { LOGIN_URL } from "@/config";
import router from "@/routers";
import { useUserStore } from "@/stores/modules/user";

/**
 * @description OZON 真实后端的 axios 实例
 *
 * 为什么不复用 `@/api/index.ts`（脚手架那套）：
 *  - 它的响应拦截器假定返回体是 `{code,msg,data}` 包装，而真实后端**原样返回 JSON**；
 *  - 它发送的鉴权头是 `x-access-token`，真实后端要的是 `Authorization: Bearer <token>`。
 * 所以这里单独一个实例、单独一套拦截器，不污染脚手架原有逻辑。
 *
 * baseURL 取 `/api`（.env 里的 VITE_API_URL）：
 *  - 开发：走 vite.config.ts 的 dev 代理转到 http://127.0.0.1:8849，同源、无跨域（后端刻意没开 CORS）；
 *  - 生产：由部署层的反向代理处理 `/api` 前缀。
 */

/** 后端返回的结构化错误 */
export interface BackendError {
  /** HTTP 状态码；网络层失败（后端没启动/断网/超时）时是 undefined */
  status?: number;
  /** 后端 `detail` 或本地兜底文案 */
  message: string;
  /** true 表示请求根本没到服务端（后端未启动、断网、超时） */
  isNetworkError: boolean;
}

const backend = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "/api",
  timeout: 20000,
  headers: { "Content-Type": "application/json" }
});

/** 请求拦截：带上 Bearer 令牌 */
backend.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const { token } = useUserStore();
    if (token) config.headers.set("Authorization", `Bearer ${token}`);
    return config;
  },
  error => Promise.reject(error)
);

/** 把 axios 的异常统一转成 BackendError，交给调用方决定怎么展示 */
const toBackendError = (error: AxiosError): BackendError => {
  // 没有 response：请求没到服务端 —— 后端未启动、断网、或超时
  if (!error.response) {
    const isTimeout = error.code === "ECONNABORTED" || error.message.includes("timeout");
    return {
      message: isTimeout ? "请求超时，请稍后重试" : "无法连接后端服务，请确认后端已启动（127.0.0.1:8849）",
      isNetworkError: true
    };
  }

  const { status, data } = error.response;
  // 后端所有错误体都是 `{"detail": "..."}`，原样透出，不要吞掉
  const detail = (data as { detail?: unknown } | undefined)?.detail;
  const message =
    typeof detail === "string" && detail
      ? detail
      : ({
          400: "请求参数有误",
          401: "请先登录",
          403: "无权访问该店铺",
          404: "店铺不存在",
          422: "请求参数不合法",
          500: "服务端内部错误",
          503: "店铺库不可用（服务端配置故障）"
        }[status] ?? `请求失败（HTTP ${status}）`);

  return { status, message, isNetworkError: false };
};

/** 响应拦截：直接返回 body（`http.get<T>` 的 T 就是真实响应体） */
backend.interceptors.response.use(
  (response: AxiosResponse) => response.data,
  (error: AxiosError) => Promise.reject(toBackendError(error))
);

/**
 * @description 401 的统一处理：清掉令牌、把用户送回登录页
 *
 * 注意这里**不做"静默失败"** —— 返回 false 让调用方知道这次数据没拿到，
 * 由调用方决定展示什么（看板会显示错误面板，而不是伪装成空数据）。
 */
export const handleUnauthorized = (): false => {
  const userStore = useUserStore();
  userStore.setToken("");
  if (router.currentRoute.value.path !== LOGIN_URL) {
    router.replace(LOGIN_URL);
  }
  return false;
};

export default backend;
