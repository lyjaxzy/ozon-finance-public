import type { Login, ResultData } from "@/api/interface";
import authButtonList from "@/assets/json/authButtonList.json";
import authMenuList from "@/assets/json/authMenuList.json";

/**
 * @description 用户登录
 *
 * ⚠️ 后端尚未就绪，此处为【本地离线 mock】：
 * 任意非空用户名 + 任意非空密码都会登录成功，并返回一个本地假 token。
 * 接后端时把 MOCK_LOGIN 置为 false，并恢复下方 http.post 调用即可。
 *
 * @param params Login.ReqLoginForm
 * @returns Promise<ResultData<Login.ResLogin>>
 */
export const MOCK_LOGIN = true;

/** 本地假 token（仅用于离线演示，不具备任何鉴权能力） */
const createMockToken = () => {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `mock-token.${random}`;
};

export const loginApi = async (params: Login.ReqLoginForm): Promise<ResultData<Login.ResLogin>> => {
  if (MOCK_LOGIN) {
    // 模拟网络往返，让登录按钮的 loading 态可见
    await new Promise(resolve => setTimeout(resolve, 300));
    // 只做最基本的非空校验，密码不做任何比对（mock 阶段任意密码可登录）
    if (!params.username || !params.password) {
      throw new Error("请输入用户名和密码");
    }
    return { code: 200, msg: "登录成功（本地 mock）", data: { access_token: createMockToken() } };
  }
  // 后端就绪后启用下面这行（并删除上方 mock 分支）
  // return http.post<Login.ResLogin>(PORT1 + `/login`, params, { loading: false });
  throw new Error("登录接口尚未接入：请将 MOCK_LOGIN 置为 true，或补全 login.ts 中的真实请求");
};

/**
 * @description 获取菜单列表（本地 JSON，已替换为我们的看板菜单）
 * @returns Promise<Menu.MenuOptions[]>
 */
export const getAuthMenuListApi = () => {
  return authMenuList;
  // 后端就绪后：return http.get<Menu.MenuOptions[]>(PORT1 + `/menu/list`, {}, { loading: false });
};

/**
 * @description 获取按钮权限（本地 JSON）
 * @returns Promise<Login.ResAuthButtons>
 */
export const getAuthButtonListApi = () => {
  return authButtonList;
  // 后端就绪后：return http.get<Login.ResAuthButtons>(PORT1 + `/auth/buttons`, {}, { loading: false });
};

/**
 * @description 用户退出登录（本地 mock，仅清空前端的 token）
 */
export const logoutApi = async (): Promise<ResultData<null>> => {
  // 后端就绪后：return http.post(PORT1 + `/logout`);
  return { code: 200, msg: "已退出登录（本地 mock）", data: null };
};
