#!/usr/bin/env python3
"""構造探査台をヘッドレスブラウザで実際に操作して確かめる (tests/e2e/*.py を順に実行).

    python3 tests/e2e/run_all.py

必要な練習データを先に組み立ててから、各検査を別プロセスで走らせる。
どれか 1 つでも RESULT NG なら終了コード 1。playwright が無ければ 0 で skip。
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CHECKS = ["split", "msg", "map", "tim2", "sample"]
FIXTURES = [
    ("work/PACK.IDX", ["tools/make_archive.py"]),
    ("work/FONT.TMS", ["tools/make_tim2.py"]),
    ("work/BOKU2SAMPLE/BOKU2.IDX", ["tools/make_boku2_sample.py"]),
]


def main() -> int:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("skip: playwright がありません (pip install playwright && python3 -m playwright install chromium)")
        return 0
    for marker, cmd in FIXTURES:
        if not os.path.exists(os.path.join(REPO, marker)):
            subprocess.run([sys.executable, *cmd], cwd=REPO, check=True, capture_output=True)
    failed = []
    for name in CHECKS:
        res = subprocess.run([sys.executable, os.path.join(HERE, name + ".py")],
                             cwd=REPO, capture_output=True, text=True, timeout=300)
        ok = res.returncode == 0 and "RESULT OK" in res.stdout
        print(f"{'OK ' if ok else 'NG '} {name}")
        if not ok:
            failed.append(name)
            print(res.stdout[-1500:], res.stderr[-1500:])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
