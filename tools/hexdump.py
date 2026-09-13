#!/usr/bin/env python3
"""バイナリエディタ (HxD など) の画面を自分で書いたもの.

    # ヘッダとポインタテーブルを構造として読む
    python3 tools/hexdump.py work/MSG_ENC.BIN --struct

    # 3 番目のメッセージのあたりを 16 進で眺める
    python3 tools/hexdump.py work/MSG_ENC.BIN --table answers/custom.tbl \\
        --offset 0x10C --length 64

右側の文字欄は、指定した文字コードで解釈した結果です。--table を付けないと
Shift-JIS として読みます。制御コードは «BR» のように囲んで表示します。
HxD で「Shift-JIS 表示に切り替える」「テーブルを読み込む」のがまさにこれです。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp


def parse_int(text: str) -> int:
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def decode_window(data: bytes, start: int, end: int, codec: scrp.Codec) -> dict[int, str]:
    """{開始オフセット: 表示文字列} を作る.

    先に範囲全体を通して解釈しておくのがポイントです。1 行ずつ独立に解釈すると、
    行の境界にまたがった 2 バイト文字が別の文字に化けて読みにくくなります。
    """
    out: dict[int, str] = {}
    i = start
    while i < end:
        b = data[i]
        if b == scrp.END:
            out[i] = "«END»"
            i += 1
            continue
        if b in scrp.CONTROL_CODES:
            tag, argc = scrp.CONTROL_CODES[b]
            out[i] = f"«{tag}»"
            i += 1 + argc
            continue
        try:
            ch, size = codec.decode_char(data, i)
        except scrp.ScrpError:
            out[i] = "."
            i += 1
            continue
        out[i] = ch
        i += size
    return out


def text_pane(decoded: dict[int, str], base: int, stop: int) -> str:
    return "".join(decoded[i] for i in range(base, stop) if i in decoded)


def body_ranges(path: str) -> list[tuple[int, int]] | None:
    """SCRP なら本文 (メッセージ) の範囲を返す。そうでなければ None (#129).

    見出しとポインタ表は**文字ではない**ので、そこに出た「表に無いバイト」を
    文字表に足してはいけない。範囲が分かるときだけ分けて数える。
    """
    try:
        archive = scrp.read_archive(path)
    except Exception:
        return None
    return [(p, p + len(archive.raw_block(i))) for i, p in enumerate(archive.pointers)]


def unknown_report(data: bytes, decoded: dict[int, str], table_path: str,
                   bodies: list[tuple[int, int]] | None = None) -> list[str]:
    """表に無いバイトを数えて並べる (課題 3 の「手で足す」の材料).

    文字欄では表に無いバイトが「.」になるだけなので、**どの値が足りないのか**が
    読み取れなかった。点を目で数えて 16 進欄と突き合わせるしかない (#84)。

    もう 1 つ、黙って間違える所がある。2 バイトで 1 文字のコードは、前半が表に
    無いと**後半だけが別の 1 文字として読まれる**。「旅」(E0 3E) が「.ま」になり、
    それらしい日本語に見えてしまう。だから「次の文字は当てにできない」と言う。

    そして 3 つ目 (#129)。**見出しとポインタ表は文字ではない**。既定の窓
    (先頭から 256 バイト) は `work/MSG_ENC.BIN` ではほぼ全部がそこなので、
    **完成した答えの表を渡しても「9 種類 / 98 個 足りない」と出て**、
    そのとおりに足すと文字でない値が 8 つ表に混ざる。`bodies` が分かるときは
    本文の中だけを「足すもの」として数え、外は数だけ報せる。
    """
    from collections import Counter

    def in_body(i: int) -> bool:
        return bodies is None or any(a <= i < b for a, b in bodies)

    dots = [i for i, ch in decoded.items() if ch == "."]
    outside = [i for i in dots if not in_body(i)]
    decoded = {i: ch for i, ch in decoded.items() if ch != "." or in_body(i)}
    tally = Counter(data[i] for i in dots if in_body(i))
    if not tally:
        if outside:
            return ["", f"表に無いバイトは本文の中には 0 個です "
                        f"(見出しとポインタ表に {len(outside)} 個ありますが、"
                        f"そこは文字ではないので足しません)"]
        return []
    total = sum(tally.values())
    where_note = "本文の中に " if bodies is not None else ""
    lines = [f"\n表に無いバイトが {where_note}{len(tally)} 種類 / 計 {total} 個ありました:"]
    leads = []
    for value, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
        where = [i for i, ch in decoded.items() if ch == "." and data[i] == value]
        spots = "、".join(f"0x{i:04X}" for i in sorted(where)[:4])
        more = " ほか" if len(where) > 4 else ""
        # 続くバイトが毎回違うなら、それは 1 文字ではなく **2 バイトの前半** の疑いが濃い。
        # そのときは「この値を 1 つ足す」では済まず、組の数だけ 4 桁で足すことになる。
        # 数えて言うだけで、決めつけない (#120)
        pairs = sorted({data[i + 1] for i in where if i + 1 < len(data)})
        note = ""
        if count >= 3 and len(pairs) == count:
            note = f" — 続くバイトが毎回違う ({len(pairs)} 通り)"
            leads.append((value, pairs))
        lines.append(f"  0x{value:02X}  {count} 回  ({spots}{more}){note}")
    # ここで実在の対応 (B2=。 など) を例に出すと課題 3 の答えを漏らすので、書き方だけ言う
    lines.append(f"  {table_path} に「16 進=文字」の行を足します。1 バイトなら 2 桁、"
                 "2 バイトで 1 文字なら 4 桁で書きます (docs/01)")
    lines.append("  表に無いバイトのすぐ次の文字は当てにできません。2 バイトで 1 文字なら、"
                 "後半だけが別の字として読まれます")
    for value, pairs in leads:
        # **全部**挙げる。「N 行です」と言いながら例を 4 つしか出さないと、
        # 言われたとおりに足しても読めない字が残る (最初そう書いて自分で踏んだ #120)。
        # 数は「表に無い出現数」が上限なので、増えすぎる心配はない
        shown = " ".join(f"{value:02X}{p:02X}" for p in pairs)
        lines.append(f"  0x{value:02X} が 2 バイトの前半なら、足すのは 1 行ではなく "
                     f"**{len(pairs)} 行** です (4 桁で: {shown})")
    if outside:
        lines.append(f"  見出しとポインタ表にも {len(outside)} 個ありますが、"
                     "そこは文字ではないので**上には数えていません** (足すと表が汚れます)")
    return lines


def show_struct(path: str) -> None:
    archive = scrp.read_archive(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    enc = scrp.ENCODING_NAMES.get(archive.encoding_id, "?")
    print(f"{path}  {len(raw):,} バイト")
    print()
    print(f"  0x00  マジック      {raw[:4].decode('ascii', 'replace')!r}")
    print(f"  0x04  version       {archive.version}")
    print(f"  0x08  count         {archive.count}")
    print(f"  0x0C  encoding      {archive.encoding_id} ({enc})")
    print(f"  0x10  ポインタテーブル ({archive.count} x 4 = {archive.count * 4} バイト)")
    print()
    print("   id   ポインタ   生バイト (LE)    本文サイズ")
    for i, ptr in enumerate(archive.pointers):
        raw_le = struct.pack("<I", ptr).hex(" ").upper()
        size = len(archive.raw_block(i))
        shared = archive.pointers.index(ptr) != i
        note = f"  ← id {archive.pointers.index(ptr)} と同じ場所" if shared else ""
        print(f"  {i:3}   0x{ptr:05X}   {raw_le}      {size:4} バイト{note}")


def main() -> int:
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(errors="replace")     # Windows の cp932 コンソール/リダイレクトで落ちない
    ap = argparse.ArgumentParser(description="16 進ダンプ / SCRP 構造の表示")
    ap.add_argument("binary")
    ap.add_argument("--table", help="独自文字コードの .tbl")
    ap.add_argument("--offset", default="0", help="開始位置 (0x 付きで 16 進)")
    ap.add_argument("--length", default="256", help="表示バイト数")
    ap.add_argument("--width", type=int, default=16, help="1 行のバイト数")
    ap.add_argument("--struct", action="store_true", help="SCRP のヘッダとポインタ表を表示")
    ap.add_argument("--message", type=int, help="指定 id のメッセージの範囲だけ表示")
    args = ap.parse_args()

    if args.struct:
        show_struct(args.binary)
        return 0

    with open(args.binary, "rb") as fh:
        data = fh.read()

    codec: scrp.Codec
    if args.table:
        codec = scrp.load_table(args.table)
    else:
        codec = scrp.SjisCodec()

    start = parse_int(args.offset)
    length = parse_int(args.length)
    if args.message is not None:
        archive = scrp.read_archive(args.binary)
        start = archive.pointers[args.message]
        length = len(archive.raw_block(args.message))
        print(f"id {args.message}: 0x{start:X} から {length} バイト")
        print()
    end = min(len(data), start + length)

    decoded = decode_window(data, start, end, codec)
    for base in range(start, end, args.width):
        stop = min(end, base + args.width)
        chunk = data[base:stop]
        hex_part = " ".join(f"{b:02X}" for b in chunk).ljust(args.width * 3 - 1)
        print(f"{base:08X}  {hex_part}  {text_pane(decoded, base, stop)}")
    if args.table:
        for line in unknown_report(data, decoded, args.table, body_ranges(args.binary)):
            print(line)
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
