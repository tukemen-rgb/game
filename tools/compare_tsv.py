#!/usr/bin/env python3
"""2 つの TSV を id で突き合わせて、本文が一致するかを言う (= 往復の確認).

    # 課題 6: 直した訳文が、入れ直したデータから同じに取り出せるか
    python3 tools/compare_tsv.py work/qa_fixed.tsv work/verify.tsv \\
        --left translation --right original

    # 課題 8: 取り出した全文が、答えと一致するか
    python3 tools/compare_tsv.py work/all.tsv work/BOKU2SAMPLE/answer.tsv \\
        --ignore "<VOICE:"

「TSV は直したがデータに反映されていない」は、この工程で最も多い事故です。
入れ直したら必ず取り出し直して、**元の文と突き合わせる**。目で 39 行を見比べる
のは現実的ではないので、機械にやらせます。

一致すれば終了コード 0、1 行でも違えば 1 を返します。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

#: どちらの列を本文として読むか
COLUMNS = ("original", "translation")


def text_of(row: dict, column: str) -> str:
    """指定した列の本文。translation が空なら original を使う (この一式の約束)."""
    if column == "translation":
        return scrp.final_text(row)
    return row.get("original", "")


def index_by_id(rows: list[dict], column: str, path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        rid = row.get("id", "").strip()
        if rid in out:
            raise scrp.ScrpError(f"{path}: id {rid!r} が 2 回出てきます")
        out[rid] = text_of(row, column)
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")     # Windows の cp932 コンソールで落ちない
    ap = argparse.ArgumentParser(description="2 つの TSV を id で突き合わせる")
    ap.add_argument("left", help="基準にする TSV")
    ap.add_argument("right", help="突き合わせる TSV")
    ap.add_argument("--left", dest="left_column", choices=COLUMNS, default="original",
                    help="左のどの列を本文として読むか (既定: original)")
    ap.add_argument("--right", dest="right_column", choices=COLUMNS, default="original",
                    help="右のどの列を本文として読むか (既定: original)")
    ap.add_argument("--ignore", action="append", default=[], metavar="文字列",
                    help="この文字列を含む行は比べない (何回でも指定できる)")
    ap.add_argument("--show", type=int, default=10, help="食い違いを出す件数 (既定: 10)")
    args = ap.parse_args()

    left = index_by_id(scrp.read_tsv(args.left), args.left_column, args.left)
    right = index_by_id(scrp.read_tsv(args.right), args.right_column, args.right)

    def skip(rid: str) -> bool:
        return any(word in left.get(rid, "") or word in right.get(rid, "")
                   for word in args.ignore)

    ids = [rid for rid in left if not skip(rid)]
    only_left = [rid for rid in ids if rid not in right]
    only_right = [rid for rid in right if rid not in left and not skip(rid)]
    both = [rid for rid in ids if rid in right]
    differ = [rid for rid in both if left[rid] != right[rid]]

    print(f"{args.left} ({args.left_column}) と {args.right} ({args.right_column}) を "
          f"id で突き合わせました")
    print(f"  一致 {len(both) - len(differ)} 行 / 食い違い {len(differ)} 行"
          + (f" / 左だけ {len(only_left)} 行" if only_left else "")
          + (f" / 右だけ {len(only_right)} 行" if only_right else "")
          + (f" / 比べなかった {len(left) - len(ids)} 行" if len(ids) != len(left) else ""))

    for rid in differ[:args.show]:
        print(f"\n  id {rid}")
        print(f"    左: {left[rid]}")
        print(f"    右: {right[rid]}")
    if len(differ) > args.show:
        print(f"\n  ほか {len(differ) - args.show} 行 (--show で増やせます)")
    for rid in only_left[:args.show]:
        print(f"  id {rid} は {args.right} にありません")
    for rid in only_right[:args.show]:
        print(f"  id {rid} は {args.left} にありません")

    if differ or only_left or only_right:
        print("\n== 結果: 一致しませんでした。入れ直しと取り出し直しの手順を見直してください")
        return 1
    print("\n== 結果: 全部一致しました")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
