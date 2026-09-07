# mcp-suite — causal-memory.com 基础设施

causal-memory.com 整套 MCP 服务的部署编排。本仓只含**编排与配置模板**；
各 MCP 服务源码在各自独立仓库，按下方清单克隆到本目录即可构建。

## 架构

```
causal-memory.com (nginx, 443)
 ├── /               → Next.js 官网 (:3000, systemd 裸进程)
 ├── /hub/           → MCPHub 聚合网关 (:3100, 统一入口 + 用户/分组/Key 管理)
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
| `systemd/` | 官网 Next.js 单元（causal-memory 本体已容器化） |
| `deploy/crontab` + `deploy/cron_tasks.sh` | 数据更新/在线因子/IC 巡检定时任务 |
| `mcphub/mcp_settings.example.json` | MCPHub 后端服务器 + 分组模板 |
| `causal-memory/tokens.example.json` | 租户 token 映射模板（热更新，fail-closed；目录模式：tokens/ 下所有 *.json 合并，官网桥接写 cloud.json） |
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
cp .env.example .env && $EDITOR .env                       # 填 3 个密钥
cp licenses/licenses.example.json /opt/athena-mcp/licenses/licenses.json  # 生成 ak_ key
mkdir -p causal-memory/tokens && cp causal-memory/tokens.example.json causal-memory/tokens/tokens.json
cp mcphub/mcp_settings.example.json /opt/mcp-hub/mcp_settings.json

# 3. 启动
docker compose build && docker compose up -d

# 4. nginx + TLS
cp nginx/causal-memory.conf /etc/nginx/conf.d/ && certbot --nginx -d causal-memory.com

# 5. 定时任务
crontab deploy/crontab

# 6. 官网（可选）
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

**加 causal-memory 租户**（独立记忆库）：
编辑 `causal-memory/tokens/tokens.json` 加 `"<token>": "<租户名>"`，保存即热生效（mtime 触发，无需重启）。
然后在 MCPHub 建 `owner=该用户, visibility=private` 的 causal-memory server，headers 填 `Authorization: Bearer <token>`。

**轮换 license key**：改 `/opt/athena-mcp/licenses/licenses.json`（各服务热读，无需重启），同步改 mcphub 配置里的 `X-License-Key`。

**更新某个服务**：`cd <服务目录> && git pull && cd .. && docker compose up -d --build <服务名>`。

## 铁律

- 真实 `.env` / `tokens.json` / `licenses.json` / `mcp_settings.json` **永不提交**，只提交 `*.example` 模板
- 所有后端只监听 `127.0.0.1`，公网只露 nginx 443
- qlib 数据（qlib_data）与 factor_mining 数据量大，不入库，挂卷注入
