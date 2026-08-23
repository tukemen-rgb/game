#!/usr/bin/env python3
"""練習用の本体プログラム (ELF32 / MIPS / リトルエンディアン) を作る。

PS2 の実機データは権利の問題があって置けないので、同じ形のものを自分で
組み立てます。市販ソフトの ``SLPS_123.45`` と構造は同じです。

  * ELF32 リトルエンディアン / e_machine = 8 (MIPS)
  * PT_LOAD 1 個。0x00100000 に載る (PS2 の実行アドレスの定番)
  * 命令は 4 バイト固定長の MIPS
  * ``lui`` + ``addiu`` の組で文字列のアドレスを作る箇所を仕込んである

最後の点が肝です。探査台の「文字列を使っている場所を全部探す」が
この組を拾えるかどうかを、答えが分かっている状態で確かめられます。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "work" / "BOOT.ELF"

BASE = 0x00100000          # コードが載る番地
TEXT_OFF = 0x1000          # ファイル内でコードが始まる位置
RODATA_AT = 0x400          # コード先頭からの、文字列領域までの距離

REG = {
    "$zero": 0, "$at": 1, "$v0": 2, "$v1": 3, "$a0": 4, "$a1": 5, "$a2": 6, "$a3": 7,
    "$t0": 8, "$t1": 9, "$t2": 10, "$t3": 11, "$t4": 12, "$t5": 13, "$t6": 14, "$t7": 15,
    "$s0": 16, "$s1": 17, "$s2": 18, "$s3": 19, "$s4": 20, "$s5": 21, "$s6": 22, "$s7": 23,
    "$t8": 24, "$t9": 25, "$k0": 26, "$k1": 27, "$gp": 28, "$sp": 29, "$fp": 30, "$ra": 31,
}


def r(name: str) -> int:
    return REG[name]


# --- 命令を組み立てる小道具 (逆アセンブラの逆) ---

def i_type(op: int, rs: str, rt: str, imm: int) -> int:
    return (op << 26) | (r(rs) << 21) | (r(rt) << 16) | (imm & 0xFFFF)


def r_type(rs: str, rt: str, rd: str, sa: int, funct: int) -> int:
    return (r(rs) << 21) | (r(rt) << 16) | (r(rd) << 11) | (sa << 6) | funct


def j_type(op: int, target: int) -> int:
    return (op << 26) | ((target >> 2) & 0x3FFFFFF)


def addiu(rt, rs, imm): return i_type(0x09, rs, rt, imm)
def ori(rt, rs, imm): return i_type(0x0D, rs, rt, imm)
def lui(rt, imm): return i_type(0x0F, "$zero", rt, imm)
def lw(rt, off, base): return i_type(0x23, base, rt, off)
def sw(rt, off, base): return i_type(0x2B, base, rt, off)
def lbu(rt, off, base): return i_type(0x24, base, rt, off)
def sb(rt, off, base): return i_type(0x28, base, rt, off)
def beq(rs, rt, off): return i_type(0x04, rs, rt, off)
def bne(rs, rt, off): return i_type(0x05, rs, rt, off)
def slt(rd, rs, rt): return r_type(rs, rt, rd, 0, 0x2A)
def addu(rd, rs, rt): return r_type(rs, rt, rd, 0, 0x21)
def subu(rd, rs, rt): return r_type(rs, rt, rd, 0, 0x23)
def and_(rd, rs, rt): return r_type(rs, rt, rd, 0, 0x24)
def sll(rd, rt, sa): return r_type("$zero", rt, rd, sa, 0x00)
def srl(rd, rt, sa): return r_type("$zero", rt, rd, sa, 0x02)
def jr(rs): return r_type(rs, "$zero", "$zero", 0, 0x08)
def jal(t): return j_type(0x03, t)
def j(t): return j_type(0x02, t)


NOP = 0

# --- 仕込む文字列。実物でもこういう名前が生で入っている ---
STRINGS = [
    "cdrom0:\\BOKU2.IMG;1",
    "cdrom0:\\BOKU2.IDX;1",
    "index open failed\n",
    "read error at sector %d\n",
    "MAP/NATSU00.PAK",
    "BGM/TITLE.VAG",
]

# main / load から lui + addiu で参照されるもの (答え合わせ用)
REFERENCED = {STRINGS[0], STRINGS[1], STRINGS[2], STRINGS[3]}


def build_rodata() -> tuple[bytes, dict[str, int]]:
    """文字列を並べて、それぞれの番地を返す。"""
    blob = bytearray()
    addrs: dict[str, int] = {}
    for s in STRINGS:
        addrs[s] = BASE + RODATA_AT + len(blob)
        blob += s.encode("ascii") + b"\x00"
    while len(blob) % 4:
        blob += b"\x00"
    return bytes(blob), addrs


def hi_lo(addr: int) -> tuple[int, int]:
    """lui / addiu の組に割る。

    addiu は符号付きで足すので、下位 16 ビットの最上位が立っていたら
    上位側を 1 繰り上げておく必要があります。逆アセンブル側でこれを
    間違えると、復元したアドレスが 0x10000 ずれます。
    """
    lo = addr & 0xFFFF
    hi = (addr >> 16) & 0xFFFF
    if lo & 0x8000:
        hi = (hi + 1) & 0xFFFF
        lo -= 0x10000
    return hi, lo


def build() -> bytes:
    rodata, addrs = build_rodata()

    # 関数の配置を先に決める。分岐先の計算に番地が要るため
    main_at = BASE
    load_at = BASE + 0x100
    strlen_at = BASE + 0x180

    code: dict[int, list[int]] = {}

    def emit(at: int, words: list[int]) -> None:
        code[at] = words

    # --- main: 2 つのファイル名で load を呼ぶ ---
    words = [
        addiu("$sp", "$sp", -32),
        sw("$ra", 0x1C, "$sp"),
        sw("$s0", 0x18, "$sp"),
    ]
    for name in (STRINGS[1], STRINGS[0]):        # .IDX を先に開き、次に .IMG
        hi, lo = hi_lo(addrs[name])
        words += [
            lui("$a0", hi),
            addiu("$a0", "$a0", lo),
            jal(load_at),
            NOP,                                  # 遅延スロット
            addu("$s0", "$v0", "$zero"),
        ]
    words += [
        lw("$ra", 0x1C, "$sp"),
        lw("$s0", 0x18, "$sp"),
        jr("$ra"),
        addiu("$sp", "$sp", 32),                  # 遅延スロットで後始末
    ]
    emit(main_at, words)

    # --- load: 失敗したらメッセージのアドレスを返す ---
    hi_err, lo_err = hi_lo(addrs[STRINGS[2]])
    hi_rd, lo_rd = hi_lo(addrs[STRINGS[3]])
    # 分岐の量は「分岐命令の次の次から数えた命令数」。遅延スロットの分だけずれる
    emit(load_at, [
        addiu("$sp", "$sp", -16),                 # +00
        sw("$ra", 0x0C, "$sp"),                   # +04
        addu("$s1", "$a0", "$zero"),              # +08
        jal(strlen_at),                           # +0C
        NOP,                                      # +10  遅延スロット
        bne("$v0", "$zero", 5),                   # +14  名前が空でなければ +2C へ
        NOP,                                      # +18  遅延スロット
        lui("$v0", hi_err),                       # +1C
        addiu("$v0", "$v0", lo_err),              # +20  → "index open failed"
        j(load_at + 0x34),                        # +24
        NOP,                                      # +28  遅延スロット
        lui("$v0", hi_rd),                        # +2C
        addiu("$v0", "$v0", lo_rd),               # +30  → "read error at sector %d"
        lw("$ra", 0x0C, "$sp"),                   # +34
        jr("$ra"),                                # +38
        addiu("$sp", "$sp", 16),                  # +3C  遅延スロットで後始末
    ])

    # --- strlen: 素朴な 1 バイトずつの走査 ---
    emit(strlen_at, [
        addu("$v0", "$zero", "$zero"),            # +00
        lbu("$t0", 0, "$a0"),                     # +04  ループの先頭
        beq("$t0", "$zero", 5),                   # +08  0 なら +20 へ抜ける
        NOP,                                      # +0C  遅延スロット
        addiu("$v0", "$v0", 1),                   # +10
        addiu("$a0", "$a0", 1),                   # +14
        j(strlen_at + 4),                         # +18
        NOP,                                      # +1C  遅延スロット
        jr("$ra"),                                # +20
        NOP,                                      # +24  遅延スロット
    ])

    # --- 全体を 1 本のバイト列にする ---
    text = bytearray(RODATA_AT)
    for at, words in code.items():
        off = at - BASE
        blob = b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words)
        text[off:off + len(blob)] = blob
    text += rodata

    filesz = len(text)

    # --- ELF ヘッダ / プログラムヘッダ ---
    ehsize, phentsize, shentsize = 52, 32, 40
    phoff = ehsize
    shoff = TEXT_OFF + filesz
    shstr = b"\x00.text\x00.rodata\x00.shstrtab\x00"

    eh = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\x00" * 8
    eh += struct.pack("<HHIIIIIHHHHHH",
                      2,          # e_type = 実行ファイル
                      8,          # e_machine = MIPS
                      1, BASE, phoff, shoff,
                      0x20924001, # e_flags (MIPS III / 32 ビット ABI 相当)
                      ehsize, phentsize, 1, shentsize, 4, 3)

    ph = struct.pack("<IIIIIIII",
                     1,           # PT_LOAD
                     TEXT_OFF, BASE, BASE,
                     filesz, filesz,
                     5,           # r-x
                     0x1000)

    body = bytearray(eh + ph)
    body += b"\x00" * (TEXT_OFF - len(body))
    body += text

    # --- セクションヘッダ (実機の製品版では削られていることも多い) ---
    strtab_off = len(body) + shentsize * 4
    secs = [
        (0, 0, 0, 0, 0, 0),
        (1, 1, 6, BASE, TEXT_OFF, RODATA_AT),                       # .text
        (7, 1, 2, BASE + RODATA_AT, TEXT_OFF + RODATA_AT, len(rodata)),  # .rodata
        (15, 3, 0, 0, strtab_off, len(shstr)),                      # .shstrtab
    ]
    for name, typ, flags, addr, off, size in secs:
        body += struct.pack("<IIIIIIIIII", name, typ, flags, addr, off, size, 0, 0, 4, 0)
    body += shstr
    return bytes(body)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    OUT.write_bytes(data)

    _, addrs = build_rodata()
    print(f"{OUT.relative_to(REPO)} を書きました ({len(data):,} バイト)")
    print()
    print("答え合わせ用:")
    print(f"  入口 (entry)      0x{BASE:08X}")
    print(f"  PT_LOAD           ファイル 0x{TEXT_OFF:X} → メモリ 0x{BASE:08X}")
    print(f"  main              0x{BASE:08X}")
    print(f"  load              0x{BASE + 0x100:08X}")
    print(f"  strlen            0x{BASE + 0x180:08X}")
    print(f"  文字列は {len(STRINGS)} 本。うち 4 本が lui + addiu の組から参照されています")
    print("  (残り 2 本はどこからも参照されていません。実物でも未使用の文字列は残ります)")
    for s in STRINGS:
        mark = "参照あり" if s in REFERENCED else "参照なし"
        print(f"    0x{addrs[s]:08X}  {mark}  {s!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
