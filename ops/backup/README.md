# 生产数据备份与恢复演练

对应阶段一 **第 0 步**:先把唯一的真实业务资产保护起来,再谈开发。

## 现状与问题(2026-09-18 实测)

`<DATA_ROOT>` 是生产数据根,里面有:

| 文件 | 大小 |
|---|---|
| `data\stores\store_alpha.db` | 173 MB（10,903 单 / 36,373 流水 / 10,899 条利润事实）|
| `data\stores\store_beta.db` | 7.3 MB |
| `data\multi_store.db` | 9.0 MB |
| `data\desktop\purchase_costs.db` | 2.7 MB |
| `config\credentials.dpapi.json` | OZON 与飞书凭据（DPAPI 加密）|

改造前只有一份 **2026-09-07 的手工快照**,存在三个问题:

1. **陈旧** —— 备份 10,737 单 / 34,390 流水,生产库已 10,903 单 / 36,373 流水,
   缺约 1,983 条流水（含 09-08/09-09 刚补回的数据）
2. **与数据同盘同根** —— `backups\` 就在数据根内,数据根一坏它跟着没
3. **从未验证可恢复** —— `backup_runs` 表 0 行,没有任何恢复记录

## 方案

```
生产数据根                    备份目标
<DATA_ROOT> → D:\OzonFinanceBackup\ozon\<快照ID>\
                               ├─ data\stores\store_alpha.db   (SQLite 在线备份)
                               ├─ config\...                 (原样复制)
                               └─ manifest.json              (哈希 + 行数 + 完整性)
```

### 关键设计

| 决策 | 原因 |
|---|---|
| 用 SQLite 官方 `Connection.backup()` | 应用运行时裸拷贝可能拿到撕裂的页;官方 API 保证一致性快照 |
| 备份写到**数据根之外** | 避免数据根被误删/损坏时连备份一起丢 |
| 每份快照带 `manifest.json` | 记录每个文件的 sha256 + 各表行数 + integrity_check,可独立验证 |
| 保留策略:最近 7 份每日 + 4 份每周 | 自动清理,不会无限增长 |
| **恢复演练**是一等命令 | 没验证过的备份不算备份 |
| 绝不写入生产库 | 包括不写应用自己的 `backup_runs` 表,避免两套系统互相干扰 |

## 用法

```powershell
$py = 'C:\Users\26060\anaconda3\python.exe'
$s  = 'D:\xzy\ozon-finance\ops\backup\ozon_backup.py'

& $py $s run       # 备份 + 校验 + 清理过期（最小间隔 20 小时）
& $py $s list      # 列出所有快照
& $py $s verify    # 校验最新快照（哈希 + 完整性）
& $py $s drill     # 恢复演练：真还原到临时目录并比对行数与哈希
```

## 自动化(已配置)

| 机制 | 内容 |
|---|---|
| 计划任务 | `OzonFinance-DailyBackup` —— 每日 03:30,下次运行 2026/9/19 03:30 |
| 登录补跑 | 启动文件夹 `OzonFinance-Backup.cmd` —— 机器夜间关机时由它接手 |
| 去重 | 两者都调 `run`,由 **最小间隔 20 小时** 保证不会重复备份 |

> 为什么用「启动文件夹」而不是第二个计划任务:
> `/SC ONLOGON` 创建计划任务**需要管理员权限**,当前会话没有;
> 启动文件夹不需要提权,效果等价。

日志:`D:\OzonFinanceBackup\logs\backup-YYYY-MM-DD.log`

## 已知局限(必须说清楚)

**本机 C: 与 D: 是同一块物理 NVMe 盘(磁盘0)。**

因此本方案防的是**误删、库损坏、逻辑错误**,
**防不了磁盘物理损坏** —— 盘一坏,C 和 D 一起没。

真正的离盘副本需要下列之一,当前**都没有**:

- 外接硬盘 / U 盘
- NAS 或网络共享
- 网盘同步目录

拿到介质后,把 `D:\OzonFinanceBackup` 整个同步过去即可,或告诉我,我配第二层。

## 恢复步骤(真要救火时)

1. **停止应用**(否则文件被占用):
   `Get-Process OzonFinance* | Stop-Process`
2. **先保住现场**:把损坏的 `<DATA_ROOT>\data` 改名,不要直接覆盖
3. **从快照还原**对应文件,保持原有相对路径
4. **验证**:`& $py $s verify <快照ID>`,并用 sqlite 打开确认 `PRAGMA integrity_check` 为 ok
5. **启动应用**确认能读到数据

> 注意:`config\credentials.dpapi.json` 是 **DPAPI 加密**的,只能被**同一个 Windows 用户**
> 在**同一台机器**上解密。换机器恢复时,凭据需要重新录入,这是 Windows 的设计而非缺陷。
