# web —— OZON 财务系统前端

基于开源脚手架 **Geeker-Admin** 构建，不是从零搭的（见 `docs/adr/0004`）。

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/HalseySpicy/Geeker-Admin |
| 版本 | v1.2.0（package.json 中为 1.2.0） |
| 获取方式 | `codeload.github.com` tar 包（本机 `github.com` 直连不稳定） |
| 许可证 | MIT —— 见同目录 `LICENSE`，**必须随本项目一并保留** |

## 我们拿它做什么

只复用它**已验证的基础设施**：布局、菜单、路由守卫、权限指令、主题、
ProTable 与 ECharts 封装。**业务页面与 API 客户端是我们的，独立分层**，
不混进脚手架目录 —— 否则下次升级上游时会被搅在一起。

## 相对上游做了哪些改动

1. 包名 `geeker-admin` → `ozon-finance-web`，版本归零为 `0.1.0`
2. **移除脚本** `commit`（上游实现是 `git add -A && czg && git push`，
   会绕过我们的门禁直接推送）、`prepare`（husky 安装 git 钩子，
   与本项目 `.githooks` + `core.hooksPath` 冲突）、`release`、`lint:lint-staged`
3. 移除对应依赖：husky / lint-staged / czg / commitizen / standard-version /
   @commitlint/*
4. 未纳入 `.husky/`、`.vscode/`、`CHANGELOG.md`

## 尚未做（**下一步**）

- **示例页面尚未清理**：`src/views` 下仍是上游的演示页。
  需要逐个替换为我们的页面，而不是留着当参考 —— 留着会误导后来人。
- 尚未接我们的只读 API。

## 开发命令

```powershell
cd web
pnpm install
pnpm dev           # 开发服务器
pnpm build:pro     # 生产构建（含 vue-tsc 类型检查）
pnpm type:check    # 只做类型检查
```
