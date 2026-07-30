#!/usr/bin/env python3
"""相対検索 (Monkey-Moore と同じ考え方) を自分で書いたもの.

文字コードが分からないファイルから日本語を見つける方法です。

考え方
------
ほとんどのゲームは、フォントを「あいうえお…」の順に並べます。すると
文字コードも連番になるので、

    「こんにちは」 → こ→ん の差は +26、ん→に の差は -35、…

という **差の並び** は、コードの起点がどこであっても変わりません。だから
「差の並びが一致するバイト列」を探せば、起点が未知でも文章が見つかります。
見つかった位置の先頭バイトから逆算すれば、そのまま文字テーブルになります。

    # 独自コードのファイルから、答えを見ずに「こんなところ」を探す
    python3 tools/relative_search.py work/MSG_ENC.BIN --search こんなところ

うまくいかないときは
--------------------
* 濁点・半濁点 (が, ぱ) や小さい字 (っ, ゃ) は、フォントの並びから外されて
  いたり別の場所に置かれていたりします。まずは清音だけの語で試すこと。
* 漢字・記号は連番になっていないので混ぜないこと。
* それでも出ない場合は --order nosmall (小書き文字を詰めた並び) や
  --width 2 (2 バイトコード) を試します。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

HIRAGANA_FULL = [chr(c) for c in range(0x3041, 0x3094)]
SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎ")
HIRAGANA_NOSMALL = [c for c in HIRAGANA_FULL if c not in SMALL_KANA]
KATAKANA_FULL = [chr(c) for c in range(0x30A1, 0x30F4)]


def build_order(name: str, charset_file: str | None) -> list[str]:
    """検索に使う「フォントの並び」の仮説を返す."""
    if charset_file:
        with open(charset_file, encoding="utf-8") as fh:
            return [line.rstrip("\n") for line in fh
                    if line.strip() and not line.startswith("#")]
    if name == "unicode":
        return HIRAGANA_FULL + KATAKANA_FULL
    if name == "nosmall":
        return HIRAGANA_NOSMALL + [c for c in KATAKANA_FULL if c not in set("ァィゥェォッャュョヮ")]
    raise scrp.ScrpError(f"未知の並び順: {name}")


def deltas(query: str, order: list[str]) -> list[int]:
    index = {ch: i for i, ch in enumerate(order)}
    unknown = [ch for ch in query if ch not in index]
    if unknown:
        raise scrp.ScrpError(
            f"仮定した並びに無い文字: {''.join(unknown)}\n"
            "  濁点付き・小書き文字・漢字・記号は相対検索には使えません。"
        )
    positions = [index[ch] for ch in query]
    return [b - a for a, b in zip(positions, positions[1:])]


def read_code(data: bytes, i: int, width: int, endian: str) -> int:
    if width == 1:
        return data[i]
    return int.from_bytes(data[i : i + 2], "little" if endian == "le" else "big")


def search(data: bytes, query: str, order: list[str], width: int, endian: str,
           modulus: int) -> list[tuple[int, int]]:
    """(オフセット, 先頭文字のコード) の一覧を返す."""
    diffs = deltas(query, order)
    span = width * len(query)
    hits = []
    for i in range(0, len(data) - span + 1):
        ok = True
        prev = read_code(data, i, width, endian)
        for k, delta in enumerate(diffs):
            cur = read_code(data, i + width * (k + 1), width, endian)
            if (cur - prev) % modulus != delta % modulus:
                ok = False
                break
            prev = cur
        if ok:
            hits.append((i, read_code(data, i, width, endian)))
    return hits


def derived_mapping(base_code: int, query: str, order: list[str], width: int,
                    endian: str, modulus: int) -> dict[bytes, str]:
    """ヒットした位置から逆算した文字テーブル (仮説) を組み立てる."""
    index = {ch: i for i, ch in enumerate(order)}
    origin = (base_code - index[query[0]]) % modulus
    mapping: dict[bytes, str] = {}
    for i, ch in enumerate(order):
        code = (origin + i) % modulus
        raw = bytes([code]) if width == 1 else code.to_bytes(2, "little" if endian == "le" else "big")
        mapping[raw] = ch
    return mapping


def preview(data: bytes, offset: int, mapping: dict[bytes, str], width: int,
            before: int = 8, after: int = 40) -> str:
    """導き出したテーブルで前後を仮デコードして表示する."""
    codec = scrp.TableCodec(mapping)
    start = max(0, offset - width * before)
    out = []
    i = start
    limit = min(len(data), offset + width * after)
    while i < limit:
        b = data[i]
        if b == scrp.END:
            out.append("⏎")
            i += 1
            continue
        if b in scrp.CONTROL_CODES:
            tag, argc = scrp.CONTROL_CODES[b]
            out.append(f"<{tag}>")
            i += 1 + argc
            continue
        try:
            ch, size = codec.decode_char(data, i)
        except scrp.ScrpError:
            out.append("・")
            i += width
            continue
        out.append(ch)
        i += size
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="相対検索で未知の文字コードから日本語を探す")
    ap.add_argument("binary", help="調べるファイル (SCRP でなくても可)")
    ap.add_argument("--search", required=True, help="探す語 (清音のかなだけにするのがコツ)")
    ap.add_argument("--order", default="unicode", choices=["unicode", "nosmall"],
                    help="フォントの並びの仮説 (既定: unicode)")
    ap.add_argument("--charset-file", help="並びを 1 行 1 文字のファイルで与える")
    ap.add_argument("--width", type=int, default=1, choices=[1, 2], help="1 文字のバイト数")
    ap.add_argument("--endian", default="le", choices=["le", "be"], help="--width 2 のときの並び")
    ap.add_argument("--max-hits", type=int, default=10, help="表示するヒット数")
    ap.add_argument("--derive", help="1 件目のヒットから作った .tbl を書き出す")
    args = ap.parse_args()

    if len(args.search) < 3:
        raise scrp.ScrpError("検索語は 3 文字以上にしてください (短いと偶然の一致が増えます)")

    with open(args.binary, "rb") as fh:
        data = fh.read()
    order = build_order(args.order, args.charset_file)
    modulus = 256 if args.width == 1 else 65536

    hits = search(data, args.search, order, args.width, args.endian, modulus)
    diffs = deltas(args.search, order)
    print(f"検索語 {args.search!r} の差の並び: "
          + ", ".join(f"{d:+}" for d in diffs))
    print(f"{args.binary} ({len(data):,} バイト) → ヒット {len(hits)} 件")
    if not hits:
        print()
        print("見つかりませんでした。--order nosmall / --width 2 を試すか、")
        print("清音だけの別の語 (例: ここは / たたかい / まちのひと) で探してみてください。")
        return 1

    print()
    for offset, code in hits[: args.max_hits]:
        mapping = derived_mapping(code, args.search, order, args.width, args.endian, modulus)
        first = list(order)[0]
        origin = next(k for k, v in mapping.items() if v == first)
        print(f"  0x{offset:05X}  先頭文字 {args.search[0]!r} = 0x{code:0{args.width * 2}X}"
              f"  → {first!r} = 0x{origin.hex().upper()} と推定")
        print(f"           {preview(data, offset, mapping, args.width)}")
    if len(hits) > args.max_hits:
        print(f"  ... 残り {len(hits) - args.max_hits} 件")

    if args.derive:
        offset, code = hits[0]
        mapping = derived_mapping(code, args.search, order, args.width, args.endian, modulus)
        scrp.save_table(args.derive, mapping, header=(
            f"{os.path.basename(args.binary)} 0x{offset:X} のヒットから相対検索で推定したテーブル\n"
            "かな以外 (漢字・記号) は入っていません。バイナリを見ながら手で足していきます。"
        ))
        print()
        print(f"推定テーブルを書き出しました: {args.derive} ({len(mapping)} エントリ)")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
