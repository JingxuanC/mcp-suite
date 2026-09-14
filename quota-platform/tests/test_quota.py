#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quota-platform 单元测试（unittest，无第三方依赖）。

覆盖：健康检查、免费组放行、日配额拦截、重度工具配额、
tools/list 不计次、陌生 key 落 default 档、分组路径识别、
管理面鉴权、拦截记录、SSE 流式透传。
"""

import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as qp  # noqa: E402


# ---------------------------------------------------------------- mock 上游

class MockUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    hits = 0

    def _any(self):
        type(self).hits += 1
        if self.path.split('?')[0].endswith('/sse'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for i in range(3):
                try:
                    self.wfile.write(f'data: chunk{i}\n\n'.encode())
                    self.wfile.flush()
                    time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    break
            self.close_connection = True
            return
        if self.path.split('?')[0].startswith('/fail'):
            payload = b'{"err":true}'
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        payload = json.dumps({'ok': True, 'received': len(body)}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = _any

    def log_message(self, *a):
        pass


def rpc_call(tool):
    return json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': tool, 'arguments': {}}}).encode()


def rpc_list():
    return json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode()


# ---------------------------------------------------------------- 测试基座

class QuotaPlatformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name

        with open(os.path.join(root, 'mcp_settings.json'), 'w') as f:
            json.dump({'bearerKeys': [
                {'token': 'tok-alice', 'name': 'alice', 'owner': 'alice', 'enabled': True,
                 'kind': 'user', 'accessType': 'groups', 'allowedGroups': ['alpha', 'data']},
                {'token': 'tok-bob', 'name': 'bob', 'owner': 'bob', 'enabled': True,
                 'kind': 'user', 'accessType': 'all'},
                {'token': 'tok-disabled', 'name': 'ghost', 'enabled': False},
            ]}, f)

        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), MockUpstreamHandler)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.upstream_port = cls.upstream.server_address[1]

    @classmethod
    def write_config(cls, upstream_port):
        cls.cfg_path = os.path.join(cls.tmp.name, 'config.json')
        with open(cls.cfg_path, 'w') as f:
            json.dump({
                'upstream': f'http://127.0.0.1:{upstream_port}',
                'admin_token': 'secret-test',
                'mcp_settings_path': os.path.join(cls.tmp.name, 'mcp_settings.json'),
                'groups': {'data': {'tier': 'free'}, 'alpha': {'tier': 'paid'}},
                'tiers': {
                    'free': {'daily': 10 ** 6, 'monthly': 10 ** 7, 'heavy_daily': None},
                    'paid': {'daily': 2, 'monthly': 5, 'heavy_daily': 1},
                    'default': {'daily': 1, 'monthly': 3, 'heavy_daily': 0},
                },
                'key_overrides': {},
                'heavy_tools': ['backtest_run'],
            }, f)

    def setUp(self):
        MockUpstreamHandler.hits = 0
        self.write_config(self.upstream_port)  # 每个用例重置 config，避免规则串扰
        self.db = os.path.join(self.tmp.name, f'test-{time.time_ns()}.db')
        self.cfg = qp.Config(self.cfg_path)
        self.store = qp.QuotaStore(self.db)
        self.inv = qp.KeyInventory(self.cfg.current()['mcp_settings_path'])
        self.inv.reload()

        self.data = qp.ThreadingHTTPServer(('127.0.0.1', 0), qp.DataPlaneHandler)
        self.data.cfg, self.data.store, self.data.inventory = self.cfg, self.store, self.inv
        self.admin = qp.ThreadingHTTPServer(('127.0.0.1', 0), qp.AdminPlaneHandler)
        self.admin.cfg, self.admin.store, self.admin.inventory = self.cfg, self.store, self.inv
        threading.Thread(target=self.data.serve_forever, daemon=True).start()
        threading.Thread(target=self.admin.serve_forever, daemon=True).start()
        self.data_port = self.data.server_address[1]
        self.admin_port = self.admin.server_address[1]

    def tearDown(self):
        self.data.shutdown()
        self.admin.shutdown()
        self.data.server_close()
        self.admin.server_close()

    # -- 请求辅助 ----------------------------------------------------

    def call(self, port, method, path, body=None, token=None, admin_token=None,
             cookie=None, headers_extra=None, return_headers=False):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        headers = {}
        if body is not None:
            headers['Content-Length'] = str(len(body))
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if admin_token:
            headers['X-Admin-Token'] = admin_token
        if cookie:
            headers['Cookie'] = cookie
        headers.update(headers_extra or {})
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        hdrs = dict(resp.getheaders())
        conn.close()
        return (resp.status, raw, hdrs) if return_headers else (resp.status, raw)

    # -- 用例 ----------------------------------------------------------

    def test_healthz(self):
        status, raw = self.call(self.data_port, 'GET', '/healthz')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['status'], 'ok')

    def test_free_group_unlimited(self):
        for _ in range(5):
            status, _ = self.call(self.data_port, 'POST', '/data/messages',
                                  rpc_call('get_price'), token='tok-alice')
            self.assertEqual(status, 200)
        self.assertEqual(MockUpstreamHandler.hits, 5)

    def test_paid_daily_quota_blocks(self):
        for i in range(2):
            status, _ = self.call(self.data_port, 'POST', '/alpha/messages',
                                  rpc_call('factor_screen'), token='tok-alice')
            self.assertEqual(status, 200)
        status, raw = self.call(self.data_port, 'POST', '/alpha/messages',
                                rpc_call('factor_screen'), token='tok-alice')
        self.assertEqual(status, 429)
        self.assertIn('配额', json.loads(raw)['error']['message'])
        self.assertEqual(MockUpstreamHandler.hits, 2)  # 第三次未触达上游

    def test_heavy_quota(self):
        s1, _ = self.call(self.data_port, 'POST', '/alpha/messages',
                          rpc_call('backtest_run'), token='tok-bob')
        self.assertEqual(s1, 200)
        s2, raw = self.call(self.data_port, 'POST', '/alpha/messages',
                            rpc_call('backtest_run'), token='tok-bob')
        self.assertEqual(s2, 429)
        self.assertIn('重度', json.loads(raw)['error']['message'])

    def test_tools_list_not_counted(self):
        s, _ = self.call(self.data_port, 'POST', '/messages', rpc_list(), token='tok-alice')
        self.assertEqual(s, 200)
        s, _ = self.call(self.data_port, 'POST', '/messages', rpc_call('x'), token='tok-alice')
        self.assertEqual(s, 200)  # default 档 daily=1，恰好用完
        s, _ = self.call(self.data_port, 'POST', '/messages', rpc_call('x'), token='tok-alice')
        self.assertEqual(s, 429)

    def test_unknown_key_uses_default_tier(self):
        s1, _ = self.call(self.data_port, 'POST', '/messages', rpc_call('x'), token='nope')
        self.assertEqual(s1, 200)
        s2, _ = self.call(self.data_port, 'POST', '/messages', rpc_call('x'), token='nope')
        self.assertEqual(s2, 429)

    def test_disabled_key_treated_as_unknown(self):
        s1, _ = self.call(self.data_port, 'POST', '/messages', rpc_call('x'), token='tok-disabled')
        self.assertEqual(s1, 200)  # default daily=1
        s2, _ = self.call(self.data_port, 'POST', '/messages', rpc_call('x'), token='tok-disabled')
        self.assertEqual(s2, 429)

    def test_group_extraction_with_base_path(self):
        s, _ = self.call(self.data_port, 'POST', '/hub/alpha/messages?session=1',
                         rpc_call('factor_screen'), token='tok-alice')
        self.assertEqual(s, 200)
        usage = self.store.get_usage('alice', 'alpha')
        self.assertEqual(usage['day_calls'], 1)
        # 全局路由归 global，与分组配额互不影响
        s, _ = self.call(self.data_port, 'POST', '/messages', rpc_call('factor_screen'),
                         token='tok-alice')
        self.assertEqual(s, 200)

    def test_get_requests_not_counted(self):
        for _ in range(3):
            s, _ = self.call(self.data_port, 'GET', '/alpha/sse', token='tok-alice')
            self.assertEqual(s, 200)

    def test_sse_streaming_passthrough(self):
        s, raw = self.call(self.data_port, 'GET', '/alpha/sse', token='tok-alice')
        self.assertEqual(s, 200)
        self.assertEqual(raw, b'data: chunk0\n\ndata: chunk1\n\ndata: chunk2\n\n')

    def test_upstream_failure_502(self):
        # 指向不存在端口验证 502（直接另起 proxy 太重，改 config 上游即可）
        with open(self.cfg_path) as f:
            saved = f.read()
        try:
            with open(self.cfg_path) as f:
                cfg = json.load(f)
            cfg['upstream'] = 'http://127.0.0.1:1'
            with open(self.cfg_path, 'w') as f:
                json.dump(cfg, f)
            s, raw = self.call(self.data_port, 'GET', '/data/sse', token='tok-alice')
            self.assertEqual(s, 502)
            self.assertEqual(json.loads(raw)['error'], 'upstream_unreachable')
        finally:
            with open(self.cfg_path, 'w') as f:
                f.write(saved)

    def test_admin_requires_token(self):
        s, _ = self.call(self.admin_port, 'GET', '/api/state')
        self.assertEqual(s, 401)
        s, raw = self.call(self.admin_port, 'GET', '/api/state', admin_token='secret-test')
        self.assertEqual(s, 200)
        state = json.loads(raw)
        self.assertIn('config', state)

    def test_admin_state_shows_blocks(self):
        self.call(self.data_port, 'POST', '/alpha/messages', rpc_call('t'), token='tok-alice')
        self.call(self.data_port, 'POST', '/alpha/messages', rpc_call('t'), token='tok-alice')
        self.call(self.data_port, 'POST', '/alpha/messages', rpc_call('t'), token='tok-alice')
        _, raw = self.call(self.admin_port, 'GET', '/api/state', admin_token='secret-test')
        state = json.loads(raw)
        self.assertEqual(state['blocks_24h'], 1)
        self.assertEqual(state['recent_blocks'][0]['key_name'], 'alice')
        # token 不出现在任何 API 输出里
        self.assertNotIn('tok-alice', raw.decode())

    def test_admin_save_rules_validation_and_effect(self):
        bad = json.dumps({'tiers': {'paid': {'daily': -1}}}).encode()
        s, _ = self.call(self.admin_port, 'POST', '/api/config', bad, admin_token='secret-test')
        self.assertEqual(s, 400)

        good = json.dumps({'key_overrides': {
            'alice': {'tier_by_group': {'alpha': {'daily': 50, 'monthly': 100, 'heavy_daily': 5}}}
        }}).encode()
        s, _ = self.call(self.admin_port, 'POST', '/api/config', good, admin_token='secret-test')
        self.assertEqual(s, 200)
        for _ in range(3):
            s, _ = self.call(self.data_port, 'POST', '/alpha/messages',
                             rpc_call('t'), token='tok-alice')
            self.assertEqual(s, 200)

    def test_admin_lookup_key(self):
        s, raw = self.call(self.admin_port, 'POST', '/api/lookup_key',
                           json.dumps({'token': 'tok-alice'}).encode(), admin_token='secret-test')
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(raw)['name'], 'alice')
        # 响应不含 token 本体
        self.assertNotIn('tok-alice', raw.decode())
        s, _ = self.call(self.admin_port, 'POST', '/api/lookup_key',
                         json.dumps({'token': 'nope'}).encode(), admin_token='secret-test')
        self.assertEqual(s, 404)
        # 未鉴权不可用
        s, _ = self.call(self.admin_port, 'POST', '/api/lookup_key',
                         json.dumps({'token': 'tok-alice'}).encode())
        self.assertEqual(s, 401)

    def test_inventory_masked(self):
        _, raw = self.call(self.admin_port, 'GET', '/api/state', admin_token='secret-test')
        state = json.loads(raw)
        names = [k['name'] for k in state['inventory']]
        self.assertIn('alice', names)
        self.assertIn('bob', names)
        self.assertNotIn('ghost', names)  # disabled 不入库

    # -- 调用流水 ----------------------------------------------------

    def test_call_log_written(self):
        s, _ = self.call(self.data_port, 'POST', '/data/messages',
                         rpc_call('get_price'), token='tok-alice')
        self.assertEqual(s, 200)
        rows = self.store.recent_calls('alice')
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['tool'], 'get_price')
        self.assertEqual(r['group_name'], 'data')
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['reason'], '')
        self.assertGreaterEqual(r['latency_ms'], 0)

    def test_blocked_call_logged(self):
        for _ in range(2):  # paid daily=2
            s, _ = self.call(self.data_port, 'POST', '/alpha/messages',
                             rpc_call('t'), token='tok-alice')
            self.assertEqual(s, 200)
        s, _ = self.call(self.data_port, 'POST', '/alpha/messages',
                         rpc_call('t'), token='tok-alice')
        self.assertEqual(s, 429)
        rows = self.store.recent_calls('alice')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['status'], 'blocked')  # 倒序，最新在前
        self.assertEqual(rows[0]['reason'], 'daily')
        self.assertEqual(rows[1]['status'], 'ok')

    def test_http_error_logged(self):
        s, _ = self.call(self.data_port, 'POST', '/fail/messages',
                         rpc_call('x'), token='tok-bob')
        self.assertEqual(s, 500)
        rows = self.store.recent_calls('bob')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'http_error')

    def test_non_counted_call_not_logged(self):
        s, _ = self.call(self.data_port, 'POST', '/messages', rpc_list(), token='tok-alice')
        self.assertEqual(s, 200)
        self.assertEqual(self.store.recent_calls('alice'), [])

    def test_prune_calls(self):
        self.store.log_call('alice', 'data', 'old_tool', 1, 'ok')
        import sqlite3
        con = sqlite3.connect(self.db)
        con.execute('UPDATE calls SET ts = ? WHERE tool = ?',
                    (int(time.time()) - 31 * 86400, 'old_tool'))
        con.execute('INSERT INTO calls(ts,key_name,group_name,tool,latency_ms,status,reason) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (int(time.time()), 'alice', 'data', 'new_tool', 1, 'ok', ''))
        con.commit()
        con.close()
        n = self.store.prune_calls(30)
        self.assertEqual(n, 1)
        rows = self.store.recent_calls('alice')
        self.assertEqual([r['tool'] for r in rows], ['new_tool'])

    # -- 管理 API：state 扩展 / key 详情 ------------------------------

    def test_admin_state_series_and_stats(self):
        for _ in range(2):
            self.call(self.data_port, 'POST', '/data/messages',
                      rpc_call('get_price'), token='tok-alice')
        self.call(self.data_port, 'POST', '/alpha/messages',
                  rpc_call('t'), token='tok-alice')
        self.call(self.data_port, 'POST', '/alpha/messages',
                  rpc_call('t'), token='tok-alice')
        self.call(self.data_port, 'POST', '/alpha/messages',
                  rpc_call('t'), token='tok-alice')  # 第 3 次 alpha → 429
        s, raw = self.call(self.admin_port, 'GET', '/api/state', admin_token='secret-test')
        self.assertEqual(s, 200)
        state = json.loads(raw)
        series = state['series_7d']
        self.assertEqual(len(series), 7)
        today = series[-1]
        self.assertEqual(today['date'], time.strftime('%Y-%m-%d'))
        self.assertEqual(today['total'], 5)
        self.assertEqual(today['blocked'], 1)
        self.assertAlmostEqual(state['today_success_rate'], 4 / 5, places=3)
        self.assertIsNotNone(state['today_latency']['p50'])
        self.assertIn('p95', state['today_latency'])

    def test_key_detail_api(self):
        self.call(self.data_port, 'POST', '/alpha/messages', rpc_call('t'), token='tok-alice')
        self.call(self.data_port, 'POST', '/data/messages', rpc_call('u'), token='tok-alice')
        s, raw = self.call(self.admin_port, 'GET', '/api/key/alice', admin_token='secret-test')
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertEqual(d['key'], 'alice')
        self.assertTrue(d['in_inventory'])
        self.assertFalse(d['disabled'])
        self.assertIsNone(d['expires_at'])
        groups = {u['group_name'] for u in d['usage']}
        self.assertEqual(groups, {'alpha', 'data'})
        alpha_usage = [u for u in d['usage'] if u['group_name'] == 'alpha'][0]
        self.assertEqual(alpha_usage['day_calls'], 1)
        self.assertIn('alpha', d['limits'])
        self.assertEqual(d['limits']['alpha']['daily'], 2)  # paid 档
        self.assertEqual(len(d['recent_calls']), 2)
        self.assertEqual(d['recent_calls'][0]['tool'], 'u')  # 倒序
        self.assertEqual(d['success_rate'], 1.0)
        # 未鉴权不可用
        s, _ = self.call(self.admin_port, 'GET', '/api/key/alice')
        self.assertEqual(s, 401)

    def test_calls_query_api(self):
        self.call(self.data_port, 'POST', '/data/messages', rpc_call('get_price'), token='tok-alice')
        self.call(self.data_port, 'POST', '/data/messages', rpc_call('get_hist'), token='tok-alice')
        self.call(self.data_port, 'POST', '/fail/messages', rpc_call('get_price'), token='tok-bob')
        # 无筛选：全部 3 条，倒序
        s, raw = self.call(self.admin_port, 'GET', '/api/calls', admin_token='secret-test')
        self.assertEqual(s, 200)
        d = json.loads(raw)
        self.assertEqual(len(d['calls']), 3)
        self.assertFalse(d['has_more'])
        self.assertIn('id', d['calls'][0])
        self.assertGreater(d['calls'][0]['id'], d['calls'][1]['id'])
        # key / 状态 / 工具模糊 / 分组筛选
        s, raw = self.call(self.admin_port, 'GET', '/api/calls?key=alice', admin_token='secret-test')
        self.assertEqual(len(json.loads(raw)['calls']), 2)
        s, raw = self.call(self.admin_port, 'GET', '/api/calls?status=http_error', admin_token='secret-test')
        rows = json.loads(raw)['calls']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['key_name'], 'bob')
        s, raw = self.call(self.admin_port, 'GET', '/api/calls?tool=hist', admin_token='secret-test')
        rows = json.loads(raw)['calls']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['tool'], 'get_hist')
        s, raw = self.call(self.admin_port, 'GET', '/api/calls?group=data', admin_token='secret-test')
        self.assertEqual(len(json.loads(raw)['calls']), 2)
        # limit + before_id 翻页
        s, raw = self.call(self.admin_port, 'GET', '/api/calls?limit=2', admin_token='secret-test')
        d = json.loads(raw)
        self.assertEqual(len(d['calls']), 2)
        self.assertTrue(d['has_more'])
        last_id = d['calls'][-1]['id']
        s, raw = self.call(self.admin_port, 'GET',
                           f'/api/calls?limit=2&before_id={last_id}', admin_token='secret-test')
        d2 = json.loads(raw)
        self.assertEqual(len(d2['calls']), 1)
        self.assertLess(d2['calls'][0]['id'], last_id)
        # 未鉴权
        s, _ = self.call(self.admin_port, 'GET', '/api/calls')
        self.assertEqual(s, 401)

    # -- key 禁用 / 过期 ----------------------------------------------

    def test_key_disable_enable(self):
        s, raw = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                           json.dumps({'disabled': True}).encode(), admin_token='secret-test')
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)['disabled'])
        # 数据面拦截，403 + 中文文案，先于配额检查（free 组也不放行）
        s, raw = self.call(self.data_port, 'POST', '/data/messages',
                           rpc_call('get_price'), token='tok-alice')
        self.assertEqual(s, 403)
        err = json.loads(raw)['error']
        self.assertEqual(err['message'], 'key 已禁用')
        self.assertEqual(MockUpstreamHandler.hits, 0)  # 未触达上游
        # 落流水 reason=disabled
        rows = self.store.recent_calls('alice')
        self.assertEqual(rows[0]['status'], 'blocked')
        self.assertEqual(rows[0]['reason'], 'disabled')
        # 持久化到 config.json，且不影响其他字段
        with open(self.cfg_path) as f:
            on_disk = json.load(f)
        self.assertTrue(on_disk['key_overrides']['alice']['disabled'])
        self.assertIn('tiers', on_disk)
        # 恢复启用
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'disabled': False}).encode(), admin_token='secret-test')
        self.assertEqual(s, 200)
        s, _ = self.call(self.data_port, 'POST', '/data/messages',
                         rpc_call('get_price'), token='tok-alice')
        self.assertEqual(s, 200)

    def test_key_expiry(self):
        past = int(time.time()) - 60
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'expires_at': past}).encode(), admin_token='secret-test')
        self.assertEqual(s, 200)
        s, raw = self.call(self.data_port, 'POST', '/data/messages',
                           rpc_call('get_price'), token='tok-alice')
        self.assertEqual(s, 403)
        self.assertEqual(json.loads(raw)['error']['message'], 'key 已过期')
        # 清掉过期时间恢复
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'expires_at': None}).encode(), admin_token='secret-test')
        self.assertEqual(s, 200)
        s, _ = self.call(self.data_port, 'POST', '/data/messages',
                         rpc_call('get_price'), token='tok-alice')
        self.assertEqual(s, 200)
        # 未来时间不过期
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'expires_at': int(time.time()) + 3600}).encode(),
                         admin_token='secret-test')
        self.assertEqual(s, 200)
        s, _ = self.call(self.data_port, 'POST', '/data/messages',
                         rpc_call('get_price'), token='tok-alice')
        self.assertEqual(s, 200)

    def test_key_status_validation(self):
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'disabled': 'yes'}).encode(), admin_token='secret-test')
        self.assertEqual(s, 400)
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'expires_at': 'tomorrow'}).encode(), admin_token='secret-test')
        self.assertEqual(s, 400)
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'disabled': True}).encode())
        self.assertEqual(s, 401)

    def test_status_preserves_tier_override(self):
        good = json.dumps({'key_overrides': {
            'alice': {'tier_by_group': {'alpha': {'daily': 50, 'monthly': 100, 'heavy_daily': 5}}}
        }}).encode()
        s, _ = self.call(self.admin_port, 'POST', '/api/config', good, admin_token='secret-test')
        self.assertEqual(s, 200)
        s, _ = self.call(self.admin_port, 'POST', '/api/key/alice/status',
                         json.dumps({'disabled': True}).encode(), admin_token='secret-test')
        self.assertEqual(s, 200)
        ov = self.cfg.current()['key_overrides']['alice']
        self.assertTrue(ov['disabled'])
        self.assertEqual(ov['tier_by_group']['alpha']['daily'], 50)  # 额度覆盖未被覆盖



class AdminAuthTest(QuotaPlatformTest):
    """管理台账号登录 / 会话 / 限流。

    背景：原先只有静态 X-Admin-Token（复制粘贴）。令牌是共享秘密，无法区分谁在用、
    无法单独吊销；改成账号口令 + 短期会话。旧令牌保留给脚本/应急。
    """

    PW = 'test-password-1234'

    def setUp(self):
        super().setUp()
        # 每个用例一个独立文件：共用路径会让前一个用例建的账号泄漏给后一个
        self.users_path = os.path.join(self.tmp.name, 'admin_users-%s.json' % time.time_ns())
        self.users = qp.AdminUsers(self.users_path)
        self.sessions = qp.SessionStore(ttl=3600, idle=3600)
        self.throttle = qp.LoginThrottle(max_fails=3, lock_seconds=60)
        self.admin.users = self.users
        self.admin.sessions = self.sessions
        self.admin.throttle = self.throttle

    def login(self, username='admin', password=None, headers_extra=None):
        return self.call(self.admin_port, 'POST', '/api/login',
                         body=json.dumps({'username': username,
                                          'password': password or self.PW}),
                         headers_extra=headers_extra, return_headers=True)

    # ── 口令哈希 ──

    def test_password_hash_is_scrypt_and_roundtrips(self):
        h = qp.hash_password(self.PW)
        self.assertTrue(h.startswith('scrypt$'))
        self.assertNotIn(self.PW, h)              # 绝不明文
        self.assertTrue(qp.verify_password(self.PW, h))
        self.assertFalse(qp.verify_password('wrong', h))

    def test_hash_salt_is_random(self):
        self.assertNotEqual(qp.hash_password(self.PW), qp.hash_password(self.PW))

    def test_verify_password_is_fail_closed(self):
        for bad in (None, '', 'scrypt$bogus', 'md5$deadbeef', 'plaintext'):
            self.assertFalse(qp.verify_password('x', bad))
        self.assertFalse(qp.verify_password('x', None))

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            qp.hash_password('short')

    def test_users_file_is_0600_and_has_no_plaintext(self):
        self.users.set_password('admin', self.PW)
        mode = os.stat(self.users_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        with open(self.users_path) as f:
            self.assertNotIn(self.PW, f.read())

    def test_verify_unknown_user_and_disabled_user(self):
        self.users.set_password('admin', self.PW)
        self.assertFalse(self.users.verify('nobody', self.PW))
        self.users.set_disabled('admin', True)
        self.assertFalse(self.users.verify('admin', self.PW))

    # ── 登录流程 ──

    def test_me_unauthenticated(self):
        s, raw = self.call(self.admin_port, 'GET', '/api/me')
        body = json.loads(raw)
        self.assertEqual(s, 200)
        self.assertFalse(body['authenticated'])
        self.assertFalse(body['users_configured'])

    def test_login_without_any_user_configured_is_503(self):
        s, raw, _ = self.login()
        self.assertEqual(s, 503)
        self.assertEqual(json.loads(raw)['error'], 'no_admin_users')

    def test_login_success_sets_httponly_samesite_cookie(self):
        self.users.set_password('admin', self.PW)
        s, raw, hdrs = self.login()
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(raw)['username'], 'admin')
        sc = hdrs.get('Set-Cookie', '')
        self.assertIn('HttpOnly', sc)
        self.assertIn('SameSite=Strict', sc)
        self.assertIn('Path=/', sc)

    def test_login_wrong_password_401(self):
        self.users.set_password('admin', self.PW)
        s, raw, _ = self.login(password='nope')
        self.assertEqual(s, 401)
        self.assertEqual(json.loads(raw)['error'], 'bad_credentials')

    def test_session_cookie_authorizes_and_replaces_token(self):
        self.users.set_password('admin', self.PW)
        _, _, hdrs = self.login()
        cookie = hdrs['Set-Cookie'].split(';')[0]
        s, _ = self.call(self.admin_port, 'GET', '/api/state', cookie=cookie)
        self.assertEqual(s, 200)

    def test_bogus_cookie_is_rejected(self):
        s, raw = self.call(self.admin_port, 'GET', '/api/state',
                           cookie='qp_session=forged-session-id')
        self.assertEqual(s, 401)

    def test_logout_invalidates_session(self):
        self.users.set_password('admin', self.PW)
        _, _, hdrs = self.login()
        cookie = hdrs['Set-Cookie'].split(';')[0]
        s, _, clear = self.call(self.admin_port, 'POST', '/api/logout', cookie=cookie,
                                return_headers=True)
        self.assertEqual(s, 200)
        self.assertIn('Max-Age=0', clear.get('Set-Cookie', ''))
        s, _ = self.call(self.admin_port, 'GET', '/api/state', cookie=cookie)
        self.assertEqual(s, 401)

    def test_legacy_admin_token_still_works(self):
        self.users.set_password('admin', self.PW)
        s, _ = self.call(self.admin_port, 'GET', '/api/state', admin_token='secret-test')
        self.assertEqual(s, 200)
        s, _ = self.call(self.admin_port, 'GET', '/api/state', admin_token='wrong')
        self.assertEqual(s, 401)

    def test_state_hides_admin_token_from_config(self):
        s, raw = self.call(self.admin_port, 'GET', '/api/state', admin_token='secret-test')
        self.assertEqual(s, 200)
        self.assertNotIn('admin_token', json.loads(raw)['config'])

    # ── CSRF ──

    def test_cookie_post_with_foreign_origin_is_rejected(self):
        self.users.set_password('admin', self.PW)
        _, _, hdrs = self.login()
        cookie = hdrs['Set-Cookie'].split(';')[0]
        s, raw = self.call(self.admin_port, 'POST', '/api/reload_inventory', body='',
                           cookie=cookie,
                           headers_extra={'Origin': 'https://evil.example'})
        self.assertEqual(s, 403)
        self.assertEqual(json.loads(raw)['error'], 'csrf_origin_mismatch')

    def test_token_post_ignores_origin(self):
        """令牌不是浏览器自动携带的凭据，不存在 CSRF，不该被 Origin 检查拦住。"""
        s, _ = self.call(self.admin_port, 'POST', '/api/reload_inventory', body='',
                         admin_token='secret-test',
                         headers_extra={'Origin': 'https://evil.example'})
        self.assertEqual(s, 200)

    # ── 限流 ──

    def test_lockout_after_repeated_failures(self):
        self.users.set_password('admin', self.PW)
        codes = [self.login(password='bad')[0] for _ in range(4)]
        self.assertEqual(codes[:3], [401, 401, 401])
        self.assertEqual(codes[3], 429)
        s, raw, _ = self.login()          # 口令就算对了也被锁
        self.assertEqual(s, 429)
        self.assertIn('retry_after', json.loads(raw))

    def test_lockout_is_per_ip_and_per_user(self):
        t = qp.LoginThrottle(max_fails=2, lock_seconds=60)
        t.fail('1.1.1.1', 'a')
        t.fail('1.1.1.1', 'a')
        self.assertGreater(t.check('1.1.1.1', 'a'), 0)
        self.assertEqual(t.check('2.2.2.2', 'a'), 0)
        t.succeed('1.1.1.1', 'a')
        self.assertEqual(t.check('1.1.1.1', 'a'), 0)

    def test_throttle_expires(self):
        t = qp.LoginThrottle(max_fails=1, lock_seconds=1)
        t.fail('3.3.3.3', 'a')
        self.assertGreater(t.check('3.3.3.3', 'a'), 0)
        time.sleep(1.1)
        self.assertEqual(t.check('3.3.3.3', 'a'), 0)

    # ── 会话生命周期 ──

    def test_session_expires_on_ttl_and_idle(self):
        s = qp.SessionStore(ttl=1, idle=1)
        sid = s.create('admin')
        self.assertIsNotNone(s.touch(sid))
        time.sleep(1.1)
        self.assertIsNone(s.touch(sid))

    def test_session_idle_timeout_independent_of_ttl(self):
        s = qp.SessionStore(ttl=3600, idle=1)
        sid = s.create('admin')
        time.sleep(1.1)
        self.assertIsNone(s.touch(sid))     # 长时间没人动 → 失效

    def test_change_password_revokes_other_sessions(self):
        self.users.set_password('admin', self.PW)
        _, _, h1 = self.login()
        c1 = h1['Set-Cookie'].split(';')[0]
        _, _, _ = self.login()
        s, raw = self.call(self.admin_port, 'POST', '/api/password',
                           body=json.dumps({'old_password': self.PW,
                                            'new_password': 'brand-new-password'}),
                           cookie=c1)
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(raw)['revoked_sessions'], 1)
        self.assertTrue(qp.verify_password('brand-new-password',
                                           self.users.current()['admin']['hash']))

    def test_change_password_requires_old_password(self):
        self.users.set_password('admin', self.PW)
        _, _, h = self.login()
        c = h['Set-Cookie'].split(';')[0]
        s, raw = self.call(self.admin_port, 'POST', '/api/password',
                           body=json.dumps({'old_password': 'wrong',
                                            'new_password': 'brand-new-password'}),
                           cookie=c)
        self.assertEqual(s, 401)

    def test_change_password_rejects_weak_new_password(self):
        self.users.set_password('admin', self.PW)
        _, _, h = self.login()
        c = h['Set-Cookie'].split(';')[0]
        s, raw = self.call(self.admin_port, 'POST', '/api/password',
                           body=json.dumps({'old_password': self.PW,
                                            'new_password': 'short'}),
                           cookie=c)
        self.assertEqual(s, 400)
        self.assertEqual(json.loads(raw)['error'], 'weak_password')

    def test_change_password_not_allowed_for_token_sessions(self):
        s, raw = self.call(self.admin_port, 'POST', '/api/password',
                           body=json.dumps({'old_password': 'x',
                                            'new_password': 'brand-new-password'}),
                           admin_token='secret-test')
        self.assertEqual(s, 403)
        self.assertEqual(json.loads(raw)['error'], 'session_required')

    # ── cookie 作用域 ──

    def test_cookie_path_follows_forwarded_prefix(self):
        """nginx 在 /quota/ 上剥前缀，会话 cookie 应限制在 /quota/，
        否则会被浏览器发到 /hub/ 的 MCP 调用上。"""
        self.users.set_password('admin', self.PW)
        s, raw, hdrs = self.call(self.admin_port, 'POST', '/api/login',
                                 body=json.dumps({'username': 'admin',
                                                  'password': self.PW}),
                                 headers_extra={'X-Forwarded-Prefix': '/quota',
                                                'X-Forwarded-Proto': 'https'},
                                 return_headers=True)
        self.assertEqual(s, 200)
        sc = hdrs['Set-Cookie']
        self.assertIn('Path=/quota/', sc)
        self.assertIn('Secure', sc)

    def test_client_ip_trusts_last_forwarded_hop(self):
        """nginx 用 $proxy_add_x_forwarded_for 追加真实来源到末尾，前面的可伪造。"""
        self.users.set_password('admin', self.PW)
        for _ in range(3):
            self.call(self.admin_port, 'POST', '/api/login',
                      body=json.dumps({'username': 'admin', 'password': 'bad'}),
                      headers_extra={'X-Forwarded-For': '1.2.3.4, 9.9.9.9'})
        # 锁定的是最后一跳 9.9.9.9；伪造的 1.2.3.4 不该被锁
        self.assertGreater(self.throttle.check('9.9.9.9', 'admin'), 0)
        self.assertEqual(self.throttle.check('1.2.3.4', 'admin'), 0)




# ---------------------------------------------------------------- API key 自助申请 / 审批

class MockMcphubKeys(BaseHTTPRequestHandler):
    """只实现建/查/停用 key 的 mcphub 管理 API。"""

    created = []
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        if 'Bearer good-admin-key' not in self.headers.get('Authorization', ''):
            self._send(401, {'success': False, 'message': 'unauthorized'})
            return True
        return False

    def do_GET(self):
        MockMcphubKeys.calls.append(('GET', self.path))
        if self.path != '/hub/api/auth/keys':
            return self._send(404, {'success': False, 'message': 'not found'})
        if self._deny():
            return
        return self._send(200, {'success': True, 'data': [
            {'id': k['id'], 'name': k['name'], 'token': k['token'][:8] + '...****'}
            for k in MockMcphubKeys.created]})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        payload = json.loads(self.rfile.read(n) or b'{}')
        MockMcphubKeys.calls.append(('POST', self.path, payload))
        if self.path != '/hub/api/auth/keys':
            return self._send(404, {'success': False, 'message': 'not found'})
        if self._deny():
            return
        kid = 'kid-%d' % (len(MockMcphubKeys.created) + 1)
        token = 'mcphub_' + os.urandom(32).hex()
        MockMcphubKeys.created.append({'id': kid, 'name': payload.get('name'),
                                       'token': token, 'groups': payload.get('allowedGroups')})
        return self._send(201, {'success': True,
                                'data': {'id': kid, 'name': payload.get('name'), 'token': token},
                                'message': 'The token is only shown once.'})

    def do_PUT(self):
        n = int(self.headers.get('Content-Length') or 0)
        payload = json.loads(self.rfile.read(n) or b'{}')
        MockMcphubKeys.calls.append(('PUT', self.path, payload))
        if self._deny():
            return
        return self._send(200, {'success': True, 'data': {'enabled': payload.get('enabled')}})


class KeyRequestFlowTest(unittest.TestCase):
    """申请 → 审批（建 key）→ 领取（取完即抹），以及限流/脱敏/错误语义。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        MockMcphubKeys.created = []
        MockMcphubKeys.calls = []
        cls.mock = ThreadingHTTPServer(('127.0.0.1', 0), MockMcphubKeys)
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()
        cls.mock_port = cls.mock.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.mock.shutdown()
        cls.mock.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        MockMcphubKeys.created = []
        MockMcphubKeys.calls = []
        self.root = tempfile.mkdtemp(dir=self.tmp.name)
        self.cfg_path = os.path.join(self.root, 'config.json')
        with open(self.cfg_path, 'w') as f:
            json.dump({
                'upstream': 'http://127.0.0.1:1',
                'admin_token': 'legacy-ok',
                'mcp_settings_path': os.path.join(self.root, 'none.json'),
                'mcphub_api_base': 'http://127.0.0.1:%d/hub/api' % self.mock_port,
                'groups': {'data': {'tier': 'free'}, 'alpha': {'tier': 'paid'},
                           'memory': {'tier': 'paid'}},
                'tiers': {'free': {'daily': 1000, 'monthly': 9999, 'heavy_daily': None},
                          'paid': {'daily': 200, 'monthly': 4000, 'heavy_daily': 20}},
                'apply_groups': ['data', 'alpha', 'memory'],
                'apply_rate_hour': 2, 'apply_rate_day': 5,
            }, f)
        self.cfg = qp.Config(self.cfg_path)
        self.store = qp.QuotaStore(os.path.join(self.root, 'q.db'))
        self.inv = qp.KeyInventory(self.cfg.current()['mcp_settings_path'])
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), qp.AdminPlaneHandler)
        self.srv.cfg, self.srv.store, self.srv.inventory = self.cfg, self.store, self.inv
        self.srv.mcphub = qp.McphubAdmin(self.cfg.current()['mcphub_api_base'],
                                         'good-admin-key')
        self.srv.pending = qp.PendingTokens(ttl_seconds=3600)
        self.srv.apply_limiter = qp.ApplyRateLimiter(per_hour=2, per_day=5)
        self.srv.users = qp.AdminUsers(os.path.join(self.root, 'u.json'))
        self.srv.sessions = qp.SessionStore()
        self.srv.throttle = qp.LoginThrottle()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        hdrs = {'Content-Type': 'application/json'}
        hdrs.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers=hdrs)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw or b'{}')
        except json.JSONDecodeError:
            return r.status, {'_raw': raw[:2000].decode('utf-8', 'replace')}

    ADMIN = {'X-Admin-Token': 'legacy-ok'}

    # ── 单号与内存 token ──

    def test_request_no_format_and_uniqueness(self):
        nos = {qp.new_request_no() for _ in range(500)}
        self.assertEqual(len(nos), 500)
        for n in list(nos)[:20]:
            self.assertRegex(n, r'^REQ-[A-HJ-NP-Z2-9]{8}$')  # 无 I/O/0/1

    def test_pending_tokens_pop_is_one_shot(self):
        p = qp.PendingTokens(ttl_seconds=60)
        p.put('REQ-AAAAAAAA', 'mcphub_secret', 'k', ['data'])
        self.assertEqual(p.peek('REQ-AAAAAAAA')['key_name'], 'k')   # peek 不取走
        self.assertEqual(p.pop('REQ-AAAAAAAA')['token'], 'mcphub_secret')
        self.assertIsNone(p.pop('REQ-AAAAAAAA'))                    # 取完即抹
        self.assertEqual(len(p.pending()), 0)

    def test_pending_tokens_ttl(self):
        p = qp.PendingTokens(ttl_seconds=0)
        p.put('REQ-BBBBBBBB', 't', 'k', ['data'])
        time.sleep(0.01)
        self.assertIsNone(p.peek('REQ-BBBBBBBB'))

    def test_pending_listing_hides_token(self):
        p = qp.PendingTokens()
        p.put('REQ-CCCCCCCC', 'mcphub_secret', 'k', ['data'])
        self.assertNotIn('token', p.pending()['REQ-CCCCCCCC'])

    # ── 限流 ──

    def test_rate_limiter_hour_and_day(self):
        r = qp.ApplyRateLimiter(per_hour=2, per_day=3)
        self.assertEqual(r.check('1.1.1.1'), 0)
        r.record('1.1.1.1')
        r.record('1.1.1.1')
        self.assertGreater(r.check('1.1.1.1'), 0)      # 超每小时
        self.assertEqual(r.check('2.2.2.2'), 0)        # 别的 IP 不受影响

    def test_rate_limiter_day_cap(self):
        r = qp.ApplyRateLimiter(per_hour=100, per_day=2)
        r.record('3.3.3.3')
        r.record('3.3.3.3')
        self.assertGreater(r.check('3.3.3.3'), 0)

    # ── mcphub 客户端错误语义 ──

    def test_mcphub_requires_admin_key(self):
        with self.assertRaises(qp.MCPhubError):
            qp.McphubAdmin('http://127.0.0.1:1/hub/api', '').list_keys()

    def test_mcphub_401_is_reported(self):
        bad = qp.McphubAdmin(self.cfg.current()['mcphub_api_base'], 'wrong')
        with self.assertRaises(qp.MCPhubError) as cm:
            bad.list_keys()
        self.assertIn('401', str(cm.exception))

    def test_mcphub_html_instead_of_json_is_caught(self):
        """API 前缀写错时 mcphub 返回 SPA 的 HTML + 200，必须识别成错误而不是当成功。"""
        class Spa(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b'<!DOCTYPE html><html></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        spa = ThreadingHTTPServer(('127.0.0.1', 0), Spa)
        threading.Thread(target=spa.serve_forever, daemon=True).start()
        try:
            client = qp.McphubAdmin('http://127.0.0.1:%d' % spa.server_address[1], 'k')
            with self.assertRaises(qp.MCPhubError) as cm:
                client.list_keys()
            self.assertIn('不是 JSON', str(cm.exception))
        finally:
            spa.shutdown()
            spa.server_close()

    def test_create_key_uses_group_scoped_access(self):
        kid, name, token = qp.McphubAdmin(
            self.cfg.current()['mcphub_api_base'], 'good-admin-key'
        ).create_key('测试客户-ab12', ['data', 'alpha'])
        payload = [c for c in MockMcphubKeys.calls if c[0] == 'POST'][-1][2]
        self.assertEqual(payload['accessType'], 'groups')     # 收窄，不是 all
        self.assertEqual(payload['allowedGroups'], ['data', 'alpha'])
        self.assertEqual(payload['kind'], 'system')
        self.assertTrue(token.startswith('mcphub_'))
        self.assertTrue(kid)

    # ── HTTP 流程 ──

    def test_apply_meta_is_public(self):
        s, b = self.call('GET', '/api/apply/meta')
        self.assertEqual(s, 200)
        self.assertEqual([g['name'] for g in b['groups']], ['data', 'alpha', 'memory'])

    def test_apply_validation(self):
        s, b = self.call('POST', '/api/apply', {})
        self.assertEqual((s, b['error']), (400, 'name_required'))
        s, b = self.call('POST', '/api/apply', {'name': 'x'})
        self.assertEqual((s, b['error']), (400, 'contact_required'))
        s, b = self.call('POST', '/api/apply', {'name': 'x', 'contact': 'y'})
        self.assertEqual((s, b['error']), (400, 'groups_required'))
        s, b = self.call('POST', '/api/apply', {'name': 'x', 'contact': 'y', 'groups': ['nope']})
        self.assertEqual((s, b['error']), (400, 'groups_required'))

    def test_admin_endpoints_require_auth(self):
        for method, path in (('GET', '/api/requests'),
                             ('GET', '/api/requests/1/token'),
                             ('POST', '/api/requests/1/approve')):
            s, _ = self.call(method, path, {} if method == 'POST' else None)
            self.assertEqual(s, 401, '%s %s 应当 401' % (method, path))

    def _apply(self, name='客户甲', groups=('data', 'alpha')):
        s, b = self.call('POST', '/api/apply', {'name': name, 'contact': 'a@b.c',
                                                'purpose': '测试', 'groups': list(groups)})
        self.assertEqual(s, 200)
        return b['req_no'], b['id']

    def test_full_flow_approve_pickup_wipe(self):
        req_no, rid = self._apply()
        s, b = self.call('GET', '/api/apply/' + req_no)
        self.assertEqual(b['status'], 'pending')
        self.assertNotIn('token', b)

        s, b = self.call('POST', '/api/requests/%d/approve' % rid,
                         {'groups': ['data'], 'tier': 'free'}, self.ADMIN)
        self.assertEqual(s, 200, b)
        key_name = b['key_name']
        # 收窄授权 + 档位写入
        post = [c for c in MockMcphubKeys.calls if c[0] == 'POST'][-1][2]
        self.assertEqual(post['allowedGroups'], ['data'])
        self.assertEqual(
            self.cfg.current()['key_overrides'][key_name]['tier_by_group']['data']['daily'], 1000)

        s, b = self.call('GET', '/api/apply/' + req_no)
        self.assertTrue(b['claimable'])
        s, b = self.call('POST', '/api/apply/%s/pickup' % req_no)
        self.assertEqual(s, 200)
        token = b['token']
        self.assertTrue(token.startswith('mcphub_'))
        self.assertTrue(b['access']['per_group'][0]['url'].endswith('/hub/mcp/data'))
        self.assertIn('必须使用带组名的地址', b['access']['note'])

        s, b = self.call('POST', '/api/apply/%s/pickup' % req_no)
        self.assertEqual((s, b['error']), (410, 'already_picked_or_expired'))
        self.assertIsNone(self.srv.pending.peek(req_no))
        s, b = self.call('GET', '/api/requests/%d/token' % rid, None, self.ADMIN)
        self.assertEqual(s, 404)

    def test_approve_rejects_unknown_group_and_tier(self):
        req_no, rid = self._apply()
        s, b = self.call('POST', '/api/requests/%d/approve' % rid,
                         {'groups': ['nope']}, self.ADMIN)
        self.assertEqual(s, 400)
        s, b = self.call('POST', '/api/requests/%d/approve' % rid,
                         {'groups': ['data'], 'tier': 'nope'}, self.ADMIN)
        self.assertEqual((s, b['error']), (400, 'unknown_tier'))

    def test_approve_twice_is_conflict(self):
        req_no, rid = self._apply()
        self.call('POST', '/api/requests/%d/approve' % rid, {'groups': ['data']}, self.ADMIN)
        s, b = self.call('POST', '/api/requests/%d/approve' % rid, {'groups': ['data']}, self.ADMIN)
        self.assertEqual((s, b['error']), (409, 'already_approved'))

    def test_reissue_disables_old_key_and_reissues(self):
        req_no, rid = self._apply()
        s, b = self.call('POST', '/api/requests/%d/approve' % rid,
                         {'groups': ['data']}, self.ADMIN)
        old = b['key_name']
        s, b = self.call('POST', '/api/requests/%d/reissue' % rid,
                         {'groups': ['alpha'], 'tier': 'paid'}, self.ADMIN)
        self.assertEqual(s, 200, b)
        self.assertNotEqual(b['key_name'], old)
        self.assertTrue(any(c[0] == 'PUT' and c[2].get('enabled') is False
                            for c in MockMcphubKeys.calls), '旧 key 应被停用')
        s, b = self.call('GET', '/api/requests/%d/token' % rid, None, self.ADMIN)
        self.assertEqual(s, 200)

    def test_reject_flow(self):
        req_no, rid = self._apply()
        s, b = self.call('POST', '/api/requests/%d/reject' % rid,
                         {'reason': '用途不明确'}, self.ADMIN)
        self.assertEqual(s, 200)
        s, b = self.call('GET', '/api/apply/' + req_no)
        self.assertEqual(b['status'], 'rejected')
        self.assertEqual(b['reject_reason'], '用途不明确')
        s, b = self.call('POST', '/api/apply/%s/pickup' % req_no)
        self.assertEqual((s, b['error']), (409, 'not_approved'))

    def test_pickup_before_approval_is_rejected(self):
        req_no, _ = self._apply()
        s, b = self.call('POST', '/api/apply/%s/pickup' % req_no)
        self.assertEqual((s, b['error']), (409, 'not_approved'))

    def test_unknown_request_no_is_404(self):
        s, b = self.call('GET', '/api/apply/REQ-ZZZZZZZZ')
        self.assertEqual((s, b['error']), (404, 'not_found'))

    def test_secrets_are_masked_in_state(self):
        s, b = self.call('GET', '/api/state', None, self.ADMIN)
        self.assertEqual(s, 200)
        self.assertNotIn('admin_token', b['config'])
        self.assertNotIn('mcphub_admin_key', b['config'])
        self.assertIn('pending_requests', b)

    def test_apply_page_is_served(self):
        s, b = self.call('GET', '/apply')
        self.assertEqual(s, 200)   # HTML 页，解析失败会落到 _raw
        self.assertIn('_raw', b)
        self.assertIn('申请 API Key', b['_raw'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
