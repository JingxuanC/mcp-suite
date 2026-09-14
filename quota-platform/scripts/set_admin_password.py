#!/usr/bin/env python3
"""创建 / 重置 quota-platform 管理员账号（口令只落 scrypt 哈希）。

用法（在服务器上跑，交互输入口令不回显）::

    python3 scripts/set_admin_password.py --username admin
    python3 scripts/set_admin_password.py --username ops --config-dir /opt/mcp-suite/quota-platform
    python3 scripts/set_admin_password.py --username admin --disable      # 停用账号
    python3 scripts/set_admin_password.py --list

改完立即生效（AdminUsers 按 mtime 热加载），**不需要重启服务**。

为什么不走管理台 API：改口令需要已登录，而"还没有账号"时正是要用它的场景
（先有鸡还是先有蛋）。而且这样调用方可以不把口令写进 shell 历史。
"""

import argparse
import getpass
import json
import os
import stat
import sys

# 允许直接从仓库根目录跑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import AdminUsers, PW_MIN_LEN  # noqa: E402


def fmt_mtime(path):
    try:
        return time_str(os.stat(path).st_mtime)
    except OSError:
        return '-'


def time_str(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description='创建/重置 quota-platform 管理员账号')
    ap.add_argument('--config-dir', default=here,
                    help='含 config.json 的目录（默认: %s）' % here)
    ap.add_argument('--users-file', default=None,
                    help='账号文件路径（默认 <config-dir>/admin_users.json）')
    ap.add_argument('--username', default='admin')
    ap.add_argument('--password', default=None,
                    help='直接给口令（不推荐：会进 shell 历史与 argv；省略则交互输入）')
    ap.add_argument('--password-file', default=None, metavar='PATH',
                    help='从文件读口令（首行，末尾换行会去掉）；PATH 用 - 表示 stdin。'
                         '口令含特殊字符时的推荐方式，不进 shell 历史/argv')
    ap.add_argument('--disable', action='store_true', help='停用该账号（不设口令）')
    ap.add_argument('--list', action='store_true', help='只列出已有账号')
    args = ap.parse_args()

    path = args.users_file
    if path is None:
        # 与 server.py 的解析规则保持一致：config.json 里的 admin_users_path 优先
        cfg_path = os.path.join(args.config_dir, 'config.json')
        configured = ''
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                configured = (json.load(f) or {}).get('admin_users_path') or ''
        except (OSError, json.JSONDecodeError):
            pass
        path = configured or os.path.join(args.config_dir, 'admin_users.json')

    users = AdminUsers(path)

    if args.list:
        cur = users.current()
        print('账号文件: %s' % path)
        if not os.path.exists(path):
            print('  (不存在 —— 尚未创建任何管理员账号)')
            return 0
        mode = stat.S_IMODE(os.stat(path).st_mode)
        print('  权限: %o %s' % (mode, '✓ 仅属主可读' if mode == 0o600 else '⚠ 建议 chmod 600'))
        if not cur:
            print('  (无有效账号)')
        for name in sorted(cur):
            u = cur[name]
            print('  %-16s %s  创建于 %s'
                  % (name, '已停用' if u.get('disabled') else '启用中',
                     time_str(u['created_at']) if u.get('created_at') else '-'))
        return 0

    if args.disable:
        if not users.set_disabled(args.username, True):
            print('✗ 账号不存在: %s' % args.username, file=sys.stderr)
            return 1
        print('✓ 已停用账号 %s（会话不受影响，如需立刻踢出请重启服务）' % args.username)
        return 0

    pw = args.password
    if pw is None and args.password_file:
        # 首行即口令，去掉行尾换行（echo 会带上）；不复用 --password 的 shell 展开
        if args.password_file == '-':
            pw = sys.stdin.readline().rstrip('\r\n')
        else:
            try:
                with open(args.password_file, 'r', encoding='utf-8') as f:
                    pw = f.readline().rstrip('\r\n')
            except OSError as e:
                print('✗ 读口令文件失败: %s' % e, file=sys.stderr)
                return 1
        if not pw:
            print('✗ 口令文件为空', file=sys.stderr)
            return 1
    if pw is None:
        pw = getpass.getpass('新口令（至少 %d 位，输入不回显）: ' % PW_MIN_LEN)
        again = getpass.getpass('再输一次: ')
        if pw != again:
            print('✗ 两次输入不一致', file=sys.stderr)
            return 1
    existed = args.username in users.current()   # 必须在写入前判断
    try:
        users.set_password(args.username, pw)
    except ValueError as e:
        print('✗ %s' % e, file=sys.stderr)
        return 1

    mode = stat.S_IMODE(os.stat(path).st_mode)
    print('✓ %s 账号 %s' % ('已更新' if existed else '已创建', args.username))
    print('  账号文件: %s (权限 %o)' % (path, mode))
    if mode != 0o600:
        print('  ⚠ 权限不是 600，建议 chmod 600 %s' % path)
    print('  现在可在管理台用 用户名 + 口令 登录（无需重启服务）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
