# mcp-suite 部署仓库

Athena 量化 MCP 套件的部署即代码（infra as code）：`https://causal-memory.com/hub/mcp` 统一入口背后的全部部署配置。

## 架构

```
用户 → https://causal-memory.com/hub/mcp (Bearer token, MCPHub 鉴权/分组/审计)
         └─ nginx 443 → MCPHub (127.0.0.1:3100, BASE_PATH=/hub)
              ├─ astock-data   :50052  A股全维数据（45 工具）
              ├─ factor-miner  :50053  因子挖掘/qlib回测/LGBM（16 工具）
              ├─ causal        :50057  因果分析（8 工具）
              ├─ global-data   :50058  美股/宏观/舆情（14 工具）
              ├─ kronos        :50059  Kronos K线零样本预测（4 工具）
              ├─ causal-memory :9938   Agent 记忆（17 工具，systemd 裸二进制）
              └─ fetch / time / sequential-thinking（stdio 外部 MCP）
```

## 目录

| 路径 | 内容 |
|---|---|
| `docker-compose.yml` | 全部容器服务（6 MCP + Redis + MCPHub） |
| `deploy/crontab` | 定时任务定义（`crontab deploy/crontab` 安装） |
| `deploy/cron_tasks.sh` | 周期任务统一入口：update_data / daily_compute / weekly_ic |
| `systemd/` | causal-memory（MCP HTTP）+ 官网 Next.js 的 service 单元 |
| `nginx/causal-memory.conf` | 443 终结 + 路径路由（/hub/ 透传 MCPHub，/astock/ 等直连单服务） |
| `mcphub/mcp_settings.example.json` | MCPHub 配置模板（分组/可见性/bearerAuth/baseUrl） |

## 密钥管理（不入库）

| 密钥 | 位置 |
|---|---|
| 后端 MCP license key | `/opt/athena-mcp/licenses/licenses.json`（挂卷只读注入） |
| cron 用 license key | `/opt/mcp-suite/.env` 的 `MCP_LICENSE_KEY`（gitignore） |
| MCPHub admin 密码 | `.env` 的 `MCPHUB_ADMIN_PASSWORD`（compose 注入） |
| 用户 bearer key | MCPHub 面板/API 签发，存于 `/opt/mcp-hub/mcp_settings.json` 的 `bearerKeys` |

## 数据挂载审计（容器服务的数据全部落宿主机）

| 数据 | 宿主机路径 | 容器路径 | 用途 |
|---|---|---|---|
| qlib cn_data | `/opt/athena-mcp/qlib_data` | `/app/.qlib/qlib_data` | 日线原始+前复权（update_data 增量更新） |
| 因子 h5 数据集 | `/opt/athena-mcp/data/factor_mining` | `/app/data/factor_mining` | daily_pv_all.h5 / debug.h5 |
| LGBM 模型 | `/opt/athena-mcp/models` | `/app/models` | lgbm_rolling.pkl |
| license 用量计数 | `/opt/mcp-suite/usage/<svc>` | `/app/usage` | 每服务独立 |
| MCPHub 配置/状态 | `/opt/mcp-hub/` | `/app/mcp_settings.json`、`/app/data` | 含 bearerKeys，勿覆盖 |
| causal-memory DB | `/opt/causal-memory/data` | —（systemd） | causal.db |
| kronos 模型权重 | —（镜像内预下载） | `/models` | 换 KRONOS_MODEL 才需重下 |
| Redis | —（纯缓存 TTL 48h） | — | dfactor:{symbol} |

## 定时任务

| 时间 | 任务 | 说明 |
|---|---|---|
| 交易日 15:40 / 18:10 | `update_data` | qlib cn_data 增量更新 + h5 重建；覆盖率不足自动保留旧数据 |
| 交易日 20:00 | `factor_daily_compute` | 在线因子（reversal20）写 Redis `dfactor:*` |
| 周日 10:00 | `factor_recent_ic` | reversal20 近 60 日截面 IC 衰减巡检，结果在 `cron_tasks.log` |

安装/更新：`crontab deploy/crontab`（会先备份当前 crontab 到 `/opt/mcp-suite/crontab.bak.<ts>`，见下）。

## 运维备忘（踩过的坑）

1. **MCPHub `enableKeepAlive` 别开**：自研后端不实现 `ping`，keepalive 会触发指数级重连风暴
2. **MCPHub `groups` 必须是数组**（文档示例是对象，对象会让分组路由 `groups.find is not a function` 挂死）
3. **`bearerKeys` 存在 `mcp_settings.json` 里**：改配置必须原地编辑（先备份），直接覆盖会抹掉所有用户 key
4. **后端 URL 是 127.0.0.1 时 server 必须有 `owner: "admin"`**，否则 SSRF 保护拦截
5. **子路径部署**：MCPHub 设 `BASE_PATH=/hub` + nginx `proxy_pass` 不带尾斜杠（完整透传）
6. **factor-miner 的 HAS_LGB 是 import 时判定**：装/修 lightgbm 后必须重启容器
7. **update_data 的 h5 重建失败只记日志**：看 `manifest.json` 的 `h5_sha256` 是否非空来确认数据健康

## 新机器部署

```bash
# 1. 克隆各 MCP 服务仓库到 /opt/mcp-suite/<repo>（astock-data-mcp / factor-miner-mcp /
#    causal-mcp / global-data-mcp / kronos-mcp），本仓库也放 /opt/mcp-suite
# 2. 配置密钥
mkdir -p /opt/athena-mcp/{licenses,qlib_data,data/factor_mining,models}
cp licenses.json /opt/athena-mcp/licenses/
echo 'MCP_LICENSE_KEY=ak_xxx' > /opt/mcp-suite/.env
echo 'MCPHUB_ADMIN_PASSWORD=xxx' >> /opt/mcp-suite/.env
# 3. 启动
cd /opt/mcp-suite && docker compose build && docker compose up -d
# 4. systemd + nginx + cron
cp systemd/*.service /etc/systemd/system/ && systemctl daemon-reload
cp nginx/causal-memory.conf /etc/nginx/conf.d/ && nginx -t && systemctl reload nginx
crontab -l > /opt/mcp-suite/crontab.bak.$(date +%s) 2>/dev/null; crontab deploy/crontab
# 5. 初始化数据集
docker exec mcp-factor-miner python3 -m factor_miner.gen_data --debug
docker exec mcp-factor-miner python3 -m factor_miner.gen_data --full
# 6. MCPHub 用户 key：面板 https://<domain>/hub/ 或 POST /api/auth/keys
```
