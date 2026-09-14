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
import base64
import binascii
import http.client
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

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
CREATE TABLE IF NOT EXISTS calls (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  key_name   TEXT NOT NULL,
  group_name TEXT NOT NULL,
  tool       TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL,           -- ok / http_error / blocked
  reason     TEXT NOT NULL DEFAULT '' -- blocked 时的原因（daily/disabled/...）
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_key_ts ON calls(key_name, ts);
"""

DEFAULT_CONFIG = {
    'upstream': 'http://127.0.0.1:3100',       # mcphub 地址
    'data_plane': {'host': '0.0.0.0', 'port': 3200},
    'admin_plane': {'host': '0.0.0.0', 'port': 3300},
    'admin_token': 'change-me',                # 管理面访问令牌
    # ── 管理台账号登录（推荐；令牌保留给脚本与应急）──
    'admin_users_path': '',                    # 管理员账号文件，默认与 config.json 同目录
    'admin_session_ttl': 43200,                # 会话最长有效期（秒，默认 12h）
    'admin_session_idle': 1800,                # 空闲超时（秒，默认 30min）
    'admin_login_max_fails': 5,                # 连续失败多少次锁 IP/账号
    'admin_login_lock_seconds': 900,           # 锁定时长（秒）
    'mcp_settings_path': '/mnt/mcp_settings.json',  # 只读挂载 mcphub 的配置文件
    'log_retention_days': 30,                  # 调用流水保留天数（启动时 + 每小时清理）
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


# ---------------------------------------------------------------- 管理员账号 / 会话
#
# 为什么不再靠「复制管理令牌」：
#   · 令牌是**静态共享秘密**，粘进浏览器 localStorage 后无法区分谁在用，也无法
#     单独吊销——换人、离职、怀疑泄漏只能全量换令牌，所有自动化脚本一起挂。
#   · 无法审计：日志里只知道"带着令牌的人"，不知道"谁"。
# 改账号口令后：
#   · 口令只以 scrypt 哈希落盘（不可逆），每个管理员一个账号；
#   · 登录换取**短期会话**（默认 12h 上限 + 30min 空闲过期），可单独吊销；
#   · 失败限流（按 IP 与按 IP+账号双维度），在线猜口令不可行；
#   · 会话带 username，后续配额改动能追到人。
# 旧的 X-Admin-Token **继续可用**（脚本 / 应急后门），人类入口只走登录。

SCRYPT_N, SCRYPT_R, SCRYPT_P = 16384, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
PW_MIN_LEN = 10
SESSION_COOKIE = 'qp_session'


def _b64e(b):
    return base64.b64encode(b).decode('ascii')


def hash_password(password, *, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """→ ``scrypt$n$r$p$<b64salt>$<b64hash>``。

    scrypt 是内存硬函数，抗 GPU/ASIC 离线爆破（PBKDF2 在同等校验耗时下弱得多）。
    只存哈希，永不落明文，也不做可逆加密。
    """
    if not isinstance(password, str) or len(password) < PW_MIN_LEN:
        raise ValueError('口令至少 %d 位' % PW_MIN_LEN)
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
                        dklen=32, maxmem=SCRYPT_MAXMEM)
    return 'scrypt$%d$%d$%d$%s$%s' % (n, r, p, _b64e(salt), _b64e(dk))


def verify_password(password, stored):
    """常量时间校验。格式非法 / 口令为空一律 False（fail-closed）。"""
    if not password or not isinstance(stored, str):
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split('$')
        if algo != 'scrypt':
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        want = base64.b64decode(hash_b64, validate=True)
        dk = hashlib.scrypt(password.encode('utf-8'), salt=salt,
                            n=int(n), r=int(r), p=int(p), dklen=len(want),
                            maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError, binascii.Error, MemoryError):
        return False
    return hmac.compare_digest(dk, want)


class AdminUsers:
    """``admin_users.json``（权限 0600）热加载。文件不存在 = 未启用账号登录。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0
        self._users = {}

    def reload(self, force=False):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = 0
        if not force and mtime == self._mtime:
            return
        users = {}
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for u in (raw.get('users') or []):
                name = str(u.get('username') or '').strip()
                if name and u.get('hash'):
                    users[name] = {'hash': str(u['hash']),
                                   'disabled': bool(u.get('disabled')),
                                   'created_at': u.get('created_at')}
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            users = {}
        with self._lock:
            self._users = users
            self._mtime = mtime

    def current(self):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = 0
        if mtime != self._mtime:
            self.reload()
        with self._lock:
            return dict(self._users)

    @property
    def configured(self):
        return bool(self.current())

    def names(self):
        return sorted(self.current())

    def verify(self, username, password):
        u = self.current().get(str(username or '').strip())
        if not u or u.get('disabled'):
            # 账号不存在也跑一次 scrypt：否则响应时间快慢会变成账号枚举侧信道
            verify_password(password or 'x', _DUMMY_HASH)
            return False
        return verify_password(password, u['hash'])

    def set_password(self, username, password):
        """新增或重置口令（先算哈希，口令太短直接抛 ValueError）。"""
        name = str(username or '').strip()
        if not name:
            raise ValueError('用户名不能为空')
        digest = hash_password(password)
        with self._lock:
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raw = {}
            except (OSError, json.JSONDecodeError):
                raw = {}
            users = [u for u in (raw.get('users') or [])
                     if str((u or {}).get('username') or '').strip() != name]
            users.append({'username': name, 'hash': digest, 'disabled': False,
                          'created_at': int(time.time())})
            raw['users'] = users
            self._write_raw(raw)
        self.reload(force=True)

    def set_disabled(self, username, disabled):
        name = str(username or '').strip()
        with self._lock:
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raw = {}
            except (OSError, json.JSONDecodeError):
                raw = {}
            hit = False
            for u in (raw.get('users') or []):
                if str((u or {}).get('username') or '').strip() == name:
                    u['disabled'] = bool(disabled)
                    hit = True
            if not hit:
                return False
            self._write_raw(raw)
        self.reload(force=True)
        return True

    def _write_raw(self, raw):
        d = os.path.dirname(os.path.abspath(self.path)) or '.'
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        tmp = self.path + '.tmp'
        # 0600：口令哈希与 config.json 里的 admin_token 同级敏感，不能让同机
        # 其他用户读到（离线爆破的入口）。
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class SessionStore:
    """内存会话表：重启即失效（管理员重新登录，可接受，换来零持久化状态）。"""

    def __init__(self, ttl=43200, idle=1800):
        self.ttl = int(ttl)
        self.idle = int(idle)
        self._lock = threading.Lock()
        self._s = {}

    def create(self, username, ip='', method='password'):
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._gc(now)
            self._s[sid] = {'username': username, 'ip': ip, 'method': method,
                            'created_at': now, 'last_seen': now}
        return sid

    def touch(self, sid):
        """→ 会话副本（并刷新 last_seen）；过期或不存在 → None。"""
        if not sid:
            return None
        now = time.time()
        with self._lock:
            self._gc(now)
            s = self._s.get(sid)
            if not s:
                return None
            s['last_seen'] = now
            return dict(s)

    def destroy(self, sid):
        if not sid:
            return False
        with self._lock:
            return self._s.pop(sid, None) is not None

    def destroy_user(self, username, keep_sid=None):
        """吊销某账号的全部会话（改口令后调用），本会话除外。"""
        with self._lock:
            n = 0
            for sid in [k for k, v in self._s.items()
                        if v['username'] == username and k != keep_sid]:
                self._s.pop(sid, None)
                n += 1
        return n

    def _gc(self, now):
        for sid in [k for k, v in self._s.items()
                    if now - v['created_at'] > self.ttl
                    or now - v['last_seen'] > self.idle]:
            self._s.pop(sid, None)

    def count(self):
        with self._lock:
            self._gc(time.time())
            return len(self._s)


class LoginThrottle:
    """登录失败限流：按 IP、按 (IP, 账号) 两个维度计数，锁定期内直接拒绝。

    内存实现、重启清零。目标不是对抗大规模分布式爆破（那要靠 nginx limit_req
    / fail2ban），而是让**在线猜口令**不可行。
    """

    def __init__(self, max_fails=5, lock_seconds=900):
        self.max_fails = max(1, int(max_fails))
        self.lock_seconds = max(1, int(lock_seconds))
        self._lock = threading.Lock()
        self._fails = {}   # key -> [count, first_fail_ts, locked_until]

    def check(self, ip, username):
        """→ 还需等待的秒数（0 = 允许尝试）。

        必须**向上取整**：``int()`` 截断会让锁定最后 1 秒返回 0，而调用方用
        ``if wait:`` 判断是否拒绝 —— 那 1 秒内限流等于被绕过。
        """
        now = time.time()
        with self._lock:
            worst = 0.0
            for k in (('ip', ip), ('user', ip, username)):
                e = self._fails.get(k)
                if e and e[2] > now:
                    worst = max(worst, e[2] - now)
            return math.ceil(worst)

    def fail(self, ip, username):
        now = time.time()
        with self._lock:
            for k in (('ip', ip), ('user', ip, username)):
                e = self._fails.get(k)
                # 距上次失败过久 → 重新计数（避免几周前的 1 次失败累积到锁定）
                if not e or now - e[1] > self.lock_seconds * 4:
                    e = [0, now, 0.0]
                e[0] += 1
                e[1] = now
                if e[0] >= self.max_fails:
                    e[2] = now + self.lock_seconds
                    e[0] = 0
                self._fails[k] = e

    def succeed(self, ip, username):
        """登录成功 → 清掉该 IP 与账号的失败计数。

        必须连 per-IP 一起清：某 IP 上的失败计数可能来自其他账号的错口令，若只清
        per-user 键，同一出口 IP（办公室 NAT）的合法管理员会被别人的失败连坐锁死，
        而且**口令正确也解不开**（检查在验证之前）。
        """
        with self._lock:
            self._fails.pop(('user', ip, username), None)
            self._fails.pop(('ip', ip), None)


# 账号不存在时用来对齐耗时的哑哈希（scrypt 参数与真实账号一致）
_DUMMY_HASH = hash_password('dummy-password-for-timing')


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
        conn.execute('PRAGMA journal_mode=WAL')  # 热路径读写不互锁
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

    # -- 调用流水 ----------------------------------------------------

    def log_call(self, key_name, group, tool, latency_ms, status, reason=''):
        """单条 INSERT，调用方负责 try/except（记录失败不得影响转发）。"""
        with self.lock:
            c = self._conn()
            c.execute(
                'INSERT INTO calls(ts,key_name,group_name,tool,latency_ms,status,reason) '
                'VALUES(?,?,?,?,?,?,?)',
                (int(time.time()), key_name, group, tool or '', int(latency_ms), status, reason or ''),
            )
            c.commit()
            c.close()

    def prune_calls(self, retention_days):
        """删除 N 天前的流水，返回删除行数。"""
        cutoff = int(time.time()) - int(retention_days) * 86400
        with self.lock:
            c = self._conn()
            cur = c.execute('DELETE FROM calls WHERE ts < ?', (cutoff,))
            n = cur.rowcount
            c.commit()
            c.close()
        return n

    def daily_series(self, days=7, heavy_tools=()):
        """近 N 天每日 total/heavy/blocked（按本地日期分桶，含今天）。"""
        start = _day_start() - (days - 1) * 86400
        with self.lock:
            c = self._conn()
            rows = c.execute(
                'SELECT ts, tool, status FROM calls WHERE ts >= ?', (start,)).fetchall()
            c.close()
        heavy_set = set(heavy_tools or ())
        buckets = {}
        for i in range(days):
            d = time.strftime('%Y-%m-%d', time.localtime(start + i * 86400))
            buckets[d] = {'date': d, 'total': 0, 'heavy': 0, 'blocked': 0}
        for r in rows:
            d = time.strftime('%Y-%m-%d', time.localtime(r['ts']))
            b = buckets.get(d)
            if b is None:
                continue
            b['total'] += 1
            if r['status'] == 'blocked':
                b['blocked'] += 1
            if r['tool'] in heavy_set:
                b['heavy'] += 1
        return list(buckets.values())

    def call_stats(self, key_name=None):
        """今日成功率与 p50/p95 延迟。成功率 = ok / 全部（blocked 也算未成功）；
        延迟只统计真正打到上游的（排除 blocked 的 0ms）。"""
        start = _day_start()
        sql = 'SELECT latency_ms, status FROM calls WHERE ts >= ?'
        args = [start]
        if key_name is not None:
            sql += ' AND key_name = ?'
            args.append(key_name)
        with self.lock:
            c = self._conn()
            rows = c.execute(sql, args).fetchall()
            c.close()
        total = len(rows)
        ok = sum(1 for r in rows if r['status'] == 'ok')
        lat = sorted(r['latency_ms'] for r in rows if r['status'] != 'blocked')
        return {
            'total': total,
            'ok': ok,
            'success_rate': round(ok / total, 4) if total else None,
            'p50': _percentile(lat, 0.50),
            'p95': _percentile(lat, 0.95),
        }

    def key_group_usage(self, key_name):
        """该 key 各分组的今日/本月用量。"""
        day = time.strftime('%Y-%m-%d')
        month = day[:7]
        with self.lock:
            c = self._conn()
            rows = c.execute(
                'SELECT group_name, period, calls, heavy FROM usage '
                'WHERE key_name=? AND period IN (?,?)', (key_name, day, month)).fetchall()
            c.close()
        out = {}
        for r in rows:
            e = out.setdefault(r['group_name'], {
                'group_name': r['group_name'],
                'day_calls': 0, 'day_heavy': 0, 'month_calls': 0, 'month_heavy': 0})
            if r['period'] == day:
                e['day_calls'], e['day_heavy'] = r['calls'], r['heavy']
            else:
                e['month_calls'], e['month_heavy'] = r['calls'], r['heavy']
        return sorted(out.values(), key=lambda x: -x['month_calls'])

    def recent_calls(self, key_name=None, limit=50):
        rows = self.query_calls(key_name=key_name, limit=limit)
        for r in rows:
            r.pop('id', None)
        return rows

    def query_calls(self, key_name=None, group=None, status=None, tool=None,
                    since=None, before_id=None, limit=200):
        """全局调用流水查询：filters 均可选，before_id 用于向前翻页（id 倒序）。"""
        sql = ('SELECT id, ts, key_name, group_name, tool, latency_ms, status, reason '
               'FROM calls')
        conds, args = [], []
        if key_name:
            conds.append('key_name = ?')
            args.append(key_name)
        if group:
            conds.append('group_name = ?')
            args.append(group)
        if status:
            conds.append('status = ?')
            args.append(status)
        if tool:
            conds.append('tool LIKE ?')
            args.append('%' + tool.replace('%', '').replace('_', '') + '%')
        if since is not None:
            conds.append('ts >= ?')
            args.append(int(since))
        if before_id is not None:
            conds.append('id < ?')
            args.append(int(before_id))
        if conds:
            sql += ' WHERE ' + ' AND '.join(conds)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(max(1, min(int(limit), 500)))
        with self.lock:
            c = self._conn()
            rows = c.execute(sql, args).fetchall()
            c.close()
        return [dict(r) for r in rows]


def _day_start():
    """本地今日 00:00 的 unix ts。"""
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo), 1)


# ---------------------------------------------------------------- 判定逻辑

TOOL_GROUP_PREFIX = {
    'astock-data-': 'data',
    'global-data-': 'data',
    'factor-miner-': 'alpha',
    'causal-': 'alpha',
    'kronos-': 'alpha',
    'causal-memory-': 'memory',
}


def extract_group(raw_path, cfg, tool_name=''):
    """判定请求所属分组。

    优先级：① 路径任一段命中 groups 配置（mcphub 分组路由形如
    /{group}/sse、/{group}/messages）；② 工具名前缀映射（cfg.tool_groups，
    缺省用内置 TOOL_GROUP_PREFIX）；③ 兜底 global。

    为什么要按工具名兜底：接入 URL 由客户端配置决定，实测大量用户走
    /hub/mcp、/hub/sse 这类不带组名的路径 -> 全部落 global -> 吃 default 档
    （daily 500）-> 连本该免费的行情查询都被限流。按工具名前缀归类则不受
    接入路径影响。
    """
    known = cfg.get('groups', {})
    if not known:
        return 'global'
    for seg in re.findall(r'/([^/?]+)', raw_path.split('?')[0]):
        if seg in known:
            return seg
    table = cfg.get('tool_groups') or TOOL_GROUP_PREFIX
    if tool_name:
        # 最长前缀优先：causal-memory-* 必须先于 causal-* 匹配，否则会被抢走
        for prefix, g in sorted(table.items(), key=lambda kv: -len(kv[0])):
            if tool_name.startswith(prefix) and g in known:
                return g
    return 'global'


def classify_request(body_bytes, cfg):
    """返回 (是否计次, 是否重度, 工具名)。只统计 JSON-RPC tools/call。"""
    if not body_bytes:
        return False, False, ''
    try:
        req = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, False, ''
    if not isinstance(req, dict) or req.get('method') != 'tools/call':
        return False, False, ''
    name = (req.get('params') or {}).get('name', '')
    heavy = name in set(cfg.get('heavy_tools') or [])
    return True, heavy, name


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


def key_status_block(cfg, key_name):
    """key 级拦截（先于配额检查）：disabled / expires_at 过期。返回 (reason, 文案) 或 None。"""
    ov = (cfg.get('key_overrides') or {}).get(key_name) or {}
    if ov.get('disabled'):
        return 'disabled', 'key 已禁用'
    exp = ov.get('expires_at')
    if exp is not None:
        try:
            if time.time() >= float(exp):
                return 'expired', 'key 已过期'
        except (TypeError, ValueError):
            pass  # 配置里写了非法 expires_at 不拦截，避免误伤
    return None


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
        _countable_pre, _heavy_pre, _tool_pre = classify_request(body, cfg)
        group = extract_group(self.path, cfg, _tool_pre)

        # key 级拦截（先于配额检查，且对所有请求生效，不只 tools/call）
        blocked = key_status_block(cfg, key_name)
        if blocked:
            reason, text = blocked
            countable, _, tool = classify_request(body, cfg)
            if countable:
                self._log_call(key_name, group, tool, 0, 'blocked', reason)
            return self._json(403, {
                'jsonrpc': '2.0',
                'error': {
                    'code': -32003,
                    'message': text,
                    'data': {'key': key_name, 'group': group, 'quota': reason},
                },
            })

        countable, heavy, tool = classify_request(body, cfg)
        if countable:
            limits = resolve_limits(cfg, key_name, group)
            usage = self.server.store.get_usage(key_name, group)
            reason = check_quota(limits, usage, heavy)
            if reason:
                self.server.store.add_block(key_name, group, reason)
                self._log_call(key_name, group, tool, 0, 'blocked', reason)
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
            t0 = time.monotonic()
            upstream_status = self._forward(cfg, body)
            latency_ms = int((time.monotonic() - t0) * 1000)
            # 响应已完整转发后再落流水，热路径上只多一条 WAL INSERT
            status = 'ok' if upstream_status is not None and upstream_status < 400 else 'http_error'
            self._log_call(key_name, group, tool, latency_ms, status)
            return

        self._forward(cfg, body)

    def _log_call(self, key_name, group, tool, latency_ms, status, reason=''):
        try:
            self.server.store.log_call(key_name, group, tool, latency_ms, status, reason)
        except Exception as e:  # 流水记录失败只记日志，绝不影响转发
            print(f'[quota-platform] log_call 失败: {e}', file=sys.stderr)

    def _forward(self, cfg, body):
        """透传并返回上游状态码（连接失败返回 502）。"""
        up = urlparse(cfg['upstream'])
        conn = HTTPConnection(up.hostname, up.port or 80, timeout=300)
        try:
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP_BY_HOP and k.lower() != 'host'}
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            self._json(502, {'error': 'upstream_unreachable', 'detail': str(e)})
            return 502

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
        except (BrokenPipeError, ConnectionResetError, http.client.IncompleteRead):
            # 客户端中途断开（SSE/长连接很常见）或上游 chunked 流未收尾就关闭。
            # IncompleteRead 不是异常业务状态，只是对端没把流读完；按断开处理即可，
            # 否则每条被掐断的流都会往日志里丢一段 traceback。
            pass
        finally:
            conn.close()
            self.close_connection = True
        return resp.status

    def log_message(self, fmt, *args):  # 静音默认日志，避免刷量
        pass


# ---------------------------------------------------------------- 管理面

class AdminPlaneHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'quota-platform/0.1'

    def _json(self, status, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra_headers or ()):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ── 依赖装配（惰性） ────────────────────────────────────────
    # 管理面依赖 users/sessions/throttle。宿主（main()）会注入，但测试或
    # 其他嵌入方式可能只装配了 cfg/store/inventory —— 这里按需补默认实例，
    # 避免因为少挂一个属性就 AttributeError 崩掉整个管理面。

    @property
    def _sessions(self):
        s = getattr(self.server, 'sessions', None)
        if s is None:
            cfg = self.server.cfg.current()
            s = SessionStore(ttl=cfg.get('admin_session_ttl') or 43200,
                             idle=cfg.get('admin_session_idle') or 1800)
            self.server.sessions = s
        return s

    @property
    def _users(self):
        u = getattr(self.server, 'users', None)
        if u is None:
            base = os.path.dirname(os.path.abspath(getattr(self.server.cfg, 'path', '.') or '.'))
            cfg = self.server.cfg.current()
            u = AdminUsers(cfg.get('admin_users_path') or
                           os.path.join(base, 'admin_users.json'))
            self.server.users = u
        return u

    @property
    def _throttle(self):
        t = getattr(self.server, 'throttle', None)
        if t is None:
            cfg = self.server.cfg.current()
            t = LoginThrottle(max_fails=cfg.get('admin_login_max_fails') or 5,
                              lock_seconds=cfg.get('admin_login_lock_seconds') or 900)
            self.server.throttle = t
        return t

    # ── 客户端信息 ──────────────────────────────────────────────

    def _client_ip(self):
        """真实客户端 IP。

        nginx 用 ``$proxy_add_x_forwarded_for`` 把 ``$remote_addr`` **追加到末尾**，
        因此客户端可以伪造前面几跳、伪造不了最后一跳 —— 只取最后一个才可信。
        """
        xff = self.headers.get('X-Forwarded-For', '')
        if xff:
            last = xff.split(',')[-1].strip()
            if last:
                return last
        return (self.client_address or ('?', 0))[0]

    def _cookie(self, name):
        for part in self.headers.get('Cookie', '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == name:
                return v
        return ''

    def _cookie_path(self):
        """会话 cookie 的 Path。

        nginx 在 ``/quota/`` 上剥掉了前缀（``proxy_pass .../``），应用侧看不到
        ``/quota``；靠 nginx 传的 ``X-Forwarded-Prefix`` 还原，这样会话 cookie
        不会被浏览器发到 ``/hub/`` 的 MCP 调用上。直接访问 ``:3300`` 时无该头 → ``/``。
        """
        pfx = (self.headers.get('X-Forwarded-Prefix') or '').strip().rstrip('/')
        return (pfx + '/') if pfx else '/'

    def _session_cookie_header(self, sid, max_age):
        secure = ''
        if (self.headers.get('X-Forwarded-Proto') or '').lower() == 'https':
            secure = '; Secure'
        return ('%s=%s; Path=%s; Max-Age=%d; HttpOnly; SameSite=Strict%s'
                % (SESSION_COOKIE, sid, self._cookie_path(), max_age, secure))

    def _clear_cookie_header(self):
        return ('%s=; Path=%s; Max-Age=0; HttpOnly; SameSite=Strict'
                % (SESSION_COOKIE, self._cookie_path()))

    # ── 鉴权 ────────────────────────────────────────────────────

    def _session(self):
        """→ ``(username, method, sid)``；无有效会话 → ``(None, None, None)``。"""
        sid = self._cookie(SESSION_COOKIE)
        if sid:
            s = self._sessions.touch(sid)
            if s:
                return s['username'], 'session', sid
        return None, None, None

    def _token_ok(self):
        token = self.headers.get('X-Admin-Token', '')
        cfg = self.server.cfg.current()
        return bool(token) and hmac.compare_digest(
            hashlib.sha256(token.encode()).digest(),
            hashlib.sha256(str(cfg.get('admin_token', '')).encode()).digest())

    def _origin_ok(self):
        """Cookie 鉴权下 POST 的纵深防御。

        ``SameSite=Strict`` 已经让跨站请求带不上 cookie，这里再核对一次
        Origin/Referer 的 host 是否等于 Host；非浏览器客户端不带这两个头，放行。
        """
        for h in ('Origin', 'Referer'):
            v = self.headers.get(h)
            if not v:
                continue
            return urlparse(v).netloc == (self.headers.get('Host') or '')
        return True

    def _authed(self, need_origin=True):
        username, method, sid = self._session()
        if username:
            if need_origin and self.command == 'POST' and not self._origin_ok():
                self._json(403, {'error': 'csrf_origin_mismatch',
                                 'hint': 'Origin 与 Host 不一致，已拒绝'})
                return False
            return True
        if self._token_ok():
            return True
        self._json(401, {'error': 'unauthorized',
                         'hint': '先在管理台登录（POST /api/login），'
                                 '或在脚本里带 X-Admin-Token 头'})
        return False

    def _read_json(self):
        """→ dict；非法 JSON / 非对象 → None（调用方回 400）。"""
        length = int(self.headers.get('Content-Length') or 0)
        try:
            obj = json.loads(self.rfile.read(length) or b'{}')
        except (json.JSONDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def _me(self):
        """登录状态探测（**不需要**鉴权）：管理台据此决定渲染登录页还是看板。"""
        username, method, _ = self._session()
        token = self._token_ok()
        return self._json(200, {
            'authenticated': bool(username) or token,
            'username': username or ('token' if token else None),
            'method': method or ('token' if token else None),
            'users_configured': self._users.configured,
            'active_sessions': self._sessions.count(),
            'cookie_path': self._cookie_path(),
        })

    def _login(self):
        ip = self._client_ip()
        body = self._read_json()
        if body is None:
            return self._json(400, {'error': 'invalid_json'})
        username = str(body.get('username') or '').strip()
        password = body.get('password') or ''

        wait = self._throttle.check(ip, username)
        if wait:
            return self._json(429, {'error': 'too_many_attempts', 'retry_after': wait,
                                    'hint': '登录失败次数过多，请 %d 秒后重试' % wait})
        if not username or not password:
            return self._json(400, {'error': 'missing_credentials',
                                    'hint': '需要 username 与 password'})
        if not self._users.configured:
            return self._json(503, {
                'error': 'no_admin_users',
                'hint': '尚未配置管理员账号。在服务器执行：'
                        'python3 scripts/set_admin_password.py --username admin'})
        if not self._users.verify(username, password):
            self._throttle.fail(ip, username)
            left = self._throttle.check(ip, username)
            payload = {'error': 'bad_credentials', 'hint': '用户名或口令错误'}
            if left:
                payload['retry_after'] = left
            return self._json(401, payload)

        self._throttle.succeed(ip, username)
        cfg = self.server.cfg.current()
        ttl = int(cfg.get('admin_session_ttl') or 43200)
        sid = self._sessions.create(username, ip=ip)
        return self._json(200, {'ok': True, 'username': username, 'expires_in': ttl},
                          extra_headers=[('Set-Cookie',
                                          self._session_cookie_header(sid, ttl))])

    def _logout(self):
        _, _, sid = self._session()
        self._sessions.destroy(sid)
        return self._json(200, {'ok': True},
                          extra_headers=[('Set-Cookie', self._clear_cookie_header())])

    def _change_password(self):
        username, method, sid = self._session()
        if method != 'session':
            return self._json(403, {'error': 'session_required',
                                    'hint': '请用账号登录后再改口令（令牌会话不能改口令）'})
        body = self._read_json()
        if body is None:
            return self._json(400, {'error': 'invalid_json'})
        if not self._users.verify(username, body.get('old_password') or ''):
            return self._json(401, {'error': 'bad_credentials', 'hint': '原口令不正确'})
        try:
            self._users.set_password(username, body.get('new_password') or '')
        except ValueError as e:
            return self._json(400, {'error': 'weak_password', 'hint': str(e)})
        revoked = self._sessions.destroy_user(username, keep_sid=sid)
        return self._json(200, {'ok': True, 'revoked_sessions': revoked,
                                'hint': '已吊销该账号的其他会话'})

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/healthz':
            return self._json(200, {'status': 'ok'})
        if path == '/api/me':
            return self._me()
        if path == '/' or path == '/index.html':
            return self._serve_html()
        if not self._authed():
            return
        if path == '/api/state':
            cfg = self.server.cfg.current()
            masked = {k: v for k, v in cfg.items() if k != 'admin_token'}
            stats = self.server.store.call_stats()
            return self._json(200, {
                'config': masked,
                'inventory': self.server.inventory.all_masked(),
                'series_7d': self.server.store.daily_series(7, cfg.get('heavy_tools')),
                'today_success_rate': stats['success_rate'],
                'today_latency': {'p50': stats['p50'], 'p95': stats['p95']},
                **self.server.store.state(),
            })
        if path == '/api/calls':
            return self._calls_query()
        m = re.fullmatch(r'/api/key/([^/]+)', path)
        if m:
            return self._key_detail(unquote(m.group(1)))
        self._json(404, {'error': 'not_found'})

    def _calls_query(self):
        qs = parse_qs(urlparse(self.path).query)
        def one(k):
            v = qs.get(k)
            return v[0] if v and v[0] else None
        def num(k):
            v = one(k)
            if v is None:
                return None
            try:
                return int(v)
            except ValueError:
                return None
        rows = self.server.store.query_calls(
            key_name=one('key'), group=one('group'), status=one('status'),
            tool=one('tool'), since=num('since'), before_id=num('before_id'),
            limit=num('limit') or 200)
        return self._json(200, {
            'calls': rows,
            'has_more': len(rows) >= max(1, min(num('limit') or 200, 500)),
        })

    def _key_detail(self, name):
        cfg = self.server.cfg.current()
        ov = (cfg.get('key_overrides') or {}).get(name) or {}
        usage = self.server.store.key_group_usage(name)
        stats = self.server.store.call_stats(name)
        groups = sorted(set((cfg.get('groups') or {}).keys()) |
                        {u['group_name'] for u in usage} | {'global'})
        return self._json(200, {
            'key': name,
            'in_inventory': any(k['name'] == name for k in self.server.inventory.all_masked()),
            'disabled': bool(ov.get('disabled')),
            'expires_at': ov.get('expires_at'),
            'limits': {g: resolve_limits(cfg, name, g) for g in groups},
            'usage': usage,
            'success_rate': stats['success_rate'],
            'today_calls': stats['total'],
            'latency': {'p50': stats['p50'], 'p95': stats['p95']},
            'recent_calls': self.server.store.recent_calls(name, 50),
        })

    def do_POST(self):
        path = self.path.split('?')[0]
        # 登录/登出必须在鉴权之前
        if path == '/api/login':
            return self._login()
        if path == '/api/logout':
            return self._logout()
        if not self._authed():
            return
        if path == '/api/password':
            return self._change_password()
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
        m = re.fullmatch(r'/api/key/([^/]+)/status', path)
        if m:
            return self._set_key_status(unquote(m.group(1)))
        self._json(404, {'error': 'not_found'})

    def _set_key_status(self, name):
        """写 key_overrides[name] 的 disabled / expires_at（保留 tier_by_group 等其他字段），热生效。"""
        length = int(self.headers.get('Content-Length') or 0)
        try:
            req = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return self._json(400, {'error': 'invalid_json'})
        if not isinstance(req, dict):
            return self._json(400, {'error': 'body 必须是对象'})
        cfg = self.server.cfg.current()
        overrides = dict(cfg.get('key_overrides') or {})
        entry = dict(overrides.get(name) or {})
        if 'disabled' in req:
            if not isinstance(req['disabled'], bool):
                return self._json(400, {'error': 'disabled 必须是 bool'})
            entry['disabled'] = req['disabled']
        if 'expires_at' in req:
            exp = req['expires_at']
            if exp is not None and not (isinstance(exp, (int, float)) and not isinstance(exp, bool)):
                return self._json(400, {'error': 'expires_at 必须是 unix 时间戳或 null'})
            entry['expires_at'] = exp
            if exp is not None:
                entry['expires_at'] = int(exp)
        overrides[name] = entry
        try:
            self.server.cfg.save_quota_rules({'key_overrides': overrides})
        except (OSError, json.JSONDecodeError) as e:
            return self._json(500, {'error': f'config 写入失败: {e}'})
        return self._json(200, {'ok': True, 'key': name,
                                'disabled': bool(entry.get('disabled')),
                                'expires_at': entry.get('expires_at')})

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

    # 管理员账号 / 会话 / 登录限流（只挂在管理面；数据面用不到）
    users_path = (cfg.current().get('admin_users_path') or
                  os.path.join(os.path.dirname(os.path.abspath(args.config)),
                               'admin_users.json'))
    users = AdminUsers(users_path)
    sessions = SessionStore(ttl=cfg.current().get('admin_session_ttl') or 43200,
                            idle=cfg.current().get('admin_session_idle') or 1800)
    throttle = LoginThrottle(max_fails=cfg.current().get('admin_login_max_fails') or 5,
                             lock_seconds=cfg.current().get('admin_login_lock_seconds') or 900)

    def prune_loop():
        while True:
            try:
                days = int(cfg.current().get('log_retention_days') or 30)
                n = store.prune_calls(days)
                if n:
                    print(f'[quota-platform] 流水清理：删除 {n} 条 {days} 天前记录', file=sys.stderr)
            except Exception as e:
                print(f'[quota-platform] 流水清理失败: {e}', file=sys.stderr)
            time.sleep(3600)

    try:
        store.prune_calls(cfg.current().get('log_retention_days') or 30)
    except Exception as e:
        print(f'[quota-platform] 启动流水清理失败: {e}', file=sys.stderr)
    threading.Thread(target=prune_loop, daemon=True).start()

    dcfg = cfg.current()['data_plane']
    acfg = cfg.current()['admin_plane']

    data_srv = ThreadingHTTPServer((dcfg['host'], args.data_port or dcfg['port']), DataPlaneHandler)
    data_srv.cfg, data_srv.store, data_srv.inventory = cfg, store, inventory
    admin_srv = ThreadingHTTPServer((acfg['host'], args.admin_port or acfg['port']), AdminPlaneHandler)
    admin_srv.cfg, admin_srv.store, admin_srv.inventory = cfg, store, inventory
    admin_srv.users, admin_srv.sessions, admin_srv.throttle = users, sessions, throttle

    threading.Thread(target=data_srv.serve_forever, daemon=True).start()
    threading.Thread(target=admin_srv.serve_forever, daemon=True).start()
    print(f"[quota-platform] 数据面 :{data_srv.server_address[1]}  →  "
          f"{cfg.current()['upstream']}")
    print(f"[quota-platform] 管理面 :{admin_srv.server_address[1]}  "
          f"(账号登录 {users_path}，已配置账号: {', '.join(users.names()) or '无 —— 运行 scripts/set_admin_password.py 创建'})")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        data_srv.shutdown()
        admin_srv.shutdown()


if __name__ == '__main__':
    main()
