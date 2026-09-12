# quota-platform — MCP 调用统一配额管控平台

sidecar proxy：挡在 MCPHub 前面，统一做 **key 识别 → 分组 → 配额判定 → 计量**，转发前拦截，SSE 流式透传。零第三方依赖（仅 Python 3.9+ 标准库）。

```
用户/智能体
    │  Authorization: Bearer <key>
    ▼
:3200 数据面（quota-platform）   ← 计量 + 配额 enforcement（本项目）
    │
    ▼
:3100 mcphub                     ← 认证 / 路由 / 审计（保持不动）
    │
    ▼
mcp-suite 各服务 50052-50061     ← LicenseStore 仅作最后一道兜底
```

## 为什么需要它

MCPHub 只鉴权（key→用户→分组可见性）、不计量：key 过了校验就放行，调 1 次和 1 万次一样。各 MCP 服务自己的 `licenses.json` 配额分散、按文件不按用户、无看板。quota-platform 补上的就是 **per-key × per-group × 日/月 的配额与用量看板**，免费组（data）和收费组（alpha：因子挖掘+回测）可以各设各的额度。

## 快速开始

```bash
cp config.example.json config.json   # 改 admin_token / groups / tiers
python server.py --config config.json
# 数据面 :3200  管理面 :3300
```

跑测试：`python -m unittest discover -s tests -v`（27 个用例）

## 管控模型

| 概念 | 说明 |
|---|---|
| **分组（group）** | 由请求路径识别：`/{group}/messages` 等，路径任一段命中 `groups` 配置即归入该组；全局路由归 `global`。生产分组：`data`(免费) / `alpha`(付费: 因子+回测) / `memory` |
| **档位（tier）** | 每组一个档：`daily` / `monthly` / `heavy_daily`（null = 不限） |
| **重度工具** | `heavy_tools` 名单内的 `tools/call` 单独计重度配额，给回测/深度研究这类贵工具单独限次 |
| **key 覆盖** | `key_overrides` 按 key 名对指定组覆盖额度（如 CI 机器人给高额度） |
| **陌生 key / OAuth token** | 自动落 `anonymous` + `default` 档，不会放行无限用 |

计量规则：只统计 JSON-RPC `tools/call`；`tools/list`、SSE 连接、管理 API 不计次。判定在转发前完成，超限直接返回 429 JSON-RPC 错误，不触达上游。

key 库存：只读挂载 mcphub 的 `mcp_settings.json`，按 mtime 热加载；完整 token 永不出 mcphub 侧，平台只按 token 精确匹配识别身份。管理令牌支持 `QUOTA_ADMIN_TOKEN` 环境变量注入（compose 从 .env 注入，优先于配置文件）。

## 管理面（:3300）

生产环境经 nginx `https://causal-memory.com/quota/` 访问（前缀由 nginx 剥离）。输入管理令牌后：
- 总览卡片：今日/本月调用、重度调用、24h 拦截、活跃 key、今日成功率、今日 p50/p95 延迟
- 近 7 天调用量折线图（总调用 / 拦截，Canvas 手绘）
- 今日分组用量、key 用量 TOP、最近拦截列表
- key 列表逐行禁用/启用开关、今日用量/上限迷你进度条（取该 key 最紧张的组）、今日被拦截标记与一键「提额」（进下钻并聚焦对应组的额度输入）；点 key 名进下钻：分组用量（今日/本月）、生效配额档、成功率与延迟、近 50 条调用流水、过期时间设置、配额覆盖编辑器（按组填 daily/monthly/heavy_daily，留空=不限，保存前弹 diff 确认）
- 规则可视化编辑（tiers 表格 / groups 表格带档位下拉 / heavy_tools 标签），保存即生效；key 级覆盖只走 key 下钻，主保存不再触碰 key_overrides
- key 库存列表（脱敏）

### 调用流水与 key 状态

- 数据面每次 `tools/call` 转发完成后落一条流水到 `calls` 表（key/分组/工具名/上游耗时/status: ok·http_error·blocked/原因）；WAL 模式单条 INSERT，记录失败只记日志不影响转发。保留期 `log_retention_days`（默认 30 天），启动时 + 每小时清理
- key 级状态写在 `key_overrides[name]` 里：`disabled: true` → 数据面 403「key 已禁用」；`expires_at: <unix_ts>` 到期 → 403「key 已过期」。两条检查先于配额检查，管理面 `POST /api/key/<name>/status` 热生效
- 管理 API：`GET /api/key/<name>` 返回该 key 分组用量/近 50 条流水/成功率/p50·p95/生效配额档/状态

## 在 mcp-suite 中的部署

本目录已接入根 `docker-compose.yml`（`quota-platform` 服务，host 网络与 mcphub 同机）：

- 数据面 `127.0.0.1:3200`：nginx `/hub/` 改指这里（原 `:3100`），再转发给 mcphub
- 管理面 `127.0.0.1:3300`：nginx `/quota/` 入口
- `/mnt/mcp_settings.json`：ro 挂载 `/opt/mcp-hub/mcp_settings.json`
- `/data`：持久化 `config.json` + `quota.db`（挂 `/opt/mcp-suite/quota-platform`）
- `systemd/mcphub-firewall.service` 已扩展：3200/3300 与 3100 一样只许 127.0.0.1

上线步骤：

```bash
# 1. .env 加 QUOTA_ADMIN_TOKEN，首次启动自动生成 /opt/mcp-suite/quota-platform/config.json
# 2. 检查 config.json 里的 groups/tiers（默认 data 免费 / alpha+memory 收费）
docker compose up -d quota-platform
docker compose ps quota-platform   # 健康检查: curl -s 127.0.0.1:3200/healthz
# 3. nginx 换 conf 后 nginx -t && systemctl reload nginx
# 4. 各服务 licenses.json 的 daily 配额调为保守大值（如 10000），退化为全局兜底
cp nginx/causal-memory.conf /etc/nginx/conf.d/ && nginx -t && systemctl reload nginx
cp systemd/mcphub-firewall.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart mcphub-firewall
```

## 与现有 LicenseStore 的关系

迁移后 `mcp_gateway.py` 的 LicenseStore 退化为**全局兜底**：所有 key 共用一个保守日上限，正常情况永不触发；不再承担按用户/分组的差异化配额。两层独立，quota-platform 挂了服务也不会裸奔。

## 已知边界

- 计量单位是"调用次数"，不是 token / 金额；按量计费可在 `classify_request` 处扩展权重
- 按月配额按自然月滚动
- 配置编辑是 JSON 文本编辑（带校验），后续可表单化
- mcphub 的 activity log（含 keyId/keyName 全量审计）可另行对接，与本平台计数交叉验证
