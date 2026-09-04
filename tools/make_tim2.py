#!/usr/bin/env python3
"""TIM2 (PS2 の標準画像形式) を組み立てる。

練習用のフォント画像 (17 文字 × N 行、1 文字 23 ドット) と、各画素形式の小さな
試験画像を作る。ブラウザ側の読み取り (web/app.js の tim2 ブロック) の答え合わせ用。

    python3 tools/make_tim2.py            # work/FONT.TM2 と work/FONT.TMS を作る
    python3 tools/make_tim2.py --json     # 試験用の画像とその画素を JSON で出す

形式は Sony の TIM2 仕様のとおり:
  ファイル見出し 16 バイト ("TIM2", 版, 形式, 絵の数, 予備 8)
  絵の見出し 48 バイト (絵全体の長さ, パレットの長さ, 画素の長さ, 見出しの長さ,
  パレットの色数, 形式, ミップマップ数, パレットの種類, 画素の種類, 幅, 高さ, GS レジスタ 24)
  画素 → パレット
8bit 索引のパレットは、パレットの種類の最上位ビットが 0 なら CSM1 の並び
(32 色ごとに 8〜15 と 16〜23 が入れ替わる) で格納する。
"""

from __future__ import annotations

import argparse
import json
import os
import struct

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def csm1_index(i: int) -> int:
    m = i & 0x18
    if m == 0x08:
        return i + 8
    if m == 0x10:
        return i - 8
    return i


def build_tim2(width: int, height: int, image_type: int, pixels, clut=None,
               clut_type: int = 0, linear_clut: bool = False, fmt: int = 0) -> bytes:
    """pixels: 画素の種類に応じた値の並び (索引か、(r,g,b,a) の組)。clut: [(r,g,b,a)]."""
    if image_type == 5:
        data = bytes(pixels)
    elif image_type == 4:
        out = bytearray()
        for i in range(0, len(pixels), 2):
            lo = pixels[i] & 15
            hi = (pixels[i + 1] & 15) if i + 1 < len(pixels) else 0
            out.append(lo | (hi << 4))
        data = bytes(out)
    elif image_type == 3:
        data = b"".join(struct.pack("<4B", r, g, b, a // 2) for r, g, b, a in pixels)
    elif image_type == 2:
        data = b"".join(struct.pack("<3B", r, g, b) for r, g, b, _ in pixels)
    elif image_type == 1:
        data = b"".join(struct.pack("<H", (r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10) | (0x8000 if a else 0))
                        for r, g, b, a in pixels)
    else:
        raise ValueError("unsupported image type")

    clut_bytes = b""
    n_colors = 0
    if clut is not None:
        n_colors = len(clut)
        stored = list(clut)
        if image_type == 5 and not linear_clut and n_colors >= 32:
            # 格納順 j には、論理色 csm1_index(j) を置く (読む側が csm1_index で戻す)
            stored = [clut[csm1_index(j)] for j in range(n_colors)]
        kind = clut_type & 0x3F
        if kind == 3:
            clut_bytes = b"".join(struct.pack("<4B", r, g, b, a // 2) for r, g, b, a in stored)
        elif kind == 2:
            clut_bytes = b"".join(struct.pack("<3B", r, g, b) for r, g, b, _ in stored)
        elif kind == 1:
            clut_bytes = b"".join(struct.pack("<H", (r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10) | (0x8000 if a else 0))
                                  for r, g, b, a in stored)
        else:
            raise ValueError("unsupported clut type")
        if linear_clut:
            clut_type |= 0x80

    header_size = 48
    total = header_size + len(data) + len(clut_bytes)
    pic = struct.pack("<IIIHHBBBBHH", total, len(clut_bytes), len(data), header_size, n_colors,
                      0, 1, clut_type, image_type, width, height)
    pic += b"\0" * 24                     # GS レジスタ (表示には使わない)
    head = b"TIM2" + struct.pack("<BBH", 4, fmt, 1) + b"\0" * 8
    if fmt:
        head += b"\0" * (0x80 - len(head))
    return head + pic + data + clut_bytes


def font_sheet(rows: int = 4, cols: int = 17, cell: int = 23) -> tuple[bytes, list[int]]:
    """番号ごとに違う模様を描いたフォント画像 (8bit 索引, 256 色)。返り値は (TIM2, 画素の並び)."""
    w, h = cols * cell, rows * cell
    px = [0] * (w * h)
    for n in range(rows * cols):
        cx, cy = (n % cols) * cell, (n // cols) * cell
        # 枠 + 番号に応じた縞 (色 1 + n%200)
        color = 1 + (n % 200)
        for y in range(cell):
            for x in range(cell):
                on = (x == 0 or y == 0 or x == cell - 1 or y == cell - 1)
                on = on or ((x + y) % (2 + n % 5) == 0 and 3 < x < cell - 4 and 3 < y < cell - 4)
                if on:
                    px[(cy + y) * w + cx + x] = color
    clut = [(0, 0, 0, 0)] + [((i * 37) % 256, (i * 91) % 256, (i * 53) % 256, 255) for i in range(1, 256)]
    return build_tim2(w, h, 5, px, clut, clut_type=3), px


def test_images() -> list[dict]:
    """各画素形式の 4×2 の試験画像と、期待する RGBA (透明度は表示用に伸ばしたもの)."""
    rgba = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 255, 128),
            (16, 32, 48, 255), (200, 100, 50, 255), (0, 0, 0, 0), (128, 128, 128, 255)]
    out = []
    # 32bit 直接色
    out.append({"name": "32bit", "w": 4, "h": 2, "tim2": build_tim2(4, 2, 3, rgba).hex(),
                "rgba": [list(c) for c in rgba]})
    # 24bit
    out.append({"name": "24bit", "w": 4, "h": 2, "tim2": build_tim2(4, 2, 2, rgba).hex(),
                "rgba": [[r, g, b, 255] for r, g, b, _ in rgba]})
    # 16bit (5bit に丸まる)
    q = lambda v: round((v >> 3) * 255 / 31)
    out.append({"name": "16bit", "w": 4, "h": 2, "tim2": build_tim2(4, 2, 1, rgba).hex(),
                "rgba": [[q(r), q(g), q(b), 255 if a else 0] for r, g, b, a in rgba]})
    # 8bit 索引 (CSM1 並び) : 索引 9 と 17 が入れ替わりの影響を受ける位置
    clut = [((i * 7) % 256, (i * 13) % 256, (i * 29) % 256, 255) for i in range(256)]
    idx = [0, 1, 9, 17, 24, 31, 100, 255]
    out.append({"name": "8bit-csm1", "w": 4, "h": 2, "tim2": build_tim2(4, 2, 5, idx, clut, clut_type=3).hex(),
                "rgba": [list(clut[i]) for i in idx]})
    out.append({"name": "8bit-linear", "w": 4, "h": 2,
                "tim2": build_tim2(4, 2, 5, idx, clut, clut_type=3, linear_clut=True).hex(),
                "rgba": [list(clut[i]) for i in idx]})
    # 4bit 索引 (16 色)、128 バイト揃えの見出し
    clut16 = [((i * 16) % 256, 255 - i * 16, i * 8, 255) for i in range(16)]
    idx4 = [0, 1, 2, 15, 7, 8, 3, 4]
    out.append({"name": "4bit-fmt1", "w": 4, "h": 2,
                "tim2": build_tim2(4, 2, 4, idx4, clut16, clut_type=3, fmt=1).hex(),
                "rgba": [list(clut16[i]) for i in idx4]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="試験画像を JSON で出す")
    ap.add_argument("--out", default=os.path.join(REPO, "work"))
    args = ap.parse_args()
    if args.json:
        print(json.dumps(test_images()))
        return
    os.makedirs(args.out, exist_ok=True)
    tim2, _ = font_sheet()
    with open(os.path.join(args.out, "FONT.TM2"), "wb") as f:
        f.write(tim2)
    # .tms 風: 0x80 バイトの前置き + TIM2 (僕の夏休み 2 の bk_font.tms と同じ置き方)
    with open(os.path.join(args.out, "FONT.TMS"), "wb") as f:
        f.write(b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + tim2)
    print(f"{os.path.join(args.out, 'FONT.TM2')}: {len(tim2):,} バイト (17 × 4 文字, 23 ドット)")


if __name__ == "__main__":
    main()
