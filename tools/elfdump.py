#!/usr/bin/env python3
"""PS2 の本体プログラム (ELF32 / MIPS) をコードのところまで読む.

    # ヘッダと区画を見る
    python3 tools/elfdump.py work/BOOT.ELF

    # 入口から 64 命令を逆アセンブルする
    python3 tools/elfdump.py work/BOOT.ELF --disasm --count 64

    # 指定した番地から読む
    python3 tools/elfdump.py work/BOOT.ELF --disasm --addr 0x00100100

    # コードが指している文字列を全部拾う (どの関数が何を読んでいるか)
    python3 tools/elfdump.py work/BOOT.ELF --xref

ここまでのツールは「データがどこにあるか」を見るものでした。これは
「そのデータをプログラムがどう読んでいるか」を見るためのものです。
圧縮の伸張ルーチンやファイル名の組み立てはコードの中にしかないので、
最後はここに来ます。

capstone が入っていればそれを使い、無ければ内蔵のデコーダで読みます
(``pip install capstone``)。内蔵のほうは探査台の JavaScript と同じ表を
使っていて、tests/test_disasm.mjs で capstone と突き合わせています。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

REGS = [
    "$zero", "$at", "$v0", "$v1", "$a0", "$a1", "$a2", "$a3",
    "$t0", "$t1", "$t2", "$t3", "$t4", "$t5", "$t6", "$t7",
    "$s0", "$s1", "$s2", "$s3", "$s4", "$s5", "$s6", "$s7",
    "$t8", "$t9", "$k0", "$k1", "$gp", "$sp", "$fp", "$ra",
]

SPECIAL = {
    0x00: "sll", 0x02: "srl", 0x03: "sra", 0x04: "sllv", 0x06: "srlv", 0x07: "srav",
    0x08: "jr", 0x09: "jalr", 0x0A: "movz", 0x0B: "movn", 0x0C: "syscall",
    0x0D: "break", 0x0F: "sync",
    0x10: "mfhi", 0x11: "mthi", 0x12: "mflo", 0x13: "mtlo",
    0x14: "dsllv", 0x16: "dsrlv", 0x17: "dsrav",
    0x18: "mult", 0x19: "multu", 0x1A: "div", 0x1B: "divu",
    0x1C: "dmult", 0x1D: "dmultu", 0x1E: "ddiv", 0x1F: "ddivu",
    0x20: "add", 0x21: "addu", 0x22: "sub", 0x23: "subu",
    0x24: "and", 0x25: "or", 0x26: "xor", 0x27: "nor",
    0x28: "mfsa", 0x29: "mtsa",
    0x2A: "slt", 0x2B: "sltu", 0x2C: "dadd", 0x2D: "daddu", 0x2E: "dsub", 0x2F: "dsubu",
    # 例外を出す比較。コンパイラがゼロ除算の検査に必ず入れてくる
    0x30: "tge", 0x31: "tgeu", 0x32: "tlt", 0x33: "tltu", 0x34: "teq", 0x36: "tne",
    0x38: "dsll", 0x3A: "dsrl", 0x3B: "dsra", 0x3C: "dsll32", 0x3E: "dsrl32", 0x3F: "dsra32",
}
OPS = {
    0x02: "j", 0x03: "jal", 0x04: "beq", 0x05: "bne", 0x06: "blez", 0x07: "bgtz",
    0x08: "addi", 0x09: "addiu", 0x0A: "slti", 0x0B: "sltiu",
    0x0C: "andi", 0x0D: "ori", 0x0E: "xori", 0x0F: "lui",
    0x14: "beql", 0x15: "bnel", 0x16: "blezl", 0x17: "bgtzl",
    0x18: "daddi", 0x19: "daddiu", 0x1A: "ldl", 0x1B: "ldr",
    0x1E: "lq", 0x1F: "sq",                  # R5900 の 128 ビット転送
    0x20: "lb", 0x21: "lh", 0x22: "lwl", 0x23: "lw", 0x24: "lbu", 0x25: "lhu",
    0x26: "lwr", 0x27: "lwu",
    0x28: "sb", 0x29: "sh", 0x2A: "swl", 0x2B: "sw", 0x2C: "sdl", 0x2D: "sdr",
    0x2E: "swr", 0x2F: "cache",
    0x30: "ll", 0x31: "lwc1", 0x32: "lwc2", 0x33: "pref", 0x34: "lld", 0x35: "ldc1",
    0x36: "lqc2", 0x37: "ld",
    0x38: "sc", 0x39: "swc1", 0x3A: "swc2", 0x3C: "scd", 0x3D: "sdc1",
    0x3E: "sqc2", 0x3F: "sd",
}
REGIMM = {
    0x00: "bltz", 0x01: "bgez", 0x02: "bltzl", 0x03: "bgezl",
    0x08: "tgei", 0x09: "tgeiu", 0x0A: "tlti", 0x0B: "tltiu", 0x0C: "teqi", 0x0E: "tnei",
    0x10: "bltzal", 0x11: "bgezal", 0x12: "bltzall", 0x13: "bgezall",
}
MEM_OPS = {0x1A, 0x1B, 0x1E, 0x1F,
           0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27,
           0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F,
           0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
           0x38, 0x39, 0x3A, 0x3C, 0x3D, 0x3E, 0x3F}
# SPECIAL のうち、例外を出す比較 (rs, rt の 2 つだけを取る)
TRAP = {0x30, 0x31, 0x32, 0x33, 0x34, 0x36}
# REGIMM のうち、即値と比べて例外を出すもの
TRAPI = {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0E}

# R5900 の MMI (マルチメディア命令)。ここでは掛け算まわりの定番だけ
MMI = {
    0x00: "madd", 0x01: "maddu", 0x04: "plzcw",
    0x10: "mfhi1", 0x11: "mthi1", 0x12: "mflo1", 0x13: "mtlo1",
    0x18: "mult1", 0x19: "multu1", 0x1A: "div1", 0x1B: "divu1",
    0x20: "madd1", 0x21: "maddu1",
}

# 補助プロセッサとの受け渡し。rs の値が動作を選ぶ
COP_MOVE = {0: "mfc", 2: "cfc", 4: "mtc", 6: "ctc"}
# 浮動小数の演算 (fmt = single / double / word / long)
FPU_FMT = {16: "s", 17: "d", 20: "w", 21: "l"}
FPU_OPS = {0x00: "add", 0x01: "sub", 0x02: "mul", 0x03: "div",
           0x04: "sqrt", 0x05: "abs", 0x06: "mov", 0x07: "neg"}
FPU_1SRC = {0x04, 0x05, 0x06, 0x07}
FPU_CVT = {0x0C: "round.w", 0x0D: "trunc.w", 0x0E: "ceil.w", 0x0F: "floor.w",
           0x20: "cvt.s", 0x21: "cvt.d", 0x24: "cvt.w"}
FPU_COND = ["f", "un", "eq", "ueq", "olt", "ult", "ole", "ule",
            "sf", "ngle", "seq", "ngl", "lt", "nge", "le", "ngt"]

PT_LOAD = 1


def sx16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _imm(v: int) -> str:
    if v < 0:
        return f"-0x{-v:X}"
    return str(v) if v < 10 else f"0x{v:X}"


def _show(text: str) -> str:
    """1 行に収まる形にする. 改行やタブをそのまま出すと表示が崩れる."""
    return '"' + text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def decode(word: int, addr: int) -> tuple[str, str]:
    """1 命令を (ニーモニック, オペランド) に戻す.

    MIPS の命令は必ず 4 バイト固定長です。可変長の x86 と違って
    「どこから読み始めるか」で意味が変わらないので、表引きで足ります。
    """
    op = word >> 26
    rs, rt, rd = (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
    sa, funct = (word >> 6) & 31, word & 63
    off = sx16(word & 0xFFFF)
    target = ((addr + 4) & 0xF0000000) | ((word & 0x3FFFFFF) << 2)
    branch = (addr + 4 + off * 4) & 0xFFFFFFFF

    if word == 0:
        return "nop", ""

    if op == 0:
        mn = SPECIAL.get(funct)
        if mn is None:
            return ".word", f"0x{word:08X}"
        if funct == 0x08:
            return "jr", REGS[rs]
        if funct == 0x09:
            return "jalr", REGS[rs] if rd == 31 else f"{REGS[rd]}, {REGS[rs]}"
        if funct in (0x0C, 0x0D):
            return mn, f"0x{(word >> 6) & 0xFFFFF:X}"
        if funct == 0x0F:
            return mn, ""
        if funct in TRAP:
            return mn, f"{REGS[rs]}, {REGS[rt]}"
        if funct == 0x28:
            return mn, REGS[rd]
        if funct == 0x29:
            return mn, REGS[rs]
        if funct in (0x10, 0x12):
            return mn, REGS[rd]
        if funct in (0x11, 0x13):
            return mn, REGS[rs]
        if funct in (0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F):
            return mn, f"{REGS[rs]}, {REGS[rt]}"
        if funct in (0x00, 0x02, 0x03, 0x38, 0x3A, 0x3B, 0x3C, 0x3E, 0x3F):
            return mn, f"{REGS[rd]}, {REGS[rt]}, {sa}"
        if funct in (0x04, 0x06, 0x07, 0x14, 0x16, 0x17):
            return mn, f"{REGS[rd]}, {REGS[rt]}, {REGS[rs]}"
        if funct in (0x21, 0x25) and rt == 0:
            return "move", f"{REGS[rd]}, {REGS[rs]}"
        return mn, f"{REGS[rd]}, {REGS[rs]}, {REGS[rt]}"

    if op == 1:
        mn = REGIMM.get(rt)
        if mn is None:
            return ".word", f"0x{word:08X}"
        if rt in TRAPI:
            return mn, f"{REGS[rs]}, 0x{word & 0xFFFF:X}"
        return mn, f"{REGS[rs]}, 0x{branch:08X}"

    if op == 0x1C:                                  # R5900 の MMI
        mn = MMI.get(funct)
        if mn is None:
            return ".word", f"0x{word:08X}"
        if funct in (0x10, 0x12):
            return mn, REGS[rd]
        if funct in (0x11, 0x13):
            return mn, REGS[rs]
        if funct == 0x04:
            return mn, f"{REGS[rd]}, {REGS[rs]}"
        if funct in (0x18, 0x19, 0x1A, 0x1B):
            return mn, f"{REGS[rs]}, {REGS[rt]}"
        return mn, f"{REGS[rd]}, {REGS[rs]}, {REGS[rt]}"

    if op in (0x10, 0x11, 0x12):                    # COP0 / COP1 / COP2
        n = op - 0x10
        if rs in COP_MOVE:
            dst = f"$f{rd}" if n == 1 else f"${rd}"
            return COP_MOVE[rs] + str(n), f"{REGS[rt]}, {dst}"
        if rs == 8:
            return (f"bc{n}{'t' if rt & 1 else 'f'}{'l' if rt & 2 else ''}",
                    f"0x{branch:08X}")
        if op == 0x11 and rs in FPU_FMT:
            fmt = FPU_FMT[rs]
            fd, fs = sa, rd
            if funct in FPU_OPS:
                m = f"{FPU_OPS[funct]}.{fmt}"
                if funct in FPU_1SRC:
                    return m, f"$f{fd}, $f{fs}"
                return m, f"$f{fd}, $f{fs}, $f{rt}"
            if funct in FPU_CVT:
                return f"{FPU_CVT[funct]}.{fmt}", f"$f{fd}, $f{fs}"
            if funct >= 0x30:
                return f"c.{FPU_COND[funct & 15]}.{fmt}", f"$f{fs}, $f{rt}"
        return ".word", f"0x{word:08X}"

    mn = OPS.get(op)
    if mn is None:
        return ".word", f"0x{word:08X}"

    if op in (0x02, 0x03):
        return mn, f"0x{target:08X}"
    if op in (0x04, 0x05, 0x14, 0x15):
        # beq rs, $zero は beqz、beq $zero, $zero は無条件分岐 b と書くのが慣例
        if rt == 0 and op == 0x04:
            if rs == 0:
                return "b", f"0x{branch:08X}"
            return "beqz", f"{REGS[rs]}, 0x{branch:08X}"
        if rt == 0 and op == 0x05:
            return "bnez", f"{REGS[rs]}, 0x{branch:08X}"
        return mn, f"{REGS[rs]}, {REGS[rt]}, 0x{branch:08X}"
    if op in (0x06, 0x07, 0x16, 0x17):
        return mn, f"{REGS[rs]}, 0x{branch:08X}"
    if op == 0x0F:
        return mn, f"{REGS[rt]}, 0x{word & 0xFFFF:X}"
    if op in MEM_OPS:
        return mn, f"{REGS[rt]}, {_imm(off)}({REGS[rs]})"
    if op in (0x0C, 0x0D, 0x0E):
        return mn, f"{REGS[rt]}, {REGS[rs]}, 0x{word & 0xFFFF:X}"
    return mn, f"{REGS[rt]}, {REGS[rs]}, {_imm(off)}"


# --------------------------------------------------------------------------
# ELF


class Elf:
    """ELF32 リトルエンディアンの、必要なところだけ."""

    def __init__(self, data: bytes) -> None:
        if data[:4] != b"\x7fELF":
            raise scrp.ScrpError("ELF ではありません (先頭が 7F 45 4C 46 でない)")
        if data[4] != 1 or data[5] != 1:
            raise scrp.ScrpError("ELF32 リトルエンディアンではありません")
        self.data = data
        (self.type, self.machine, _ver, self.entry, phoff, shoff, self.flags,
         _ehsize, phentsize, phnum, shentsize, shnum, shstrndx) = struct.unpack_from(
            "<HHIIIIIHHHHHH", data, 16)
        self.segments = []
        for i in range(phnum):
            p = phoff + i * phentsize
            typ, off, vaddr, _paddr, filesz, memsz, flg, _al = struct.unpack_from(
                "<IIIIIIII", data, p)
            self.segments.append(dict(type=typ, offset=off, vaddr=vaddr,
                                      filesz=filesz, memsz=memsz, flags=flg))
        self.sections = []
        for i in range(shnum):
            p = shoff + i * shentsize
            nameoff, typ, flg, addr, off, size = struct.unpack_from("<IIIIII", data, p)
            self.sections.append(dict(nameoff=nameoff, type=typ, flags=flg,
                                      addr=addr, offset=off, size=size, name=""))
        if shstrndx < len(self.sections):
            base = self.sections[shstrndx]["offset"]
            for sec in self.sections:
                end = data.find(b"\x00", base + sec["nameoff"])
                sec["name"] = data[base + sec["nameoff"]:end].decode("ascii", "replace")

    def to_offset(self, vaddr: int) -> int:
        """メモリ上の番地 → ファイル内の位置. 対応が無ければ -1."""
        for s in self.segments:
            if s["type"] == PT_LOAD and s["vaddr"] <= vaddr < s["vaddr"] + s["filesz"]:
                return s["offset"] + (vaddr - s["vaddr"])
        for s in self.sections:
            if s["addr"] and s["addr"] <= vaddr < s["addr"] + s["size"] and s["type"] != 8:
                return s["offset"] + (vaddr - s["addr"])
        return -1

    def string_at(self, vaddr: int, maxlen: int = 80) -> str | None:
        """その番地に読める文字列があれば返す."""
        off = self.to_offset(vaddr)
        if off < 0 or off >= len(self.data):
            return None
        end = self.data.find(b"\x00", off, min(len(self.data), off + maxlen))
        if end < 0:
            return None
        raw = self.data[off:end]
        if len(raw) < 3:
            return None
        try:
            text = raw.decode("cp932")
        except UnicodeDecodeError:
            return None
        if sum(1 for c in text if c.isprintable()) < len(text) * 0.9:
            return None
        return text


def disasm(elf: Elf, addr: int, count: int) -> list[tuple[int, int, str, str, str]]:
    """(番地, 命令語, ニーモニック, オペランド, 注記) を並べて返す.

    注記に出るのが ``lui`` + ``addiu`` から復元したアドレスです。MIPS は
    32 ビットの値を 1 命令で作れないので、住所は必ずこの 2 命令に割れます。
    その先に文字列があれば、どの言葉をどこで使っているかが分かります。
    """
    use_capstone = _capstone()
    out = []
    hi: dict[int, int] = {}
    for _ in range(count):
        off = elf.to_offset(addr)
        if off < 0 or off + 4 > len(elf.data):
            break
        word = struct.unpack_from("<I", elf.data, off)[0]
        if use_capstone:
            mn, ops = _capstone_one(use_capstone, elf.data[off:off + 4], addr)
        else:
            mn, ops = decode(word, addr)

        note = ""
        op = word >> 26
        rs, rt = (word >> 21) & 31, (word >> 16) & 31
        if op == 0x0F:
            hi[rt] = word & 0xFFFF
        else:
            full = None
            if op in (0x09, 0x0D) and rs in hi:
                up = hi[rs] << 16
                full = (up | (word & 0xFFFF)) if op == 0x0D else (up + sx16(word & 0xFFFF))
            elif op in MEM_OPS and rs in hi:
                full = (hi[rs] << 16) + sx16(word & 0xFFFF)
            if full is not None:
                full &= 0xFFFFFFFF
                text = elf.string_at(full)
                note = f"→ 0x{full:08X}" + (f"  {_show(text)}" if text else "")
            if op == 0:
                hi.pop((word >> 11) & 31, None)
            else:
                hi.pop(rt, None)

        out.append((addr, word, mn, ops, note))
        addr = (addr + 4) & 0xFFFFFFFF
    return out


def xrefs(elf: Elf, limit: int = 4000) -> list[tuple[int, int, str]]:
    """コード全体から「文字列を指している場所」を集める."""
    regions = [s for s in elf.segments
               if s["type"] == PT_LOAD and s["filesz"] > 4 and s["flags"] & 1]
    if not regions:
        regions = [s for s in elf.segments if s["type"] == PT_LOAD and s["filesz"] > 4]
    hits: list[tuple[int, int, str]] = []
    seen: set[int] = set()
    for s in regions:
        hi: dict[int, int] = {}
        end = min(len(elf.data), s["offset"] + s["filesz"])
        for off in range(s["offset"], end - 3, 4):
            word = struct.unpack_from("<I", elf.data, off)[0]
            if not word:
                continue
            op = word >> 26
            rs, rt = (word >> 21) & 31, (word >> 16) & 31
            if op == 0x0F:
                hi[rt] = word & 0xFFFF
                continue
            full = None
            if op in (0x09, 0x0D) and rs in hi:
                up = hi[rs] << 16
                full = (up | (word & 0xFFFF)) if op == 0x0D else (up + sx16(word & 0xFFFF))
            elif op in MEM_OPS and rs in hi:
                full = (hi[rs] << 16) + sx16(word & 0xFFFF)
            if full is not None:
                full &= 0xFFFFFFFF
                text = elf.string_at(full)
                if text and full not in seen:
                    seen.add(full)
                    hits.append((s["vaddr"] + (off - s["offset"]), full, text))
                    if len(hits) >= limit:
                        return hits
            if op == 0:
                hi.pop((word >> 11) & 31, None)
            elif op not in (0x28, 0x29, 0x2B, 0x3F):
                hi.pop(rt, None)
    return hits


# --------------------------------------------------------------------------
# capstone (あれば使う)

_CS = None


def _capstone():
    global _CS
    if _CS is not None:
        return _CS or None
    try:
        import capstone

        _CS = capstone.Cs(capstone.CS_ARCH_MIPS,
                          capstone.CS_MODE_MIPS32 | capstone.CS_MODE_LITTLE_ENDIAN)
    except Exception:
        _CS = False
        return None
    return _CS


def _capstone_one(md, raw: bytes, addr: int) -> tuple[str, str]:
    for i in md.disasm(raw, addr):
        return i.mnemonic, i.op_str
    return ".word", f"0x{struct.unpack('<I', raw)[0]:08X}"


# --------------------------------------------------------------------------

ELF_TYPES = {1: "再配置可能", 2: "実行ファイル", 3: "共有ライブラリ", 4: "コアダンプ"}
MACHINES = {2: "SPARC", 3: "x86", 8: "MIPS", 40: "ARM", 62: "x86-64"}


def print_header(elf: Elf) -> None:
    print("ELF ヘッダ")
    print(f"  種類          {ELF_TYPES.get(elf.type, hex(elf.type))}")
    print(f"  命令セット    {MACHINES.get(elf.machine, hex(elf.machine))}"
          + ("  (PS2 は R5900)" if elf.machine == 8 else ""))
    print(f"  入口          0x{elf.entry:08X}")
    print()
    print("読み込み区画 (メモリのどこに載るか)")
    print(f"  {'種類':<10} {'ファイル内':>10} {'メモリ上':>12} {'大きさ':>12}  権限")
    for s in elf.segments:
        kind = "PT_LOAD" if s["type"] == PT_LOAD else f"0x{s['type']:X}"
        perm = ("r" if s["flags"] & 4 else "-") + ("w" if s["flags"] & 2 else "-") \
             + ("x" if s["flags"] & 1 else "-")
        print(f"  {kind:<10} 0x{s['offset']:08X} 0x{s['vaddr']:08X} "
              f"{s['filesz']:>12,}  {perm}")
    named = [s for s in elf.sections if s["name"] and s["size"]]
    if named:
        print()
        print("セクション")
        for s in named:
            print(f"  {s['name']:<12} 0x{s['addr']:08X} {s['size']:>10,} バイト"
                  f"  (ファイル 0x{s['offset']:X})")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="ELF32 / MIPS の本体プログラムを読む",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("path", help="ELF ファイル (PS2 なら SLPS_xxx.xx など)")
    ap.add_argument("--disasm", action="store_true", help="逆アセンブルする")
    ap.add_argument("--addr", default=None, help="読み始める番地 (既定は入口)")
    ap.add_argument("--count", type=int, default=64, help="命令数 (既定 64)")
    ap.add_argument("--xref", action="store_true",
                    help="コードが指している文字列を全部拾う")
    args = ap.parse_args()

    data = open(args.path, "rb").read()
    elf = Elf(data)

    if not args.disasm and not args.xref:
        print_header(elf)
        print()
        print(f"逆アセンブルするには --disasm を付けてください "
              f"(例: --disasm --addr 0x{elf.entry:08X} --count 64)")
        return 0

    if args.disasm:
        addr = elf.entry if args.addr is None else int(args.addr, 0)
        engine = "capstone" if _capstone() else "内蔵デコーダ"
        print(f"; 0x{addr:08X} から {args.count} 命令  ({engine})")
        for at, word, mn, ops, note in disasm(elf, addr, args.count):
            line = f"{at:08X}  {word:08X}  {mn:<9}{ops}"
            print(line + (f"   {note}" if note else ""))
        if args.xref:
            print()

    if args.xref:
        hits = xrefs(elf)
        print(f"; コードが指している文字列  {len(hits)} 件")
        for frm, to, text in hits:
            print(f"{frm:08X}  → 0x{to:08X}  {text!r}")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
