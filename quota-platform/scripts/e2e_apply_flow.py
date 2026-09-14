#!/usr/bin/env python3
"""端到端：申请 → 审批（建 key）→ 领取 → 取完即抹。

用一个 mock mcphub 顶替真服务，验证 quota-platform 的完整链路与错误语义。
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as qp  # noqa: E402


# ── mock mcphub ────────────────────────────────────────────────────
class MockMcphub(BaseHTTPRequestHandler):
    created = []
    calls = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        MockMcphub.calls.append(('GET', self.path, dict(self.headers)))
        if self.path == '/hub/api/auth/keys':
            if 'Bearer test-admin-key' not in self.headers.get('Authorization', ''):
                return self._json(401, {'success': False, 'message': 'unauthorized'})
            return self._json(200, {'success': True, 'data': [
                {'id': k['id'], 'name': k['name'], 'token': k['token'][:8] + '...****',
                 'kind': 'system', 'accessType': 'groups',
                 'allowedGroups': k['groups'], 'enabled': True} for k in MockMcphub.created]})
        # 模拟真实的 SPA catch-all：非 /hub/api 路径返回 200 + HTML（假成功）
        if not self.path.startswith('/hub/api'):
            body = b'<!DOCTYPE html><html><head><title>MCPHub Dashboard</title></head></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._json(404, {'success': False, 'message': 'not found'})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        payload = json.loads(self.rfile.read(n) or b'{}')
        MockMcphub.calls.append(('POST', self.path, payload))
        if self.path != '/hub/api/auth/keys':
            return self._json(404, {'success': False, 'message': 'not found'})
        if 'Bearer test-admin-key' not in self.headers.get('Authorization', ''):
            return self._json(401, {'success': False, 'message': 'unauthorized'})
        if not payload.get('name'):
            return self._json(400, {'success': False, 'message': 'Key name is required'})
        kid = 'kid-%d' % (len(MockMcphub.created) + 1)
        token = 'mcphub_' + os.urandom(32).hex()
        MockMcphub.created.append({'id': kid, 'name': payload['name'],
                                   'token': token, 'groups': payload.get('allowedGroups')})
        return self._json(201, {
            'success': True,
            'data': {'id': kid, 'name': payload['name'], 'token': token,
                     'kind': payload.get('kind'), 'accessType': payload.get('accessType'),
                     'allowedGroups': payload.get('allowedGroups'), 'enabled': True},
            'message': 'Bearer key created. The token is only shown once.'})

    def do_PUT(self):
        n = int(self.headers.get('Content-Length') or 0)
        payload = json.loads(self.rfile.read(n) or b'{}')
        MockMcphub.calls.append(('PUT', self.path, payload))
        return self._json(200, {'success': True, 'data': {'id': self.path.rsplit('/', 1)[-1],
                                                          'enabled': payload.get('enabled')}})


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b'{}'), dict(r.getheaders())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}'), dict(e.headers)


def main():
    tmp = tempfile.mkdtemp()
    mock = ThreadingHTTPServer(('127.0.0.1', 0), MockMcphub)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    cfg_path = os.path.join(tmp, 'config.json')
    json.dump({
        'upstream': 'http://127.0.0.1:1',
        'admin_token': 'legacy-token-xyz',
        'mcp_settings_path': os.path.join(tmp, 'none.json'),
        'mcphub_api_base': 'http://127.0.0.1:%d/hub/api' % mock_port,
        'groups': {'data': {'tier': 'free'}, 'alpha': {'tier': 'paid'},
                   'memory': {'tier': 'paid'}},
        'tiers': {'free': {'daily': 1000, 'monthly': 10000, 'heavy_daily': None},
                  'paid': {'daily': 200, 'monthly': 4000, 'heavy_daily': 20}},
        'apply_groups': ['data', 'alpha', 'memory'],
        'apply_rate_hour': 3, 'apply_rate_day': 10,
    }, open(cfg_path, 'w'))
    os.environ['MCPHUB_ADMIN_KEY'] = 'test-admin-key'

    cfg = qp.Config(cfg_path)
    store = qp.QuotaStore(os.path.join(tmp, 'q.db'))
    inv = qp.KeyInventory(cfg.current()['mcp_settings_path'])
    srv = qp.ThreadingHTTPServer(('127.0.0.1', 0), qp.AdminPlaneHandler)
    srv.cfg, srv.store, srv.inventory = cfg, store, inv
    srv.mcphub = qp.McphubAdmin(cfg.current()['mcphub_api_base'], 'test-admin-key')
    srv.pending = qp.PendingTokens(ttl_seconds=3600)
    srv.apply_limiter = qp.ApplyRateLimiter(per_hour=3, per_day=10)
    srv.users = qp.AdminUsers(os.path.join(tmp, 'users.json'))
    srv.sessions = qp.SessionStore()
    srv.throttle = qp.LoginThrottle()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    B = 'http://127.0.0.1:%d' % srv.server_address[1]

    ok_all = []

    def check(label, cond, detail=''):
        ok_all.append(bool(cond))
        print(('  ✅ ' if cond else '  ❌ ') + label + (('  → ' + str(detail)[:150]) if detail else ''))

    print('════ 1. 公开元信息 ════')
    s, b, _ = http('GET', B + '/api/apply/meta')
    check('GET /api/apply/meta 免鉴权', s == 200 and b.get('ok'), b)
    check('返回可选分组', [g['name'] for g in b.get('groups', [])] == ['data', 'alpha', 'memory'])

    print('\n════ 2. 申请（匿名） ════')
    s, b, _ = http('POST', B + '/api/apply', {})
    check('缺称呼 → 400 name_required', s == 400 and b.get('error') == 'name_required', b)
    s, b, _ = http('POST', B + '/api/apply', {'name': '某量化团队'})
    check('缺联系方式 → 400', s == 400 and b.get('error') == 'contact_required', b)
    s, b, _ = http('POST', B + '/api/apply', {'name': '某量化团队', 'contact': 'a@b.com'})
    check('缺分组 → 400', s == 400 and b.get('error') == 'groups_required', b)
    s, b, _ = http('POST', B + '/api/apply', {'name': '某量化团队', 'contact': 'a@b.com',
                                              'purpose': 'A股因子回测', 'groups': ['data', 'alpha'],
                                              'volume': '1万/月'})
    check('正常提交 → 200 + 单号', s == 200 and str(b.get('req_no', '')).startswith('REQ-'), b)
    req_no = b['req_no']
    req_id = b['id']

    print('\n════ 3. 状态查询（匿名） ════')
    s, b, _ = http('GET', B + '/api/apply/' + req_no)
    check('pending 状态', s == 200 and b['status'] == 'pending', b)
    check('不泄露 token', 'token' not in b)
    check('未通过时 claimable 不存在', 'claimable' not in b)
    s, b, _ = http('GET', B + '/api/apply/REQ-NOSUCHNO')
    check('不存在的单号 → 404', s == 404, b)
    s, b, _ = http('GET', B + '/api/apply/' + req_no + '/pickup')
    # 该路径只在 POST 注册；GET 落到鉴权分支返 401，同样拿不到密钥
    check('GET 不能领取（必须 POST）', s in (401, 404, 405), s)

    print('\n════ 4. 未登录不能审批 ════')
    s, b, _ = http('POST', B + '/api/requests/%d/approve' % req_id, {'groups': ['data']})
    check('匿名审批 → 401', s == 401, b)
    s, b, _ = http('GET', B + '/api/requests')
    check('匿名看申请列表 → 401', s == 401, b)

    print('\n════ 5. 管理员审批（调 mcphub 建 key） ════')
    H = {'X-Admin-Token': 'legacy-token-xyz'}
    s, b, _ = http('GET', B + '/api/requests', headers=H)
    check('列申请单（含待办数）', s == 200 and b['pending_count'] == 1, b.get('pending_count'))
    s, b, _ = http('POST', B + '/api/requests/%d/approve' % req_id,
                   {'groups': ['nope']}, headers=H)
    check('非法分组 → 400', s == 400, b)
    s, b, _ = http('POST', B + '/api/requests/%d/approve' % req_id,
                   {'groups': ['data', 'alpha'], 'tier': 'paid'}, headers=H)
    check('审批通过 → 200', s == 200 and b.get('ok'), b.get('hint'))
    key_name = b.get('key_name')
    check('key 名带单号后缀（唯一）',
          key_name and key_name.endswith(req_no.split('-')[-1][:6].lower()), key_name)
    created = MockMcphub.created[-1]
    check('mcphub 建 key 用了收窄授权',
          created['groups'] == ['data', 'alpha']
          and MockMcphub.calls[-1][2].get('accessType') == 'groups', MockMcphub.calls[-1][2])
    check('配额档位已写入 key_overrides',
          (cfg.current().get('key_overrides') or {}).get(key_name, {})
          .get('tier_by_group', {}).get('alpha', {}).get('daily') == 200,
          (cfg.current().get('key_overrides') or {}).get(key_name))
    s, b, _ = http('POST', B + '/api/requests/%d/approve' % req_id, {'groups': ['data']}, headers=H)
    check('重复审批 → 409', s == 409, b.get('error'))

    print('\n════ 6. 状态变为可领取 ════')
    s, b, _ = http('GET', B + '/api/apply/' + req_no)
    check('approved + claimable', s == 200 and b['status'] == 'approved' and b['claimable'], b)
    check('仍未泄露 token', 'token' not in b)

    print('\n════ 7. 领取（取完即抹） ════')
    s, b, _ = http('POST', B + '/api/apply/' + req_no + '/pickup')
    check('领取 → 200 且返回 token', s == 200 and b.get('token', '').startswith('mcphub_'), str(b)[:120])
    token = b['token']
    check('token 与 mcphub 建的一致', token == created['token'])
    check('返回接入说明（带组名地址 + 警示）',
          b['access']['per_group'][0]['url'].endswith('/hub/mcp/data')
          and '必须使用带组名的地址' in b['access']['note'], b['access']['base'] if 'base' in b['access'] else '')
    s, b, _ = http('POST', B + '/api/apply/' + req_no + '/pickup')
    check('二次领取 → 410（已抹除）', s == 410 and b.get('error') == 'already_picked_or_expired', b)
    check('内存里已无该 token', srv.pending.peek(req_no) is None)
    s, b, _ = http('GET', B + '/api/apply/' + req_no)
    check('状态显示已领取', s == 200 and b['picked_at'] and not b['claimable'], b)
    s, b, _ = http('GET', B + '/api/requests/%d/token' % req_id, headers=H)
    check('管理台查已领走的 token → 404', s == 404, b.get('error'))

    print('\n════ 8. 改发（token 丢失场景） ════')
    s, b, _ = http('POST', B + '/api/requests/%d/reissue' % req_id,
                   {'groups': ['data'], 'tier': 'free'}, headers=H)
    check('改发 → 200 且换新 key 名', s == 200 and b['key_name'] != key_name, b.get('key_name'))
    check('旧 key 已停用',
          any(c[0] == 'PUT' and c[2].get('enabled') is False for c in MockMcphub.calls), '')
    s, b, _ = http('GET', B + '/api/requests/%d/token' % req_id, headers=H)
    check('管理台可代取新 token（人工转发兜底）', s == 200 and b['token'].startswith('mcphub_'), s)
    s, b, _ = http('POST', B + '/api/apply/' + req_no + '/pickup')
    check('改发后申请人仍能领', s == 200, s)

    print('\n════ 9. 拒绝流程 ════')
    s, b, _ = http('POST', B + '/api/apply', {'name': '第二个申请', 'contact': 'c@d.com',
                                              'groups': ['memory']})
    req2, id2 = b['req_no'], b['id']
    s, b, _ = http('POST', B + '/api/requests/%d/reject' % id2,
                   {'reason': '用途描述不足，请补充团队规模'}, headers=H)
    check('拒绝 → 200', s == 200, b)
    s, b, _ = http('GET', B + '/api/apply/' + req2)
    check('申请人看到拒绝理由', s == 200 and b['status'] == 'rejected'
          and '用途描述不足' in b['reject_reason'], b.get('reject_reason'))
    s, b, _ = http('POST', B + '/api/apply/' + req2 + '/pickup')
    check('被拒后不能领取', s == 409, b.get('error'))

    print('\n════ 10. 限流 ════')
    codes = []
    for i in range(4):
        s, b, _ = http('POST', B + '/api/apply',
                       {'name': '刷子%d' % i, 'contact': 'x@y.com', 'groups': ['data']})
        codes.append(s)
    check('超过每小时上限 → 429', 429 in codes, codes)

    print('\n════ 11. 脱敏 ════')
    s, b, _ = http('GET', B + '/api/state', headers=H)
    check('state 不回 admin_token', 'admin_token' not in b['config'])
    check('state 不回 mcphub_admin_key', 'mcphub_admin_key' not in b['config'])
    check('state 带待办数', 'pending_requests' in b, b.get('pending_requests'))

    print('\n════ 12. mcphub 客户端错误语义 ════')
    bad = qp.McphubAdmin('http://127.0.0.1:%d/hub/api' % mock_port, '')
    try:
        bad.create_key('x', ['data'])
        check('无 admin key → 报错', False)
    except qp.MCPhubError as e:
        check('无 admin key → 明确报错', 'MCPHUB_ADMIN_KEY' in str(e), e)
    wrong = qp.McphubAdmin('http://127.0.0.1:%d/hub/api' % mock_port, 'wrong-key')
    try:
        wrong.list_keys()
        check('错误 admin key → 报错', False)
    except qp.MCPhubError as e:
        check('错误 admin key → HTTP 401 上报', 'HTTP 401' in str(e), e)
    # 把 API 前缀写错（少 /hub/api）→ mcphub 会返回 SPA 的 HTML 且状态码 200，
    # 必须被识别成"不是 JSON"而不是当成功解析
    spa = qp.McphubAdmin('http://127.0.0.1:%d' % mock_port, 'test-admin-key')
    try:
        r = spa._call('GET', '/auth/keys')
        check('API 前缀写错时识别非 JSON（SPA 假成功）', False, str(r)[:80])
    except qp.MCPhubError as e:
        check('API 前缀写错时识别非 JSON（SPA 假成功）', '不是 JSON' in str(e), e)

    print('\n' + '═' * 60)
    print('结果：%d/%d 通过' % (sum(ok_all), len(ok_all)))
    return 0 if all(ok_all) else 1


if __name__ == '__main__':
    sys.exit(main())
