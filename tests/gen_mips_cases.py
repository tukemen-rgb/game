#!/usr/bin/env python3
"""capstone の出したニーモニックを、回帰テスト用の答えとして書き出す.

    pip install capstone
    python3 tests/gen_mips_cases.py

自分で書いた逆アセンブラが正しいかどうかは、自分では確かめられません。
そこで定評のある逆アセンブラ (capstone) に同じ命令語を読ませて、答えを
``tests/mips_cases.json`` に固めておきます。以降は capstone が無い環境でも
``tests/test_disasm.mjs`` と ``tests/run_tests.py`` がこの答えと突き合わせます。

capstone は MIPS32 として読むので、PS2 の R5900 独自命令 (lq / sq / lqc2 /
sqc2) だけは答えが食い違います。そこは食い違って正しいので、テスト側で
オペコード単位で除外しています。
"""
from __future__ import annotations

import json
import random
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "tests" / "mips_cases.json"
ADDR = 0x00100000


def words() -> list[int]:
    """網羅・実物・乱数の 3 種類を混ぜる."""
    seen: set[int] = set()

    # 1. 全オペコード / 全 funct を、いくつかのレジスタの組で
    for op in range(64):
        for rs, rt, rd, sa in ((0, 0, 0, 0), (29, 31, 2, 0), (4, 5, 6, 3), (8, 0, 9, 31)):
            for immv in (0, 1, 0xFFFF, 0x8000, 0x1234):
                seen.add((op << 26) | (rs << 21) | (rt << 16) | (immv & 0xFFFF))
                seen.add((op << 26) | (rs << 21) | (rt << 16) | (rd << 11)
                         | (sa << 6) | (immv & 63))

    # 2. 練習用の本体プログラムに実際に入っている命令
    elf = REPO / "work" / "BOOT.ELF"
    if elf.exists():
        data = elf.read_bytes()
        for off in range(0x1000, min(len(data), 0x1400), 4):
            seen.add(struct.unpack_from("<I", data, off)[0])

    # 3. 乱数。表の抜けは、こういう当てずっぽうでいちばん見つかる
    rnd = random.Random(20240823)
    for _ in range(1500):
        seen.add(rnd.getrandbits(32))

    return sorted(seen)


def main() -> int:
    try:
        import capstone
    except ImportError:
        print("capstone が入っていません: pip install capstone", file=sys.stderr)
        return 2

    md = capstone.Cs(capstone.CS_ARCH_MIPS,
                     capstone.CS_MODE_MIPS32 | capstone.CS_MODE_LITTLE_ENDIAN)
    cases = []
    skipped = 0
    for w in words():
        got = None
        for i in md.disasm(struct.pack("<I", w), ADDR):
            got = i.mnemonic
        if got is None:
            skipped += 1          # capstone も読めない語。答えが無いので使わない
            continue
        cases.append([w, got])

    OUT.write_text(json.dumps({"addr": ADDR, "cases": cases}, separators=(",", ":")) + "\n")
    print(f"{OUT.relative_to(REPO)} に {len(cases):,} 件"
          f" (capstone も読めなかった {skipped:,} 件は除外)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
