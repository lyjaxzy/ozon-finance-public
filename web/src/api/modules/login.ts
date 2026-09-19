import type { ResLogin, ResMe } from "@/api/interfaces/backend";
import type { Login } from "@/api/interface";
import backend, { type BackendError } from "@/api/backendRequest";
import authButtonList from "@/assets/json/authButtonList.json";
import authMenuList from "@/assets/json/authMenuList.json";

/**
 * @description 用户登录 —— 真实后端 `POST /api/auth/login`
 *
 * 后端刻意**没有**对请求体做任何包装，响应就是
 * `{access_token, token_type, expires_in, user}`。
 * 失败时返回 HTTP 401 + `{"detail": "用户名或口令错误"}`，
 * 我们把它包成 BackendError 抛出去，由登录页原样展示 `detail`，**不吞掉**。
 *
 * 口令按后端 `api/data/users.json` 里的 pbkdf2 校验，前端**不做任何哈希**：
 * 后端认定的是明文口令本身（admin/admin 等），先 md5 反而永远登不上。
 */
export const loginApi = async (params: { username: string; password: string }): Promise<ResLogin> => {
  return backend.post<unknown, ResLogin>("/auth/login", params);
};

/**
 * @description 当前登录用户 + 可见店铺列表（`GET /api/auth/me`）
 *
 * `stores` 只含该用户**可见**的店铺，页面据此决定默认店铺，
 * 避免运营账号（operator01）打开页面就往无权限的店铺上撞 403。
 */
export const getMeApi = async (): Promise<ResMe> => {
  return backend.get<unknown, ResMe>("/auth/me");
};

/** 判断一个错误是不是后端返回的结构化错误 */
export const isBackendError = (error: unknown): error is BackendError => {
  return typeof error === "object" && error !== null && "message" in error && "isNetworkError" in error;
};

/**
 * @description 获取菜单列表
 *
 * ⚠️ 仍是本地 JSON（`src/assets/json/authMenuList.json`）：后端只提供只读财务接口，
 * **没有**菜单/按钮权限接口。菜单是前端的静态结构，不是本次「接真实 API」的范围。
 *
 * 返回体保持脚手架约定的 `{code, msg, data}` 包装形状，
 * 这样 `stores/modules/auth.ts` 里现有的 `const { data } = await ...` 不用改。
 */
export const getAuthMenuListApi = () => {
  return { code: 200, msg: "ok", data: authMenuList.data as Menu.MenuOptions[] };
};

/**
 * @description 获取按钮权限
 *
 * ⚠️ 同菜单：仍是本地 JSON，后端没有对应接口。
 * 真实的权限隔离在**服务端**做（跨店 403），前端隐藏入口不算隔离。
 */
export const getAuthButtonListApi = () => {
  return { code: 200, msg: "ok", data: authButtonList.data as Login.ResAuthButtons };
};

/**
 * @description 用户退出登录
 *
 * 后端没有登出接口、也没有令牌吊销名单（api/README 8.1-2），
 * 登出只能是前端清掉本地令牌，等令牌自然过期。
 */
export const logoutApi = async (): Promise<void> => {
  return Promise.resolve();
};
