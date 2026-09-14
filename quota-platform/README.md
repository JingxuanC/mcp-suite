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
cp config.example.json config.json   # 改 groups / tiers
python server.py --config config.json
# 数据面 :3200  管理面 :3300
```

创建管理台账号（口令只落 scrypt 哈希，交互输入不回显）：

```bash
python3 scripts/set_admin_password.py --username admin   # 建号/改密，立即生效
python3 scripts/set_admin_password.py --list             # 看有哪些账号
python3 scripts/set_admin_password.py --username ops --disable
```

跑测试：`python -m unittest discover -s tests -v`（106 个用例，另见 `scripts/e2e_apply_flow.py`）

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

生产环境经 nginx `https://causal-memory.com/quota/` 访问（前缀由 nginx 剥离）。**用账号 + 口令登录**，登录后：

### API key 自助申请 / 审批

新客户不必再让你去 mcphub 手点建 key 再复制分发。链路：

```
申请人（公网）                    配额平台                          mcphub
 /quota/apply 填表  ──POST──▶  key_requests(pending)
                                      │
 你在管理台「申请审批」 ──▶ 通过 ─────┼──POST /hub/api/auth/keys──▶ 建 key（收窄到勾选分组）
                                      │◀──── 明文 token（只出现一次）
                            key_overrides 写配额档位
                                      │
 申请人凭单号 ──▶ 领取页 ──▶ 显示 token + 接入片段，随即抹除
```

公开入口在主页 **https://causal-memory.com/apply**（由 website 仓渲染，经站内
`/api/key-request/*` 服务端代理转发到下面这些接口）—— 申请页**不再挂在管控台路径下**，
以免公开 URL 暴露内部平台结构。管控台这里只提供 API：

| 端点 | 鉴权 | 说明 |
|---|---|---|
| ~~`GET /apply`~~ | — | 已移除，返回 404 并提示去主页（避免两个页面各自漂移） |
| `GET /api/apply/meta` | 匿名 | 可选分组等元信息 |
| `POST /api/apply` | 匿名 | 提交申请 → 返回申请单号 `REQ-XXXXXXXX`；按 IP 限流 |
| `GET /api/apply/<单号>` | 匿名 | 查状态（**不返回密钥**） |
| `POST /api/apply/<单号>/pickup` | 匿名 | 领取密钥 → 返回后**立即从内存抹除** |
| `GET /api/requests` | 管理 | 申请列表 + 待办数 |
| `POST /api/requests/<id>/approve` | 管理 | 通过：建 key + 写档位（`groups`、`tier`） |
| `POST /api/requests/<id>/reject` | 管理 | 拒绝（`reason` 会展示给申请人） |
| `POST /api/requests/<id>/reissue` | 管理 | 改发：建新 key + 停用旧 key |
| `GET /api/requests/<id>/token` | 管理 | 兜底：查看待领取的密钥，人工转发 |

**两个刻意的设计**

1. **明文 token 不落库**。mcphub 建 key 的响应里 token 只出现一次（它自己只存掩码），
   所以必须当场接住。本平台把它放在**内存**里等领取，领走即删 —— 夜间 `quota.db`
   备份因此永远不含可用密钥。代价：等待领取期间若服务重启，token 丢失，管理台
   点「改发」即可（建新 key + 停用旧 key）。
2. **收窄授权**。新 key 一律 `accessType=groups` + `allowedGroups=勾选的分组`
   （mcphub 的 `sseService` 对这类 key 是 fail-closed）。因此客户端**必须走带组名的
   地址** `/hub/mcp/<组名>`；不带组名的全局路由 `/hub/mcp` 会被 mcphub 拒绝。
   领取页会把完整接入片段（每个组的地址 + 请求头 + Claude Desktop 示例）写清楚。

**前置配置**

```jsonc
{
  "mcphub_api_base": "http://127.0.0.1:3100/hub/api",  // 注意必须带 /api
  "apply_groups": ["data", "alpha", "memory"],          // 允许申请的分组（须与 mcphub groups 同名）
  "apply_default_tier": "paid",
  "apply_rate_hour": 3, "apply_rate_day": 10,           // 同 IP 申请频率上限
  "apply_pickup_hours": 168                             // 审批后未领取则作废
}
```

`mcphub_admin_key` 用**环境变量 `MCPHUB_ADMIN_KEY`** 注入（compose 从 `.env` 读），
不要写进 `config.json`。它需要是一把 mcphub 的 **system + all-access** key
（`auth.js` 的 `validateBearerAuth` 只放行这种 key 访问管理路由）；
建议专门建一把 `quota-platform-admin` 而不是复用 `admin-all`，便于单独吊销。

已知坑：mcphub 管理 API 前缀**必须带 `/api`**。`/hub/auth/keys` 会落到它的 SPA 首页
并返回 **200 + HTML**（看起来像成功）；本平台会识别成 `mcphub 返回的不是 JSON`。

### 鉴权

| 方式 | 用途 | 说明 |
|---|---|---|
| **账号 + 口令**（推荐） | 人 | `POST /api/login` 校验 scrypt 哈希，下发 `qp_session` 会话 cookie |
| `X-Admin-Token` 头 | 脚本 / 应急 | 保留兼容，静态共享秘密，不适合给人用 |

口令与会话的几个要点：

- **口令只存 scrypt 哈希**（`scrypt$n$r$p$salt$hash`，n=16384），不可逆；账号文件 `admin_users.json` 权限 `0600`
- **会话 cookie 是 `HttpOnly; SameSite=Strict`**，JS 读不到（XSS 拿不走），默认 12h 上限 + 30min 空闲过期；`Secure` 在 HTTPS 下自动加
- **cookie Path 跟随 `X-Forwarded-Prefix`**：经 nginx `/quota/` 访问时限制在 `/quota/`，不会被发到 `/hub/` 的 MCP 调用上
- **登录失败限流**：按 IP 与 (IP, 账号) 双维度，默认连续 5 次失败锁 15 分钟；账号不存在时也跑一次 scrypt，避免用响应时间枚举账号
- **改口令会吊销该账号的其他会话**（`POST /api/password`，需原口令）
- **CSRF**：cookie 鉴权的 POST 额外核对 `Origin`/`Referer` 与 `Host`（`SameSite=Strict` 之外的纵深防御）；令牌鉴权不做该校验（不是浏览器自动携带的凭据）
- 页面内 `POST /api/me` 探测登录态：未登录显示登录页，未建账号时直接给出建号命令
- 会话存在内存里：服务重启后需重新登录（换来零持久化状态）

登录后：
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
- 全局日志：`GET /api/calls?key=&group=&status=&tool=&since=&before_id=&limit=` 跨 key 查流水（tool 为模糊匹配，before_id 向前翻页，limit 上限 500，返回 has_more）；管理台「调用日志」区块可视化筛选 + 加载更多

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
