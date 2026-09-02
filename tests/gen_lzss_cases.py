#!/usr/bin/env python3
"""ブラウザ側 LZSS の突き合わせ用データを作る (Python で圧縮した結果を固める)."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import lzss
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cases = [
    "ぼくのなつやすみ。むしとりにいこう。".encode("cp932"),
    ("きょうはいいてんき。" * 40).encode("cp932"),
    bytes(range(256)) + b"AAAAAAAA" + bytes(i % 13 for i in range(2000)),
    b"", b"a",
]
out = [{"packed": list(lzss.compress(s)), "orig": list(s)} for s in cases]
p = os.path.join(REPO, "tests", "lzss_cases.json")
json.dump(out, open(p, "w"), separators=(",", ":"))
print(f"{len(cases)} 件を tests/lzss_cases.json に書きました")
