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

    def call(self, port, method, path, body=None, token=None, admin_token=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        headers = {}
        if body is not None:
            headers['Content-Length'] = str(len(body))
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if admin_token:
            headers['X-Admin-Token'] = admin_token
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw

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

    def test_inventory_masked(self):
        _, raw = self.call(self.admin_port, 'GET', '/api/state', admin_token='secret-test')
        state = json.loads(raw)
        names = [k['name'] for k in state['inventory']]
        self.assertIn('alice', names)
        self.assertIn('bob', names)
        self.assertNotIn('ghost', names)  # disabled 不入库


if __name__ == '__main__':
    unittest.main(verbosity=2)
