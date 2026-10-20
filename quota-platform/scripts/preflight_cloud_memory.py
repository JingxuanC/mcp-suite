#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动前检查：causal-memory 多租户 token 存储 + 网站 cloud.json 桥接。

背景（审计 P0-2）：causal-memory 在 `CAUSAL_MEMORY_TOKENS_FILE` 目录缺失、
不可读、没有任何有效条目时，启动会进入 `auth=open` 并服务共享库。Rust 侧
本次不改（无 cargo），所以部署脚本必须在启动前检查，并且用坏 token 探针
确认 `/memory/mcp` 返回 401 而不是 200。

用法：
  python3 scripts/preflight_cloud_memory.py \
      --tokens-dir /opt/mcp-suite/causal-memory/tokens \
      --bridge-file /opt/mcp-suite/causal-memory/tokens/cloud.json \
      --probe-url https://causal-memory.com/memory/mcp

退出码：0 通过；1 检查失败；2 参数/配置错误。
仅用 Python 标准库；不打印任何 token/tenant 明文。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

OPEN_ENV = 'CAUSAL_MEMORY_ALLOW_OPEN'
BAD_TOKEN = 'cm_' + '0' * 48  # deliberately invalid, never a real credential


def truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def load_token_map(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('not a JSON object')
    return data


def check_tokens(path, allow_open=False):
    result = {'ok': True, 'errors': [], 'warnings': [], 'entries': 0, 'files': []}
    if truthy(os.environ.get(OPEN_ENV)) and not allow_open:
        result['errors'].append('%s=1 is set; /mcp would serve the shared open store' % OPEN_ENV)
    if not path:
        result['errors'].append('tokens path is not set (CAUSAL_MEMORY_TOKENS_FILE)')
        result['ok'] = False
        return result
    if os.path.isfile(path):
        files = [path]
    elif os.path.isdir(path):
        try:
            files = sorted(os.path.join(path, n) for n in os.listdir(path)
                           if n.endswith('.json'))
        except OSError as exc:
            result['errors'].append('tokens directory unreadable: %s' % exc)
            result['ok'] = False
            return result
    else:
        result['errors'].append('tokens path does not exist: %s' % path)
        result['ok'] = False
        return result

    if not files:
        result['errors'].append('no *.json token files under %s' % path)
        result['ok'] = False
        return result

    seen = {}
    for file_path in files:
        result['files'].append(file_path)
        try:
            if os.path.getsize(file_path) > 10 * 1024 * 1024:
                raise ValueError('file too large')
            data = load_token_map(file_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result['errors'].append('cannot parse %s: %s' % (os.path.basename(file_path), exc))
            continue
        if not data:
            result['warnings'].append('empty token map: %s' % os.path.basename(file_path))
        for key, tenant in data.items():
            if not isinstance(key, str) or not key:
                result['errors'].append('empty token key in %s' % os.path.basename(file_path))
                continue
            if not isinstance(tenant, str) or not tenant:
                # Do not echo key material; the key may be a real bearer token.
                result['errors'].append('non-string tenant in %s'
                                        % os.path.basename(file_path))
                continue
            if key in seen:
                result['warnings'].append('duplicate token key across %s and %s'
                                          % (seen[key], os.path.basename(file_path)))
            seen[key] = os.path.basename(file_path)
            result['entries'] += 1

    if result['entries'] == 0:
        result['errors'].append('no usable token entries (causal-memory would start auth=open)')
    result['ok'] = not result['errors']
    return result


def check_bridge(path):
    result = {'ok': True, 'errors': [], 'warnings': [], 'entries': 0}
    if not path:
        result['warnings'].append('bridge file not provided; skipped cloud.json check')
        return result
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        result['errors'].append('bridge parent directory does not exist: %s' % parent)
    elif not os.access(parent, os.W_OK):
        result['errors'].append('bridge parent directory is not writable: %s' % parent)
    if os.path.exists(path):
        try:
            data = load_token_map(path)
            result['entries'] = len(data)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result['errors'].append('cloud.json is corrupt: %s' % exc)
    result['ok'] = not result['errors']
    return result


def probe_memory(url):
    """POST initialize with a bad bearer; expect 401."""
    body = json.dumps({
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                   'clientInfo': {'name': 'quota-preflight', 'version': '0'}},
    }).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + BAD_TOKEN,
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.getcode()
            resp.read(256)
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # DNS/TLS/timeout: unavailable is not open, but report it
        return {'ok': False, 'errors': ['probe failed: %s' % exc], 'status': None}
    if code == 401:
        return {'ok': True, 'errors': [], 'status': code}
    return {
        'ok': False,
        'errors': ['bad token probe returned HTTP %s, expected 401 '
                   '(200/2xx means auth=open)' % code],
        'status': code,
    }


def main():
    ap = argparse.ArgumentParser(description='cloud-memory fail-closed preflight')
    ap.add_argument('--tokens-dir', default=os.environ.get('CAUSAL_MEMORY_TOKENS_FILE', ''),
                    help='causal-memory tokens directory (or a single json file)')
    ap.add_argument('--bridge-file', default=os.environ.get('CM_TOKENS_BRIDGE', ''),
                    help='website cloud.json path (optional)')
    ap.add_argument('--probe-url', default=os.environ.get('MEMORY_PREFLIGHT_URL', ''),
                    help='e.g. https://causal-memory.com/memory/mcp')
    ap.add_argument('--allow-open', action='store_true',
                    help='permit CAUSAL_MEMORY_ALLOW_OPEN=1 (not recommended)')
    ap.add_argument('--skip-probe', action='store_true', help='skip the live 401 probe')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    args = ap.parse_args()

    tokens = check_tokens(args.tokens_dir, allow_open=args.allow_open)
    bridge = check_bridge(args.bridge_file)
    probe = {'ok': True, 'errors': [], 'status': None, 'skipped': True}
    if args.probe_url and not args.skip_probe:
        probe = probe_memory(args.probe_url)
        probe['skipped'] = False

    ok = bool(tokens['ok'] and bridge['ok'] and probe['ok'])
    report = {
        'ok': ok,
        'tokens': tokens,
        'bridge': bridge,
        'probe': probe,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('[preflight] tokens: %s (%d entries, %d files)'
              % ('OK' if tokens['ok'] else 'FAIL', tokens['entries'], len(tokens['files'])))
        print('[preflight] bridge: %s (%d entries)'
              % ('OK' if bridge['ok'] else 'FAIL', bridge['entries']))
        if probe.get('skipped'):
            print('[preflight] probe: SKIPPED')
        else:
            print('[preflight] probe: %s (HTTP %s)'
                  % ('OK' if probe['ok'] else 'FAIL', probe.get('status')))
        for section in (tokens, bridge, probe):
            for msg in section['errors']:
                print('  ERROR: ' + msg, file=sys.stderr)
            for msg in section.get('warnings', []):
                print('  WARN:  ' + msg, file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
