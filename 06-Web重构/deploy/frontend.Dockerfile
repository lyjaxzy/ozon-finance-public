# OZON 云端看板 — 前端镜像 (Nginx 托管构建产物 + 代理 API)
FROM node:20-alpine AS build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
# 构建时注入后端地址(仅用于 /api 代理目标在运行时通过 nginx location 统一走, 这里保留默认)
RUN npm run build

FROM nginx:alpine
COPY --from=build /web/dist /usr/share/nginx/html
# 用 nginx 配置把 /api 代理到后端
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
