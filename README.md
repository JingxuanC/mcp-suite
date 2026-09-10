# mcp-suite — causal-memory.com 基础设施

causal-memory.com 整套 MCP 服务的部署编排。本仓只含**编排与配置模板**；
各 MCP 服务源码在各自独立仓库，按下方清单克隆到本目录即可构建。

## 架构

```
causal-memory.com (nginx, 443)
 ├── /               → Next.js 官网 (:3000, systemd 裸进程)
 ├── /hub/           → quota-platform (:3200, 统一计量/配额) → MCPHub (:3100, 用户/分组/Key)
 ├── /quota/         → quota-platform 管理台 (:3300, 令牌鉴权，用量看板+配额规则)
 ├── /memory/mcp     → causal-memory (:50061, Bearer 多租户, 每租户独立 SQLite)
 ├── /astock/mcp     → astock-data-mcp (:50052, 45 工具, X-License-Key 鉴权)
 ├── /factor-miner/mcp → factor-miner-mcp (:50053, 16 工具, 含 qlib 回测)
 ├── /causal/mcp     → causal-mcp (:50057, 8 工具)
 └── /global/mcp     → global-data-mcp (:50058, 14 工具)

每个服务暴露 /metrics（Prometheus 文本，免鉴权，仅监听 127.0.0.1）
```

## 目录

| 路径 | 内容 |
|---|---|
| `docker-compose.yml` | 全部容器服务编排 |
| `nginx/causal-memory.conf` | 统一入口反代（SSE 透传、/memory 需固定 Host 头防 rmcp DNS 重绑定拦截） |
| `systemd/` | 官网 Next.js 单元 + mcphub/quota 端口防火墙（host 网络下只许 127.0.0.1） |
| `quota-platform/` | 统一配额管控（sidecar proxy：key 识别→分组→配额→计量，纯标准库） |
| `deploy/crontab` + `deploy/cron_tasks.sh` | 数据更新/在线因子/IC 巡检定时任务 |
| `mcphub/mcp_settings.example.json` | MCPHub 后端服务器 + 分组模板 |
| `causal-memory/tokens.example.json` | 租户 token 映射模板（热更新，fail-closed；目录模式：tokens/ 下所有 *.json 合并，官网桥接写 cloud.json） |
| `cm-tenant-shim/` | causal-memory 租户路由 shim（127.0.0.1:51061，解 MCPHub 双 bearer 拼接；见运维速查） |
| `licenses/licenses.example.json` | 数据服务 license key 模板 |
| `.env.example` | 编排层密钥模板 |

## 全新服务器部署

```bash
# 1. 克隆本仓 + 各服务源码（目录名与 compose build context 一致）
git clone <this-repo> /opt/mcp-suite && cd /opt/mcp-suite
git clone <factor-miner-mcp> factor-miner-mcp
git clone <astock-data-mcp>  astock-data-mcp
git clone <global-data-mcp>  global-data-mcp
git clone <causal-mcp>       causal-mcp
git clone <kronos-mcp>       kronos-mcp
git clone <causal-memory>    causal-memory-src   # Rust 仓，用 Dockerfile.server 构建

# 2. 生成真实配置（模板 → 实体，实体被 gitignore）
cp .env.example .env && $EDITOR .env                       # 填 4 个密钥
cp licenses/licenses.example.json /opt/athena-mcp/licenses/licenses.json  # 生成 ak_ key
mkdir -p causal-memory/tokens && cp causal-memory/tokens.example.json causal-memory/tokens/tokens.json
cp mcphub/mcp_settings.example.json /opt/mcp-hub/mcp_settings.json

# 3. 启动
docker compose build && docker compose up -d

# 4. nginx + TLS
cp nginx/causal-memory.conf /etc/nginx/conf.d/ && certbot --nginx -d causal-memory.com

# 5. 防火墙（host 网络的 mcphub/quota 端口只许本机，公网只露 nginx 443）
cp systemd/mcphub-firewall.service /etc/systemd/system/ && systemctl enable --now mcphub-firewall

# 6. 定时任务
crontab deploy/crontab

# 7. 官网（可选）
cp systemd/causal-memory-web.service /etc/systemd/system/ && systemctl enable --now causal-memory-web
```

## 运维速查

**加 MCPHub 用户 Key**（客户端只需这一把，后端 token 全藏在 hub 配置里）：

```bash
docker exec mcphub node bin/cli.js login --url http://localhost:3100/hub --username admin
docker exec mcphub node bin/cli.js keys create --name <用户> --access-type groups --groups data,alpha
```

分组：`data`(A股+全球数据) / `alpha`(因子+Kronos+因果) / `memory`(因果记忆)。
⚠️ `memory` 分组路由到 hub 配置里 baked 的租户库——要独立记忆须走下一条。

**配额与用量**（quota-platform，替代逐个服务改 licenses.json）：

```bash
# 管理台: https://causal-memory.com/quota/ （令牌 = .env 的 QUOTA_ADMIN_TOKEN）
# 规则模型: 组(data=free / alpha+memory=paid) × 档(daily/monthly/heavy_daily) × key 覆盖
docker restart quota-platform        # 改 /opt/mcp-suite/quota-platform/config.json 后重载（管理台保存则即时生效）
curl -s 127.0.0.1:3200/healthz      # 数据面健康检查
```

**上线自检**（quota-platform 部署/重启后，用真实 key 端到端验证配额链路）：

```bash
QUOTA_ADMIN_TOKEN=$(grep QUOTA_ADMIN_TOKEN .env | cut -d= -f2) \
  python3 quota-platform/scripts/verify_quota.py --key <一把真实用户 key>
# 全部 PASS（退出码 0）即可放心切 nginx /hub/ → :3200；FAIL 时按脚本报错逐条排查
```

各服务 `licenses.json` 的 daily 配额保持保守大值即可（LicenseStore 已退化为全局兜底，quota-platform 挂了也不裸奔）。

**causal-memory 租户隔离**（每个 MCPHub key 独立记忆库，2026-09 起）：

- 原理：MCPHub 的 causal-memory 条目配 `headers`(静态 admin token) + `passthroughHeaders: ["Authorization"]`，调用时 hub 会把两个 bearer 拼成 `"Bearer admin, Bearer <调用者key>"`；`cm-tenant-shim/`（127.0.0.1:51061，systemd 单元 `cm-tenant-shim.service`）取**最后一个** bearer 转发给 causal-memory:50061 → 调用者落到自己租户；hub 自己的工具发现无调用者上下文，只有静态 admin 头，正常注册。
- 加租户：`causal-memory/tokens/tokens.json` 加 `"<该用户的 MCPHub key>": "<租户名>"`，保存即热生效（mtime 触发，无需重启任何服务）。未登记的 key 调记忆工具直接 401（fail closed）。
- 客户端零改动：用户仍只持自己那把 MCPHub key。
- ⚠️ 不要给 causal-memory 条目去掉静态头（工具发现会 401 触发 hub 的 OAuth 误探测），也不要指望 mcphub 1.0.34 的 passthrough 单独工作（无静态头时 passthrough 不生效）。
- 旁路：`/memory/mcp` 直连仍走租户 token（不经过 hub/shim）。

**轮换 license key**：改 `/opt/athena-mcp/licenses/licenses.json`（各服务热读，无需重启），同步改 mcphub 配置里的 `X-License-Key`。

**更新某个服务**：`cd <服务目录> && git pull && cd .. && docker compose up -d --build <服务名>`。

## 铁律

- 真实 `.env` / `tokens.json` / `licenses.json` / `mcp_settings.json` / `quota-platform/config.json` **永不提交**，只提交 `*.example` 模板
- 所有后端只监听 `127.0.0.1`，公网只露 nginx 443（host 网络端口由 mcphub-firewall 兜底）
- qlib 数据（qlib_data）与 factor_mining 数据量大，不入库，挂卷注入
