import http from "@/api";
import { PORT1 } from "@/api/config/servicePort";
import { ResPage, User } from "@/api/interface/index";

/**
 * @description 获取用户列表
 * @param params User.ReqUserParams
 * @returns Promise<ResPage<User.ResUserList>>
 */
export const getUserList = (params: User.ReqUserParams) => {
  return http.post<ResPage<User.ResUserList>>(PORT1 + `/user/list`, params);
};

/**
 * @description 获取树形用户列表
 * @param params User.ReqUserParams
 * @returns Promise<ResPage<User.ResUserList>>
 */
export const getUserTreeList = (params: User.ReqUserParams) => {
  return http.post<ResPage<User.ResUserList>>(PORT1 + `/user/tree/list`, params);
};

/**
 * @description 新增用户
 * @param params { id: string }
 * @returns Promise<void>
 */
export const addUser = (params: { id: string }) => {
  return http.post(PORT1 + `/user/add`, params);
};

/**
 * @description 批量添加用户
 * @param params FormData
 * @returns Promise<void>
 */
export const batchAddUser = (params: FormData) => {
  return http.post(PORT1 + `/user/import`, params);
};

/**
 * @description 编辑用户
 * @param params { id: string }
 * @returns Promise<void>
 */
export const editUser = (params: { id: string }) => {
  return http.post(PORT1 + `/user/edit`, params);
};

/**
 * @description 删除用户
 * @param params { id: string[] }
 * @returns Promise<void>
 */
export const deleteUser = (params: { id: string[] }) => {
  return http.post(PORT1 + `/user/delete`, params);
};

/**
 * @description 切换用户状态
 * @param params { id: string; status: number }
 * @returns Promise<void>
 */
export const changeUserStatus = (params: { id: string; status: number }) => {
  return http.post(PORT1 + `/user/change`, params);
};

/**
 * @description 重置用户密码
 * @param params { id: string }
 * @returns Promise<void>
 */
export const resetUserPassWord = (params: { id: string }) => {
  return http.post(PORT1 + `/user/rest_password`, params);
};

/**
 * @description 导出用户数据
 * @param params User.ReqUserParams
 * @returns Promise<void>
 */
export const exportUserInfo = (params: User.ReqUserParams) => {
  return http.download(PORT1 + `/user/export`, params);
};

/**
 * @description 获取用户状态字典
 * @returns Promise<User.ResStatus[]>
 */
export const getUserStatus = () => {
  return http.get<User.ResStatus[]>(PORT1 + `/user/status`);
};

/**
 * @description 获取用户性别字典
 * @returns Promise<User.ResGender[]>
 */
export const getUserGender = () => {
  return http.get<User.ResGender[]>(PORT1 + `/user/gender`);
};

/**
 * @description 获取用户部门列表
 * @returns Promise<User.ResDepartment[]>
 */
export const getUserDepartment = () => {
  return http.get<User.ResDepartment[]>(PORT1 + `/user/department`, {}, { cancel: false });
};

/**
 * @description 获取用户角色字典
 * @returns Promise<User.ResRole[]>
 */
export const getUserRole = () => {
  return http.get<User.ResRole[]>(PORT1 + `/user/role`);
};
