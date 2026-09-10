#!/usr/bin/env python3
"""SCRP ファイルからテキストを抽出して TSV にする (= 抽出工程).

    python3 tools/dump_text.py work/SCRIPT.BIN -o work/SCRIPT.tsv
    python3 tools/dump_text.py work/MSG_ENC.BIN --table answers/custom.tbl \\
        -o work/MSG_ENC.tsv

出力される TSV の列は id / offset / size / original / translation です。
translation 列が校正・翻訳の作業欄で、ここが空の行は insert_text.py が
original をそのまま使います。offset と size は元データ上の位置と長さで、
「入れ直したときに容量が足りるか」を見るための情報です。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp


def main() -> int:
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(errors="replace")     # Windows の cp932 コンソール/リダイレクトで落ちない
    ap = argparse.ArgumentParser(description="SCRP ファイルからテキストを抽出する")
    ap.add_argument("binary", help="入力の .BIN")
    ap.add_argument("-o", "--out", help="出力 TSV (省略時は入力と同じ場所に .tsv)")
    ap.add_argument("--table", help="独自文字コードの .tbl")
    ap.add_argument("--no-prefill", action="store_true",
                    help="translation 列を空にする (既定は original のコピー)")
    ap.add_argument("--preview", type=int, default=5, help="標準出力に出す先頭行数")
    args = ap.parse_args()

    archive = scrp.read_archive(args.binary)
    codec = scrp.make_codec(archive.encoding_id, args.table)
    try:
        rows_raw = archive.decode_all(codec)
    except scrp.ScrpError as exc:
        if not args.table:
            raise
        # 表を育てている途中なら、最初の 1 バイトだけ言われても進め方が決まらない。
        # ファイル全体を見て「あと何が足りないか」を出す (#84)
        print(f"エラー: {exc}", file=sys.stderr)
        ok, bad, missing = archive.survey_unreadable(codec)
        print(f"  この表ではまだ全文を読めません。{archive.count} 件のうち"
              f"読めるのが {len(ok)} 件、読めないのが {len(bad)} 件です。", file=sys.stderr)
        if missing:
            total = sum(missing.values())
            print(f"  表に無いバイト値は {len(missing)} 種類 / 計 {total} 個:", file=sys.stderr)
            for value, count in sorted(missing.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
                print(f"    0x{value:02X}  {count} 回", file=sys.stderr)
            if len(missing) > 8:
                print(f"    ほか {len(missing) - 8} 種類", file=sys.stderr)
            print("    (数えているのはバイト値です。2 バイトで 1 文字のコードがあると、"
                  "その後半も別の値として数に入ります)", file=sys.stderr)
        if ok:
            print(f"  読める id: {', '.join(str(i) for i in ok[:10])}"
                  + (" ほか" if len(ok) > 10 else ""), file=sys.stderr)
            print("  1 件だけ読むなら: python3 tools/hexdump.py "
                  f"{args.binary} --table {args.table} --message {ok[0]}", file=sys.stderr)
        return 1

    out_path = args.out or os.path.splitext(args.binary)[0] + ".tsv"
    rows = []
    for i, (offset, size, text) in enumerate(rows_raw):
        rows.append({
            "id": i,
            "offset": f"0x{offset:X}",
            "size": size,
            "original": text,
            "translation": "" if args.no_prefill else text,
        })
    scrp.write_tsv(out_path, rows)

    body_bytes = sum(size for _, size, _ in rows_raw)
    unique = len({text for _, _, text in rows_raw})
    shared = archive.count - len(set(archive.pointers))
    print(f"{args.binary}: {archive.count} メッセージ / "
          f"文字コード {scrp.ENCODING_NAMES.get(archive.encoding_id, '?')} / "
          f"本文 {body_bytes:,} バイト")
    print(f"  重複しない本文 {unique} 件"
          + (f" / 同じ場所を指しているポインタ {shared} 本" if shared else ""))
    print(f"  -> {out_path}")

    if args.preview:
        print()
        for i, (offset, size, text) in enumerate(rows_raw[: args.preview]):
            print(f"  [{i:3}] 0x{offset:05X} {size:3}B  {text}")
        if archive.count > args.preview:
            print(f"  ... 残り {archive.count - args.preview} 件")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
