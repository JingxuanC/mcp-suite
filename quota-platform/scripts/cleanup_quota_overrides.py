#!/usr/bin/env python3
"""Remove auto-generated paid overrides from free groups.

Approval historically wrote the selected tier into *every* granted group,
including free groups such as data/global. That lowered the free 1,000,000/day
limit to paid 2,000/day. This script removes only overrides that exactly match
the paid tier for a group whose configured tier is free; custom limits (for
example a 20k/day data override) are preserved.

Usage:
  python3 scripts/cleanup_quota_overrides.py --config config.json          # dry-run
  python3 scripts/cleanup_quota_overrides.py --config config.json --apply  # write
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path


def clean_overrides(cfg: dict, tier_name: str = 'paid') -> tuple[dict, list[str]]:
    """Return (new_cfg, messages). Pure enough for tests; does not write."""
    new_cfg = json.loads(json.dumps(cfg))  # deep copy
    groups = new_cfg.get('groups') or {}
    tier = (new_cfg.get('tiers') or {}).get(tier_name) or {}
    overrides = new_cfg.get('key_overrides') or {}
    messages = []
    for key_name in sorted(overrides):
        entry = overrides.get(key_name) or {}
        tbg = entry.get('tier_by_group') or {}
        for group in list(tbg):
            group_tier = (groups.get(group) or {}).get('tier', 'default')
            if group_tier != 'free':
                continue
            if tbg.get(group) != tier:
                continue
            del tbg[group]
            messages.append(f'{key_name}: removed {group} (free group, paid {tier_name} override)')
        if 'tier_by_group' in entry and not tbg:
            del entry['tier_by_group']
        if not entry:
            del overrides[key_name]
    new_cfg['key_overrides'] = overrides
    return new_cfg, messages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--tier', default='paid')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    path = Path(args.config)
    cfg = json.loads(path.read_text(encoding='utf-8'))
    new_cfg, messages = clean_overrides(cfg, args.tier)
    if not messages:
        print('no matching overrides; nothing to do')
        return 0
    for line in messages:
        print(line)
    print(f'{len(messages)} override(s) match')
    if not args.apply:
        print('dry-run only; pass --apply to write')
        return 0

    backup = path.with_name(path.name + '.bak-cleanup-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(new_cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)
    print(f'wrote {path}; backup {backup}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
