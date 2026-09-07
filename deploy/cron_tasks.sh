#!/bin/bash
# mcp-suite 周期任务统一入口（由 crontab 调用，见 deploy/crontab）
# 用法: cron_tasks.sh update_data | daily_compute | weekly_ic
# license key 从 /opt/mcp-suite/.env 读取（MCP_LICENSE_KEY=ak_xxx），不入库
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
: "${MCP_LICENSE_KEY:?需要在 /opt/mcp-suite/.env 里配置 MCP_LICENSE_KEY}"

MCP="http://127.0.0.1:50053/mcp"
LOG="/opt/mcp-suite/cron_tasks.log"
export MCP_LICENSE_KEY MCP

# 当前在线因子：reversal20（20日反转，近60日全市场 IC +0.053）
read -r -d '' FACTOR_CODE <<'PYEOF'
import pandas as pd

df = pd.read_hdf('daily_pv.h5')
close = df['$close']

factor = -close.groupby(level='instrument').pct_change(20)
factor.name = 'value'
factor.to_frame().sort_index().to_hdf('result.h5', key='factor', mode='w')
PYEOF
export FACTOR_CODE

call_tool() { # $1=工具名 $2=arguments JSON
  python3 - "$1" "$2" <<'PY'
import json, os, sys, urllib.request
name, args = sys.argv[1], json.loads(sys.argv[2])
body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}}
req = urllib.request.Request(os.environ["MCP"], data=json.dumps(body).encode(), method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Accept", "application/json, text/event-stream")
req.add_header("X-License-Key", os.environ["MCP_LICENSE_KEY"])
raw = urllib.request.urlopen(req, timeout=60).read().decode()
for line in raw.splitlines():
    if line.startswith("data: "):
        print(line[6:])
        break
PY
}

case "${1:-}" in
  update_data)
    # qlib cn_data 增量更新 + 重建 daily_pv_all.h5（交易日 15:40/18:10 双跑）
    echo "$(date -Is) update_data submit:" >> "$LOG"
    call_tool update_data '{}' >> "$LOG" 2>&1
    ;;
  daily_compute)
    # 每日收盘后：算在线因子写 Redis dfactor:{symbol}（依赖 update_data 已完成，20:00 跑）
    ARGS=$(python3 -c "import json,os; print(json.dumps({'factors':[{'name':'reversal20','code':os.environ['FACTOR_CODE']}]}))")
    echo "$(date -Is) daily_compute submit:" >> "$LOG"
    call_tool factor_daily_compute "$ARGS" >> "$LOG" 2>&1
    ;;
  weekly_ic)
    # 每周衰减巡检：reversal20 近 60 交易日截面 IC（周日 10:00 跑）
    ARGS=$(python3 -c "import json,os; print(json.dumps({'name':'reversal20','code':os.environ['FACTOR_CODE'],'lookback_days':60}))")
    echo "$(date -Is) weekly_ic:" >> "$LOG"
    call_tool factor_recent_ic "$ARGS" >> "$LOG" 2>&1
    ;;
  *)
    echo "usage: $0 update_data|daily_compute|weekly_ic" >&2; exit 1;;
esac
