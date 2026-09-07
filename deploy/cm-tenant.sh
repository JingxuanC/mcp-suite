#!/bin/bash
# causal-memory 租户 token 管理（tokens.json 由 mtime 热更新，改完即生效，无需重启）
# 用法:
#   cm-tenant.sh add <租户名>      生成 token 并写入，打印一次（请当场复制发给用户）
#   cm-tenant.sh list              列出 租户名 + token 前 10 位
#   cm-tenant.sh revoke <租户名>   按租户名吊销其全部 token
set -euo pipefail
FILE="$(dirname "$0")/../causal-memory/tokens/tokens.json"
[ -f "$FILE" ] || { echo "tokens.json 不存在: $FILE（先从 tokens.example.json 复制）" >&2; exit 1; }

cmd="${1:-}"; name="${2:-}"
case "$cmd" in
  add)
    [ -n "$name" ] || { echo "用法: cm-tenant.sh add <租户名>" >&2; exit 1; }
    token="cm_$(openssl rand -hex 24)"
    FILE="$FILE" NAME="$name" TOKEN="$token" python3 - <<'PY'
import json, os
p, name, token = os.environ["FILE"], os.environ["NAME"], os.environ["TOKEN"]
m = json.load(open(p))
if name in m.values():
    raise SystemExit(f"租户 {name} 已存在，先 revoke 再 add")
m[token] = name
tmp = p + ".tmp"
with open(tmp, "w") as f: json.dump(m, f, indent=1)
os.chmod(tmp, 0o600); os.replace(tmp, p)   # 原子替换 + mtime 触发热更新
print(f"tenant={name}\ntoken={token}\n（token 只打印这一次）")
PY
    ;;
  list)
    python3 - "$FILE" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
for tok, tenant in sorted(m.items(), key=lambda kv: kv[1]):
    print(f"{tenant:20} {tok[:10]}...")
PY
    ;;
  revoke)
    [ -n "$name" ] || { echo "用法: cm-tenant.sh revoke <租户名>" >&2; exit 1; }
    FILE="$FILE" NAME="$name" python3 - <<'PY'
import json, os
p, name = os.environ["FILE"], os.environ["NAME"]
m = json.load(open(p))
gone = [t for t, v in m.items() if v == name]
if not gone: raise SystemExit(f"租户 {name} 不存在")
for t in gone: del m[t]
tmp = p + ".tmp"
with open(tmp, "w") as f: json.dump(m, f, indent=1)
os.chmod(tmp, 0o600); os.replace(tmp, p)
print(f"已吊销 {name} 的 {len(gone)} 把 token，即时生效")
PY
    ;;
  *) echo "用法: cm-tenant.sh add|list|revoke <租户名>" >&2; exit 1;;
esac
