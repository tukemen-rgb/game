#!/usr/bin/env python3
"""校正練習用の「不具合入り訳文」を作る (答えの側のスクリプト).

exercises/qa_target.tsv は、翻訳会社から戻ってきた訳文のつもりのファイルです。
そこに仕込んである不具合の一覧がこのスクリプト自体です。先に自分で
tools/proofread.py と目視で洗い出してから開いてください。

    python3 tools/make_sample.py
    python3 tools/dump_text.py work/SCRIPT.BIN -o work/SCRIPT.tsv
    python3 answers/plant_errors.py
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import scrp

# (id, 置換前, 置換後, 仕込んだ不具合の説明)
PLANTED = [
    (0, "リィンフォルト王国の城下町だ。", "リンフォルト王国の城下街だ。",
     "用語集違反 2 件: リィンフォルト→リンフォルト / 城下町→城下街"),
    (2, "<VAR:00>！", "あなた！",
     "プレイヤー名の変数 <VAR:00> を固定文字に置き換えてしまった"),
    (3, "３００ギル", "300ゴールド",
     "半角数字の混在 + 用語集違反 (ギル→ゴールド)"),
    (7, "……そっか。", "...そっか。",
     "三点リーダを半角ピリオド 3 つで書いている"),
    (8, "前線に立つか。<BR>度胸", "前線に立つか<BR>。度胸",
     "行頭禁則違反: 2 行目が「。」で始まっている"),
    (9, "決めろ。<BR>足が", "決めろ。足が",
     "<BR> を削ったので 1 行が 18 文字を超えた"),
    (11, "薬草は一つ２０ギル、<BR>火の魔石は", "やくそうは一つ２０ギル、<BR>火の魔晶石は",
     "用語集違反 2 件: 薬草→やくそう / 魔石→魔晶石"),
    (12, "１０％ほど引いてあげる。", "１０％ほど<BR>引いてあげる。",
     "<BR> を増やして 1 ページ 4 行になった"),
    (13, "また来てね♪", "また来てね〜♪",
     "波ダッシュ U+301C を使用 (Shift-JIS に無いのでフォントにも無い)"),
    (16, "たちは体力を", "ﾀﾁは体力を",
     "半角カナの混在"),
    (18, "たしかに王印がある。", "たしかに王印がある。　",
     "行末に全角スペースが残っている"),
    (20, "だが、扉はもう開かぬ。<WAIT>", "だが、扉はもう開かぬ。",
     "<WAIT> が抜けている (次のセリフに流れてしまう)"),
    (25, "<NAME:01>やったね、", "<NAME:00>やったね、",
     "話者名の参照先を間違えている (セリカのセリフが門番になっている)"),
    (28, "だった……。", "だった…………。",
     "三点リーダが 4 つ続いている"),
    (30, "扉は固く閉ざされている。", "扉には薔薇の紋章が刻まれている。",
     "フォントに無い漢字を使用 (実機では □ になる)"),
    (31, "セーブしますか？", "",
     "訳文が空 (作業漏れ)"),
    (33, "準備はいい？", "準備はいい?",
     "半角の疑問符"),
    (34, "迷わず薬草を使って。", "迷わずクスリ草を使って。",
     "用語集違反: 薬草→クスリ草"),
]


def main() -> int:
    src = os.path.join(REPO, "work", "SCRIPT.tsv")
    out = os.path.join(REPO, "exercises", "qa_target.tsv")
    if not os.path.exists(src):
        print(f"{src} がありません。先に make_sample.py と dump_text.py を実行してください。",
              file=sys.stderr)
        return 1

    rows = scrp.read_tsv(src)
    by_id = {int(row["id"]): row for row in rows}

    for rid, before, after, _ in PLANTED:
        row = by_id[rid]
        if before not in row["translation"]:
            print(f"id {rid}: 置換前の文字列が見つかりません: {before!r}", file=sys.stderr)
            return 1
        row["translation"] = row["translation"].replace(before, after, 1)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    scrp.write_tsv(out, rows)
    print(f"{out} を書き出しました ({len(rows)} 行 / 仕込んだ箇所 {len(PLANTED)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
