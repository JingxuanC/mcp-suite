#!/bin/bash
# P0 收官：数据复核 → 因子入 Redis 验证 → 落到真实 cron 路径
# 用法: p0_finish.sh check | verify | deploy
set -u
cd /opt/mcp-suite
FIX=/opt/mcp-suite/factor-miner-mcp/factor_miner/update_data.py
CT=mcp-factor-miner

h5_cross() { # 打印 h5 最新截面的标的数
  docker exec -u appuser -e HOME=/app $CT python3 -c '
import pandas as pd
df = pd.read_hdf("/app/data/factor_mining/daily_pv_all.h5", key="data")
s = df.groupby(level="datetime").size()
print("  最新交易日 %s 截面 %d 只（前一日 %s %d 只）" % (
    str(s.index[-1])[:10], s.iloc[-1], str(s.index[-2])[:10], s.iloc[-2]))
print("  近 5 日:", ", ".join("%s:%d" % (str(d)[:10], n) for d, n in s.tail(5).items()))'
}

keys() { docker exec redis redis-cli --scan --pattern 'dfactor:*' 2>/dev/null | wc -l; }

case "${1:-}" in
  check)
    echo "== 1) 数据复核 =="
    h5_cross
    echo "== 2) Redis 现状 =="
    echo "  dfactor 键数: $(keys)"
    ;;
  verify)
    echo "== 3) daily_compute（以容器将采用的 REDIS_URL 口径跑）=="
    docker exec -u appuser -e HOME=/app -e REDIS_URL=redis://redis:6379/0 $CT \
      python3 -c '
import sys, json; sys.path.insert(0,"/app")
import factor_worker as fw
code = "import pandas as pd\n\ndf = pd.read_hdf(\"daily_pv.h5\")\nclose = df[\"$close\"]\n\nfactor = -close.groupby(level=\"instrument\").pct_change(20)\nfactor.name = \"value\"\nfactor.to_frame().sort_index().to_hdf(\"result.h5\", key=\"factor\", mode=\"w\")\n"
print("  返回:", fw.factor_daily_compute([{"name": "reversal20", "code": code}]))' 2>&1 | tail -3
    echo "== 4) Redis 复核 =="
    echo "  dfactor 键数: $(keys)"
    ;;
  deploy)
    echo "== 5) 重建容器以带上新 REDIS_URL（compose 已改）=="
    docker compose up -d factor-miner 2>&1 | tail -3
    sleep 6
    echo "  注入修复后的源码（镜像重建前的临时手段，重建后即固化）:"
    docker cp "$FIX" $CT:/app/factor_miner/update_data.py
    docker exec $CT md5sum /app/factor_miner/update_data.py
    docker restart $CT >/dev/null && echo "  已重启，等待服务就绪..." && sleep 12
    echo "== 6) 走真实 cron 路径（服务进程内调用）=="
    ./deploy/cron_tasks.sh daily_compute 2>&1 | tail -4
    echo "== 7) Redis 复核 =="
    echo "  dfactor 键数: $(keys)"
    echo "== 8) 服务健康 =="
    docker ps --filter name=^$CT$ --format "  {{.Status}}"
    ;;
  *) echo "usage: $0 check|verify|deploy" >&2; exit 1;;
esac
