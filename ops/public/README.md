# 对外开放（公网 URL）—— 路线、命令与安全边界

> 目标：**你本机服务一启用，别人就能通过一个长期不变的 URL 访问**；
> 访问范围限定为**你邀请的人**。

## 零、先看清这件事的性质

这套系统里是**真实经营数据**（订单号、商品名、收入、采购成本、利润、回款）。
对外开放意味着：任何能到达这个 URL 的人都能看到登录页并尝试登录。
所以「对外」不是一个环境变量，而是一次**安全边界的改变**，至少要满足下面三条，
缺一条都不要开：

| 必须 | 现状 | 怎么做到 |
|---|---|---|
| 1. 强口令（不能再是 `admin`/`admin`） | 默认是口令=用户名 | `python ops/public/prepare_public.py`（已生成，见下节） |
| 2. 强 JWT 密钥（不能是仓库里的默认值） | 默认值公开在版本历史里 | 同上，脚本会生成 64 位十六进制密钥 |
| 3. 只提供**生产构建**的前端，不是 Vite 开发服务器 | 开发服务器会暴露源码 | `cd web; pnpm build:pro`（FastAPI 会挂 `web/dist`） |

第 4 条是**代码保证**的：`OZON_PUBLIC_MODE=1` 时，上面任何一条不满足就
**拒绝启动**（`api/preflight.py`，实现见 ADR-0010）。提示像这样：

```
RuntimeError: OZON_PUBLIC_MODE=1 但对外暴露的前提没满足（拒绝启动，逐条修完再启）：
  1) JWT 密钥还是默认值 —— 默认值公开在仓库里，任何人都能自己签一个合法令牌。
  2) 用户表在仓库目录内（…\api\data\users.json）—— 仓库里的 users.json 是开发用账号，口令等于用户名。
  3) 账号 admin 的口令是弱口令（能被「admin」登进去）
```

## 一、一次性准备（已经做过，换口令时重跑）

```powershell
cd D:\xzy\ozon-finance
python ops\public\prepare_public.py            # 生成（已存在则拒绝覆盖）
python ops\public\prepare_public.py --force    # 重新生成（会换掉所有口令）
python ops\public\prepare_public.py --check    # 只自检，不写文件
```

生成到**仓库之外**（不会进版本历史、也不会同步到公开仓库）：

| 文件 | 内容 |
|---|---|
| `<DATA_ROOT>Platform\config\users.json` | 生产用户表（3 个角色，口令是 20 位随机串，只存哈希） |
| `<DATA_ROOT>Platform\config\platform.env` | `OZON_PUBLIC_MODE=1`、`OZON_JWT_SECRET`、`OZON_USERS_PATH`… |

**口令只在生成时打印一次**，丢了只能 `--force` 重生成（所有人换口令）。

## 二、启动（对外模式，一个端口）

```powershell
cd D:\xzy\ozon-finance
Get-Content "<DATA_ROOT>Platform\config\platform.env" |
  Where-Object { $_ -and -not $_.StartsWith('#') } |
  ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item -Path "Env:$k" -Value $v }
python -m uvicorn api.app:app --host 127.0.0.1 --port 8849
```

`--host 127.0.0.1` 是**故意的**：服务只监听本机，公网入口由隧道提供
（隧道从本机连出去，不需要在防火墙/路由器上开端口 —— 这也是为什么不需要公网 IP）。

**页面与接口同源**：FastAPI 挂载 `web/dist`，所以 `/` 是页面、`/api/*` 是接口，
不需要 CORS，隧道也只需要一条规则指向 `127.0.0.1:8849`。

## 三、两条路线（都满足「长期有效 + 只允许邀请的人」）

### 路线 B：Tailscale（**不需要域名**，推荐你现在用这条）

永久 URL 形如 `https://<机器名>.<你的 tailnet>.ts.net`，只对**加入你 tailnet 的人**可见。

```powershell
# 1) 装客户端（你和每个要访问的同事都要装）
winget install --id Tailscale.Tailscale -e

# 2) 登录（会打开浏览器）
tailscale up

# 3) 把本机 8849 发布到 tailnet（HTTPS 由 Tailscale 自动签发证书）
tailscale serve --bg --https=443 http://127.0.0.1:8849
tailscale serve status          # 这里会显示你的永久 URL

# 4) 邀请同事：Tailscale 后台 → Users → Invite（对方接受后即可访问）
```

⚠️ **不要**执行 `tailscale funnel`：那会把服务放到**公网**，任何拿到 URL 的人都能访问，
与「只允许我邀请的人」相反。

| 优点 | 代价 |
|---|---|
| 不需要域名、零成本 | 每个访问者要装 Tailscale 并接受邀请 |
| URL 永久不变；**将来把服务搬到云服务器，只要新机器加入同一 tailnet，URL 也不变** | 不能给「临时看一次的客户」用 |

> 最后一条正好对上你「保留以后部署到云服务器的可能性」：Tailscale 让你
> **迁机器而不换 URL**（阶段 D 上云时不用再通知所有人换地址）。

### 路线 A：Cloudflare Tunnel（需要**一个域名**，同事零安装）

```powershell
# 1) 装 cloudflared
winget install --id Cloudflare.cloudflared -e

# 2) 登录（浏览器里选你的域名，授权 Cloudflare 管理 DNS）
cloudflared tunnel login

# 3) 建命名隧道（命名隧道 = 永久 URL；临时隧道 trycloudflare.com 每次重启都变）
cloudflared tunnel create ozon-finance

# 4) 把域名指到隧道
cloudflared tunnel route dns ozon-finance ozon.你的域名

# 5) 配置文件 %USERPROFILE%\.cloudflared\config.yml
#    tunnel: <第 3 步输出的 UUID>
#    credentials-file: C:\Users\<你>\.cloudflared\<UUID>.json
#    ingress:
#      - hostname: ozon.你的域名
#        service: http://127.0.0.1:8849
#      - service: http_status:404

# 6) 跑起来 + 装成开机自启服务
cloudflared tunnel run ozon-finance
cloudflared service install
```

然后开**访问控制**（这一步才是「只允许我邀请的人」）：

```
Cloudflare Zero Trust → Access → Applications → Add an application → Self-hosted
  Application domain: ozon.你的域名
  Policy: Allow → Include → Emails → 填你和同事的邮箱
```

同事访问时输入邮箱 → 收验证码 → 进入系统。**不用装任何客户端、也不用记你的口令**
（免费版 Access 支持 50 个用户）。

## 四、还没做的两件事（要提前说清）

| 项 | 现状 | 影响 |
|---|---|---|
| **登录失败限速/锁定** | **没有** | 任何人只要能到达登录页就可以无限次试口令。路线 B 靠 tailnet 挡、路线 A 靠 Access 挡；**两条路线的网关都是比口令更重要的那道门** |
| **审计日志只到成本库** | 成本变更留痕有；登录/查看没有留痕 | 谁在什么时候看过哪些数据，目前查不到（阶段 E 的「审计中心」） |

## 五、出问题时

```powershell
# 服务本身通不通（本机）
curl http://127.0.0.1:8849/api/health

# 对外模式自检（在正式启动前先跑这个，能省一次「启动了但被拦」）
python ops\public\prepare_public.py --check

# 看一下端口上是谁
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -eq 8849 }
```

> ⚠️ **重启服务的坑**：Windows 上 uvicorn 的子进程可能比启动它的 shell 活得久。
> 重启前先按端口杀干净，否则会出现「新进程绑定失败退出、端口上还是旧代码」——
> 我踩过一次：以为挂载顺序没修好，其实是旧进程还在服务。
> ```powershell
> Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -eq 8849 } |
>   ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
> ```
