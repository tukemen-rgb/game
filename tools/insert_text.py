#!/usr/bin/env python3
"""TSV のテキストを SCRP ファイルに入れ直す (= 再挿入・ポインタ再計算).

    python3 tools/insert_text.py work/SCRIPT.tsv -o work/SCRIPT_new.BIN \\
        --original work/SCRIPT.BIN

ここでやっていることは 2 つだけです。

1. 各メッセージを文字コードに戻して 0xFF 終端のブロックにする
2. 全ブロックを並べ直し、それぞれの先頭オフセットでポインタテーブルを作り直す

長さが変わったのにポインタを直さないと、ゲーム側は前のオフセットを読み続けて
セリフが途中から始まったり文字化けしたりします。ROM ハックでいちばん多い
失敗がこれです。

容量の制約 (--max-size / --original) を付けると、元のファイルサイズに収まって
いるかを確認できます。実機のディスクイメージでは前後にほかのファイルが詰まって
いるので、勝手に大きくできないことが普通です。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp


def main() -> int:
    ap = argparse.ArgumentParser(description="TSV のテキストを SCRP ファイルに入れ直す")
    ap.add_argument("tsv", help="入力 TSV (dump_text.py の出力を編集したもの)")
    ap.add_argument("-o", "--out", required=True, help="出力する .BIN")
    ap.add_argument("--table", help="独自文字コードの .tbl")
    ap.add_argument("--encoding", choices=sorted(scrp.ENCODING_IDS),
                    help="文字コード (--original を付けない場合は必須)")
    ap.add_argument("--original", help="元の .BIN。文字コードと容量の基準として使う")
    ap.add_argument("--max-size", type=int, help="出力の上限バイト数")
    ap.add_argument("--allow-grow", action="store_true",
                    help="上限を超えても書き出す (警告のみ)")
    ap.add_argument("--pool-duplicates", action="store_true",
                    help="同一本文をまとめてポインタを共有させる")
    args = ap.parse_args()

    max_size = args.max_size
    if args.original:
        base = scrp.read_archive(args.original)
        encoding_id = base.encoding_id
        if max_size is None:
            max_size = len(base.data)
    elif args.encoding:
        encoding_id = scrp.ENCODING_IDS[args.encoding]
    else:
        ap.error("--original か --encoding のどちらかが必要です")

    codec = scrp.make_codec(encoding_id, args.table)
    rows = scrp.read_tsv(args.tsv)
    if not rows:
        raise scrp.ScrpError(f"{args.tsv}: 行がありません")

    for expected, row in enumerate(rows):
        if row["id"].strip() != str(expected):
            raise scrp.ScrpError(
                f"{args.tsv}:{row['_lineno']}: id が {row['id']!r} です。"
                f"0 から連番で、行の順序も変えないでください (期待 {expected})"
            )

    blobs = []
    errors = []
    for row in rows:
        text = scrp.final_text(row)
        try:
            blobs.append(scrp.encode_message(text, codec))
        except scrp.ScrpError as exc:
            errors.append(f"  id {row['id']} ({args.tsv}:{row['_lineno']}): {exc}")
            blobs.append(b"\xff")
    if errors:
        print(f"{len(errors)} 件のエンコードエラー:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1

    data = scrp.build_archive(encoding_id, blobs, pool_duplicates=args.pool_duplicates)

    over = max_size is not None and len(data) > max_size
    if over and not args.allow_grow:
        print(f"エラー: {len(data):,} バイトで、上限 {max_size:,} バイトを "
              f"{len(data) - max_size:,} バイト超えています。", file=sys.stderr)
        print("       文章を詰めるか、--pool-duplicates か --allow-grow を検討してください。",
              file=sys.stderr)
        return 1

    with open(args.out, "wb") as fh:
        fh.write(data)

    body = len(data) - scrp.HEADER_SIZE - len(blobs) * 4
    print(f"{args.out}: {len(data):,} バイト ({len(blobs)} メッセージ / 本文 {body:,} バイト)")
    if max_size is not None:
        diff = len(data) - max_size
        room = "" if diff == 0 else (f"{diff:+,} バイト")
        print(f"  容量 {max_size:,} バイトに対して {room or 'ぴったり'}"
              + ("  ※超過" if over else ""))
    shared = len(blobs) - len(set(blobs)) if args.pool_duplicates else 0
    if shared:
        print(f"  同一本文 {shared} 件をまとめました")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
