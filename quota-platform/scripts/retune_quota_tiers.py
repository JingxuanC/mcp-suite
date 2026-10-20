#!/usr/bin/env python3
"""Retune paid/default tier limits and migrate exact tier-copied overrides.

Approvals copy the selected tier's limits into `key_overrides[].tier_by_group`.
Changing `tiers.paid` alone would leave existing approved keys on the old
values, so this script also rewrites overrides whose value is byte-for-byte the
old tier, while preserving custom overrides.

Usage:
  python3 scripts/retune_quota_tiers.py --config config.json \
      --paid-monthly 60000 --paid-heavy 50 \
      --default-monthly 15000 --default-heavy 50          # dry-run
  ... --apply
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import time
from pathlib import Path


def retune(cfg: dict, *, paid_monthly: int, paid_heavy: int,
           default_monthly: int, default_heavy: int) -> tuple[dict, list[str]]:
    new_cfg = copy.deepcopy(cfg)
    tiers = new_cfg.setdefault('tiers', {})
    old_paid = copy.deepcopy(tiers.get('paid') or {})
    old_default = copy.deepcopy(tiers.get('default') or {})
    new_paid = dict(old_paid)
    new_paid['monthly'] = paid_monthly
    new_paid['heavy_daily'] = paid_heavy
    new_default = dict(old_default)
    new_default['monthly'] = default_monthly
    new_default['heavy_daily'] = default_heavy
    tiers['paid'] = new_paid
    tiers['default'] = new_default

    messages = []
    for key_name in sorted((new_cfg.get('key_overrides') or {})):
        entry = new_cfg['key_overrides'].get(key_name) or {}
        tbg = entry.get('tier_by_group') or {}
        for group, limits in list(tbg.items()):
            if limits == old_paid:
                tbg[group] = dict(new_paid)
                messages.append(f'{key_name}: {group} paid -> monthly={paid_monthly}, heavy={paid_heavy}')
            elif limits == old_default:
                tbg[group] = dict(new_default)
                messages.append(f'{key_name}: {group} default -> monthly={default_monthly}, heavy={default_heavy}')
    return new_cfg, messages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--paid-monthly', type=int, required=True)
    parser.add_argument('--paid-heavy', type=int, required=True)
    parser.add_argument('--default-monthly', type=int, required=True)
    parser.add_argument('--default-heavy', type=int, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    path = Path(args.config)
    cfg = json.loads(path.read_text(encoding='utf-8'))
    new_cfg, messages = retune(
        cfg,
        paid_monthly=args.paid_monthly,
        paid_heavy=args.paid_heavy,
        default_monthly=args.default_monthly,
        default_heavy=args.default_heavy,
    )
    for line in messages:
        print(line)
    print(f'{len(messages)} override(s) will be migrated')
    print('paid monthly=%d heavy=%d; default monthly=%d heavy=%d'
          % (args.paid_monthly, args.paid_heavy, args.default_monthly, args.default_heavy))
    if not args.apply:
        print('dry-run only; pass --apply to write')
        return 0

    backup = path.with_name(path.name + '.bak-retune-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(new_cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)
    print(f'wrote {path}; backup {backup}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
