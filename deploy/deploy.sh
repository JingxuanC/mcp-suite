#!/bin/bash
# mcp-suite 标准部署流程（2026-09-14 由 DSH agent 添加）
#
#   bash deploy/deploy.sh                 # 拉取全部服务并重建
#   bash deploy/deploy.sh causal kronos   # 只部署指定服务
#
# 为什么要有这个脚本：手工流程漏掉 prune 会把根分区打满。2026-09-14 两轮
# `docker compose up -d --build` 后磁盘从 75% 涨到 **98%（仅剩 962M）**，
# 因为每次构建都往 build cache 里堆层，而手工流程没人记得 prune。
# 这里在构建前先按水位清理，构建后再清一次。
#
# 顺序不能变：
#   pull → prune → build → **restart mcphub** → 健康检查
# 最后一步尤其关键：mcphub 缓存上游的 tools/list，不重启的话线上客户端
# 看到的仍是旧的 inputSchema（改了 required/描述也不会生效）。

set -uo pipefail

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SUITE_DIR" || exit 1

# 服务名 → 目录名
declare -A SVC_DIR=(
    [factor-miner]=factor-miner-mcp
    [astock-data]=astock-data-mcp
    [global-data]=global-data-mcp
    [causal]=causal-mcp
    [kronos]=kronos-mcp
)
ALL_SVC=(factor-miner astock-data global-data causal kronos)

# 磁盘水位低于此值就先清 build cache（GB）
MIN_FREE_GB=8

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓ %s\033[0m\n' "$1"; }
warn() { printf '  \033[33m! %s\033[0m\n' "$1"; }
die()  { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }

free_gb() { df --output=avail -BG / | tail -1 | tr -dc '0-9'; }

maybe_prune() {
    local why="$1" f
    f=$(free_gb)
    if [ "$f" -ge "$MIN_FREE_GB" ]; then
        ok "磁盘剩余 ${f}G，无需清理"
        return
    fi
    warn "磁盘仅剩 ${f}G（<${MIN_FREE_GB}G），清理（${why}）"
    # 先清**悬空**镜像/容器：安全，完全不碰构建缓存
    docker image prune -f >/dev/null 2>&1
    docker container prune -f >/dev/null 2>&1
    if [ "$(free_gb)" -lt "$MIN_FREE_GB" ]; then
        # 还紧才动构建缓存，且**保留 3G**。
        # 原写法 `builder prune -af` 会把 torch/pyqlib/mlflow 的缓存全部删掉
        # → 下次重建必须重下约 2GB（实测一次重建被迫重下、耗 40 分钟以上）。
        # 保留额度后，改代码类重建（Dockerfile 里 pip 层在 `COPY . .` 之前）
        # 仍然秒级命中缓存。
        warn "仍不足，压缩构建缓存（保留 3G）"
        docker builder prune -f --keep-storage 3GB >/dev/null 2>&1
    fi
    ok "清理后剩余 $(free_gb)G"
}

# 目标服务
if [ "$#" -gt 0 ]; then
    TARGETS=("$@")
else
    TARGETS=("${ALL_SVC[@]}")
fi
for t in "${TARGETS[@]}"; do
    [ -n "${SVC_DIR[$t]:-}" ] || die "未知服务 '$t'（可选：${ALL_SVC[*]}）"
done

step "0/4 部署前磁盘水位"
maybe_prune "构建前"

step "1/4 拉取代码"
# umbrella 仓（含 mcp-common / compose / quota-platform）
if git -C "$SUITE_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    br=$(git -C "$SUITE_DIR" rev-parse --abbrev-ref HEAD)
    git -C "$SUITE_DIR" fetch origin -q 2>/dev/null
    if git -C "$SUITE_DIR" pull --ff-only origin "$br" >/dev/null 2>&1; then
        ok "mcp-suite @ $(git -C "$SUITE_DIR" rev-parse --short HEAD)"
    else
        warn "mcp-suite pull 失败（可能有未提交改动）—— 继续用当前工作副本"
    fi
fi

for t in "${TARGETS[@]}"; do
    d="${SVC_DIR[$t]}"
    [ -d "$d" ] || { warn "$t: 目录 $d 不存在，跳过"; continue; }
    br=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
    git -C "$d" fetch origin -q 2>/dev/null
    if out=$(git -C "$d" pull --ff-only origin "$br" 2>&1); then
        ok "$t @ $(git -C "$d" rev-parse --short HEAD)"
    else
        warn "$t: pull 失败 —— $(echo "$out" | tail -1)"
    fi
done

step "2/4 构建并启动"
docker compose up -d --build "${TARGETS[@]}" || die "docker compose 失败"
ok "已重建: ${TARGETS[*]}"

step "3/4 重启 mcphub（清 tools/list schema 缓存）"
docker restart mcphub >/dev/null 2>&1 && ok "mcphub restarted" || warn "mcphub 重启失败"

step "4/4 健康检查"
sleep 6
bad=0
for t in "${TARGETS[@]}"; do
    cname="mcp-$t"
    st=$(docker ps --filter "name=^${cname}$" --format '{{.Status}}')
    if [ -z "$st" ]; then
        warn "$cname 未运行"; bad=1
    elif echo "$st" | grep -qiE "restarting|unhealthy|Exited"; then
        warn "$cname 状态异常: $st"; bad=1
        docker logs --tail 15 "$cname" 2>&1 | sed 's/^/      /'
    else
        ok "$cname  $st"
    fi
done
docker ps --filter "name=^mcphub$" --format '  mcphub  {{.Status}}' || true

printf '\n'
if [ "$bad" -eq 0 ]; then
    printf '\033[32m✓ 部署完成 —— 磁盘剩余 %sG\033[0m\n' "$(free_gb)"
    printf '  复验：在本地跑 python3 verify_hub.py（线上 MCP 端到端）\n'
else
    printf '\033[31m✗ 有服务状态异常，见上面日志\033[0m\n'
    exit 1
fi
