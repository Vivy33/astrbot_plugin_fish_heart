"""把 views/heart 同步到 pages/heart。

AstrBot 4.24.2–4.28 只扫描 `pages/<名字>/`，4.29 起才优先 `views/`（并把
`pages/` 当兼容别名）。为了让旧版本也能打开面板，两份目录必须完全一致；
改完页面跑一次这个脚本，`--check` 用来在发版前检查有没有漂移。

用法：
    python tools/sync_pages.py          # 同步 views → pages
    python tools/sync_pages.py --check  # 只检查，不一致时退出码 1
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "views" / "heart"
DST = ROOT / "pages" / "heart"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    if not SRC.is_dir():
        print(f"找不到源目录：{SRC}")
        return 2

    DST.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in SRC.iterdir() if p.is_file())
    names = {p.name for p in files}
    drift: list[str] = []

    for p in files:
        q = DST / p.name
        if q.exists() and filecmp.cmp(p, q, shallow=False):
            continue
        drift.append(p.name)
        if not check:
            shutil.copy2(p, q)

    extra = sorted(
        p.name for p in DST.iterdir() if p.is_file() and p.name not in names
    )
    for name in extra:
        drift.append(name)
        if not check:
            (DST / name).unlink()

    if check:
        if drift:
            print("pages/heart 与 views/heart 不一致：" + "、".join(drift))
            return 1
        print("pages/heart 与 views/heart 一致")
        return 0

    print(f"已同步 {len(files)} 个文件到 pages/heart")
    if extra:
        print("已清理多余文件：" + "、".join(extra))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
