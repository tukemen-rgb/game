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
CHECKS = ["split", "msg", "map", "tim2", "fontdraft", "sample", "build", "broken"]

#: (これができていれば作らなくてよい印, 作る道具). **上から順に** 実行する。
#: make_archive.py は make_sample.py の出力 (SCRIPT.BIN など) を材料にするので、
#: 順番を入れ替えると、まっさらな取得直後に 1 つ目で落ちる (#82)。
FIXTURES = [
    ("work/SCRIPT.BIN", ["tools/make_sample.py"]),
    ("work/RINFOLT.iso", ["tools/make_iso.py"]),
    ("work/PACK.IDX", ["tools/make_archive.py"]),
    ("work/FONT.TMS", ["tools/make_tim2.py"]),
    ("work/BOKU2SAMPLE/BOKU2.IDX", ["tools/make_boku2_sample.py"]),
]


def build_fixtures(repo: str = REPO) -> str | None:
    """練習データを作る。作れなければ、その道具が出した言葉をそのまま返す.

    印が既にあっても、**作る道具のほうが新しければ作り直す**。道具を直したのに
    古い練習データが残っていると、検査は古いデータを見たまま緑になる (#82)。
    """
    for marker, cmd in FIXTURES:
        path = os.path.join(repo, marker)
        tool = os.path.join(repo, cmd[0])
        if os.path.exists(path) and os.path.getmtime(path) >= os.path.getmtime(tool):
            continue
        res = subprocess.run([sys.executable, *cmd], cwd=repo, capture_output=True, text=True)
        if res.returncode != 0:
            return (f"練習データを作れませんでした: python3 {cmd[0]} (終了コード {res.returncode})\n"
                    + (res.stdout + res.stderr).strip())
        if not os.path.exists(path):
            return f"python3 {cmd[0]} は通りましたが {marker} ができていません"
    return None


def main() -> int:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("skip: playwright がありません (pip install playwright && python3 -m playwright install chromium)")
        return 0
    problem = build_fixtures()
    if problem:
        print(problem)
        return 1
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
