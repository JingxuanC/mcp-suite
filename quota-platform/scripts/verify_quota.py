#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota-platform 上线验证脚本（纯标准库）。

用法（部署机上）：
    QUOTA_ADMIN_TOKEN=<令牌> python3 quota-platform/scripts/verify_quota.py \
        --key <一把真实用户 key> [--base http://127.0.0.1:3200] [--admin-url http://127.0.0.1:3300]

做什么：
    1. 健康检查（数据面 / 管理面）
    2. 通过管理面临时给测试 key 设小配额（alpha 组 daily=10 / heavy_daily=1）
    3. data 组打 3 次普通调用 —— 期望全部放行（免费组）
    4. alpha 组打 2 次普通 + 2 次重度调用 —— 期望普通放行、第 2 次重度返回 429
       （验证 key 识别、分组识别、日配额、重度配额、转发前拦截）
    5. SSE 流式连通性抽查
    6. 恢复管理面前的原 key_overrides，不留副作用

注意：验证会消耗测试 key 的少量配额（alpha 组 2 次普通 + 1 次重度）。
"""

import argparse
import http.client
import json
import os
import sys
import time
from urllib.parse import urlparse


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name, ok, detail=''):
        mark = 'PASS' if ok else 'FAIL'
        color = '\033[32m' if ok else '\033[31m'
        print(f'  {color}{mark}\033[0m  {name}' + (f'  —  {detail}' if detail else ''))
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        return ok


def req(base_url, method, path, body=None, headers=None, timeout=30, read_limit=None):
    """返回 (status, headers_dict, body_bytes)。read_limit 用于 SSE 抽查（读一点就断）。"""
    u = urlparse(base_url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    h = dict(headers or {})
    if body is not None:
        h['Content-Length'] = str(len(body))
    conn.request(method, path, body=body, headers=h)
    resp = conn.getresponse()
    if read_limit:
        raw = resp.read(read_limit)
    else:
        raw = resp.read()
    out = (resp.status, dict(resp.getheaders()), raw)
    conn.close()
    return out


def rpc_call(tool, req_id=1):
    return json.dumps({'jsonrpc': '2.0', 'id': req_id, 'method': 'tools/call',
                       'params': {'name': tool, 'arguments': {}}}).encode()


def main():
    ap = argparse.ArgumentParser(description='quota-platform 上线验证')
    ap.add_argument('--base', default='http://127.0.0.1:3200', help='数据面地址')
    ap.add_argument('--admin-url', default='http://127.0.0.1:3300', help='管理面地址')
    ap.add_argument('--key', required=True, help='一把真实用户 bearer key（会被临时设小配额）')
    ap.add_argument('--group-data', default='data', help='免费组名（默认 data）')
    ap.add_argument('--group-alpha', default='alpha', help='收费组名（默认 alpha）')
    ap.add_argument('--admin-token', default=os.environ.get('QUOTA_ADMIN_TOKEN', ''),
                    help='管理令牌（默认取环境变量 QUOTA_ADMIN_TOKEN）')
    args = ap.parse_args()

    if not args.admin_token:
        print('错误：缺少管理令牌（--admin-token 或环境变量 QUOTA_ADMIN_TOKEN）')
        return 2

    c = Checker()
    auth = {'Authorization': f'Bearer {args.key}'}
    admin = {'X-Admin-Token': args.admin_token, 'Content-Type': 'application/json'}

    print('■ 阶段 1：健康检查')
    try:
        s, _, raw = req(args.base, 'GET', '/healthz', timeout=5)
        c.check('数据面健康', s == 200 and json.loads(raw).get('status') == 'ok', f'HTTP {s}')
    except OSError as e:
        c.check('数据面健康', False, str(e))
        return finish(c)
    try:
        s, _, raw = req(args.admin_url, 'GET', '/api/state', headers=admin, timeout=5)
        state = json.loads(raw) if s == 200 else {}
        c.check('管理面鉴权', s == 200, f'HTTP {s}')
    except OSError as e:
        c.check('管理面鉴权', False, str(e))
        return finish(c)

    groups = state.get('config', {}).get('groups', {})
    heavy_tools = state.get('config', {}).get('heavy_tools') or ['backtest_run']
    heavy_tool = heavy_tools[0] if heavy_tools else 'backtest_run'

    # token → key 名（key_overrides 按 key 名索引）
    s, _, raw = req(args.admin_url, 'POST', '/api/lookup_key',
                    body=json.dumps({'token': args.key}).encode(), headers=admin)
    if s != 200:
        c.check('测试 key 在库存中', False, f'HTTP {s}（key 未收录时会落 anonymous/default 档）')
        return finish(c)
    key_name = json.loads(raw)['name']
    c.check('测试 key 在库存中', True, f'key 名 = {key_name}')

    if args.group_data not in groups:
        print(f'警告：配置里没有组 {args.group_data}，data 组用例将按路径直推（归 global/default 档）')
    if args.group_alpha not in groups:
        print(f'警告：配置里没有组 {args.group_alpha}')

    # 记录原 key_overrides，验证后恢复
    saved_overrides = json.dumps({'key_overrides': state.get('config', {}).get('key_overrides', {})})

    print('■ 阶段 2：临时收紧测试 key 配额（alpha 组 daily=10 / heavy_daily=1）')
    test_limits = {'daily': 10, 'monthly': 1000, 'heavy_daily': 1}
    overrides = state.get('config', {}).get('key_overrides', {})
    overrides = dict(overrides)
    entry = dict(overrides.get(key_name, {}))
    tier_by_group = dict(entry.get('tier_by_group', {}))
    tier_by_group[args.group_alpha] = test_limits
    entry['tier_by_group'] = tier_by_group
    overrides[key_name] = entry
    s, _, _ = req(args.admin_url, 'POST', '/api/config',
                  body=json.dumps({'key_overrides': overrides}).encode(), headers=admin)
    if not c.check('写入临时配额', s == 200, f'HTTP {s}'):
        return finish(c)

    print(f'■ 阶段 3：免费组放行（{args.group_data} × 3 次普通调用）')
    data_ok = 0
    for i in range(3):
        try:
            s, _, raw = req(args.base, 'POST', f'/{args.group_data}/messages',
                            body=rpc_call('get_price', i), headers=auth)
            if s == 200:
                data_ok += 1
            else:
                c.check(f'第 {i + 1} 次调用', False, f'HTTP {s}：{raw[:120]!r}')
        except OSError as e:
            c.check(f'第 {i + 1} 次调用', False, str(e))
    c.check('免费组 3/3 放行', data_ok == 3, f'实际放行 {data_ok}/3')

    print(f'■ 阶段 4：收费组配额拦截（{args.group_alpha}：普通×2 应放行，重度×2 应第 2 次 429）')
    alpha_plain_ok = 0
    for i in range(2):
        s, _, raw = req(args.base, 'POST', f'/{args.group_alpha}/messages',
                        body=rpc_call('factor_screen', i), headers=auth)
        if s == 200:
            alpha_plain_ok += 1
        else:
            c.check(f'普通调用第 {i + 1} 次', False, f'HTTP {s}：{raw[:120]!r}')
    c.check('收费组普通调用 2/2 放行（未达临时 daily=10）', alpha_plain_ok == 2)

    heavy_results = []
    for i in range(2):
        s, _, raw = req(args.base, 'POST', f'/{args.group_alpha}/messages',
                        body=rpc_call(heavy_tool, 100 + i), headers=auth)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        heavy_results.append((s, payload))
    c.check('重度调用第 1 次放行', heavy_results[0][0] == 200, f'HTTP {heavy_results[0][0]}')
    blocked = heavy_results[1][0] == 429 and 'error' in heavy_results[1][1]
    c.check('重度调用第 2 次拦截 429（转发前）', blocked,
            (heavy_results[1][1].get('error', {}).get('message', '') if blocked
             else f'HTTP {heavy_results[1][0]}'))

    print('■ 阶段 5：SSE 流式连通性抽查')
    try:
        s, hdrs, raw = req(args.base, 'GET', f'/{args.group_data}/sse', headers=auth,
                           timeout=10, read_limit=64)
        c.check('SSE 通道连通', s == 200, f'HTTP {s}, {hdrs.get("Content-Type", "?")}, 读到 {len(raw)}B')
    except OSError as e:
        c.check('SSE 通道连通', False, str(e))

    print('■ 阶段 6：恢复原始配额规则')
    s, _, _ = req(args.admin_url, 'POST', '/api/config', body=saved_overrides.encode(), headers=admin)
    c.check('恢复 key_overrides', s == 200, f'HTTP {s}')

    return finish(c)


def finish(c):
    print(f'\n结果：{c.passed} 通过 / {c.failed} 失败')
    if c.failed:
        print('处置建议：')
        print('  - 健康检查失败        → docker compose ps quota-platform / docker logs quota-platform')
        print('  - 鉴权失败            → 检查 QUOTA_ADMIN_TOKEN 与管理台令牌是否一致')
        print('  - 免费组被拦          → 检查 config.json groups/tiers（data 应指向 free 档）')
        print('  - 收费组未被拦        → 确认 nginx /hub/ 已改指 :3200（curl -s 127.0.0.1:3100 与 3200 对比）')
        print('  - 429 未出现          → 检查 heavy_tools 名单是否含被测重度工具')
        return 1
    print('✔ quota-platform 验证通过，可以切 nginx /hub/ → :3200 并把 licenses.json 调为兜底值')
    return 0


if __name__ == '__main__':
    sys.exit(main())
