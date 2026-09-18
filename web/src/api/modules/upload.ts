import http from "@/api";
import { PORT1 } from "@/api/config/servicePort";
import { Upload } from "@/api/interface/index";

/**
 * @description 文件上传
 * @param params FormData
 * @returns Promise<Upload.ResFileUrl>
 */
export const uploadImg = (params: FormData) => {
  return http.post<Upload.ResFileUrl>(PORT1 + `/file/upload/img`, params, { cancel: false });
};

/**
 * @description 视频上传
 * @param params FormData
 * @returns Promise<Upload.ResFileUrl>
 */
export const uploadVideo = (params: FormData) => {
  return http.post<Upload.ResFileUrl>(PORT1 + `/file/upload/video`, params, { cancel: false });
};
