#!/usr/bin/env python3
"""把某个 MCP 服务幂等地注册进 mcphub（servers + groups），并可同步 quota 分组。

为什么需要它：`mcphub/mcp_settings.json` 与 `quota-platform/config.json` 都在
.gitignore 里（含环境相关配置），没有版本化的"真理源"。手改 JSON 容易漏字段、
也容易把已有的 key/分组弄坏。这个脚本只做增量合并，其余字段原样保留。

## 关键：按工具过滤

mcphub 的 group 里 server 可以写成对象并带 `tools` 过滤（`match` 组就是这种写法）：

    {"name": "workbench",
     "tools": ["wb_hypothesis", "wb_gate_evaluate", ...],
     "prompts": "all", "resources": "all"}

对 workbench 来说这不是锦上添花而是**必需**：`wb_admit / wb_reject / wb_retire`
是人类专属操作。虽然服务侧已加 `WB_HUMAN_TOKEN` 强制校验（fail-closed），
但**不把它们暴露到 hub 上**是纵深防御 —— agent 连尝试的机会都没有。

## 用法

    python3 deploy/register_group.py \
        --settings /opt/mcp-hub/mcp_settings.json \
        --quota-config /opt/mcp-suite/quota-platform-data/config.json \
        --server workbench --url http://127.0.0.1:50062/mcp \
        --description "量化工作台：因子准入闸门（唯一准入权·无 LLM）" \
        --group gate --tier paid --heavy wb_gate_evaluate \
        --tools wb_hypothesis,wb_factor,wb_gate_evaluate,wb_registry,wb_report,wb_ui_report

自动化用 `--tools all`（不过滤，仅在确认无人类专属工具时使用）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# 人类专属操作：默认**永不**通过 hub 暴露
HUMAN_TOOLS = {"wb_admit", "wb_reject", "wb_retire"}


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _backup(path: Path) -> Path:
    dst = path.with_suffix(path.suffix + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, dst)
    return dst


def register_server(cfg: dict, name: str, url: str, description: str, visibility: str) -> str:
    entry = cfg.setdefault("mcpServers", {}).get(name)
    if entry is None:
        cfg["mcpServers"][name] = {
            "type": "streamable-http",
            "url": url,
            "description": description,
            "owner": "admin",
            "visibility": visibility,
            "enabled": True,
        }
        return "added"
    entry.update({"type": "streamable-http", "url": url,
                  "description": description, "enabled": True})
    entry.setdefault("owner", "admin")
    entry["visibility"] = visibility
    return "updated"


def register_group(cfg: dict, group: str, server: str, tools, description: str):
    """tools: "all" 或工具名列表。返回 (动作, 实际工具清单)。"""
    groups = cfg.setdefault("groups", [])
    g = next((x for x in groups if x.get("name") == group), None)
    if tools == "all":
        member = {"name": server, "tools": "all", "prompts": "all", "resources": "all"}
        shown = "all"
    else:
        allowed = [t for t in tools if t not in HUMAN_TOOLS]
        dropped = [t for t in tools if t in HUMAN_TOOLS]
        if dropped:
            print("  ⚠️  已从该分组剔除人类专属工具（不通过 hub 暴露）: %s" % dropped)
        member = {"name": server, "tools": allowed, "prompts": "all", "resources": "all"}
        shown = allowed
    if g is None:
        import uuid
        groups.append({"id": str(uuid.uuid4()), "name": group,
                       "description": description, "servers": [member], "owner": "admin"})
        return "added", shown
    # 已存在：确保该 server 在组内且过滤生效（替换旧的同名成员）
    members = [m for m in (g.get("servers") or [])
               if (m.get("name") if isinstance(m, dict) else m) != server]
    members.append(member)
    g["servers"] = members
    g.setdefault("description", description)
    return "updated", shown


def grant_group(cfg: dict, group: str, key_names: list) -> list:
    """把新分组授权给指定 bearer key。

    这一步容易漏但**必需**：mcphub 的 key 有 `accessType`：
      - "all"    → 所有分组通吃
      - "groups" → 只认 `allowedGroups` 里列出的分组
    一个 `accessType=groups` 的 key 去访问未授权的组，mcphub 会返回
    **401 invalid_token**（而不是 403），日志里是
    "Bearer key rejected due to scope restrictions" —— 很容易误判成 key 失效。
    实测：同一个 key 访问 `data` 成功、访问新注册的 `gate` 401。

    返回实际改动的 key 名列表。
    """
    touched = []
    if not key_names:
        return touched
    want = {k.strip() for k in key_names if k.strip()}
    for b in cfg.get("bearerKeys", []):
        name = b.get("name")
        if name not in want:
            continue
        if b.get("accessType") == "all":
            touched.append("%s(已是 all，无需改动)" % name)
            continue
        ag = b.setdefault("allowedGroups", [])
        if group not in ag:
            ag.append(group)
            touched.append(name)
    missing = want - {b.get("name") for b in cfg.get("bearerKeys", [])}
    if missing:
        print("  ⚠️  未找到这些 key，未授权: %s" % sorted(missing))
    return touched


def sync_quota(path: Path, group: str, tier: str, heavy: list):
    """quota-platform：把分组加进 groups（决定计量档位），并把重工具登记。"""
    if not path.exists():
        return "skipped（不存在）"
    cfg = _load(path)
    groups = cfg.setdefault("groups", {})
    action = "unchanged"
    if group not in groups:
        groups[group] = {"tier": tier}
        action = "added"
    else:
        groups[group].setdefault("tier", tier)
    ht = cfg.setdefault("heavy_tools", [])
    for t in heavy:
        if t and t not in ht:
            ht.append(t)
            action = "added" if action == "unchanged" else action
    _backup(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return action


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", required=True, help="mcphub 的 mcp_settings.json")
    ap.add_argument("--quota-config", default="", help="quota-platform 的 config.json（可选）")
    ap.add_argument("--server", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--visibility", default="private",
                    choices=["public", "private"])
    ap.add_argument("--group", required=True)
    ap.add_argument("--group-description", default="")
    ap.add_argument("--tier", default="paid")
    ap.add_argument("--tools", default="all",
                    help='逗号分隔的工具名，或 "all"')
    ap.add_argument("--heavy", default="", help="逗号分隔的重工具名（计入 heavy 配额）")
    ap.add_argument("--grant-key", default="",
                    help="逗号分隔的 bearer key 名，把新分组授权给它们"
                         "（accessType=groups 的 key 未授权访问会得到 401 而非 403）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    settings = Path(a.settings)
    if not settings.exists():
        print("❌ 找不到 %s" % settings)
        return 1
    cfg = _load(settings)
    tools = "all" if a.tools.strip().lower() == "all" else \
        [t.strip() for t in a.tools.split(",") if t.strip()]

    s_act = register_server(cfg, a.server, a.url, a.description or a.server, a.visibility)
    g_act, shown = register_group(cfg, a.group, a.server,
                                  tools, a.group_description or a.group)
    print("================== 注册 summary ==================")
    print("server %-12s → %s" % (a.server, s_act))
    print("group  %-12s → %s" % (a.group, g_act))
    print("  暴露工具: %s" % (shown if shown == "all" else
                              "%d 个 %s" % (len(shown), shown)))
    keys = [k.strip() for k in a.grant_key.split(",") if k.strip()]
    granted = grant_group(cfg, a.group, keys)
    if keys:
        print("  授权 key: %s" % (granted or "（无改动）"))
    if a.dry_run:
        print("(dry-run，未写入)")
        return 0
    print("  备份: %s" % _backup(settings).name)
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("✅ 已写入 %s" % settings)

    if a.quota_config:
        heavy = [t.strip() for t in a.heavy.split(",") if t.strip()]
        q = sync_quota(Path(a.quota_config), a.group, a.tier, heavy)
        print("quota group %s → %s（tier=%s, heavy=%s）" % (a.group, q, a.tier, heavy))
    print("\n别忘了重启 mcphub（清 tools/list 缓存）与 quota-platform。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
