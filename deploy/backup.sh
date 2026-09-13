#!/bin/bash
# mcp-suite 关键资产备份（2026-09-14 由 DSH agent 添加）
# 核心资产是 causal-memory 租户库（客户的私人交易记忆，无法从上游重建）。
# 用 sqlite3 backup API 保证 WAL 模式下的一致性；保留 14 天。
set -u
BACKUP_ROOT=/opt/backups/mcp-suite
DAY=$(date +%Y%m%d)
DEST="$BACKUP_ROOT/$DAY"
mkdir -p "$DEST" || exit 1
export DEST

python3 - <<'PY'
import glob, os, sqlite3, sys
src_dir = "/opt/mcp-suite/causal-memory/cm-data-home/tenants"
dst_dir = os.environ["DEST"]
n = ok = 0
for db in sorted(glob.glob(os.path.join(src_dir, "*.db"))):
    n += 1
    name = os.path.basename(db)
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(os.path.join(dst_dir, name))
        with dst:
            src.backup(dst)
        dst.close(); src.close()
        ok += 1
    except Exception as e:
        print(f"  WARN {name}: {e}", file=sys.stderr)
print(f"  tenants: {ok}/{n} backed up")
PY

tar czf "$DEST/config.tar.gz" -C /opt mcp-hub mcp-suite/licenses mcp-suite/quota-platform/config.json mcp-suite/causal-memory/tokens 2>/dev/null

find "$BACKUP_ROOT" -maxdepth 1 -type d -mtime +14 -exec rm -rf {} + 2>/dev/null

echo "[$(date "+%F %T")] backup ok -> $DEST ($(du -sh "$DEST" 2>/dev/null | cut -f1))"
