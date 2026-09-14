#!/usr/bin/env python3
"""把 canonical ``mcp-common/mcp_common.py`` 落成各服务的 vendored 副本。

为什么是 vendoring 而不是 pip 依赖
──────────────────────────────────
5 个服务是**独立 git 仓库 + 独立镜像**，构建上下文各自是自己的目录：

* pip from git：构建期需要网络 + ghproxy，且给 5 个镜像各加一层不确定性；
* git submodule：每次 pull 都得 ``--recurse-submodules``，漏了就静默用旧版；
* vendoring：零运行时/构建期耦合，复制即生效，再用本脚本的 ``--check``
  把"副本漂移"变成可被 CI 拦下来的失败。

所以 canonical 只有一份（在 mcp-suite 仓库里），副本由本脚本生成，头部带
源指纹；改了 canonical 忘记同步，``--check`` 会报错。

用法
────
::

    python3 mcp-common/sync.py            # 写入/更新各服务副本
    python3 mcp-common/sync.py --check    # 只校验，漂移则退出码 1（CI 门禁）
    python3 mcp-common/sync.py --list     # 打印目标路径与状态
"""

import argparse
import hashlib
import sys
from pathlib import Path

SERVICES = ["astock-data-mcp", "global-data-mcp", "causal-mcp",
            "kronos-mcp", "factor-miner-mcp"]

TARGET_NAME = "mcp_common.py"
SOURCE_REL = "mcp-common/mcp_common.py"

HEADER = """\
# """ + "=" * 71 + """
# 自动生成 —— 请勿直接编辑本文件
#
#   源    : @@SOURCE@@   (mcp-suite 仓库)
#   指纹  : sha256:@@DIGEST@@
#   同步  : python3 mcp-common/sync.py
#   校验  : python3 mcp-common/sync.py --check      (CI 门禁)
#
# 直接修改本文件会在下次 sync 时被覆盖，且 --check 会失败。要改公共函数，
# 请改 canonical 源文件后重新 sync。
# """ + "=" * 71 + """

"""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(canonical: str) -> str:
    """canonical 正文 → 带指纹头的副本内容。"""
    head = (HEADER.replace("@@SOURCE@@", SOURCE_REL)
                  .replace("@@DIGEST@@", sha256(canonical)))
    return head + canonical


def find_repo(root: Path, service: str):
    """在候选位置里找服务仓库目录。

    本地开发时服务仓库是 mcp-suite 的**兄弟目录**（``~/project/<svc>``），
    服务器上则是**子目录**（``/opt/mcp-suite/<svc>``）。两处都要支持。
    """
    for cand in (root / service, root.parent / service):
        if cand.is_dir() and (cand / "Dockerfile").exists():
            return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="只校验副本是否与 canonical 一致；漂移则退出码 1")
    ap.add_argument("--list", action="store_true", help="列出目标与状态后退出")
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent,
                    help="mcp-suite 仓库根目录（默认自动推断）")
    args = ap.parse_args()

    root = args.root.resolve()
    src = root / SOURCE_REL
    if not src.exists():
        print("✗ 找不到 canonical 源: %s" % src, file=sys.stderr)
        return 2

    canonical = src.read_text(encoding="utf-8")
    want = render(canonical)
    digest = sha256(canonical)

    print("canonical: %s" % src)
    print("指纹: sha256:%s  (%d 行)" % (digest, canonical.count("\n")))
    print()

    if args.list:
        for svc in SERVICES:
            repo = find_repo(root, svc)
            tgt = (repo / TARGET_NAME) if repo else None
            state = "缺失仓库" if repo is None else (
                "未同步" if not tgt.exists() else
                ("一致" if tgt.read_text(encoding="utf-8") == want else "已漂移"))
            print("  %-18s %-8s %s" % (svc, state, tgt or "-"))
        return 0

    drift, written, missing = [], [], []
    for svc in SERVICES:
        repo = find_repo(root, svc)
        if repo is None:
            missing.append(svc)
            print("  ⚠ %-18s 找不到仓库目录，跳过" % svc)
            continue
        tgt = repo / TARGET_NAME
        old = tgt.read_text(encoding="utf-8") if tgt.exists() else None

        if old == want:
            print("  = %-18s 已是最新" % svc)
            continue
        if args.check:
            drift.append(svc)
            print("  ✗ %-18s 副本与 canonical 不一致" % svc)
            continue
        tgt.write_text(want, encoding="utf-8")
        written.append(svc)
        print("  %s %-18s %s" % ("+" if old is None else "~", svc, tgt))

    print()
    if args.check:
        if drift:
            print("✗ %d 个服务副本漂移: %s" % (len(drift), ", ".join(drift)))
            print("  修复: python3 mcp-common/sync.py  然后提交各服务仓库")
            return 1
        print("✓ 全部副本与 canonical 一致")
        return 0

    print("✓ 写入 %d 个 / 已最新 %d 个%s"
          % (len(written), len(SERVICES) - len(written) - len(missing),
             ("（%d 个仓库未找到: %s）" % (len(missing), ", ".join(missing))) if missing else ""))
    if written:
        print("  下一步: 在各服务仓库 git add %s 并提交" % TARGET_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
