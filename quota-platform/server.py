#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota-platform — MCP 调用统一配额管控平台（sidecar proxy，零第三方依赖）

架构：
    用户/智能体 ──Bearer key──> :3200 数据面（识别 key → 分组 → 配额判定 → 扣减 → 透传）
                                  │
                                  ▼
                              mcphub :3100（认证 / 路由 / 审计，保持不动）
                                  │
                                  ▼
                              mcp-suite 各服务（LicenseStore 仅作兜底）

管理面 :3300 提供中文 Web UI 与 JSON API，用于用量看板与配额规则编辑。

仅使用 Python 标准库：http.server / http.client / sqlite3 / json。
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------------------------------------------------------------- 常量

HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  period     TEXT NOT NULL,
  key_name   TEXT NOT NULL,
  group_name TEXT NOT NULL,
  calls      INTEGER NOT NULL DEFAULT 0,
  heavy      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (period, key_name, group_name)
);
CREATE TABLE IF NOT EXISTS blocks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  key_name   TEXT NOT NULL,
  group_name TEXT NOT NULL,
  reason     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_ts ON blocks(ts);
"""

DEFAULT_CONFIG = {
    'upstream': 'http://127.0.0.1:3100',       # mcphub 地址
    'data_plane': {'host': '0.0.0.0', 'port': 3200},
    'admin_plane': {'host': '0.0.0.0', 'port': 3300},
    'admin_token': 'change-me',                # 管理面访问令牌
    'mcp_settings_path': '/mnt/mcp_settings.json',  # 只读挂载 mcphub 的配置文件
    'groups': {                                # 路由路径段 → 套餐档
        'data': {'tier': 'free'},
        'alpha': {'tier': 'paid'},
    },
    'tiers': {
        'free':    {'daily': 1000000, 'monthly': 30000000, 'heavy_daily': None},
        'paid':    {'daily': 2000,    'monthly': 40000,    'heavy_daily': 200},
        'default': {'daily': 500,     'monthly': 5000,     'heavy_daily': 50},
    },
    'key_overrides': {},                       # 按 key 名覆盖：{"bot-a": {"tier_by_group": {"alpha": {...}}}}
    # 重度工具名单（与 mcp-suite 各服务 ASYNC_TOOLS 对齐）
    'heavy_tools': ['factor_execute', 'factor_backtest', 'factor_oos_check',
                    'factor_daily_compute', 'ml_train_rolling', 'update_data',
                    'forecast_batch'],
}


# ---------------------------------------------------------------- 配置

class Config:
    """config.json 热加载。管理面 PUT 保存后调用 reload()。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0
        self.data = {}
        self.reload(force=True)

    def reload(self, force=False):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = 0
        if not force and mtime == self._mtime:
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                user = json.load(f)
        except (OSError, json.JSONDecodeError):
            user = {}
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in user.items()})
        env_token = os.environ.get('QUOTA_ADMIN_TOKEN')
        if env_token:
            merged['admin_token'] = env_token  # 环境变量优先（docker-compose 注入 .env）
        with self._lock:
            self.data = merged
            self._mtime = mtime

    def current(self):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = 0
        if mtime != self._mtime:
            self.reload()
        with self._lock:
            return dict(self.data)

    def save_quota_rules(self, rules):
        """管理面保存配额相关字段，其余字段（upstream 等）保持原样。"""
        with open(self.path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        for k in ('groups', 'tiers', 'key_overrides', 'heavy_tools'):
            if k in rules:
                raw[k] = rules[k]
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self.reload(force=True)


# ---------------------------------------------------------------- key 库存

class KeyInventory:
    """只读挂载 mcphub 的 mcp_settings.json，热加载 bearerKeys。

    陌生 key（未入库）由数据面按 anonymous 主体 + default 档处理；
    OAuth 短期 token 不入库，同样落 default 档。
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = -1
        self._keys = {}

    def reload(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            mtime = os.stat(self.path).st_mtime
        except (OSError, json.JSONDecodeError):
            return
        keys = {}
        for k in data.get('bearerKeys', []):
            if not k.get('enabled', True):
                continue
            token = k.get('token')
            if not token:
                continue
            keys[token] = {
                'name': k.get('name') or token[:8],
                'owner': k.get('owner') or '',
                'kind': k.get('kind', 'system'),
                'accessType': k.get('accessType', 'all'),
                'allowedGroups': k.get('allowedGroups') or [],
            }
        with self._lock:
            self._keys = keys
            self._mtime = mtime

    def lookup(self, token):
        if not token:
            return None
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = -1
        with self._lock:
            stale = mtime != self._mtime
        if stale:
            self.reload()
        with self._lock:
            info = self._keys.get(token)
            return dict(info) if info else None

    def all_masked(self):
        with self._lock:
            items = [dict(v) for v in self._keys.values()]
        for it in items:
            it['token_tail'] = None  # token 本身从不出内存
        items.sort(key=lambda x: x['name'])
        return items


# ---------------------------------------------------------------- 用量存储

class QuotaStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def bump(self, key_name, group, heavy):
        day = time.strftime('%Y-%m-%d')
        month = day[:7]
        inc_calls = 0 if heavy else 1
        inc_heavy = 1 if heavy else 0
        with self.lock:
            c = self._conn()
            for period in (day, month):
                c.execute(
                    'INSERT INTO usage(period,key_name,group_name,calls,heavy) VALUES(?,?,?,?,?) '
                    'ON CONFLICT(period,key_name,group_name) DO UPDATE SET calls=calls+excluded.calls, heavy=heavy+excluded.heavy',
                    (period, key_name, group, inc_calls, inc_heavy),
                )
            c.commit()
            c.close()

    def get_usage(self, key_name, group):
        day = time.strftime('%Y-%m-%d')
        month = day[:7]
        with self.lock:
            c = self._conn()
            rows = c.execute(
                'SELECT period, calls, heavy FROM usage WHERE key_name=? AND group_name=? AND period IN (?,?)',
                (key_name, group, day, month),
            ).fetchall()
            c.close()
        out = {'day_calls': 0, 'day_heavy': 0, 'month_calls': 0, 'month_heavy': 0}
        for r in rows:
            if r['period'] == day:
                out['day_calls'] = r['calls']
                out['day_heavy'] = r['heavy']
            else:
                out['month_calls'] = r['calls']
                out['month_heavy'] = r['heavy']
        return out

    def add_block(self, key_name, group, reason):
        with self.lock:
            c = self._conn()
            c.execute(
                'INSERT INTO blocks(ts,key_name,group_name,reason) VALUES(?,?,?,?)',
                (int(time.time()), key_name, group, reason),
            )
            c.commit()
            c.close()

    def state(self):
        day = time.strftime('%Y-%m-%d')
        month = day[:7]
        with self.lock:
            c = self._conn()
            today = c.execute(
                "SELECT COALESCE(SUM(calls),0) calls, COALESCE(SUM(heavy),0) heavy, COUNT(DISTINCT key_name) keys "
                "FROM usage WHERE period=?", (day,)).fetchone()
            month_row = c.execute(
                'SELECT COALESCE(SUM(calls),0) calls FROM usage WHERE period=?', (month,)).fetchone()
            blocks_today = c.execute(
                'SELECT COUNT(*) n FROM blocks WHERE ts >= ?',
                (int(time.time()) - 86400,)).fetchone()['n']
            group_usage = c.execute(
                'SELECT group_name, SUM(calls) calls, SUM(heavy) heavy FROM usage WHERE period=? '
                'GROUP BY group_name ORDER BY calls DESC', (day,)).fetchall()
            key_usage = c.execute(
                'SELECT key_name, group_name, calls, heavy FROM usage WHERE period=? '
                'ORDER BY calls DESC LIMIT 100', (day,)).fetchall()
            recent_blocks = c.execute(
                'SELECT ts, key_name, group_name, reason FROM blocks ORDER BY id DESC LIMIT 50').fetchall()
            c.close()
        return {
            'today_calls': today['calls'],
            'today_heavy': today['heavy'],
            'today_keys': today['keys'],
            'month_calls': month_row['calls'],
            'blocks_24h': blocks_today,
            'group_usage': [dict(r) for r in group_usage],
            'key_usage': [dict(r) for r in key_usage],
            'recent_blocks': [dict(r) for r in recent_blocks],
        }


# ---------------------------------------------------------------- 判定逻辑

def extract_group(raw_path, cfg):
    """路径任一段命中 groups 配置即为该组，否则 global。

    mcphub 分组路由形如 /{group}/sse、/{group}/messages；
    全局路由（/sse、/messages、/api/...）归 global。
    """
    known = cfg.get('groups', {})
    if not known:
        return 'global'
    for seg in re.findall(r'/([^/?]+)', raw_path.split('?')[0]):
        if seg in known:
            return seg
    return 'global'


def classify_request(body_bytes, cfg):
    """返回 (是否计次, 是否重度)。只统计 JSON-RPC tools/call。"""
    if not body_bytes:
        return False, False
    try:
        req = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, False
    if not isinstance(req, dict) or req.get('method') != 'tools/call':
        return False, False
    name = (req.get('params') or {}).get('name', '')
    heavy = name in set(cfg.get('heavy_tools') or [])
    return True, heavy


def resolve_limits(cfg, key_name, group):
    """优先级：key 覆盖（按组） > 组套餐档 > default 档。"""
    ov = (cfg.get('key_overrides') or {}).get(key_name) or {}
    per_group = ov.get('tier_by_group') or {}
    if group in per_group:
        return per_group[group]
    tier = (cfg.get('groups') or {}).get(group, {}).get('tier', 'default')
    return (cfg.get('tiers') or {}).get(tier) or (cfg.get('tiers') or {}).get('default') or {}


def check_quota(limits, usage, heavy):
    """超限时返回原因文案，否则 None。limits 中 null/缺省 = 不限。

    heavy_daily 只约束重度调用；普通调用只受 daily/monthly 约束。
    """
    if limits.get('daily') is not None and usage['day_calls'] >= limits['daily']:
        return 'daily'
    if heavy and limits.get('heavy_daily') is not None and usage['day_heavy'] >= limits['heavy_daily']:
        return 'heavy_daily'
    if limits.get('monthly') is not None and usage['month_calls'] >= limits['monthly']:
        return 'monthly'
    return None


REASON_TEXT = {
    'daily': '今日调用配额已用完，明日 00:00 重置',
    'heavy_daily': '今日重度工具配额已用完，明日 00:00 重置',
    'monthly': '本月调用配额已用完，下月 1 日重置',
}


# ---------------------------------------------------------------- 数据面

class DataPlaneHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'quota-platform/0.1'

    # -- 工具方法 --------------------------------------------------

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _bearer_token(self):
        auth = self.headers.get('Authorization', '')
        m = re.match(r'Bearer\s+(.+)$', auth.strip(), re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return self.headers.get('X-MCP-Key')  # 兼容自定义头

    def _read_body(self):
        if self.command in ('GET', 'HEAD'):
            return b''
        length = self.headers.get('Content-Length')
        if length is None:
            return b''
        try:
            n = int(length)
        except ValueError:
            return b''
        return self.rfile.read(n) if n > 0 else b''

    # -- 入口 ------------------------------------------------------

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def _handle(self):
        if self.path.split('?')[0] in ('/healthz', '/health'):
            return self._json(200, {'status': 'ok', 'service': 'quota-platform'})

        cfg = self.server.cfg.current()
        body = self._read_body()
        token = self._bearer_token()

        info = self.server.inventory.lookup(token)
        key_name = info['name'] if info else 'anonymous'
        group = extract_group(self.path, cfg)

        countable, heavy = classify_request(body, cfg)
        if countable:
            limits = resolve_limits(cfg, key_name, group)
            usage = self.server.store.get_usage(key_name, group)
            reason = check_quota(limits, usage, heavy)
            if reason:
                self.server.store.add_block(key_name, group, reason)
                return self._json(429, {
                    'jsonrpc': '2.0',
                    'error': {
                        'code': -32029,
                        'message': REASON_TEXT[reason],
                        'data': {'key': key_name, 'group': group, 'quota': reason,
                                 'usage': usage, 'limits': limits},
                    },
                })
            self.server.store.bump(key_name, group, heavy)

        self._forward(cfg, body)

    def _forward(self, cfg, body):
        up = urlparse(cfg['upstream'])
        conn = HTTPConnection(up.hostname, up.port or 80, timeout=300)
        try:
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP_BY_HOP and k.lower() != 'host'}
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            return self._json(502, {'error': 'upstream_unreachable', 'detail': str(e)})

        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP or k.lower() == 'content-length':
                    continue
                self.send_header(k, v)
            self.send_header('Connection', 'close')
            self.end_headers()
            if self.command != 'HEAD':
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
            self.close_connection = True

    def log_message(self, fmt, *args):  # 静音默认日志，避免刷量
        pass


# ---------------------------------------------------------------- 管理面

class AdminPlaneHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'quota-platform/0.1'

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        token = self.headers.get('X-Admin-Token', '')
        cfg = self.server.cfg.current()
        ok = token and hmac.compare_digest(
            hashlib.sha256(token.encode()).digest(),
            hashlib.sha256(str(cfg.get('admin_token', '')).encode()).digest())
        if not ok:
            self._json(401, {'error': 'unauthorized'})
        return ok

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/healthz':
            return self._json(200, {'status': 'ok'})
        if path == '/' or path == '/index.html':
            return self._serve_html()
        if not self._authed():
            return
        if path == '/api/state':
            cfg = self.server.cfg.current()
            masked = {k: v for k, v in cfg.items() if k != 'admin_token'}
            return self._json(200, {
                'config': masked,
                'inventory': self.server.inventory.all_masked(),
                **self.server.store.state(),
            })
        self._json(404, {'error': 'not_found'})

    def do_POST(self):
        if not self._authed():
            return
        path = self.path.split('?')[0]
        if path == '/api/reload_inventory':
            self.server.inventory.reload()
            return self._json(200, {'ok': True, 'keys': len(self.server.inventory.all_masked())})
        if path == '/api/lookup_key':
            length = int(self.headers.get('Content-Length') or 0)
            try:
                req = json.loads(self.rfile.read(length) or b'{}')
            except json.JSONDecodeError:
                return self._json(400, {'error': 'invalid_json'})
            info = self.server.inventory.lookup(str(req.get('token', '')))
            if not info:
                return self._json(404, {'error': 'key_not_found'})
            return self._json(200, info)  # inventory 不存 token 本体，天然脱敏
        if path == '/api/config':
            length = int(self.headers.get('Content-Length') or 0)
            try:
                rules = json.loads(self.rfile.read(length) or b'{}')
            except json.JSONDecodeError:
                return self._json(400, {'error': 'invalid_json'})
            try:
                self._validate_rules(rules)
            except ValueError as e:
                return self._json(400, {'error': str(e)})
            self.server.cfg.save_quota_rules(rules)
            return self._json(200, {'ok': True})
        self._json(404, {'error': 'not_found'})

    @staticmethod
    def _validate_rules(rules):
        tiers = rules.get('tiers')
        if tiers is not None:
            if not isinstance(tiers, dict) or not tiers:
                raise ValueError('tiers 必须是非空对象')
            for name, lim in tiers.items():
                if not isinstance(lim, dict):
                    raise ValueError(f'tier {name} 必须是对象')
                for k in ('daily', 'monthly', 'heavy_daily'):
                    if k in lim and lim[k] is not None and (not isinstance(lim[k], int) or lim[k] < 0):
                        raise ValueError(f'tier {name}.{k} 必须是正整数或 null')
        groups = rules.get('groups')
        if groups is not None:
            for g, v in groups.items():
                if not isinstance(v, dict) or 'tier' not in v:
                    raise ValueError(f'group {g} 需要 tier 字段')
        heavy = rules.get('heavy_tools')
        if heavy is not None and not isinstance(heavy, list):
            raise ValueError('heavy_tools 必须是数组')

    def _serve_html(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'admin.html')
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError:
            return self._json(500, {'error': 'admin.html missing'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------- 启动

def main():
    ap = argparse.ArgumentParser(description='quota-platform — MCP 调用统一配额管控')
    ap.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json'))
    ap.add_argument('--db', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quota.db'))
    ap.add_argument('--data-port', type=int, default=None, help='覆盖数据面端口')
    ap.add_argument('--admin-port', type=int, default=None, help='覆盖管理面端口')
    args = ap.parse_args()

    if not os.path.exists(args.config):
        example = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.example.json')
        if os.path.exists(example):
            import shutil
            shutil.copyfile(example, args.config)
            print(f'[quota-platform] 未发现配置文件，已从模板生成 {args.config}（请修改 admin_token 后再重启）')
        else:
            raise SystemExit(f'配置文件不存在: {args.config}')

    cfg = Config(args.config)
    store = QuotaStore(args.db)
    inventory = KeyInventory(cfg.current().get('mcp_settings_path', ''))
    inventory.reload()

    dcfg = cfg.current()['data_plane']
    acfg = cfg.current()['admin_plane']

    data_srv = ThreadingHTTPServer((dcfg['host'], args.data_port or dcfg['port']), DataPlaneHandler)
    data_srv.cfg, data_srv.store, data_srv.inventory = cfg, store, inventory
    admin_srv = ThreadingHTTPServer((acfg['host'], args.admin_port or acfg['port']), AdminPlaneHandler)
    admin_srv.cfg, admin_srv.store, admin_srv.inventory = cfg, store, inventory

    threading.Thread(target=data_srv.serve_forever, daemon=True).start()
    threading.Thread(target=admin_srv.serve_forever, daemon=True).start()
    print(f"[quota-platform] 数据面 :{data_srv.server_address[1]}  →  "
          f"{cfg.current()['upstream']}")
    print(f"[quota-platform] 管理面 :{admin_srv.server_address[1]}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        data_srv.shutdown()
        admin_srv.shutdown()


if __name__ == '__main__':
    main()
