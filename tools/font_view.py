#!/usr/bin/env python3
"""フォント画像 (グリフ) を覗く. Crystal Tile 2 でやることの最小版.

work/FONT.BIN は 16x16 ドット・1bpp のグリフを並べただけのファイルです。
1 グリフ = 16 行 x 2 バイト = 32 バイト。グリフ番号がそのまま文字テーブルの
並び順に対応します。

    # 何番目のグリフか分からない状態で、絵として確かめる
    python3 tools/font_view.py work/FONT.BIN --ascii 0-9

    # 一覧を PNG で書き出して目で探す (Pillow が必要)
    python3 tools/font_view.py work/FONT.BIN --png work/font_sheet.png

    # 答え合わせ: 文字 → グリフ番号
    python3 tools/font_view.py work/FONT.BIN --find あ --chars data/font_chars.txt

フォントが画像で持たれている場合、文字コードは「グリフの並び順の番号」でしか
ありません。つまり **フォントシートの並びを読むこと自体が文字テーブルの復元**
になります。これが実際のローカライズでいちばん最初にやる作業です。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

GLYPH_SIZE = 16
GLYPH_BYTES = GLYPH_SIZE * GLYPH_SIZE // 8


def load_glyphs(path: str) -> list[bytes]:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) % GLYPH_BYTES:
        print(f"注意: {len(data)} バイトは {GLYPH_BYTES} で割り切れません "
              f"(端数 {len(data) % GLYPH_BYTES} バイトは無視します)", file=sys.stderr)
    return [data[i:i + GLYPH_BYTES] for i in range(0, len(data) - GLYPH_BYTES + 1, GLYPH_BYTES)]


def load_chars(path: str) -> list[str]:
    chars = []
    with scrp.open_text(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            chars.append(line[0])
    return chars


def glyph_rows(glyph: bytes) -> list[list[int]]:
    rows = []
    for r in range(GLYPH_SIZE):
        bits = int.from_bytes(glyph[r * 2:(r + 1) * 2], "big")
        rows.append([(bits >> (GLYPH_SIZE - 1 - c)) & 1 for c in range(GLYPH_SIZE)])
    return rows


def print_ascii(glyphs: list[bytes], index: int, label: str = "") -> None:
    print(f"グリフ {index}" + (f"  ({label})" if label else "")
          + f"   オフセット 0x{index * GLYPH_BYTES:X}")
    for row in glyph_rows(glyphs[index]):
        print("  " + "".join("██" if bit else "・" for bit in row))
    print()


def print_ascii_grid(glyphs: list[bytes], indexes: list[int], per_row: int,
                     chars: list[str]) -> None:
    """指定した数だけ横に並べて出す (課題 4 の「並びの規則」はこれで見える).

    1 つずつ縦に流すと、21 文字で 400 行を超えて画面から消える。並びの規則
    (小書き→大, 清音→濁音) は**隣り合わせにして初めて**見える。実際に課題 4 を
    解いたとき、横に並べるまで濁点の位置に気づけなかった (#85)。
    """
    for base in range(0, len(indexes), per_row):
        group = indexes[base:base + per_row]
        heads = []
        for index in group:
            label = chars[index] if index < len(chars) else ""
            heads.append((f"グリフ {index}" + (f" ({label})" if label else "")).ljust(GLYPH_SIZE * 2))
        print("  " + "  ".join(heads).rstrip())
        rendered = [glyph_rows(glyphs[index]) for index in group]
        for r in range(GLYPH_SIZE):
            line = "  ".join("".join("██" if bit else "・" for bit in rows[r]) for rows in rendered)
            print("  " + line)
        print()


def parse_range(spec: str, count: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    bad = [i for i in out if not 0 <= i < count]
    if bad:
        raise SystemExit(f"グリフ番号が範囲外です: {bad} (0〜{count - 1})")
    return out


def write_png(glyphs: list[bytes], path: str, cols: int, scale: int) -> None:
    try:
        from PIL import Image
    except ImportError:
        raise scrp.optional_module_error(
            "PIL", "--png の代わりに --ascii 0-20 を使うと、同じ中身を文字で見られます") from None

    rows = (len(glyphs) + cols - 1) // cols
    cell = GLYPH_SIZE + 1
    img = Image.new("L", (cols * cell * scale, rows * cell * scale), 64)
    px = img.load()
    for index, glyph in enumerate(glyphs):
        gx = (index % cols) * cell
        gy = (index // cols) * cell
        for r, bits in enumerate(glyph_rows(glyph)):
            for c, bit in enumerate(bits):
                value = 255 if bit else 0
                for sy in range(scale):
                    for sx in range(scale):
                        px[(gx + c) * scale + sx, (gy + r) * scale + sy] = value
    img.save(path)


def main() -> int:
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(errors="replace")     # Windows の cp932 コンソール/リダイレクトで落ちない
    ap = argparse.ArgumentParser(description="1bpp 16x16 フォントを表示する")
    ap.add_argument("binary", help="FONT.BIN")
    ap.add_argument("--ascii", help="アスキーアートで表示するグリフ番号 (例: 0-9,20)")
    ap.add_argument("--png", help="一覧を PNG で書き出す")
    ap.add_argument("--cols", type=int, default=24, help="PNG の 1 行あたりのグリフ数")
    ap.add_argument("--across", type=int, metavar="N",
                    help="--ascii を N 個ずつ横に並べる (並びの規則はこれで見える)")
    ap.add_argument("--scale", type=int, default=2, help="PNG の拡大率")
    ap.add_argument("--chars", help="グリフ順の文字一覧 (答え合わせ用)")
    ap.add_argument("--find", help="この文字のグリフ番号を調べる (--chars が必要)")
    args = ap.parse_args()

    glyphs = load_glyphs(args.binary)
    chars = load_chars(args.chars) if args.chars else []
    print(f"{args.binary}: {len(glyphs)} グリフ ({GLYPH_SIZE}x{GLYPH_SIZE} 1bpp, "
          f"1 グリフ {GLYPH_BYTES} バイト)")
    if chars and len(chars) != len(glyphs):
        print(f"注意: 文字一覧は {len(chars)} 件でグリフ数と一致しません", file=sys.stderr)
    print()

    if args.find:
        if not chars:
            raise SystemExit("--find には --chars が必要です")
        ch = args.find[0]
        if ch not in chars:
            print(f"{ch!r} はこのフォントに入っていません")
            return 1
        index = chars.index(ch)
        print_ascii(glyphs, index, ch)
        print(f"{ch!r} = グリフ {index} (0x{index:X})")
        return 0

    if args.ascii:
        indexes = parse_range(args.ascii, len(glyphs))
        if args.across and args.across > 1:
            print_ascii_grid(glyphs, indexes, args.across, chars)
        else:
            for index in indexes:
                print_ascii(glyphs, index, chars[index] if index < len(chars) else "")
        return 0

    if args.png:
        write_png(glyphs, args.png, args.cols, args.scale)
        print(f"書き出しました: {args.png} ({args.cols} 列)")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
