# Geeker Easy-Mock → Apifox 迁移包

从 `src/assets/mock/geeker/`（原 Easy-Mock）转换而来，**接口路径 / 方法 / 响应逻辑保持一致**。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `geeker.openapi.json` | **主导入文件**（OpenAPI 3.0，符合 Apifox 导入规范） |
| `mock-scripts/*.js` | 动态接口的「高级 Mock → 脚本」（保留原 Easy-Mock 分支逻辑） |
| `geeker.mock-expectations.json` | 可选参考：登录 / 菜单 / 按钮的期望条件说明 |
| `generate.mjs` | 重新生成脚本（改了 geeker 源文件后可再跑一遍） |

## 导入步骤（Apifox）

1. 打开 Apifox 项目 → **项目设置 → 导入数据 → OpenAPI / Swagger**
2. 选择本目录下的 `geeker.openapi.json`
3. 建议选项：
   - 导入 Servers 为环境：开启
   - 接口冲突：覆盖已有接口（首次导入）或智能合并
4. 导入完成后，对下列动态接口打开 **高级 Mock → 脚本**，粘贴对应 `.js` 内容并**打开开关**：

| 接口 | 脚本文件 |
| --- | --- |
| `POST /geeker/login` | `mock-scripts/login.js` |
| `GET /geeker/menu/list` | `mock-scripts/menu-list.js` |
| `GET /geeker/auth/buttons` | `mock-scripts/auth-buttons.js` |
| `POST /geeker/user/list` | `mock-scripts/user-list.js` |
| `POST /geeker/user/tree/list` | `mock-scripts/user-tree-list.js` |
| `POST /geeker/account/list` | `mock-scripts/account-list.js` |
| `POST /geeker/file/upload/img` | `mock-scripts/file-upload-img.js` |

其余静态接口（logout、增删改、字典、上传视频等）导入后的**响应示例**即可直接作为 Mock 返回，无需脚本。

> 说明：OpenAPI 标准不携带 Mock 脚本；`x-apifox.mockScript` 扩展已写入 openapi，若你的 Apifox 版本未自动识别，按上表手动粘贴即可，逻辑与源文件一致。

## 对接前端代理

复制 Apifox 的 Mock 基址（云端或本地），改 `.env.development`：

```bash
# 示例：把原 Easy-Mock 地址换成 Apifox Mock
VITE_PROXY = [["/api","https://mock.apifox.cn/m1/你的项目ID"]]
```

前端请求仍是 `/api` + `/geeker/...`，与原来一致。

## 演示账号（与原 Easy-Mock 相同）

| 用户名 | 密码明文 | body.password（MD5） | access_token |
| --- | --- | --- | --- |
| `admin` | `123456` | `e10adc3949ba59abbe56e057f20f883e` | `bqddxxwqmfncffacvbpkuxvwvqrhln` |
| `user` | `123456` | 同上 | `unufvdotdqxuzfbdygovfmsbftlvbn` |

菜单 / 按钮权限仍按请求头 `x-access-token` 区分 admin / user。

## 重新生成

```bash
node src/assets/mock/apifox/generate.mjs
```

会从 `../geeker/` 重新读取 Easy-Mock 源文件并覆盖本目录产物。
