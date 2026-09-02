#!/usr/bin/env python3
"""LZSS 伸張・圧縮ツール — 圧縮の壁を越えるための道具.

    # ファイル全体を LZSS として伸張する
    python3 tools/lzss.py decompress work/PACKED.BIN -o work/PLAIN.BIN

    # ファイルの中から「ここから伸張したらテキストになる」場所を探す
    python3 tools/lzss.py scan work/BOKU2.IMG

    # 自分でデータを圧縮して round-trip を確かめる (学習・テスト用)
    python3 tools/lzss.py compress work/PLAIN.BIN -o work/PACKED.BIN

なぜこれが要るか
----------------
僕の夏休み2 のテキストは圧縮されている (英語化パッチの作者が「独自の圧縮を
書く必要があった」と明言)。だから ``BOKU2.IMG`` を生のまま日本語で検索しても
セリフは出てこない。圧縮を解いて初めて中身が読める。

ここで実装しているのは、日本のコンシューマ機でいちばん普及している
**奥村晴彦版 LZSS** (LZSS.C, 1989) とその近縁です。多くの PS2 タイトルが
これか、これを少しいじった変種を使っている。ゲーム独自の変種だと当たらない
こともあるが、まず定番を試すのが定石。

  * リングバッファ 4096 バイト、初期値は空白 (0x20)
  * 一致の最短長 3、最長 18 (4 ビットで長さ-3 を表す)
  * フラグは 1 バイト 8 個ぶん、下位ビットから。1=そのまま 1 バイト、
    0=(12 ビットの位置 + 4 ビットの長さ) の組

うまくいかないとき
------------------
これで伸張できないなら、そのゲームは独自変種を使っている。その場合は
本体プログラム (ELF) の伸張ルーチンを逆アセンブルして読むか、**PCSX2 で
動かして展開後のメモリを見る**ほうが早い (docs/08 参照)。メモリ上のデータは
既に伸張済みなので、そもそも圧縮を解く必要がない。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

RING = 4096          # リングバッファの大きさ
THRESHOLD = 2        # これより長い一致だけを (位置, 長さ) の組にする
MATCH_MAX = THRESHOLD + 16   # = 18。長さは 4 ビット (0..15) + THRESHOLD + 1
FILL = 0x20          # リングバッファの初期値 (空白)


def decompress(data: bytes, start: int = 0, out_limit: int | None = None,
               fill: int = FILL) -> bytes:
    """奥村版 LZSS で伸張する.

    ``start`` から読み始め、``out_limit`` バイト出したら止める (None なら入力を
    使い切るまで)。壊れた入力でも例外を出さず、そこまでを返す。
    """
    out = bytearray()
    ring = bytearray([fill]) * RING
    r = RING - MATCH_MAX          # 書き込み位置の初期値 (奥村版の慣例)
    i = start
    n = len(data)
    flags = 0
    while i < n:
        flags >>= 1
        if not (flags & 0x100):
            # 次の 8 個ぶんのフラグを読む。上位に 0xFF を置いて 8 回で尽きるようにする
            flags = data[i] | 0xFF00
            i += 1
            if i >= n:
                break
        if flags & 1:
            # そのまま 1 バイト
            c = data[i]
            i += 1
            out.append(c)
            ring[r] = c
            r = (r + 1) & (RING - 1)
        else:
            # (位置, 長さ) の組。位置 12 ビット + 長さ 4 ビット
            if i + 1 >= n:
                break
            b0 = data[i]
            b1 = data[i + 1]
            i += 2
            pos = b0 | ((b1 & 0xF0) << 4)
            length = (b1 & 0x0F) + THRESHOLD + 1
            for k in range(length):
                c = ring[(pos + k) & (RING - 1)]
                out.append(c)
                ring[r] = c
                r = (r + 1) & (RING - 1)
        if out_limit is not None and len(out) >= out_limit:
            return bytes(out[:out_limit])
    return bytes(out)


def compress(data: bytes, fill: int = FILL) -> bytes:
    """同じ形式で圧縮する. 主に round-trip の検証用 (探索は素朴).

    速さより正しさを優先。リングバッファと素朴な最長一致探索で、
    decompress とちょうど逆になることをテストで固定する。
    """
    out = bytearray()
    ring = bytearray([fill]) * RING
    r = RING - MATCH_MAX
    i = 0
    n = len(data)
    flag_pos = -1
    flag_bit = 0
    while i < n:
        if flag_bit == 0:
            out.append(0)          # フラグ用の場所を空けておく
            flag_pos = len(out) - 1
            flag_bit = 1
        # いまの位置から始まる最長一致をリングバッファの中で探す
        best_len = 0
        best_pos = 0
        maxlen = min(MATCH_MAX, n - i)
        if maxlen > THRESHOLD:
            for p in range(RING):
                length = 0
                while (length < maxlen
                       and ring[(p + length) & (RING - 1)] == data[i + length]):
                    length += 1
                if length > best_len:
                    best_len = length
                    best_pos = p
                    if length >= maxlen:
                        break
        if best_len > THRESHOLD:
            # (位置, 長さ) の組。フラグビットは 0 のまま
            b1 = ((best_pos >> 4) & 0xF0) | ((best_len - THRESHOLD - 1) & 0x0F)
            out.append(best_pos & 0xFF)
            out.append(b1)
            for k in range(best_len):
                ring[r] = data[i + k]
                r = (r + 1) & (RING - 1)
            i += best_len
        else:
            out[flag_pos] |= flag_bit    # このビットは「そのまま 1 バイト」
            out.append(data[i])
            ring[r] = data[i]
            r = (r + 1) & (RING - 1)
            i += 1
        flag_bit = (flag_bit << 1) & 0xFF
    return bytes(out)


def looks_like_text(b: bytes) -> float:
    """伸張結果が「日本語テキストらしい」度合いを 0..1 で返す.

    Shift-JIS として素直に読める割合を見る。scan の当たり判定に使う。
    """
    if not b:
        return 0.0
    # 種類が少なすぎる出力 (空白だけ・0 だけ) はテキストではない。
    # ゼロ埋めを LZSS として読むと空白の羅列になり、素朴に見ると 1.0 になるので弾く
    if len(set(b)) < 8:
        return 0.0
    ok = i = 0
    n = len(b)
    while i < n:
        c = b[i]
        if 0x20 <= c < 0x7F or c in (0x0A, 0x0D):
            ok += 1
            i += 1
        elif (0x81 <= c <= 0x9F or 0xE0 <= c <= 0xEF) and i + 1 < n \
                and 0x40 <= b[i + 1] <= 0xFC and b[i + 1] != 0x7F:
            ok += 2
            i += 2
        else:
            i += 1
    return ok / n


def scan(data: bytes, step: int = 0x800, min_out: int = 256):
    """ファイルのあちこちを LZSS として伸張してみて、テキストになる場所を探す.

    圧縮ブロックの先頭が分からないとき用。step ごとに試し、伸張結果が
    テキストらしければ当たり候補として返す。
    """
    hits = []
    for off in range(0, max(1, len(data) - 4), step):
        out = decompress(data, off, out_limit=4096)
        if len(out) < min_out:
            continue
        score = looks_like_text(out)
        if score >= 0.85:
            hits.append((off, len(out), score, out[:48]))
    return hits


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="LZSS (奥村版) の伸張・圧縮・探索",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("mode", choices=["decompress", "compress", "scan"])
    ap.add_argument("path")
    ap.add_argument("-o", "--out", help="出力先 (decompress / compress)")
    ap.add_argument("--start", default="0", help="伸張の開始位置 (16進可)")
    ap.add_argument("--fill", default="0x20", help="リングバッファ初期値 (既定 0x20)")
    args = ap.parse_args()

    data = _read(args.path)
    start = int(args.start, 0)
    fill = int(args.fill, 0)

    if args.mode == "decompress":
        out = decompress(data, start, fill=fill)
        if args.out:
            with open(args.out, "wb") as fh:
                fh.write(out)
            print(f"{args.out} に {len(out):,} バイト書きました "
                  f"(テキストらしさ {looks_like_text(out):.2f})")
        else:
            sys.stdout.buffer.write(out)
        return 0

    if args.mode == "compress":
        out = compress(data, fill=fill)
        if args.out:
            with open(args.out, "wb") as fh:
                fh.write(out)
            ratio = len(out) / max(1, len(data))
            print(f"{args.out} に {len(out):,} バイト書きました (圧縮率 {ratio:.0%})")
        else:
            sys.stdout.buffer.write(out)
        return 0

    # scan
    hits = scan(data)
    if not hits:
        print("LZSS として伸張してテキストになる場所は見つかりませんでした。")
        print("独自変種の可能性があります。PCSX2 で展開後のメモリを見るのが近道です"
              " (docs/08 参照)。")
        return 0
    print(f"当たり候補 {len(hits)} 件:")
    for off, length, score, head in hits[:40]:
        try:
            preview = head.decode("cp932", "replace")
        except Exception:
            preview = repr(head)
        print(f"  0x{off:08X}  {length:>6} バイト  らしさ {score:.2f}  {preview!r}")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
