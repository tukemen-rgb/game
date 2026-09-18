#!/usr/bin/env python3
"""僕の夏休み 2 (PS2 / SCPS-15026) のデータを一括で取り出す。

ブラウザの構造探査台と同じ読み方 (web/app.js の named-index / bokumsg ブロック) を
Python にしたもの。画面で 1 つずつ確かめた後、全部をまとめて処理するのに使う。

    python3 tools/boku2.py check 吸い出したフォルダ/                 # まず診断 (報告用の要約)
    python3 tools/boku2.py unpack BOKU2.IDX BOKU2.IMG OUT/        # 索引で本体を切り分ける
    python3 tools/boku2.py maps MAP/*.* -o OUT/maps               # マップの入れ物を部品にする
    python3 tools/boku2.py text OUT/system/system.msg OUT/maps/*/1.bin -f font.txt -o out.tsv
    python3 tools/boku2.py fontlist font.txt -o font_chars.txt    # 校正ツールのフォント一覧に

形式 (英語化パッチ Hilltop Works の公開ソースで確認したもの):

  BOKU2.IDX ("DFI")  ヘッダ 16 / レコード 16 (u16 種別, u16 続くか, u32 不明, u32 セクタ, u32 長さ)
                     名前はレコードの直後にレコード順で並ぶ (先頭は根 "/")
  マップの入れ物     u32 項目数 + (u32 位置, u32 長さ)。1 番が会話ファイル
  会話ファイル       u32 表の数 + 12 バイトの項目 (u16 長さ / u16 番号 / u16 位置) + 各表
  .msg / 各表        u32 件数 + 位置表 (8 または 4 バイト刻み) + 本文
  本文               2 バイトの並び。0x8000 終わり / 0x8001 改行 / 0x8002+u16 待ち / 0xCDCD 詰め物
                     それ以外はフォント画像 (bk_font.tms) の何番目の文字か

このツールは読むだけで、ゲームのデータもここには入っていない。
"""

from __future__ import annotations

import argparse
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp   # open_text (メモ帳/Excel のどの保存形式でも読む) と .tbl の書き出し

SECTOR = 2048

#: フォント画像の並び (公開ソース reprint.py の N_COLUMNS / asm_notes.txt の刻み 0x16)。
#: 1 行 23 字を 22 ドット刻みで描くので、画像の幅は最低 23×22 = 506 ドット要る
FONT_COLS = 23
FONT_CELL = 22

#: 文字表ぜんぶの字数。公開ソースの font.txt が 72 行 × 23 字 = 1656 字 (#167)。
#: **1 枚の画像には収まりきらない。** 512×1024 ドットの頁で 23 × 46 = 1058 マスしか
#: 無く、残り 598 字は別の画像にある (向こうの font2.txt の字数がちょうど 598 で合う)
FONT_GLYPHS = 1656

#: 文字表の **1 枚目**に入る字数 (#211/#230)。公開ソースの `font1.txt` が 1058 字で、
#: 512×1024 ドットの頁を 22 ドット刻みで割ると 23 × 46 = 1058 マス。差の 598 字が
#: 2 枚目 (向こうの `font2.txt` の字数と一致する)
FONT_PAGE1_GLYPHS = 1058

#: TIM2 の「画素の種類」を素人向けに言い換える (make_tim2.build_tim2 の書き出しと対)。
#: 番号のままだと意味が伝わらないので、1 画素に何が入っているかで言う
TIM2_PIXEL_KIND = {
    1: "1 画素 16 ビットの直接色",
    2: "1 画素 24 ビットの直接色",
    3: "1 画素 32 ビットの直接色",
    4: "1 画素 4 ビットのパレット番号",
    5: "1 画素 1 バイトのパレット番号",
}


# ---------- 索引 (DFI) ----------

def read_dfi(idx: bytes, data_size: int, rule: str = "flag") -> list[dict]:
    """レコードを歩いて {path, at, len} の一覧にする。ブラウザ側 readDfi と同じ規則.

    フォルダの閉じ方 (rule):
      "flag":  公開ソース UNPACK.py の規則。「続く」が 0 のフォルダを見たら旗を立て、次に
               「続く」が 0 のファイルで 1 段 (旗が立っていれば 2 段) 閉じる。**既定**
      "stack": ファイルの「続く」が 0 なら自分のフォルダを閉じ、閉じたフォルダの「続く」も 0 なら
               親も閉じる (何段でも)

    **既定を flag にしてある理由 (#108)。** 2 つの規則は、フォルダが 3 段以上
    まとめて閉じるときだけ答えが変わる。小さな形を 816 通り総当たりすると 34 通りで
    食い違い、**その 34 通りすべてで公開ソースの実物のコードは flag と同じ答え**を出した
    (向こうの unpackIMG をこちらの合成索引に対して実際に走らせて確かめた)。
    向こうの道具は実物のディスクで動いて英語版パッチを出しているので、
    「flag が正しい」か「実物にはこの形が出てこない」かのどちらか。どちらにしても
    **実物での裏付けがある側は flag だけ**なので、そちらを既定にする。

    check は今も両方で道筋を作って突き合わせるので、実物で食い違えばその場で分かる."""
    if idx[:4] != b"DFI\0":
        raise ValueError("先頭が DFI ではありません")
    rec_end = 16
    while rec_end + 16 <= len(idx):
        kind = idx[rec_end] | (idx[rec_end + 1] << 8)
        if kind not in (0, 1):
            break
        rec_end += 16
    rec_count = (rec_end - 16) // 16

    names: list[str] = []
    q = rec_end
    while len(names) < rec_count and q < len(idx):
        end = idx.find(b"\0", q)
        if end < 0:
            break
        s = idx[q:end]
        if len(s) > 127 or any(c < 0x21 or c > 0x7E for c in s):
            break
        names.append(s.decode("ascii"))
        q = end + 1

    entries: list[dict] = []
    stack: list[tuple[str, int]] = []
    seen: set[str] = set()
    escape = False
    for k in range(rec_count):
        p = 16 + k * 16
        is_dir = (idx[p] | (idx[p + 1] << 8)) == 1
        more = idx[p + 2] | (idx[p + 3] << 8)
        name = names[k] if k < len(names) else ""
        if is_dir:
            stack.append(("" if name == "/" else name, more))
            if more == 0:
                escape = True
            continue
        lba, length = struct.unpack_from("<II", idx, p + 8)
        at = lba * SECTOR
        base = name or f"#{len(entries)}"
        path = "/".join([d for d, _ in stack if d] + [base])
        if path in seen:                      # 同じ道筋は上書きせず ~2 を付ける (ブラウザ側と同じ)
            n = 2
            while f"{path}~{n}" in seen:
                n += 1
            path = f"{path}~{n}"
        seen.add(path)
        if length > 0 and at + length <= data_size:
            entries.append({"path": path, "at": at, "len": length})
        if more == 0:
            if rule == "flag":
                if stack:
                    stack.pop()
                if escape and stack:
                    stack.pop()
                escape = False
            else:
                d = stack.pop() if stack else None
                while d and d[1] == 0 and len(stack) > 1:
                    d = stack.pop()
    return entries


def dfi_rule_mismatch(idx: bytes, data_size: int) -> list[tuple[str, str]]:
    """2 つのフォルダ規則 (stack / flag) で道筋が違うファイルを (stack の道筋, flag の道筋) で返す."""
    a = read_dfi(idx, data_size, "stack")
    b = read_dfi(idx, data_size, "flag")
    return [(x["path"], y["path"]) for x, y in zip(a, b) if x["path"] != y["path"]]


def dfi_rule_tested(idx: bytes, data_size: int) -> int:
    """2 つの規則を突き合わせたときに、**差が出うるファイル**の数を返す (#125).

    フォルダの閉じ方の規則なので、索引が入れ子を持たなければ 2 通りは必ず同じ答えを出す。
    そのとき「2 通りで一致」と言うと、**何も試していない**のに規則が裏付いたように読める。
    フォルダの規則は docs/09 の表でまだ「確かめていない」側にあり、docs/10 は
    この行を報告の決め手に挙げているので、分母の無い「一致」は危ない。
    """
    return sum(1 for e in read_dfi(idx, data_size, "flag") if "/" in e["path"])


def dfi_name_stop(idx: bytes) -> dict | None:
    """名前の読み取りが**途中で止まった**なら、どこでなぜ止まったかを返す (#202).

    索引の名前は 0 終わりで並んでいる。読み手は「使えない字が出たら、そこから先は
    名前の置き場ではない」と見て止まる —— ごみを名前として並べないための用心で、
    これ自体は正しい。ところが**止まったことを誰も言わなかった**。

    実物で起きるのはこの形:

        レコード 30 件 / ファイル 30 件 / 名前が付いた 5 件
        → 名前が付かないファイルが多い。名前の置き場 (上の 0x…) 付近の
          64 バイトを報告してください

    原因は 6 個目の名前に混じった 1 バイト (空白など) なのに、案内しているのは
    **名前の置き場の先頭**。社長は関係の無い所を見ることになる。止まった位置と
    その 1 バイトが分かれば、そこを見ればよい。

    **社長の実物は、まさに名前が付かない吸い出しだった** (docs/09 の #1・#3)。
    この道は実際に通っている。

    @returns 最後まで読めたなら None。止まったなら
             `{"at": 止まった位置, "nth": 何個目, "byte": その 1 バイト, "head": 手前の名前}`
    """
    if idx[:4] != b"DFI\0":
        return None
    rec_end = 16
    while rec_end + 16 <= len(idx):
        if (idx[rec_end] | (idx[rec_end + 1] << 8)) not in (0, 1):
            break
        rec_end += 16
    rec_count = (rec_end - 16) // 16
    q, n = rec_end, 0
    while n < rec_count and q < len(idx):
        end = idx.find(b"\0", q)
        if end < 0:
            return {"at": q, "nth": n + 1, "byte": None, "head": ""}
        s = idx[q:end]
        bad = next((i for i, c in enumerate(s) if c < 0x21 or c > 0x7E), None)
        if len(s) > 127 or bad is not None:
            return {"at": q + (bad or 0), "nth": n + 1,
                    "byte": s[bad] if bad is not None else None,
                    "head": s[:bad].decode("ascii", "replace") if bad else ""}
        n += 1
        q = end + 1
    return None


def dfi_name_stop_note(stop: dict | None) -> str:
    """`dfi_name_stop` を 1 行の案内にする (#202)."""
    if not stop:
        return ""
    where = f"位置 0x{stop['at']:X}"
    what = (f"使えない字 0x{stop['byte']:02X} があります" if stop["byte"] is not None
            else "名前の終わりの 0 が見つかりません")
    near = f" (そこまでは `{stop['head']}` と読めています)" if stop["head"] else ""
    return (f"   名前は **{stop['nth']} 個目で止まっています**: {where} に {what}{near}。"
            "**そこから先の名前は読んでいません。** その 1 バイトの前後 64 バイトを"
            "報告してください (名前の置き場の先頭ではなく、ここ)")


#: 先頭の 4 バイトで分かる形式。**画面 (web/app.js の MAGICS) と同じ並び**に
#: しておくこと。片側だけ増えると、同じファイルを見て違うことを言う (#204)
MAGICS = (
    (b"\x7FELF", "本体プログラム (ELF)"),
    (b"TIM2", "画像 (TIM2)"),
    (b"VAGp", "音声 (VAG)"),
    (b"RXWS", "音声バンク (RXWS)"),
    (b"SShd", "音声ヘッダ (SShd)"),
    (b"\x00\x00\x01\xBA", "動画 (MPEG PS)"),
    (b"DFI\x00", "索引 (DFI)"),
    (b"RIFF", "RIFF (WAV など)"),
    (b"\x89PNG", "画像 (PNG)"),
)


def block_stats(whole: bytes) -> dict | None:
    """バイトの性質を数える (#205)。**画面 (web/app.js の blockStats) と同じ数え方**.

    末尾のゼロはセクタの詰め物なので除いて数える。ディスクイメージでは
    「80 バイトのテキスト + 1968 バイトの詰め物」が普通にあり、そのまま平均を
    取ると何もかもゼロ埋めに見えてしまう。
    """
    if not whole:
        return None
    end = len(whole)
    while end > 0 and whole[end - 1] == 0:
        end -= 1
    pad_ratio = (len(whole) - end) / len(whole)
    if end < 16:
        zeros = whole.count(0)
        return {"n": len(whole), "entropy": 0.0, "zero_ratio": zeros / len(whole),
                "print_ratio": 0.0, "pair_ratio": 0.0, "mean_diff": 0.0,
                "pad_ratio": pad_ratio, "scant": True}
    b = whole[:end]
    n = len(b)
    hist = [0] * 256
    zeros = printable = diff_sum = 0
    for i, v in enumerate(b):
        hist[v] += 1
        if v == 0:
            zeros += 1
        if (0x20 <= v < 0x7F) or v in (0x0A, 0x0D, 0x09):
            printable += 1
        if i:
            diff_sum += abs(v - b[i - 1])
    entropy = 0.0
    for count in hist:
        if count:
            pr = count / n
            entropy -= pr * math.log2(pr)
    pairs = i = 0
    while i + 1 < n:
        if (0x81 <= b[i] <= 0x9F or 0xE0 <= b[i] <= 0xEF) and 0x40 <= b[i + 1] <= 0xFC:
            pairs += 1
            i += 2
            continue
        i += 1
    return {"n": n, "entropy": entropy, "pad_ratio": pad_ratio,
            "zero_ratio": zeros / n, "print_ratio": printable / n,
            "pair_ratio": pairs * 2 / n,
            "mean_diff": diff_sum / (n - 1) if n > 1 else 0.0}


#: `classify_block` が返す名前と、人に見せる言葉。
#: **画面 (SNIFF_BY_CLASS) と同じ言葉**にしておくこと (#205)
CLASS_LABELS = {
    "jp": "日本語テキストらしい",
    "ascii": "ASCII テキストらしい",
    "zero": "ゼロ埋め",
    "high": "圧縮らしい (乱数に近い並び)",
    "wave": "波形らしい (ヘッダ無しの音声など)",
    "tile": "",                    # 「不明」は言わない。黙るほうが親切
}


def classify_block(s: dict | None) -> str:
    """バイトの性質から種類を 1 語で決める。**画面の classifyStats と同じ判定**.

    エントロピーのしきい値を固定値にしないこと。256 種類の値を n 個しか
    標本にしていないと、完全な乱数でもエントロピーは 8 に届かない。
    標本数から「乱数だったときの期待値」を出して、それと比べる。
    """
    if not s:
        return "zero"
    if s["zero_ratio"] > 0.92:
        return "zero"
    if s["pair_ratio"] > 0.45:
        return "jp"
    if s["print_ratio"] > 0.85:
        return "ascii"
    if s["entropy"] > 4.5 and s["mean_diff"] < 24:
        return "wave"
    expected_random = 8 - 255 / (2 * s["n"] * math.log(2))
    if s["n"] >= 192 and s["entropy"] > expected_random - 0.3:
        return "high"
    return "tile"


def guess_kind(head: bytes) -> str:
    """読めなかったファイルの先頭から、**分かることだけ**を言う (#204).

    `check` は読めない `.msg` の先頭 16 バイトを見せて終わっていた。
    画面のほうは同じバイトを見て「音声 (VAG)」「圧縮らしい」まで言うのに、
    **社長が最初に打つのは `check`** のほう。16 進を渡されても、素人には
    「読めなかった」以上のことが分からない。

    分からないときは**黙る** (空文字)。当てずっぽうを足すと、16 進だけの
    ほうがまだましになる。
    """
    if not head:
        return ""
    for magic, label in MAGICS:
        if head.startswith(magic):
            return label
    if all(b == 0 for b in head):
        return "ゼロ埋め (中身がありません)"
    if all(b == head[0] for b in head):
        return f"同じバイト (0x{head[0]:02X}) の繰り返し (詰め物か、壊れています)"
    # 先頭 16 バイトの散らばりだけで「圧縮らしい」とまでは言えない。
    # 言えるのは「文字ではない」ことくらいなので、そこで止める
    if all(b < 0x09 or (0x0E <= b < 0x20) or b == 0x7F for b in head):
        return "制御コードばかり (文字ではありません)"
    return ""


def guess_kind_note(head: bytes, body: bytes | None = None) -> str:
    """`guess_kind` を、報告に足せる形にする (分からなければ空文字).

    **別の形式の目印が出たときだけ**「名前と中身が違う」と言う。詰め物や
    ゼロ埋めは形式ではないので、そう言うと的外れになる。

    先頭 4 バイトで分からないときは、**もっと広く見て性質を言う** (#205)。
    16 バイトでは何も言えないが、数 KB あれば「圧縮らしい」「波形らしい」
    までは言える (画面の「性質を地図にする」と同じ判定)。
    それでも分からなければ黙る —— 「不明」と書いても何も足さない。
    """
    kind = guess_kind(head)
    if kind:
        known = any(head.startswith(m) for m, _label in MAGICS)
        return (f" ({kind}。**名前は .msg ですが、中身は別のもの**です)" if known
                else f" ({kind})")
    if body:
        label = CLASS_LABELS.get(classify_block(block_stats(body)), "")
        if label:
            return f" ({label})"
    return ""


def safe_parts(path: str) -> list[str]:
    """索引の名前をそのままフォルダ名に使うと、'..' や '\\' で出力先の外に書いてしまう。
    索引は信用しない: 区切りを揃え、上に戻る部品と空の部品を落とし、危ない文字は _ にする."""
    parts = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        parts.append("".join(c if c.isalnum() or c in "._-~#()+" else "_" for c in part))
    return parts or ["_"]


def dfi_dropped(idx: bytes, data_size: int, rule: str = "flag") -> dict:
    """索引が名乗っているファイル数と、**実際に取り出せる数**の差 (#178).

    `read_dfi` は「本体の外を指す項目」を黙って落とす。落とすこと自体は正しい
    (読めば本体の外を読む) が、**落とした数を捨てていた**ので、吸い出しが
    途中で切れていても `unpack` は「12 個に切り分けました」と言って 0 で終わっていた。
    社長には 20 個のうち 8 個が出ていないことが分からない。

    @returns {"records": 索引が名乗るファイル数, "taken": 取り出せる数,
              "outside": 本体の外を指す数}
    """
    if len(idx) < 16:
        return {"records": 0, "taken": 0, "outside": 0}
    # **`read_dfi` と同じ数え方でレコードの終わりを見つける。** 見出しの +4 の値を
    # そのまま使うと、この索引では 27 件の所を 37 件と数えてしまう
    # (名前の置き場の先頭が、たまたまレコードに見える)
    rec_end = 16
    while rec_end + 16 <= len(idx) and (idx[rec_end] | (idx[rec_end + 1] << 8)) in (0, 1):
        rec_end += 16
    rec_count = (rec_end - 16) // 16
    records = outside = 0
    for k in range(rec_count):
        p = 16 + k * 16
        if (idx[p] | (idx[p + 1] << 8)) == 1:          # フォルダの行は数えない
            continue
        records += 1
        lba, length = struct.unpack_from("<II", idx, p + 8)
        # **長さ 0 の行は「取り出せなかった」ではない。** 空き枠なので数えない。
        # 数えると、名前の置き場が壊れた索引 (レコードの終わりがずれる) で
        # 空き枠 17 個を「落とした」と言ってしまう
        if length > 0:
            if lba * SECTOR + length > data_size:
                outside += 1
        else:
            records -= 1
    # **どこが外なのかを捨てない** (#203)。数だけ出していたので、社長は
    # 「何かが足りない」ことは分かっても、**どこから足りないのか**が分からなかった。
    # 索引が指す一番後ろの位置と本体の大きさを比べれば、**あとどれだけ足りないか**
    # が出る。さらに、外を指すものが**ある位置から先に固まっている**なら
    # 吸い出しが途中で切れた形、**散らばっている**なら位置の単位の読み違い
    spans, far, first_out = [], 0, None
    for k in range(rec_count):
        p = 16 + k * 16
        if (idx[p] | (idx[p + 1] << 8)) == 1:
            continue
        lba, length = struct.unpack_from("<II", idx, p + 8)
        if length <= 0:
            continue
        at, end = lba * SECTOR, lba * SECTOR + length
        far = max(far, end)
        spans.append((at, end > data_size))
        if end > data_size and (first_out is None or at < first_out):
            first_out = at
    # **索引に並んでいる順**で見る。位置の順で並べ替えると、遠くを指すものは
    # 必ず後ろに来るので「固まっている」が**いつも真**になる (#197 と同じ型の罠)。
    # 吸い出しが途中で切れたなら、索引の**後ろのほう**が丸ごと外になる
    flags = [bad for _at, bad in spans]
    first_bad = next((i for i, bad in enumerate(flags) if bad), None)
    clean_cut = first_bad is not None and all(flags[first_bad:])
    return {"records": records, "taken": len(read_dfi(idx, data_size, rule)),
            "outside": outside, "needs": far, "have": data_size,
            "first_outside": first_out, "clean_cut": clean_cut}


def dropped_note(d: dict) -> str:
    """`dfi_dropped` の結果を、社長に読める 1 行にする。出す必要が無ければ空文字."""
    if not d["outside"]:
        return ""
    # **どこから足りないのかまで言う** (#203)
    short = d.get("needs", 0) - d.get("have", 0)
    where = ""
    if d.get("first_outside") is not None:
        where = f"外を指し始めるのは 0x{d['first_outside']:X} から。"
    if short > 0:
        where += (f"索引は最大 0x{d['needs']:X} ({d['needs']:,} バイト) までを指していますが、"
                  f"本体は {d['have']:,} バイトしかありません "
                  f"(**{short:,} バイト足りない**)。")
    if d.get("clean_cut"):
        tail = ("外を指すものが**ある位置から先に固まっています**。"
                "**吸い出しが途中で切れた形**です —— もう一度吸い出すか、"
                "上の大きさになるまで足りない分を取り込んでください")
    else:
        tail = ("外を指すものが**ばらけています**。切れたというより、"
                "**位置の単位の読み方 (バイト / セクタ) が外れている**形です。"
                "この行ごと報告してください")
    # 矢印で始まる文字列は、**return の中に直に**置くこと。いったん変数に入れて
    # から返すと、見張り (両側の矢印がそろっているか) が拾えない (#178 と同じ話)。
    # この注釈にも矢印の形を書かないこと —— 見張りは原本の字面を読むので、
    # 注釈の中の例まで矢印として数えてしまう (今それで 1 度落とした)
    return (f"→ 索引は {d['records']} 個のファイルを名乗っていますが、取り出せるのは "
            f"{d['taken']} 個です (本体の外を指す {d['outside']} 個)。"
            + where + tail)


#: 本体が空かどうかを見るときに、何個のファイルを覗くか (先頭 64 バイトずつ)
BODY_SAMPLE_FILES = 30


def body_looks_empty(img, entries: list[dict]) -> tuple[int, int]:
    """本体の中身がゼロ埋めばかりでないかを、散らばった位置で覗く (#218).

    吸い出しが途中で切れたり、コピーが終わっていないと、**索引だけが正しくて
    中身が全部ゼロ**になる。その状態でも「名前が付いた 1951 件」「索引が本体の
    100% を指しています」は緑のまま出るので、素人は形式の読み違いを疑い始める。
    原因は 1 つ上の段 (データが無い) なので、そこを先に言う。

    戻り値は (覗いた数, ゼロ埋めだった数)。
    """
    picks = [e for e in entries if e["len"] >= 16]
    if len(picks) > BODY_SAMPLE_FILES:                 # 端に寄らないよう等間隔で
        step = len(picks) / BODY_SAMPLE_FILES
        picks = [picks[int(i * step)] for i in range(BODY_SAMPLE_FILES)]
    checked = zero = 0
    for e in picks:
        img.seek(e["at"])
        head = img.read(min(64, e["len"]))
        if not head:
            continue
        checked += 1
        if not any(head):
            zero += 1
    return checked, zero


def unpack(idx_path: str, img_path: str, out_dir: str) -> int:
    with open(idx_path, "rb") as fh:
        idx = fh.read()
    size = os.path.getsize(img_path)
    entries = read_dfi(idx, size)
    dupes = sum(1 for e in entries if "~" in os.path.basename(e["path"]))
    if dupes:
        print(f"注意: 同じ名前が {dupes} 件あり `~2` を付けて区別しました。"
              "フォルダの入れ子の規則が実物と違うかもしれません", file=sys.stderr)
    # **名前を変えたら、変えたと言う** (#202)。`:` `?` `*` や空白は Windows の
    # ファイル名に使えないので `_` にしているが、黙って変えると、索引に出ている
    # 名前と手元のファイル名が食い違う。20 個のうち 3 個だけ違っていても気づけない
    renamed = [(e["path"], "/".join(safe_parts(e["path"])))
               for e in entries if "/".join(safe_parts(e["path"])) != e["path"]]
    if renamed:
        shown = ", ".join(f"{a} → {b}" for a, b in renamed[:3])
        print(f"注意: ファイル名に使えない字があった {len(renamed)} 個を `_` にしました "
              f"({shown}{' …' if len(renamed) > 3 else ''})。"
              "索引に出ている名前と手元のファイル名が違います", file=sys.stderr)
    with open(img_path, "rb") as img:
        for e in entries:
            dest = os.path.join(out_dir, *safe_parts(e["path"]))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            img.seek(e["at"])
            with open(dest, "wb") as fo:
                fo.write(img.read(e["len"]))
    unnamed = sum(1 for e in entries if os.path.basename(e["path"]).startswith("#"))
    # **切り分けた名前も返す** (#206)。docs/10 の 20 分の行は「`system/system.msg` の
    # ような**フォルダ付きの名前**が並ぶか」を見ろと言っているのに、`unpack` は
    # 「N 個に切り分けました」の 1 行しか出していなかった。名前が読めたかどうかは
    # **索引の読み方が当たっているかの一番の手がかり**で、実物ではまさにここが
    # 外れた (#1・#3 で `#0 #1 …` になった)。`ls` を打たせないと分からない、では困る
    return len(entries), unnamed, dfi_dropped(idx, size), [e["path"] for e in entries]


# ---------- マップの入れ物 ----------

def parse_map(b: bytes) -> list[dict] | None:
    got = parse_map_rec(b)
    return got[1] if got else None


def map_rec_derived_count(b: bytes, rec: int) -> int | None:
    """項目数を「最初の 0 でない位置まで」から割り出す (#126).

    こちらは先頭の u32 を**項目数**として読んでいる。公開ソースの `unpackMap` は
    そこを `header_ID` (「たいてい (いつも?) 0xE」) として読み捨て、項目は +4 から
    **最初の位置まで**並んでいるものとして回す。数はどこにも書いていない。

    どちらで読んでも部品は同じになる (余りは 0 埋めなので空として飛ばされる)。
    ただし先頭が本当に種別 ID なら、値が 1〜64 の外に出た瞬間にこちらは
    「入れ物ではない」と言ってしまう。向こうの道具は実物で動いているので、
    **こちらの数え方でどうしても読めなかったときの控え**として使う。
    先に候補へ混ぜると、12 バイト刻みの入れ物を 8 バイト刻みと読み違える
    (数が増えるぶん空の項目をまたいで辻褄が合ってしまう) ので、順番が要る。
    """
    for i in range((len(b) - 4) // rec):
        off = struct.unpack_from("<I", b, 4 + i * rec)[0]
        if not off:
            continue
        derived = (off - 4) // rec
        return derived if 1 <= derived <= 64 else None
    return None


def map_extra_after_declared(b: bytes, rec: int, declared: int) -> int | None:
    """先頭の数より**後ろにも中身のある項目**が並んでいないか (#217).

    実物の入れ物は `[u32 0xE][u32 0x80] …` の形で、公開ソースの `unpackMap` は
    先頭を種別 ID として読み捨て、**表の終わりは「最初の位置」**として回している。
    こちらは先頭を項目数として読むので、0xE = 14 個で止まる。ところが
    4〜0x80 には 8 バイト刻みで **15 項目**が並ぶ。最後の 1 個が空でなければ、
    こちらの読み方は**黙って 1 個落とす**。

    落ちる場合だけ、公開ソースの数え方に乗り換えるための関数。
    数が増えても中身が空なら乗り換えない (乗り換えると 12 バイト刻みの入れ物を
    8 バイト刻みと読み違える道が開く。#126 でそこを踏んでいる)。
    """
    derived = map_rec_derived_count(b, rec)
    if derived is None or derived <= declared:
        return None
    for i in range(declared, derived):
        at = 4 + i * rec
        if at + 8 > len(b):
            return None
        off, length = struct.unpack_from("<II", b, at)
        if off and length and off + length <= len(b):
            return derived                      # 中身のある項目が後ろにある
    return None


def parse_map_rec(b: bytes) -> tuple[int, list[dict]] | None:
    """入れ物を読み、(項目の刻み, 部品の一覧) を返す。刻み 8 が普通、12 は日記・保存画面など."""
    if len(b) < 16:
        return None
    declared = struct.unpack_from("<I", b, 0)[0]
    tries = [(rec, declared) for rec in (MAP_ENTRY, MAP_ENTRY_ALT) if 1 <= declared <= 64]
    best = _best_map_rec(b, tries)
    if best is not None:
        # **落ちている部品が無いか見る** (#217)。先頭の数を項目数として読むのは
        # こちらの解釈で、実物で動いている公開ソースは「最初の位置まで」で回す
        more = map_extra_after_declared(b, best[1], declared)
        if more is not None:
            longer = _best_map_rec(b, [(best[1], more)])
            if longer is not None and longer[0] > best[0]:
                best = longer
    if best is None:
        # こちらの数え方では読めなかった。公開ソースの数え方で読み直す
        tries = [(rec, c) for rec in (MAP_ENTRY, MAP_ENTRY_ALT)
                 if (c := map_rec_derived_count(b, rec)) is not None]
        best = _best_map_rec(b, tries)
    return (best[1], best[2]) if best else None


def _best_map_rec(b: bytes, tries: list[tuple[int, int]]):
    best = None
    for rec, n in tries:
        head = 4 + n * rec
        if head > len(b):
            continue
        items, prev, first, ok = [], 0, 0, True
        for i in range(n):
            off, length = struct.unpack_from("<II", b, 4 + i * rec)
            if not off and not length:
                items.append({"i": i, "at": 0, "len": 0})
                continue
            if off < head or off + length > len(b) or off < prev or off & 15:
                ok = False
                break
            first = first or off
            prev = off + length
            items.append({"i": i, "at": off, "len": length})
        if ok and first:
            # 12 バイト刻みの入れ物は 8 バイト刻みとしても「読めて」しまうことがある
            # (後ろの項目が空のとき)。部品が多く取れる方を採る。同じなら 8
            filled = sum(1 for it in items if it["len"])
            if best is None or filled > best[0]:
                best = (filled, rec, items)
    return best


def split_map(path: str, out_dir: str) -> int:
    with open(path, "rb") as fh:
        b = fh.read()
    items = parse_map(b)
    if items is None:
        raise ValueError(f"{path}: マップの入れ物ではありません")
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for it in items:
        if not it["len"]:
            continue
        with open(os.path.join(out_dir, f"{it['i']}.bin"), "wb") as fo:
            fo.write(b[it["at"]:it["at"] + it["len"]])
        n += 1
    return n


# ---------- 会話 ----------

def parse_msg(b: bytes, stride: int, info: dict | None = None) -> list[dict] | None:
    if len(b) < 8:
        return None
    n = struct.unpack_from("<I", b, 0)[0]
    if not 1 <= n <= 20000:
        return None
    tab = 4 + n * stride
    if tab > len(b):
        return None
    starts = [struct.unpack_from("<I", b, 4 + i * stride)[0] for i in range(n)]
    prev, nonzero = 0, 0
    for s in starts:
        if not s:
            continue
        if s < tab or s >= len(b) or s < prev or s & 1:
            return None
        prev = s
        nonzero += 1
    if not nonzero:
        return None
    # 8 バイト刻みの後ろ 4 バイトは、その項目のバイト長 (公開ソースの書き出し側で確認、#71)。
    # 鵜呑みにせず、次の位置から出した長さと突き合わせてから使う。合っていれば最後の項目も
    # 詰め物を含まずに切れる。合わなければ今までどおり次の位置だけで読む
    sizes, agree, checked = None, 0, 0
    if stride >= 8:
        sizes = [struct.unpack_from("<I", b, 4 + i * stride + 4)[0] for i in range(n)]
        for i, s in enumerate(starts):
            if not s or not sizes[i]:
                continue
            end = next((t for t in starts[i + 1:] if t), None)
            if end is None:                 # 最後の項目は突き合わせる相手がいない
                continue
            checked += 1
            if s + sizes[i] == end:
                agree += 1
    use_size = sizes is not None and checked > 0 and agree >= checked * 0.9
    items = []
    for i, s in enumerate(starts):
        if not s:
            items.append({"i": i, "at": 0, "codes": []})
            continue
        end = next((t for t in starts[i + 1:] if t), len(b))
        if use_size and sizes[i] > 0 and not sizes[i] & 1 and s + sizes[i] <= len(b):
            end = s + sizes[i]
        codes = list(struct.unpack_from(f"<{(end - s) // 2}H", b, s))
        items.append({"i": i, "at": s, "codes": codes})
    if info is not None and sizes is not None:
        info.update(len_field="ok" if use_size else "ng", agree=agree, checked=checked)
    return items


def ends_well(items: list[dict]) -> float:
    """終わりの印 (0x8000) で終わっている項目の割合。読み方の当たり外れの目安.

    位置表の刻みを間違えると、項目の切れ目が本文の途中に来る。そうなると
    「終わりの印で終わっていない項目」が増えるので、割合で見分けられる。
    """
    live = [it for it in items if it["codes"]]
    if not live:
        return 0.0
    return sum(1 for it in live if it["codes"][-1] == 0x8000) / len(live)


def pick_msg(b: bytes, info: dict | None = None) -> list[dict] | None:
    """位置表の刻み (8 / 4) を選んで読む。**両方読めたときは中身で決める** (#115).

    以前は「8 で読めたら 8」と、先に試したほうを無条件に採っていた。ところが
    8 バイト刻みは 4 バイト刻みのファイルでも通ることがある (位置表の後ろに
    隙間があり、そこが増える偶数の並びに見える場合)。そのときは項目の切れ目が
    本文の途中に来て、**文の途中でぶつ切りになった行**が出る。

    実際、練習データの `item_info.msg` は 8 でも 4 でも読めてしまう。正しいのは 8 で、
    そのとき 2 項目とも終わりの印で終わる (割合 1.00)。4 で読むと「あみ」だけの
    項目ができて 0.50 に落ちる。作為的に作った 4 バイト刻みのファイルでは逆に
    8 が 0.25 / 4 が 0.75 になる。**どちらが正しいかは、この割合が言い当てる。**

    片方しか読めないときは今までどおり (ほとんどのファイルはこちら)。
    """
    got = []
    for stride in (MSG_STRIDE, MAP_MSG_STRIDE):
        sub: dict = {}
        items = parse_msg(b, stride, sub)
        if items:
            got.append((ends_well(items), stride, items, sub))
    if not got:
        return None
    # 同点なら先に試した 8 を残す (max は最初の最大値を返す)
    best = max(got, key=lambda x: x[0])
    if info is not None:
        info.update(best[3])
        info["stride"] = best[1]
        if len(got) > 1:
            info["both_strides"] = {s: round(sc, 2) for sc, s, _i, _n in got}
    return best[2]


def parse_tables(b: bytes) -> list[dict] | None:
    if len(b) < 16:
        return None
    t = struct.unpack_from("<I", b, 0)[0]
    if not 1 <= t <= 2000:
        return None
    head = 4 + t * 12
    if head > len(b):
        return None
    tables, prev = [], 0
    for i in range(t):
        # 項目 12 バイト: u32 ? / u16 表の長さ / u16 番号 / 表の位置。位置は公開ソースの
        # MSG_notes.txt では int (u32)、読み取り (unpackMapMSG) では short (u16) と食い違う。
        # u32 で読み、範囲外なら下位 16 ビットで読み直す (どちらの形でも読める)
        size, ident, off = struct.unpack_from("<HHI", b, 4 + i * 12 + 4)
        if off + size > len(b) and (off & 0xFFFF) + size <= len(b):
            off &= 0xFFFF
        if off < head or off + size > len(b) or off < prev:
            return None
        prev = off
        msg = parse_msg(b[off:off + size], MAP_MSG_STRIDE) if size >= 8 else None
        tables.append({"i": i, "off": off, "size": size, "id": ident, "msg": msg})
    if not any(x["msg"] for x in tables):
        return None
    return tables


def parse_raw(b: bytes, max_glyph: int = 0x2000) -> list[dict] | None:
    """見出しの無い並び (日記の雛形・保存画面の文言)。0x8000 で区切られた 2 バイトの本文だけ.

    偶数長で、値の 9 割以上が文字番号か制御コードで、終わりが 1 つ以上あるときだけ読む
    (ブラウザ側 parseBokuMsgRaw と同じ)."""
    if len(b) < 4 or len(b) % 2:
        return None
    codes = list(struct.unpack_from(f"<{len(b) // 2}H", b, 0))
    ends = codes.count(0x8000)
    ok = sum(1 for c in codes if c in (0x8000, 0x8001, 0x8002, 0xCDCD) or c < max_glyph)
    if not ends or ok < len(codes) * 0.9:
        return None
    items, cur, start = [], [], 0
    for i, c in enumerate(codes):
        cur.append(c)
        if c == 0x8000:
            items.append({"i": len(items), "at": start, "codes": cur})
            cur, start = [], (i + 1) * 2
    if cur and any(c not in (0xCDCD, 0) for c in cur):
        items.append({"i": len(items), "at": start, "codes": cur})
    return items


def parse_sjis_list(b: bytes) -> list[dict] | None:
    """Shift-JIS の文言 (公開ソースの SJIS_FILES: 保存画面の入れ物の 2 番)。0x00 区切り.

    文字表は要らない。区切りが 1 つ以上あり、文字の 6 割以上がかな・漢字・全角記号・
    英数のときだけ読む (ブラウザ側 parseSjisList と同じ)."""
    if len(b) < 4 or b"\0" not in b:
        return None
    items, good, total = [], 0, 0
    start = 0
    for p in range(len(b) + 1):
        if p < len(b) and b[p] != 0:
            continue
        if p > start:
            try:
                text = b[start:p].decode("cp932")
            except UnicodeDecodeError:
                return None
            for ch in text:
                total += 1
                c = ord(ch)
                if (0x3040 <= c <= 0x30FF or 0x4E00 <= c <= 0x9FFF or 0xFF01 <= c <= 0xFF60
                        or 0x3000 <= c <= 0x303F or 0x20 <= c <= 0x7E or c == 0x0A):
                    good += 1
            items.append({"i": len(items), "at": start, "text": text})
        start = p + 1
    if not items or not total or good < total * 0.6:
        return None
    return items


def voice_id(codes: list[int]) -> str | None:
    if len(codes) != 4:
        return None
    s = ""
    for c in codes:
        lo, hi = c & 255, c >> 8
        if not (0x30 <= lo <= 0x39 and 0x30 <= hi <= 0x39):
            return None
        s += chr(lo) + chr(hi)
    return s


# 0x8002 が「待ち時間 + u16」ではなく、引数の無い「ページ送り」になるファイル
# (公開ソース MSG.py の ALT_NEWLINE_FILES)。ここでは 0x8002 の次の値も文字なので、
# 待ち時間として読むと 1 字飛ばして本文がずれる
ALT_BREAK_FILES = {"turi_info.msg", "phot_info.msg", "okan_info.msg", "item_info.msg",
                   "insect_menu.msg", "fishing.msg", "fish_info.msg"}


def is_alt_break(name: str) -> bool:
    return os.path.basename(name).lower() in ALT_BREAK_FILES


def decode(codes: list[int], glyphs: list[str] | None, tags: bool = True, alt: bool = False) -> str:
    """ブラウザ側 bokuMsgText と同じ。tags=True で校正ツールの書き方 (<BR> / <WAIT:xx>).

    alt=True (ALT_BREAK_FILES) では 0x8002 は引数の無いページ送り <BREAK>."""
    v = voice_id(codes)
    if v:
        return f"<VOICE:{v}>" if tags else "{VOICE " + v + "}"
    out = []
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0x8000:
            if not tags:
                out.append("{END}")
            break
        if c == 0x8001:
            out.append("<BR>" if tags else "\n")
        elif c == 0x8002 and alt:
            out.append("<BREAK>" if tags else "{BREAK}\n")
        elif c == 0x8002:
            n = codes[i + 1] if i + 1 < len(codes) else 0
            out.append(f"<WAIT:{n:02X}>" if tags else "{WAIT %d}" % n)
            i += 1
        elif c == 0xCDCD:
            pass
        elif c >= 0x8000:
            out.append(f"<{c:04X}>" if tags else "{%04X}" % c)
        elif glyphs and c < len(glyphs) and glyphs[c] is not None:
            out.append(glyphs[c])
        else:
            out.append(f"[{c}]")
        i += 1
    return "".join(out)


#: 校正用の書き方 (`<BR>` `<WAIT:0A>` `[123]`) を、実機の 2 バイト符号に数え戻すための切り方。
#: **decode() の裏返し**なので、向こうを直したらこちらも直すこと
VOICE_ONLY = re.compile(r"<VOICE:[0-9]{8}>")
MSG_TOKEN = re.compile(r"<BR>|<BREAK>|<WAIT:[0-9A-Fa-f]+>|<VOICE:[^>]*>|<[0-9A-Fa-f]{4}>|\[\d+\]|.", re.S)


def msg_codes(text: str) -> int:
    """その文が実機で何個の 2 バイト符号になるか (終わりの 0x8000 は数えない).

    `<WAIT:0A>` だけは **引数が付くので 2 個**。`<BREAK>` は引数の無いページ送りで 1 個。
    """
    n = 0
    for m in MSG_TOKEN.finditer(text):
        tok = m.group(0)
        if tok.startswith("<WAIT:"):
            n += 2
        else:
            n += 1
    return n


def msg_bytes(text: str) -> int:
    """その文を入れるのに要るバイト数 (符号 + 終わりの 0x8000).

    実物の `.msg` は 4 バイト境界まで `0xCDCD` で詰めてあるので、取り出した
    `size` は**詰め物の分だけ大きい**ことがある。詰め直すかどうかは入れる側の
    都合なので、ここでは**詰めない大きさ**を返す (比べる側が 4 未満の差を許す)。

    音声番号 (`<VOICE:00010001>`) の行だけは別扱い。あれは 4 個の符号が
    そのまま 1 件になっていて、**終わりの印が付かない** (`--keep-voice` で
    取り出すと `size` はちょうど 8)。
    """
    if VOICE_ONLY.fullmatch(text):
        return 8
    return (msg_codes(text) + 1) * 2


#: 公開ソースから借りてきた**形の数**。向こうの `MSG.py` / `UNPACK.py` の
#: どこを読めばいいかも一緒に書いておく (検査 `TestTheBorrowedNumbers` が
#: 実際にそこを読んで突き合わせる。#223)。
#:
#:   .msg の位置表の刻み … `readMSG` が `MSG_MODE` で `x*0x8`、
#:                          `MAP_MODE` / `OFFSET_ONLY_MODE` で `x*0x4`
#:   入れ物の項目の刻み  … `unpackMap` が `type == 0` で `entry_size = 0xC`、
#:                          それ以外で `8`
MSG_STRIDE = 8            # BOKU2.IMG の中の .msg (位置 + 長さ)
MAP_MSG_STRIDE = 4        # マップの中の会話 (位置だけ)
MAP_ENTRY = 8             # 入れ物の項目 (位置 + 長さ)
MAP_ENTRY_ALT = 12        # 日記・保存画面などの入れ物 (12 バイト刻み)

#: 実物の索引で、名前の置き場が始まる位置。英語化パッチの公開ソースが
#: `FILENAMES_START = 0x8140` と決め打ちしている値 (#222)。
#: レコードは見出し 16 バイトの後ろから 16 バイト刻みなので、
#: (0x8140 - 16) / 16 = 2067 件。社長の吸い出しで出た 1951 ファイルとの差 116 が
#: フォルダの数にあたる —— 割り切れること自体が、読み方の裏付けになっている
KNOWN_NAMES_AT = 0x8140
#: この数以上のレコードがあれば「実物なみ」とみなし、上の値と突き合わせる
#: (練習データは 33 件なので、そこで実物の値を持ち出しても雑音にしかならない)
REAL_INDEX_RECORDS_MIN = 1000

#: 索引が本体をどれだけ使い切っていれば「読めている」とみなすか。
#: ブラウザ側 (analyzeIndex) が候補から外す線と同じ 2 割にそろえてある
COVERAGE_MIN = 0.2


def parse_glyph_table(text: str) -> list:
    """文字表の 2 つの書き方 (ブラウザ側 parseGlyphTable と同じ).

    並び:   「あいうえお…」 (改行は無視。先頭が 0 番)
    対応表: 「12=あ」「13 い」「14: う」を 1 行ずつ。無い番号は None."""
    import re
    pair = re.compile(r"^\s*(\d+)\s*(?:[=:：＝]|\t| )\s*(\S)\s*$")
    table: dict[int, str] = {}
    text = text.replace("\ufeff", "")     # Windows のメモ帳/Excel が先頭に付ける BOM は文字ではない
    for line in text.replace("\r", "").split("\n"):
        m = pair.match(line)
        if m:
            table[int(m.group(1))] = m.group(2)
    if table:
        out: list = [None] * (max(table) + 1)
        for k, v in table.items():
            out[k] = v
        return out
    return list(text.replace("\r", "").replace("\n", ""))


def ansi_damage(glyphs: list | None) -> list[int]:
    """文字表のうち、**保存のときに潰れた疑いがある**番号 (#199).

    メモ帳の「ANSI」(cp932) で保存すると、cp932 に無い字は `?` になる。
    この作品のフォントには cp932 で書けない字が **4 つ** ある
    (`¥` `—` `♡` `︙` —— 公開ソースの font.txt 1656 字から数えた)。
    `♡` は台詞に普通に出るので、潰れると本文がその場で変わる。しかも
    半角の `?` になるため、校正では「半角文字が混ざっています」と出て、
    **訳文のせいに見える**。

    半角の `?` はフォントに 1 つだけ本当にある (公開ソースで確認) ので、
    **2 つ以上あれば潰れた疑い**。U+FFFD はどんな文字表にも入らない。
    """
    if not glyphs:
        return []
    marks = [i for i, g in enumerate(glyphs) if g == "?"]
    broken = [i for i, g in enumerate(glyphs) if g == "\ufffd"]
    return sorted(broken + (marks if len(marks) > 1 else []))


def ansi_damage_note(bad: list[int]) -> str:
    """`ansi_damage` の番号を、次の一手まで付けて 1 行にする."""
    if not bad:
        return ""
    return ("→ 文字表に `?` / 置き換え文字が "
            f"{len(bad)} 個あります (番号 "
            + " ".join(str(i) for i in bad[:8]) + ("…" if len(bad) > 8 else "")
            + ")。**メモ帳の「ANSI」で保存すると、cp932 に無い字が `?` になります** "
            "(この作品では ¥ — ♡ ︙ の 4 つ)。文字表は **UTF-8 で保存し直して**"
            "ください。このままだと本文の ♡ などが `?` になり、"
            "校正では「半角文字が混ざっています」と出ます")


def glyph_range_note(top: int, unseen: str = "") -> str:
    """**使われている文字番号の最大**が何を意味するか、1 行で言う (#230).

    `unseen` は「まだ診ていない所」の名前 (MAP の会話など)。そこがあるなら
    **これは本文ぜんぶの最大ではない**ので、頁の判定は出さない (#234)。
    ただし `FONT_GLYPHS` 以上という判定だけは、一部しか見ていなくても動かない
    —— 1 つでも収まらない番号が出た時点で、読み方が違うと決まる。

    数字だけ出すのは、出さないより悪い (#96)。この 1 つの数で 3 つ決まる:

    - `FONT_GLYPHS` 以上 → 文字表が 1656 字という見込みごと崩れる。
      2 バイト番号としての読み方が違うか、この作品の字数が違う。**報告する所**
    - 1 枚目に収まる → 2 枚目を探さなくても、この本文は全部読める
    - それ以外 → 2 枚目が要る。**何字ぶん**要るかまで分かる

    実物が届いた日にいちばん早く出る数で、文字表づくりの段取りがここで決まる。
    """
    if top >= FONT_GLYPHS:
        return (f"→ 使われている文字番号の最大が {top} で、この作品の文字表 "
                f"{FONT_GLYPHS} 字 (1 行 {FONT_COLS} 字 × {FONT_GLYPHS // FONT_COLS} 行) に"
                "収まりません。字数の見込みか、2 バイトを 1 字の番号として読む"
                "読み方そのものが違います。この行ごと報告してください")
    if unseen:
        return (f"  使われている文字番号の最大は {top} —— ただし**{unseen}を診ていない**ので、"
                "本文ぜんぶの数ではありません。文字表の 2 枚目が要るかどうかは、"
                "ここでは決まりません")
    if top < FONT_PAGE1_GLYPHS:
        return (f"  使われている文字番号の最大は {top}。文字表 {FONT_GLYPHS} 字のうち"
                f"**1 枚目 ({FONT_PAGE1_GLYPHS} 字) の範囲に収まる**ので、"
                "この本文を読むだけなら 2 枚目の画像は要りません")
    return (f"  使われている文字番号の最大は {top}。**1 枚目 ({FONT_PAGE1_GLYPHS} 字) を"
            f"超える**ので、2 枚目の画像が要ります (その先頭から {top - FONT_PAGE1_GLYPHS + 1} "
            "字ぶん)")


def glyph_table_trouble(text: str) -> list[str]:
    """文字表を**書き写すとき**の事故を見つける (#229).

    文字表は、フォント画像を左上から 1 行 23 字ずつ手で書き写して作る
    (docs/10 の手順 3)。公開ソースの font.txt も **72 行 × 23 字 = 1656 字**、
    font1.txt は 46 行 × 23 字、font2.txt は 26 行 × 23 字で、どの行もぴったり
    23 字だった。つまり「1 行 23 字」は実物の文字表の形そのもの。

    ここで 1 行だけ 22 字や 24 字になると、**その行から下の番号が全部ずれる**。
    ずれても全部の番号に字は当たるので、`text` は「文字表で全部読めました」と
    言い、TSV は日本語のまま出てくる。1 行だけ見ても絶対に分からない事故なので、
    **行の幅**という、書き写したときにしか残らない手がかりで見つける。

    番号は改行を捨てた並びで決まるので、行の幅そのものは読みに影響しない。
    幅は「どこで数え間違えたか」を指す目印として使う。
    """
    import re
    text = text.replace("\ufeff", "").replace("\r", "")
    lines = text.split("\n")
    pair = re.compile(r"^\s*(\d+)\s*(?:[=:：＝]|\t| )\s*(\S)\s*$")
    if any(pair.match(ln) for ln in lines):
        return []                       # 「12=あ」の対応表。行の幅に意味は無い
    # 空行は字を 1 つも足さないので番号には効かない。画像の何行目かは、空行を
    # 除いて数える。ファイルの行番号とずれたときは、そちらも添える (編集で探す先)
    rows = [(i + 1, len(ln)) for i, ln in enumerate(lines) if ln]
    if len(rows) < 2:
        return []                       # 1 行に流し込んだ書き方。幅では見られない
    notes: list[str] = []
    widths = [w for _, w in rows]
    common = max(set(widths), key=widths.count)
    # 最後の行が短いのは、まだ書き終えていないだけ (途中経過)。長いのは数え間違い
    bad = [(k, no, w) for k, (no, w) in enumerate(rows)
           if w != common and not (k == len(rows) - 1 and w < common)]
    if bad and common == FONT_COLS:
        k, no, w = bad[0]
        start = sum(widths[:k])         # その行の先頭の文字番号 (ここから下がずれる)
        in_file = f" (ファイルでは {no} 行目)" if no != k + 1 else ""
        notes.append(
            f"→ 文字表の書き写しがずれています: **{k + 1} 行目だけ {w} 字**です "
            f"(ほかの行は {common} 字)。フォント画像は 1 行 {FONT_COLS} 字なので、この行で "
            f"{w - common:+d} 字ずれたまま書き写すと、**{start} 番から下の字が全部ずれます**"
            + (f" (ほかにも {len(bad) - 1} 行)" if len(bad) > 1 else ""))
        notes.append(
            f"   直し方: フォント画像の {k + 1} 行目{in_file}を数え直してください。"
            "直したらもう一度かけて、この注意が消えることを確かめてください")
    total = sum(widths)
    # 書き上がりに近い字数のときだけ、全体の数でも見る。行を 1 つ飛ばしたり
    # 二度書いたりすると幅は全部 23 のままなので、幅では見つからない
    if total != FONT_GLYPHS and abs(total - FONT_GLYPHS) <= FONT_COLS * 2:
        gap = FONT_GLYPHS - total
        notes.append(
            f"→ 文字表の字数が合いません: **{total} 字**あります。"
            f"この作品の文字表は **{FONT_GLYPHS} 字** "
            f"(1 行 {FONT_COLS} 字 × {FONT_GLYPHS // FONT_COLS} 行) なので、"
            f"{abs(gap)} 字{'足りません' if gap > 0 else '多いです'}"
            + (f"。ちょうど {FONT_COLS} の倍数なので、行を 1 つ"
               f"{'飛ばした' if gap > 0 else '二度書いた'}疑いがあります"
               if gap % FONT_COLS == 0 else ""))
        notes.append("   まだ書き終えていない途中なら、書き終えてから見てください"
                     if gap > 0 else
                     "   直し方: 同じ行を二度書いていないか、上から数えて確かめてください")
    return notes


def font_trouble(path: str | None) -> list[str]:
    """`glyph_table_trouble` をファイル相手に回す."""
    if not path:
        return []
    with scrp.open_text(path) as fh:
        return glyph_table_trouble(fh.read())


def load_font(path: str | None) -> list | None:
    """フォント画像を左上から書き出したテキスト、または「番号=文字」の対応表."""
    if not path:
        return None
    with scrp.open_text(path) as fh:                  # BOM 付き / UTF-16 / cp932 でも同じに読む
        return parse_glyph_table(fh.read())


def text_rows(path: str, glyphs: list[str] | None, keep_voice: bool = False) -> list[tuple[str, int, int, str]]:
    """1 ファイルから (id, offset, size, text) の行を作る。入れ物 / 表の一覧 / 単体を自動で見分ける.

    音声の番号 (8 桁の数字) の項目は文章ではないので、既定では省く (校正の対象にならない)."""
    with open(path, "rb") as fh:
        b = fh.read()
    stem = os.path.splitext(os.path.basename(path))[0]
    # マップの部品 (OUT/maps/M_A01000/1.bin) はどれも 1.bin なので、id が全部 "1:…" で
    # ぶつかる。親フォルダの名前 (マップ名) を使う
    if os.path.basename(path).lower() == "1.bin":
        parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
        if parent:
            stem = parent
    return text_rows_bytes(b, stem, glyphs, keep_voice, alt=is_alt_break(path))


def text_rows_bytes(b: bytes, stem: str, glyphs, keep_voice: bool = False, alt: bool = False,
                    base: int = 0, depth: int = 0):
    got = parse_map_rec(b)
    if got:
        # 入れ物: 部品ごとに読む。刻み 8 (マップなど) の 0 番は命令列なので、
        # 見出しの無い並びとしては読まない (誤認を避ける)。刻み 12 (日記・保存画面) は全部試す
        rec, parts = got
        rows = []
        for it in parts:
            if not it["len"]:
                continue
            part = b[it["at"]:it["at"] + it["len"]]
            # 部品がさらに入れ物のことがある (fish_on_mem.bin の 11〜16 番の中の 2 番が魚の説明。
            # 公開ソース UNPACK.py の IMG_MAP_FILES)。入れ物は 8 バイト刻みの .msg と形が
            # 同じなので、先に入れ物として試す (位置が 16 バイト揃えで長さがつながる、という
            # 入れ物の方が条件が厳しい)。2 段まで降り、それより深いものは文言として読まない
            inner = parse_map_rec(part)
            if inner and sum(1 for x in inner[1] if x["len"]) >= 2:
                found = (text_rows_bytes(part, f"{stem}#{it['i']}", glyphs, keep_voice, alt,
                                         base + it["at"], depth + 1) if depth < 2 else [])
            else:
                allow_raw = rec == 12 or it["i"] != 0
                found = _rows_of(part, f"{stem}#{it['i']}", base + it["at"], glyphs, keep_voice, allow_raw, alt)
            rows += found
        return rows
    return _rows_of(b, stem, base, glyphs, keep_voice, True, alt)


def _rows_of(b: bytes, stem: str, base_off: int, glyphs, keep_voice: bool, allow_raw: bool, alt: bool = False):
    """1 つの塊から行を作る。表の一覧 → 単体 (8/4 刻み) → 見出しの無い並び の順に試す."""
    rows = []
    tables = parse_tables(b)
    if tables:
        for tb in tables:
            if not tb["msg"]:
                continue
            for it in tb["msg"]:
                if it["codes"] and (keep_voice or not voice_id(it["codes"])):
                    rows.append((f"{stem}:{tb['i']}-{it['i']}", base_off + tb["off"] + it["at"],
                                 len(it["codes"]) * 2, decode(it["codes"], glyphs, alt=alt)))
        return rows
    msg = pick_msg(b) or (parse_raw(b) if allow_raw else None)
    if msg:
        for it in msg:
            if it["codes"] and (keep_voice or not voice_id(it["codes"])):
                rows.append((f"{stem}:{it['i']}", base_off + it["at"], len(it["codes"]) * 2,
                             decode(it["codes"], glyphs, alt=alt)))
        return rows
    if allow_raw:
        sj = parse_sjis_list(b)
        if sj:
            for it in sj:
                rows.append((f"{stem}:{it['i']}", base_off + it["at"], len(it["text"].encode("cp932")),
                             it["text"].replace("\n", "<BR>")))
    return rows


def expand_patterns(paths: list[str], folder_files: bool = False) -> list[str]:
    """`MAP/*.*` のような指定を、道具の側で展開する.

    Windows のコマンドプロンプト / PowerShell は `*` を展開せずそのまま渡してくる
    (Linux / macOS のシェルは展開してから渡す)。どちらでも同じに動くよう、無いパスに
    `* ? [` が入っていれば glob で探す。folder_files=True ならフォルダは直下のファイルに
    置き換える (maps 用。text は expand_inputs が中まで辿る)。
    見つからなければ FileNotFoundError (入口で整えて表示する)."""
    import glob
    out: list[str] = []
    for p in paths:
        if os.path.exists(p):
            if folder_files and os.path.isdir(p):
                out += sorted(os.path.join(p, f) for f in os.listdir(p)
                              if os.path.isfile(os.path.join(p, f)))
            else:
                out.append(p)
        elif any(c in p for c in "*?["):
            hits = sorted(h for h in glob.glob(p) if not folder_files or os.path.isfile(h))
            if not hits:
                raise FileNotFoundError(f"{p}: 一致するファイルがありません")
            out += hits
        else:
            raise FileNotFoundError(f"{p}: ファイルがありません")
    return out


def unpicked_nearby(given: list[str]) -> list[str]:
    """渡されたファイルの**すぐ近く**にある、読めるのに渡されなかったファイル (#160).

    #159 で踏んだ穴: `text OUT/system/*.msg OUT/maps/*/1.bin` のように**並べて**
    渡すと、`system/` より深い `.msg` と入れ物 4 つが丸ごと落ちる。
    練習データでは 31 行のうち 12 行しか出ないのに、最後は
    「文字表で全部読めました」で終わる —— **足りないことに気づけない**。

    渡されたのがファイルばかりのときだけ、その**共通の親フォルダ**を辿って、
    拾えたはずのものを数える。フォルダを渡したときは `expand_inputs` が
    全部辿るので、ここは何も言わない。
    """
    files = [p for p in given if os.path.isfile(p)]
    if not files or len(files) != len(given):
        return []                      # フォルダ指定が混じっていれば黙る
    # **共通の親より上には登らない。** 1 段上げると関係ないフォルダまで数えてしまう
    root = os.path.commonpath([os.path.abspath(f) for f in files])
    if not os.path.isdir(root):
        root = os.path.dirname(root)
    if not root or not os.path.isdir(root):
        return []
    had = {os.path.abspath(f) for f in files}
    return [p for p in expand_inputs([root]) if os.path.abspath(p) not in had]


def unpicked_note(missed: list[str]) -> str:
    """`unpicked_nearby` の結果を 1 行にする。**`text` と `used` で同じ言葉**にする (#232)."""
    names = ", ".join(os.path.basename(f) for f in missed[:5])
    return (f"→ 同じ場所に、**渡されなかった**読めるファイルが {len(missed)} 個"
            f"あります ({names}{' …' if len(missed) > 5 else ''})。"
            "ファイルを並べるより、**フォルダごと渡す**と全部拾います")


def expand_inputs(paths: list[str]) -> list[str]:
    """引数のフォルダを中まで辿り、会話の入ったファイルだけを拾う.

    切り分けた本体 (OUT/) からは *.msg を、マップの部品 (OUT/maps/*/) からは 1.bin を、
    マップの入れ物 (MAP/) からはそのままのファイルを。ファイルを直接渡せばそのまま."""
    out: list[str] = []
    for p in paths:
        if not os.path.isdir(p):
            out.append(p)
            continue
        for root, dirs, files in os.walk(p):
            dirs.sort()
            for f in sorted(files):
                low = f.lower()
                if low.endswith(".msg") or f == "1.bin" or low in TEXT_CONTAINERS:
                    out.append(os.path.join(root, f))
    return out


# 本体の中で、会話以外の文言 (日記の雛形・保存画面・出来事の文・釣りの文言) が入っている
# 入れ物。公開ソースの IMG_MAP_FILES / IMG_MAP_FILES_TYPE_0 / RAW_MSG_FILES から
#: 並び順のまま出す。集合にして sorted すると、画面 (web/app.js の CONTAINERS) と
#: 「見つからない」の並びが違ってしまう。同じ報告のはずのものが違って見える (#104)
TEXT_CONTAINERS = ("diary.bin", "saveload.bin", "on_mem_event.bin", "fish_on_mem.bin")


def used_codes(paths: list[str]) -> list[int]:
    """複数ファイルで実際に使われている文字番号 (昇順)。制御コードと待ち時間の値、音声は除く.

    フォント画像を全部書き出さなくても、この番号だけ書き出せば本文は読める."""
    used: set[int] = set()
    for path in expand_inputs(paths):
        for _, _, _, text in text_rows(path, None):
            # 文字表なしの復号は [番号] の形なので、そこから拾う
            i = 0
            while True:
                i = text.find("[", i)
                if i < 0:
                    break
                j = text.find("]", i)
                if j < 0:
                    break
                if text[i + 1:j].isdigit():
                    used.add(int(text[i + 1:j]))
                i = j + 1
    return sorted(used)


def glyph_table_mapping(glyphs: list) -> dict[bytes, str]:
    """文字表を docs/01 の .tbl 用の対応 (2 バイトのリトルエンディアン → 文字) にする.

    ブラウザ側 glyphsToHexTable と同じ。tools/hexdump.py --table や dump_text.py で
    .msg のバイト列をそのまま日本語で見られる."""
    mapping: dict[bytes, str] = {}
    for i, g in enumerate(glyphs):
        if g is None or g == "":
            continue
        mapping[struct.pack("<H", i)] = g
    mapping[b"\x00\x80"] = "{END}"
    mapping[b"\x01\x80"] = "<BR>"
    mapping[b"\x02\x80"] = "<WAIT>"
    return mapping


#: TIM2 の見出しの直後に 0x70 の空きが入ることがある目印。公開ソース TIM2.py が
#: `whitespace1 == 0x4001a0` で見ているのと同じ値 (向こうの註釈は "literally why")。
#: 形式の欄が 1 のときは元から 0x80 進めるので、これはその欄が 0 のときの逃げ道 (#109)
TIM2_EXTRA_PAD_MARK = 0x4001A0


def tim2_header_at(b: bytes, at: int, fmt: int) -> int:
    """`at` から画像の見出しまでの距離。公開ソース TIM2.py と同じ判定にする."""
    if fmt:
        return 0x80
    if at + 12 <= len(b) and struct.unpack_from("<I", b, at + 8)[0] == TIM2_EXTRA_PAD_MARK:
        return 0x80
    return 0x10


def tim2_info(b: bytes) -> dict | None:
    """TIM2 の見出しだけ読む (ブラウザ側 parseTim2 の要点)。.tms の 0x80 前置きも見る."""
    for at in (0, 0x80, 0x10, 0x20, 0x40):
        if b[at:at + 4] == b"TIM2":
            fmt, count = b[at + 5], struct.unpack_from("<H", b, at + 6)[0]
            p = at + tim2_header_at(b, at, fmt)
            if p + 24 > len(b):
                return {"at": at, "format": fmt, "count": count}
            clut_colors = struct.unpack_from("<H", b, p + 14)[0]
            clut_type, image_type = b[p + 18], b[p + 19]
            w, h = struct.unpack_from("<HH", b, p + 20)
            return {"at": at, "format": fmt, "count": count, "width": w, "height": h,
                    "image_type": image_type, "clut_type": clut_type, "clut_colors": clut_colors}
    return None


def tim2_pages(b: bytes, limit: int = 8) -> list[dict]:
    """1 つのファイルに入っている TIM2 を**全部**探す (#168).

    `tim2_info` は決め打ちの数か所しか見ないので、1 枚目しか出てこない。
    ところが #167 で分かったとおり **文字表は 1 枚に収まらない**。2 枚目が
    同じファイルの後ろにあるなら、探せば見つかるはず —— それを確かめる道具。

    見出しの「絵の数」や大きさが無茶な値のものは、たまたま "TIM2" という 4 バイトが
    並んだだけなので落とす。見つかった順 (= ファイルの前から) に返す。
    """
    out, at = [], 0
    while len(out) < limit:
        at = b.find(b"TIM2", at)
        if at < 0:
            break
        info = tim2_info(b[at:at + 0x100])
        # tim2_info は先頭からの相対位置を返すので、ファイル内の位置に直す
        if info and info.get("width") and 0 < info["width"] <= 4096 and 0 < info["height"] <= 4096:
            out.append({**info, "at": at + info["at"]})
        at += 4
    return out


def font_page_cells(info: dict) -> int:
    """その画像に番号を振れるマスの数 (列 × 行)."""
    if not info.get("width") or not info.get("height"):
        return 0
    return (info["width"] // FONT_CELL) * (info["height"] // FONT_CELL)


def looks_like_a_font_page(info: dict) -> bool:
    """文字表の続きが入っていそうな画像か (#168).

    決め手は **1 行 23 字の幅で割り切れること**。文字表はどの頁も同じ升目で
    並んでいるはずなので、列数が 23 にならない画像は続きではない。
    """
    return (info.get("width") or 0) // FONT_CELL == FONT_COLS and font_page_cells(info) > 0


#: **ほかの**ファイルから読む上限。実物の索引は 1951 個あるので、全部を丸ごと読むと遅い
FONT_HUNT_HEAD = 64 * 1024
#: ただし **64KB では足りないファイルが実在する** (#219)。英語化パッチの公開ソースに
#: 残っている書き出しの名前を見ると、`title.tms` は TIM2 を **0x150100 / 0x164580 /
#: 0x18b780** に、`saveload.bin` は 0x22100 / 0x28e80 に持っている。文字表の続きが
#: この形のファイルに入っていたら、64KB しか読まないこちらは**必ず見落とす**。
#: そこで「いかにも font らしい名前」と `.tms` だけは深く読む。数を絞れば安い
DEEP_HUNT_FILES = 20


def worth_a_deep_look(path: str) -> bool:
    """文字表の続きが入っていそうな**名前**か (深く読む価値があるか)."""
    low = os.path.basename(path).lower()
    return "font" in low or low.endswith(".tms")
#: **フォント自身**のファイルから読む上限 (#171)。ここを 64KB にしていたのが誤りだった。
#: 実物の頁 1 枚は 512×1024 ドットの 8bit 索引で **51 万バイト**あり、2 枚目は 0x7D508
#: あたりに来る。64KB しか読まなければ、同じファイルの 2 枚目は**必ず見落とす** ——
#: 練習データは頁が小さいので通っていただけだった
FONT_OWN_CAP = 4 * 1024 * 1024
#: `check` が中身まで開く `.msg` の数 (#195)。
#:
#: ここは長らく **50** だった。練習データの `.msg` は 4 件なので全部入り、
#: 「先頭 50 件のうち読めた形: 4 件」で何も困らない。ところが実物は
#: **651 件**あり、**601 件は触れてもいないのに締めは「問題なし」**になる。
#: 数えたら 651 件を全部開いても 0.04 秒しか変わらなかったので、上限を上げた。
#: それでも上限は残す (壊れた吸い出しで何万件になっても止まらないように)。
#: **上限に当たったら「診ていない段」に数える** ので、黙って減ることはない
MSG_CHECK_FILES = 2000

#: 名前で拾えなかったときに、**中身の形**で探す索引の件数 (#196)。
#:
#: 長らく **400** だった (`FONT_HUNT_FILES` という名前で、本文・入れ物・フォントの
#: 3 か所が使い回していた)。実物は 1951 件あるので、
#: **401 件目から先にある本文もフォントも、名前が読めないと永久に見つからない**。
#: 名前が読めない吸い出しはまさにこの道しか無いのに、その道が 2 割で終わっていた。
#: 数えたら、全件の先頭を読んでも 32 MB / 0.03 秒。上げない理由が無かった。
#: 上限そのものは残す (壊れた索引が何万件を名乗っても止まらないように)
SHAPE_HUNT_FILES = 4000


def pick_fonts(img, entries: list[dict]) -> tuple[list[dict], bool]:
    """フォント画像らしいファイルを選ぶ。名前で拾えなければ**形で拾う** (#172).

    今までは名前に `font` が入っているかだけで選んでいた。ところが docs/09 の
    「実物で確かめたこと」に書いてあるとおり、**社長の実物では名前が付かず
    `#0 #1 …` のままだった** (#1・#3)。名前の並びの読み方はその後直したが、
    実物で通ったことはまだ一度も無い。**名前が付かなかったら、フォントの診断が
    まるごと飛ぶ** —— しかも「フォントが見つかりません」とも言わずに。

    そこで、名前で 1 つも拾えなかったときは中身を見る。決め手は
    `looks_like_a_font_page` (1 行 23 字の幅で割り切れること)。

    @returns (選んだファイル, 形で拾ったか)
    """
    named = [e for e in entries if "font" in os.path.basename(e["path"]).lower()]
    if named:
        return named, False
    found = []
    for e in entries[:SHAPE_HUNT_FILES]:
        if e["len"] < 1024:
            continue
        img.seek(e["at"])
        for p in tim2_pages(img.read(min(e["len"], FONT_HUNT_HEAD)), limit=2):
            if looks_like_a_font_page(p):
                found.append(e)
                break
        if len(found) >= 3:
            break
    return found, True


#: 形で探すとき、**いくつ見つけた所で打ち切るか** (#197)。
#: 診断は標本で足りるので全部は集めない。ただし打ち切ったら**そう言う**こと ——
#: 「.msg: 50 件」と出したら、社長は 50 件しか無いと読む
SHAPE_PICK_LIMIT = 50


def pick_by_shape(img, entries: list[dict], test, limit: int = SHAPE_PICK_LIMIT) -> list[dict]:
    """名前ではなく**中身**で選ぶ (#173).

    `pick_fonts` (#172) と同じ考え方を、本文と入れ物にも広げる。索引は 1951 個
    あるので、1 つあたりは先頭だけを読み、見つかった数で打ち切る。

    @param test 読めたら真を返す関数 (bytes -> bool)
    """
    out = []
    for e in entries[:SHAPE_HUNT_FILES]:
        if e["len"] < 16:
            continue
        img.seek(e["at"])
        try:
            if test(img.read(min(e["len"], FONT_HUNT_HEAD))):
                out.append(e)
        except (ValueError, struct.error, IndexError):
            continue
        if len(out) >= limit:
            break
    return out


def font_page_hunt(img, entries: list[dict], font_entry: dict, cells: int,
                   known: set | None = None, wide: int = 0) -> list[str]:
    """文字表の続きが入っていそうな画像を、**同じ吸い出しの中から**挙げる (#168).

    #167 で「1 枚では足りない」と言えるようになったが、**どこを見ればいいかは
    言えていなかった**。社長は「別の画像にある」と言われても、1951 個のどれかは
    分からない。探すのは道具の仕事。

    探し方は 2 段:

    1. **同じファイルの後ろ**。`bk_font.tms` は `TMS\\0` + 前置き + TIM2 という
       作りなので、2 枚目が同じファイルに続いていてもおかしくない
    2. **ほかのファイル**。決め手は `looks_like_a_font_page` —— 1 行 23 字の幅で
       割り切れること。文字表はどの頁も同じ升目で並んでいるはず

    見つからなければ「見つからなかった」と言う。**黙って何も出さない**と、
    探したのか探していないのかが分からない。
    """
    want = FONT_GLYPHS - cells
    out = []

    img.seek(font_entry["at"])
    # known は上で既に数えた頁の位置。**そこを候補に数えない** ——
    # 数えた画像を「もう 1 枚あります」と出すと、足し算が二重になる
    seen = known or set()
    same = [p for p in tim2_pages(img.read(min(font_entry["len"], FONT_OWN_CAP)))
            if p["at"] not in seen and looks_like_a_font_page(p)]
    for p in same:
        out.append(f"  ・同じファイルの位置 0x{p['at']:X} にもう 1 枚 "
                   f"({p['width']}×{p['height']} ドット / {font_page_cells(p)} マス)")

    # **1 枚目と同じ幅のものを先に挙げる** (#211)。幅 506〜527 ドットの画像は
    # 背景や UI にいくらでもあるので、「23 字で割り切れる」だけでは候補が多すぎる。
    # 文字表はどの頁も同じ升目なので、**続きなら幅は 1 枚目と同じ**はず。
    # 英語化パッチの公開ソースに残っている 2 枚の画像も 512×1024 と 512×640 で、
    # 幅は揃っていた (大きさを見ただけで、中身は使っていない)
    same, other_w = [], []
    deep = 0
    for other in entries[:SHAPE_HUNT_FILES]:
        if other is font_entry or other["len"] < 1024:
            continue
        # **名前で深さを変える** (#219)。全部を深く読むと 1951 個ぶんで重くなるが、
        # font らしい名前と `.tms` だけなら安い。実物の `title.tms` は 0x18b780 に
        # 画像を持っているので、64KB しか読まないと届かない
        cap = FONT_HUNT_HEAD
        if worth_a_deep_look(other["path"]) and deep < DEEP_HUNT_FILES:
            cap = FONT_OWN_CAP
            deep += 1
        img.seek(other["at"])
        for p in tim2_pages(img.read(min(other["len"], cap)), limit=2):
            if not (looks_like_a_font_page(p) and font_page_cells(p) >= want):
                continue
            (same if wide and p["width"] == wide else other_w).append((other["path"], p))
            break
        # **同じ幅を 5 件見つけるまでは探し続ける。** 幅の違うものが先に 5 件
        # 見つかっただけで打ち切ると、本命が一覧に載らない
        if len(same) >= 5:
            break
    for path, p in same:
        out.append(f"  ・{path} (位置 0x{p['at']:X} / {p['width']}×{p['height']} ドット / "
                   f"{font_page_cells(p)} マス) ← 1 枚目と同じ幅")
    for path, p in other_w[:3]:
        out.append(f"  ・{path} (位置 0x{p['at']:X} / {p['width']}×{p['height']} ドット / "
                   f"{font_page_cells(p)} マス)")

    if out:
        head = (f"  続きが入っていそうな画像 {len(out)} 件 "
                f"(1 行 {FONT_COLS} 字の幅で、残り {want} 字が入る大きさ):")
        if wide and same:
            head = (f"  続きが入っていそうな画像 {len(out)} 件 "
                    f"(1 行 {FONT_COLS} 字の幅で、残り {want} 字が入る大きさ。"
                    f"**1 枚目と同じ幅 {wide} ドット**のものから先に挙げます):")
        return [head] + out
    return [f"  この吸い出しの中には続きが見つかりませんでした "
            f"(1 行 {FONT_COLS} 字の幅で {want} 字ぶん入るものを "
            f"{min(len(entries), SHAPE_HUNT_FILES)} 個まで探した。"
            f"名前に font が付くものと .tms は先頭 {FONT_OWN_CAP // 1024 // 1024} MB まで、"
            f"ほかは先頭 {FONT_HUNT_HEAD // 1024} KB までを見た)。"
            "この行ごと報告してください"]


def stopped_here(problems: int, why: str) -> str:
    """途中で止めたときの締めの行。この先を診ていないことまで書く.

    素人が最初に踏むのは「フォルダ違い」と「ファイル違い」で、そこで締めの行が
    出ないと、道具が落ちたのか診た結果なのかが分からない (#73)。
    """
    return (f"\n== 結果: 確認事項 {problems} 件 (上の → の行)。{why}ここで止めました"
            "。この先 (本体・.msg・フォント・MAP) は診ていません。この出力ごと報告してください")


def find_map_dir(folder: str, depth: int = 2) -> str | None:
    """`MAP` フォルダを、直下だけでなく**少し下まで**探す (#174).

    今までは指定されたフォルダの直下しか見ていなかった。吸い出し方によっては
    包みのフォルダが 1 つ増える (`吸い出し/DATA/MAP` など) ので、そのとき
    「MAP/: 無い」となり、**物語の会話をまるごと診ないまま「問題なし」**で
    終わっていた。

    深さは 2 段まで。それ以上潜ると、無関係なフォルダを拾う危険のほうが大きい。
    """
    for d in range(depth + 1):
        base = [folder]
        for _ in range(d):
            nxt = []
            for b in base:
                try:
                    nxt += [os.path.join(b, n) for n in sorted(os.listdir(b))
                            if os.path.isdir(os.path.join(b, n))]
                except OSError:
                    continue
            base = nxt[:16]          # 枝が増えすぎないように
        for b in base:
            try:
                for n in sorted(os.listdir(b)):
                    if n.lower() == "map" and os.path.isdir(os.path.join(b, n)):
                        return os.path.join(b, n)
            except OSError:
                continue
    return None


def scan_map_folder(map_dir: str) -> dict:
    """`MAP/` を一通り読んで、件数・会話の行数・**使われている文字番号**を返す (#231).

    `check` の [MAP] の行を出すための下読み。**文字表の判定より先に呼ぶ**こと ——
    物語の会話はこの作品の本文の大半で、`.msg` だけを見て「文字表は足りている」と
    言うと、いちばん量の多い所を見ないまま太鼓判を押すことになる。
    """
    got: dict = {"files": sorted(os.listdir(map_dir)), "ok_map": 0, "ok_talk": 0,
                 "lines": 0, "bad": [], "no_talk": None, "used": set()}
    for name in got["files"]:
        path = os.path.join(map_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            b = fh.read()
        items = parse_map(b)
        if not items:
            got["bad"].append((name, b[:16].hex(" ").upper()))
            continue
        got["ok_map"] += 1
        one = next((it for it in items if it["i"] == 1 and it["len"]), None)
        if one:
            tables = parse_tables(b[one["at"]:one["at"] + one["len"]])
            if tables:
                got["ok_talk"] += 1
                got["lines"] += sum(1 for t in tables if t["msg"] for it in t["msg"] if it["codes"])
            elif got["no_talk"] is None:
                got["no_talk"] = (name, b[one["at"]:one["at"] + 16].hex(" ").upper())
        got["used"] |= used_numbers_of(b, name)
    return got


def used_numbers_of(b: bytes, name: str) -> set:
    """1 ファイルの本文が使っている文字番号 (文字表なしで読んで `[番号]` を拾う)."""
    used: set = set()
    for _, _, _, text in text_rows_bytes(b, name, None, alt=is_alt_break(name)):
        used.update(int(x) for x in re.findall(r"\[(\d+)\]", text))
    return used


def check(folder: str, out=sys.stdout) -> int:
    """吸い出したフォルダを一通り診て、報告用の要約を出す (ゲームの本文は出さない).

    docs/10 の手順に入る前に走らせる。ここで外れた所が、次の手がかりになる。"""
    def say(s=""):
        out.write(s + "\n")

    problems = 0
    # **診ていない段**を数える (#175)。→ が 1 本も出なければ「問題なし」と言って
    # いたが、それは「全部を診た結果」ではなく「診た分には問題が無かった」でしかない。
    # 社長は前者の意味で読む。何を診ていないかは、結果の行に必ず出す
    skipped: list[str] = []
    idx_path = next((os.path.join(folder, n) for n in os.listdir(folder) if n.lower() == "boku2.idx"), None)
    img_path = next((os.path.join(folder, n) for n in os.listdir(folder) if n.lower() == "boku2.img"), None)
    map_dir = find_map_dir(folder)
    say(f"== 診断: {folder}")
    say(f"BOKU2.IDX: {'あり' if idx_path else '無い'} / BOKU2.IMG: {'あり' if img_path else '無い'}"
        + " / MAP/: " + ("あり" if map_dir else "無い")
        + (f" ({os.path.relpath(map_dir, folder)})"
           if map_dir and os.path.dirname(os.path.relpath(map_dir, folder)) else ""))
    if not (idx_path and img_path):
        say("→ 索引と本体が揃っていません。吸い出したフォルダの直下を指定してください")
        # よくある間違いを 2 つ見分ける: ISO のまま / 一段深いフォルダに入っている
        names = sorted(os.listdir(folder))
        images = [n for n in names if n.lower().endswith((".iso", ".bin", ".img", ".cue", ".mdf", ".nrg"))
                  and os.path.isfile(os.path.join(folder, n)) and n.lower() not in ("boku2.img",)]
        if images:
            say(f"   ディスクイメージのまま ({', '.join(images[:3])}) のようです。"
                "先に 7-Zip などで展開して、中の BOKU2.IDX / BOKU2.IMG / MAP のあるフォルダを指定してください (docs/05)")
        for n in names:
            sub = os.path.join(folder, n)
            if os.path.isdir(sub) and any(m.lower() == "boku2.idx" for m in os.listdir(sub)):
                say(f"   一段下の {n}/ に BOKU2.IDX があります。そちらを指定してください: boku2.py check {sub}")
                break
        say(stopped_here(1, "索引と本体が見つからないので"))
        return 1

    with open(idx_path, "rb") as fh:
        idx = fh.read()
    img_size = os.path.getsize(img_path)
    say(f"\n[索引] {len(idx):,} バイト / 先頭 4 バイト {idx[:4].hex(' ').upper()}"
        + (" (DFI: 期待どおり)" if idx[:4] == b"DFI\0" else " (DFI ではない!)"))
    if idx[:4] != b"DFI\0":
        say("→ 先頭が DFI でないので、この道具の索引の読みは使えません。先頭 64 バイトを報告してください")
        say("   " + idx[:64].hex(" ").upper())
        say(stopped_here(1, "索引が読めないので"))
        return 1
    rec_end = 16
    while rec_end + 16 <= len(idx) and (idx[rec_end] | (idx[rec_end + 1] << 8)) in (0, 1):
        rec_end += 16
    entries = read_dfi(idx, img_size)
    rec_count = (rec_end - 16) // 16
    named = sum(1 for e in entries if not os.path.basename(e["path"]).startswith("#"))
    dupes = sum(1 for e in entries if "~" in os.path.basename(e["path"]))
    used = sum(e["len"] for e in entries)
    say(f"レコード {rec_count} 件 (名前の置き場は 0x{rec_end:X} から) / ファイル {len(entries)} 件 / 名前が付いた {named} 件"
        + (f" / 同じ名前 {dupes} 件" if dupes else ""))
    # **実物の索引には、外から確かめられる数がある** (#222)。英語化パッチの公開ソースは
    # 名前の置き場を `FILENAMES_START = 0x8140` と決め打ちしている。実物なみの
    # 大きさの索引でここが合っていれば、レコードの読み方 (16 バイト刻み・見出し 16 バイト)
    # が当たっている裏付けになる。ずれていれば、名前が付かない原因がここだと分かる
    if rec_count >= REAL_INDEX_RECORDS_MIN:
        if rec_end == KNOWN_NAMES_AT:
            say(f"   名前の置き場が 0x{KNOWN_NAMES_AT:X} —— 英語化パッチの公開ソースが"
                "決め打ちしている値と同じです (レコードの読み方が当たっている裏付け)")
        else:
            problems += 1
            where, want = f"0x{rec_end:X}", f"0x{KNOWN_NAMES_AT:X}"
            say(f"→ 名前の置き場が {where} です。英語化パッチの公開ソースは実物を "
                f"{want} と決め打ちしているので、**レコードの読み方が"
                f"ずれている疑い**があります (差 {rec_end - KNOWN_NAMES_AT:+,} バイト = "
                f"{(rec_end - KNOWN_NAMES_AT) / 16:+.1f} 件ぶん)。この行ごと報告してください")
    # **取り出せない項目があれば、その数と理由を言う** (#178)。今までは使用率の
    # 行から「索引の読み方が外れている疑い」とだけ言っていたが、**吸い出しが
    # 途中で切れている**ときも同じ見え方になる。数を出せば見分けがつく
    dropped = dfi_dropped(idx, img_size)
    note = dropped_note(dropped)
    if note:
        problems += 1
        say(note)
    # フォルダを解決した後の名前を出す。以前は名前の置き場から生の文字列を順に
    # 読んでいたので、根の "/" やフォルダ名そのもの (`00diary`) が混ざり、
    # ファイルはフォルダ抜きで並んでいた (`nik000.tm2`)。docs/10 が 20 分の所で
    # 見るよう言っているのは**フォルダ付きの名前が並ぶか**なので、解決できて
    # いるのに「付いていない」に見える。unpack が実際に書くのはこちらの名前 (#104)
    say(f"最初の名前: {' / '.join(e['path'] for e in entries[:5])}")
    # 「索引が指す合計」が本体に対して何割か。読み方が合っていれば本体はだいたい
    # 使い切られる。低いと、索引の読み方 (レコードの長さや位置の単位) が外れている
    # 疑いが濃い。数字だけ出して判定に使っていなかったので、12% でも「問題なし」と
    # 言っていた (#96)。基準の 2 割は、ブラウザ側が候補から外す線と同じ
    coverage = used / max(1, img_size)
    say(f"[本体] {img_size:,} バイト / 索引が指す合計 {used:,} バイト "
        f"({100 * coverage:.1f}% — 索引が本体をどれだけ使い切っているか。"
        "読み方が合っていれば普通は 5 割を超えます)")
    if coverage < COVERAGE_MIN:
        problems += 1
        say(f"→ 索引が本体の {100 * coverage:.1f}% しか指していません。"
            "索引の読み方 (レコードの長さ・位置の単位) が外れている疑いがあります。"
            "この行と下の先頭 64 バイトを報告してください")
        say("   " + idx[:64].hex(" ").upper())
    # **中身が空なら、形式の話をする前にそれを言う** (#218)。索引だけ正しくて
    # 中身がゼロの吸い出しは、この先の段 (.msg・入れ物・フォント) を全部
    # 「読めない」に見せる。原因は 1 つ上にあるので、先に名指しする
    with open(img_path, "rb") as probe:
        looked, empty = body_looks_empty(probe, entries)
    if looked >= 5 and empty >= looked * 0.9:
        problems += 1
        say(f"→ 本体の中身がほとんど空です (覗いた {looked} 個のうち {empty} 個がゼロ埋め)。"
            "吸い出しが途中で切れたか、コピーが終わっていない疑いがあります。"
            "この先の診断 (.msg・入れ物・フォント) は当てになりません。"
            "ファイルの大きさと、コピー元の残り容量を確かめてください")
    if named < len(entries) * 0.9:
        problems += 1
        say("→ 名前が付かないファイルが多い。名前の置き場 (上の 0x…) 付近の 64 バイトを報告してください")
        # **どこで止まったかが分かるなら、そこを指す** (#202)。名前の置き場の
        # 先頭を見ても何も無い。原因は途中の 1 バイトのことが多い
        stop_note = dfi_name_stop_note(dfi_name_stop(idx))
        if stop_note:
            say(stop_note)
        say("   " + idx[rec_end:rec_end + 64].hex(" ").upper())
    if dupes:
        problems += 1
        say("→ 同じ名前があります。フォルダの入れ子の規則が実物と違うかもしれません (docs/09 #18)")
    # フォルダの閉じ方は 2 通りの読み方 (こちらの stack / 公開ソースの flag) がある。実物で
    # 一致していれば安心、違えば規則の理解が足りていない (docs/09 #56)
    mism = dfi_rule_mismatch(idx, img_size)
    if mism:
        problems += 1
        say(f"→ フォルダの規則が 2 通りで食い違うファイル {len(mism)} 件 (例: {mism[0][0]} / {mism[0][1]})。"
            "この行ごと報告してください")
        # **どちらが正しいかは、名前で決まる** (#221)。英語化パッチの公開ソースは
        # `system\namemsg\namemsg.msg` のような道でファイルを開いている。
        # 上の 2 つのうち、その形になっている側が当たり。docs/09 の未解決その 2 は
        # 実物のこの行で決着する ——「報告してください」だけで終わらせない
        say("   決め方: 英語化パッチの公開ソースは system\\namemsg\\namemsg.msg のような"
            "**フォルダ付きの道**でこの作品のファイルを開いています。"
            "上の 2 つのうち、その形になっている側が正しい読み方です (docs/09 の「実物で確かめたこと」)")
    elif dfi_rule_tested(idx, img_size):
        say(f"フォルダの規則: 2 通り (stack / flag) で一致 "
            f"(フォルダの中のファイル {dfi_rule_tested(idx, img_size)} 件で突き合わせた)")
    else:
        # 入れ子が無ければ 2 通りは必ず同じ答えを出す。「一致」と書くと裏付けに見える
        say("フォルダの規則: この索引に入れ子が無いので、2 通りの違いは出ません (試せていない)")
        skipped.append("フォルダの閉じ方 (この索引に入れ子が無い)")
    msgs = [e for e in entries if e["path"].lower().endswith(".msg")]
    bases = {os.path.basename(e["path"]).lower() for e in entries}
    found = [n for n in TEXT_CONTAINERS if n in bases]
    missing = [n for n in TEXT_CONTAINERS if n not in bases]

    # **MAP の会話を、文字表の判定より先に読む** (#231)。この作品の本文の大半は
    # 物語の会話で、`.msg` だけを見て「文字表は足りている」と言うのは、いちばん
    # 量の多い所を見ないまま太鼓判を押すこと。[MAP] の行は今までどおり下に出す
    m = scan_map_folder(map_dir) if map_dir else None
    with open(img_path, "rb") as img:
        # 名前で 1 つも拾えなければ**中身の形**で探す (#173)。名前が付かない索引でも、
        # 本文と入れ物の診断が黙って飛ばないように (#172 をフォントから広げた)
        msg_by_shape = container_by_shape = False
        if not msgs:
            msg_by_shape = True
            msgs = pick_by_shape(img, entries, lambda b: bool(pick_msg(b, {})))
        # **打ち切った数を、あった数のように出さない** (#197)
        msg_capped = msg_by_shape and len(msgs) >= SHAPE_PICK_LIMIT
        say(f".msg: {len(msgs)}{' 件以上' if msg_capped else ' 件'}"
            + (f" (例: {', '.join(os.path.basename(e['path']) for e in msgs[:4])})" if msgs else "")
            + ("。名前で拾えなかったので**中身の形**で探しました" if msg_by_shape and msgs else "")
            + (f" ({SHAPE_PICK_LIMIT} 件見つけた所で打ち切りました)" if msg_capped else ""))
        if msg_capped:
            skipped.append(f"本文の残り (形で {SHAPE_PICK_LIMIT} 件見つけた所で打ち切り。"
                           "実際はもっとあります)")
        shaped_containers: list[dict] = []
        if not found:
            container_by_shape = True
            shaped_containers = pick_by_shape(img, entries, lambda b: parse_map(b) is not None)
        if found or not container_by_shape:
            say(f"[入れ物] 文言の入れ物: あり {', '.join(found) or 'なし'}"
                + (f" / 見つからない {', '.join(missing)}" if missing else ""))
        else:
            say(f"[入れ物] 名前で拾える文言の入れ物はありません ({', '.join(TEXT_CONTAINERS)})。"
                f"**中身の形**で探すと {len(shaped_containers)} 件"
                + (f" (例: {', '.join(e['path'] for e in shaped_containers[:4])})"
                   if shaped_containers else ""))
        if not msgs and not shaped_containers:
            problems += 1
            say("→ 本文の入っていそうなファイルが 1 つも見つかりません。"
                f"名前 (`.msg` / {', '.join(TEXT_CONTAINERS)}) でも、中身の形でも "
                f"{min(len(entries), SHAPE_HUNT_FILES)} 個まで探しました。"
                "この行ごと報告してください")
        ok_msg, first_bad = 0, None
        looked_msgs = min(MSG_CHECK_FILES, len(msgs))
        msg_used: set[int] = set()
        len_ok, len_ng = 0, 0            # 8 バイト刻みの後ろ 4 バイト (項目のバイト長) が合うか
        for e in msgs[:MSG_CHECK_FILES]:
            img.seek(e["at"])
            b = img.read(e["len"])
            info: dict = {}
            if pick_msg(b, info) or parse_tables(b) or parse_raw(b) or parse_sjis_list(b):
                ok_msg += 1
                if info.get("len_field") == "ok":
                    len_ok += 1
                elif info.get("len_field") == "ng":
                    len_ng += 1
                # 文字表の確認用に、使われている番号も拾っておく (文字表なしの復号は [番号] の形)
                msg_used |= used_numbers_of(b, e["path"])
            elif first_bad is None:
                first_bad = (e, b[:16], b)
        if msgs:
            looked = looked_msgs
            if msg_by_shape:
                # **形で拾ったときは、この数に意味が無い** (#197)。選び方そのものが
                # 「読めたか」なので、答えは必ず「全部読めた」になる。
                # それを数で出すと、**確かめた結果のように読める**
                say(f"  この {looked} 件は**読めたから選んだ**ものです "
                    "(名前が読めないので中身の形で拾いました)。"
                    "読めた件数は、確かめた結果ではありません")
            else:
                say(f"  先頭 {looked} 件のうち読めた形: {ok_msg} 件")
            if len(msgs) > looked:
                # **診ていないものは、診ていないと言う** (#175 と同じ理由、#195)。
                # 上限が 50 だったころ、実物の 651 件のうち 601 件は**触れてもいない**のに
                # 「先頭 50 件のうち読めた形: 50 件」としか出ず、締めは「問題なし」だった
                skipped.append(f"本文 {len(msgs) - looked} 件 "
                               f"(.msg が {len(msgs)} 件あり、先頭 {looked} 件だけ診ました)")
            if len_ok or len_ng:
                say(f"  位置表の長さの欄: 合う {len_ok} 件 / 合わない {len_ng} 件")
                if len_ng and msg_by_shape:
                    # **形で拾ったときは、選び方そのものが当て推量**なので → にしない (#173)。
                    # 入れ物も「読めてしまう」ので、長さの欄が合わないのは当たり前。
                    # ここで → を出すと、名前が読めないだけの吸い出しで毎回赤が出る
                    say(f"  位置表の長さの欄: 合わない {len_ng} 件。"
                        "ただし**名前ではなく形で拾った**ので、本文でないものが混ざっています。"
                        "名前が読めるようになってから見直してください")
                elif len_ng:
                    # 「この行ごと報告」と言いながら確認事項に数えていなかったので、
                    # 最後の行は「問題なし」のままだった。報告してほしいなら数える (#97)
                    problems += 1
                    say(f"→ 位置表の長さの欄が {len_ng} 件合いません。8 バイト刻みの後ろ 4 バイトが"
                        "その項目のバイト長だ、という読みがこの作品では違うかもしれません"
                        " (合わない分は位置だけで読んでいます)。この行ごと報告してください")
            if first_bad:
                problems += 1
                e, head, body = first_bad
                say(f"→ 読めない .msg の例: {e['path']} 先頭 16 バイト "
                    f"{head.hex(' ').upper()}{guess_kind_note(head, body)}")
        # **入れ物 (日記・保存画面・出来事・釣り) の文言も数に入れる** (#231)。
        # 名前で拾えたときは `.msg` に入らないので、そのままだと丸ごと落ちていた
        # (皮肉なことに、名前が読めない吸い出しのほうが多く読めていた)
        box_used: set[int] = set()
        boxes = ([e for e in entries if os.path.basename(e["path"]).lower() in TEXT_CONTAINERS]
                 or shaped_containers)
        for e in boxes[:MSG_CHECK_FILES]:
            img.seek(e["at"])
            box_used |= used_numbers_of(img.read(e["len"]), e["path"])
        map_used = m["used"] if m else set()
        used_here = msg_used | box_used | map_used
        # **診ていない所があるなら、いちばん大きい番号は本文ぜんぶの数ではない** (#234)。
        # #232 で `used` に足したのと同じ断り。ここで黙ると「2 枚目は要りません」を
        # 本文の一部だけで言い切ることになる
        unseen = []
        if not (m and m["files"]):
            unseen.append("MAP の会話")
        if len(msgs) > looked_msgs:
            unseen.append(f"本文 {len(msgs) - looked_msgs} 件")
        if len(boxes) > MSG_CHECK_FILES:
            unseen.append(f"入れ物 {len(boxes) - MSG_CHECK_FILES} 件")
        # MAP が無い / 空のときは、0 種と書かずに**診ていない**と言う。0 は
        # 「見た結果 0」に読めるが、ここは「見ていない」。数の意味が違う
        used_by = [f".msg {len(msg_used)}", f"入れ物 {len(box_used)}",
                   f"MAP の会話 {len(map_used)}" if m and m["files"] else "MAP は診ていない"]
        # 文字表 (font.txt) がこのフォルダにあれば、その出来具合も診る (docs/10 の手順 3 の途中経過)
        # **使われている文字番号の最大**は、実物で最初に出る大事な数 (#230)。
        # 数字だけ出さず、文字表づくりの段取りが決まる所まで言う
        if used_here:
            say(glyph_range_note(max(used_here), "と".join(unseen)))
            if max(used_here) >= FONT_GLYPHS:
                problems += 1
        font_txt = next((os.path.join(folder, n) for n in os.listdir(folder) if n.lower() == "font.txt"), None)
        if font_txt:
            glyphs = load_font(font_txt) or []
            missing = sorted(u for u in used_here if u >= len(glyphs) or glyphs[u] is None)
            say(f"[文字表] font.txt: {sum(1 for g in glyphs if g)} 字 / 本文で使われている番号 "
                f"{len(used_here)} 種 ({' / '.join(used_by)}) のうち"
                f"文字表に無い {len(missing)} 種"
                + (f" (例: {' '.join(str(u) for u in missing[:10])}{' …' if len(missing) > 10 else ''})。"
                   "フォント画像のこの番号を書き足す (docs/10 の手順 3)" if missing
                   else "。文字番号を使っている行が無いので、文字表は試せていない" if not used_here
                   else "。この範囲は全部読める"))
            # **保存のときに潰れた疑い**があれば、そう言う (#199)
            damaged = ansi_damage(glyphs)
            if damaged:
                problems += 1
                say(ansi_damage_note(damaged))
            # **書き写しで 1 行ぶんずれた疑い**があれば、そう言う (#229)。
            # ずれても全部の番号に字は当たるので、上の「全部読める」では出ない
            trouble = font_trouble(font_txt)
            if trouble:
                problems += 1
                for line in trouble:
                    say(line)
        else:
            say("[文字表] font.txt はまだ無い (作ったらこのフォルダに置くと、ここで出来具合を確かめられる)")
            skipped.append("文字表の出来具合 (font.txt がまだ無い)")
        # 名前で拾えなければ**形で拾う** (#172)。本体を開いた後でないと中身を見られない
        fonts, by_shape = pick_fonts(img, entries)
        # **どうやって見つけたか**を言う。名前で拾えなかったのに黙って形で拾うと、
        # 社長は「名前が読めていない」という大事な手がかりを受け取れない (#172)
        if not fonts:
            problems += 1
            say(f"→ [フォント] フォント画像が見つかりません。名前に font が付いたファイルも、"
                f"1 行 {FONT_COLS} 字の幅の画像もありませんでした "
                f"({min(len(entries), SHAPE_HUNT_FILES)} 個まで探した)。"
                "この行ごと報告してください")
        elif by_shape:
            say(f"[フォント] 名前に font が付いたファイルが無いので、**中身の形**で探しました "
                f"(1 行 {FONT_COLS} 字の幅): "
                + ", ".join(e["path"] for e in fonts[:3])
                + "。名前が `#0` のような番号のままなら、索引の名前の読みがこの作品では"
                  "違うということなので、その行も報告してください")
        for e in fonts[:3]:
            img.seek(e["at"])
            info = tim2_info(img.read(min(e["len"], 0x100)))
            if info:
                kind = info.get("image_type")
                say(f"[フォント] {e['path']}: TIM2 (位置 0x{info['at']:X}) "
                    + (f"{info.get('width')}×{info.get('height')} ドット / "
                       f"{TIM2_PIXEL_KIND.get(kind, f'画素の種類 {kind} (未知)')} / "
                       f"パレット {info.get('clut_colors')} 色" if "width" in info else ""))
                # 1 行 23 字を 22 ドット刻みで並べるので、幅がこれを下回ると目盛りが
                # そもそも載らない。数字を出すだけで判定していなかった (#98)
                need = FONT_COLS * FONT_CELL
                if info.get("width") and info["width"] < need:
                    problems += 1
                    say(f"→ [フォント] 幅が {info['width']} ドットで、"
                        f"1 行 {FONT_COLS} 字を {FONT_CELL} ドット刻みで並べるのに要る "
                        f"{need} ドットに足りません。文字の並びの読み方 (1 行の字数・刻み) が"
                        "この作品では違うかもしれません。この行ごと報告してください")
                elif info.get("width") and info["width"] // FONT_CELL != FONT_COLS:
                    # 「足りない」だけを見ていたので、**広すぎる**側を素通ししていた (#166)。
                    # 画面の番号振りは幅 ÷ 刻みで列数を決めるので、1024 ドット幅なら
                    # 1 行 46 字になる。足りない側と同じだけ危ない
                    problems += 1
                    say(f"→ [フォント] 幅が {info['width']} ドットあり、{FONT_CELL} ドット刻みで"
                        f"割ると 1 行 {info['width'] // FONT_CELL} 字になります "
                        f"(この作品は 1 行 {FONT_COLS} 字)。"
                        "「文字の番号を重ねる」は幅から列数を決めるので、このままでは"
                        f"文字表が丸ごとずれます。頁 1 枚は幅 {need} ドットで足ります。"
                        "この行ごと報告してください")
                # 1 枚で文字表がまかなえるか。**問題ではなく、先に知っておくこと** (#167)。
                # 手順書は「番号 0 から書き出す」で終わっているが、1 枚では終わらない
                if info.get("width") and info.get("height"):
                    # **このファイルに入っている頁を全部数える** (#170)。1 枚目だけで
                    # 数えていたので、「1 枚では N 字足りない」と言った直後に 2 枚目を
                    # 挙げていて、足し算が合っていなかった
                    img.seek(e["at"])
                    own = [p for p in tim2_pages(img.read(min(e["len"], FONT_OWN_CAP)))
                           if looks_like_a_font_page(p)]
                    cells = sum(font_page_cells(p) for p in own) or font_page_cells(info)
                    if len(own) > 1:
                        each = " + ".join(str(font_page_cells(p)) for p in own)
                        say(f"  このファイルの頁は {len(own)} 枚、マスは {each} = {cells} 個 "
                            f"(この作品の文字表は全部で {FONT_GLYPHS} 字)")
                    else:
                        say(f"  この画像のマスは {cells} 個 "
                            f"(この作品の文字表は全部で {FONT_GLYPHS} 字)")
                    if cells < FONT_GLYPHS:
                        say(f"  {FONT_GLYPHS - cells} 字ぶん足りないので、"
                            "残りは別の画像にあります。書き写しても本文に大きい番号が"
                            "残るのは、そのためです")
                        # 足りないと言うだけで終わらず、**この吸い出しの中から探す** (#168)。
                        # 上で数えた頁は候補に入れない (数えた分をもう一度挙げない)
                        for line in font_page_hunt(img, entries, e, cells,
                                                   {p["at"] for p in own} | {info["at"]},
                                                   wide=info.get("width") or 0):
                            say(line)
                    else:
                        say("  これで文字表はまかなえます")
            else:
                problems += 1
                img.seek(e["at"])
                say(f"→ [フォント] {e['path']} は TIM2 として読めません。先頭 16 バイト {img.read(16).hex(' ').upper()}")

    if map_dir:
        files = m["files"]
        ok_map, ok_talk, lines = m["ok_map"], m["ok_talk"], m["lines"]
        bad_examples, no_talk_example = m["bad"], m["no_talk"]
        say(f"\n[MAP] {len(files)} 件 / 入れ物として読めた {ok_map} 件 / 1 番が会話だった {ok_talk} 件 / 会話 {lines:,} 行")
        # 入れ物としては読めたのに会話が 1 つも取れないのは、1 番の部品の読み方
        # (表の数 + 12 バイトの項目) が外れている合図。数字を出すだけで判定して
        # いなかったので、会話 0 行でも「問題なし」と言っていた (#97)
        if ok_map and not ok_talk:
            problems += 1
            say("→ 入れ物としては読めましたが、1 番が会話として読めたファイルが 0 件です。"
                "会話ファイルの読み方 (表の数 + 12 バイトの項目) が外れている疑いがあります。"
                "この行と、下の 1 件目の先頭 16 バイトを報告してください")
            if no_talk_example:
                say(f"   {no_talk_example[0]} の 1 番: {no_talk_example[1]}")
        if bad_examples:
            problems += 1
            say("→ 入れ物として読めないファイルの例 (名前: 先頭 16 バイト):")
            for name, head in bad_examples[:5]:
                say(f"   {name}: {head}")
    # **診ていない段があるなら、「問題なし」で終わらせない** (#174)。
    # MAP が見つからないと物語の会話をまるごと診ないのに、終了コード 0 で
    # 「docs/10 の手順へ」と言っていた。社長は「診た結果、大丈夫」と読む
    # MAP が**空**でも、会話を 1 つも診ていないのは同じこと (#176)。#174 は
    # 「フォルダが無い」だけを見ていたので、空のフォルダ (吸い出しの失敗、
    # フォルダ違い) はそのまま「問題なし」で通り抜けていた
    if map_dir and not os.listdir(map_dir):
        problems += 1
        say()
        say("→ MAP が無いので、**物語の会話は 1 つも診ていません**"
            f" ({os.path.relpath(map_dir, folder)} は空でした)。会話は MAP/ の中にあるので、"
            "ここを診ないと診断の半分が欠けます。吸い出しがうまくいっているか確かめるか、"
            "MAP が別の場所にあるならその名前ごと報告してください")
    elif not map_dir:
        problems += 1
        say()
        # 矢印は文字列の先頭に置くこと。見張り
        # (test_every_diagnosis_arrow_is_explained) は矢印で始まる文字列だけを
        # 拾うので、改行を先に付けると数え落とされる
        say("→ MAP が無いので、**物語の会話は 1 つも診ていません**"
            f" ({folder} の下を 2 段まで探しました)。会話は MAP/ の中にあるので、"
            "ここを診ないと診断の半分が欠けます。吸い出したフォルダを指定し直すか、"
            "MAP が別の場所にあるならその名前ごと報告してください")
    # **別の行にする。** 締めの行に混ぜると、画面と CLI で「文字表」の見方が違う分
    # (CLI はフォルダの font.txt、画面は貼ってある表) がそのまま締めの行に出てしまい、
    # 2 つの報告を 1 行ずつ突き合わせられなくなる (tests/e2e/broken.py)
    if skipped:
        say()
        say(f"診ていない段: {len(skipped)} 件 ({', '.join(skipped)})")
    say("\n== 結果: " + ("問題なし。docs/10 の手順へ" if not problems
                       else f"確認事項 {problems} 件 (上の → の行)。この出力ごと報告してください"))
    return 1 if problems else 0


#: 上の置き換えを人に見せるときの並び (scrp.TSV_ESCAPES と同じもの)
TSV_ESCAPES_SHOWN = tuple(scrp.TSV_ESCAPES.values())


def write_tsv(rows, out) -> int:
    """校正用の TSV を書き、**書き換えた行の数**を返す (#201).

    TSV は列をタブで、行を改行で分ける。本文にタブか改行が 1 つ入るだけで
    列がずれ、読み直しは「列数が 7 で、見出しの 5 と違います」で止まる。
    保存画面の文言は Shift-JIS の生バイトを読むので、0x09 や 0x0A が
    そのまま文になることがある (合成データで確かめた)。

    前はタブを空白にしていた。**空白にすると元に戻せない**し、改行のほうは
    何もしていなかったので列がずれていた。`scrp.tsv_escape` で記号に変える。
    """
    out.write("id\toffset\tsize\toriginal\ttranslation\n")
    changed = 0
    for rid, off, size, text in rows:
        fixed = scrp.tsv_escape(text)
        changed += fixed != text
        out.write(f"{rid}\t0x{off:X}\t{size}\t{fixed}\t{fixed}\n")
    return changed


# ---------- 入口 ----------

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")      # Windows の cp932 コンソール/リダイレクトで落ちない
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("unpack", help="索引で本体を切り分ける")
    p.add_argument("idx"); p.add_argument("img"); p.add_argument("out")
    p = sub.add_parser("maps", help="マップの入れ物を部品にする")
    p.add_argument("files", nargs="+"); p.add_argument("-o", "--out", required=True)
    p = sub.add_parser("text", help="会話を TSV にする (フォルダを渡せば中の *.msg と 1.bin を全部)")
    p.add_argument("files", nargs="+"); p.add_argument("-f", "--font"); p.add_argument("-o", "--out")
    p.add_argument("--keep-voice", action="store_true", help="音声の番号の項目も残す")
    p = sub.add_parser("fontlist", help="フォントの並びを校正ツールのフォント一覧にする")
    p.add_argument("font"); p.add_argument("-o", "--out")
    p = sub.add_parser("used", help="本文で使われている文字番号だけを並べる (書き出す手間を減らす)")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("table", help="文字表を docs/01 の .tbl (16進=文字) にする。hexdump.py --table で使える")
    p.add_argument("font"); p.add_argument("-o", "--out", required=True)
    p = sub.add_parser("check", help="吸い出したフォルダを診て、報告用の要約を出す (最初に走らせる)")
    p.add_argument("folder")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except FileNotFoundError:
        raise                     # 無いファイルの案内は cli_main が出す (#79、#85)
    except (OSError, ValueError, struct.error) as exc:
        # 途中で止まった理由を 1 行で。Python の長い追跡表示は初めての人には読めない
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


def honestly_empty(path: str) -> bool:
    """1 行も出なかったのが**正しい**ファイルか (#177).

    `.msg` の先頭 u32 は件数。**読めたうえで「0 件」と書いてあるなら、空なのが
    正しい** (#160 の `hollow.msg`)。それ以外 —— 件数が 1 以上なのに 1 行も
    出ない、そもそも読めない —— は**本文が落ちている**。

    「読めないのだから分からない」で 0 を返すと、いちばん危ない場合
    (見出しが壊れて 12 行消えた) が静かに通る。**分からないときは落ちた側に倒す。**
    """
    try:
        with open(path, "rb") as fh:
            b = fh.read(FONT_HUNT_HEAD)
    except OSError:
        return False
    # 先頭 u32 が件数。**0 と書いてあれば、空なのが正しい。**
    # `pick_msg` は 8 バイト未満を読まないので、ここは自分で見る
    # (件数 0 の .msg は 4 バイトしかない —— #160 の hollow.msg がまさにそれ)
    if len(b) >= 4 and struct.unpack_from("<I", b, 0)[0] == 0:
        return True
    items = pick_msg(b, {})
    if items is not None:
        return len(items) == 0
    parts = parse_map(b)
    return parts is not None and not parts


def run(args) -> int:
    if args.cmd == "check":
        return check(args.folder)

    if args.cmd == "unpack":
        n, unnamed, dropped, paths = unpack(args.idx, args.img, args.out)
        print(f"{n} 個に切り分けました → {args.out}")
        if paths:
            # **名前を並べて見せる** (#206)。docs/10 の 20 分の行が見ろと言っている当のもの
            print("   最初の名前: " + " / ".join(paths[:5])
                  + (" …" if len(paths) > 5 else ""))
        # **取り出せなかった分を言う** (#178)。索引が名乗る数より少ないのに
        # 「N 個に切り分けました」とだけ言って 0 で終わっていた。#177 の
        # 「出るはずのものが出ていないときだけ赤にする」を unpack にも広げる
        note = dropped_note(dropped)
        if note:
            print(note, file=sys.stderr)
            return 1
        # **名前が付かなかった数を言う** (#161)。docs/10 は「`#0012.tm2` のように
        # 番号だけなら名前の読み取りに失敗している」と人に見張らせているのに、
        # この道具は数を出していなかった (`check` と画面は出している)
        if unnamed:
            print(f"→ 名前が付かなかったファイル {unnamed} 個 "
                  f"(`#0012` のような番号だけの名前になります)。"
                  f"索引の名前の置き場の読み取りが外れている疑いがあります。"
                  f"python3 tools/boku2.py check 実物/ の出力ごと報告してください",
                  file=sys.stderr)
            # **どこで止まったかが分かるなら、そこを指す** (#202)
            with open(args.idx, "rb") as fh:
                stop_note = dfi_name_stop_note(dfi_name_stop(fh.read()))
            if stop_note:
                print(stop_note, file=sys.stderr)
    elif args.cmd == "maps":
        files = expand_patterns(args.files, folder_files=True)
        total, boxes, skipped = 0, 0, []
        for f in files:
            stem = os.path.splitext(os.path.basename(f))[0]
            try:
                n = split_map(f, os.path.join(args.out, stem))
            except ValueError:
                # **関係ないファイル 1 つで全部を止めない** (#161)。
                # 吸い出した MAP/ には .DS_Store (Mac) や Thumbs.db (Windows) が
                # 必ずと言ってよいほど混ざる。しかも名前の先頭が "." だと
                # 一覧にも出ないので、**身に覚えのないファイル名のエラーだけ**が残り、
                # 並び順によっては**1 つも切り分けられない**。飛ばして名前を言う
                skipped.append(f)
                continue
            total += n
            boxes += 1
        if not total:
            # 0 個を「0 個の入れ物から 0 個の部品」で終わると、手順が進んだように読める。
            # docs/10 の 25 分の行は「0 個でないこと」を人に見張らせていた (#125)
            print(f"→ 入れ物が 1 つも見つかりませんでした (見たファイル {len(files)} 個)",
                  file=sys.stderr)
            if skipped:
                # **どれが入れ物でなかったか**を名前で言う。数だけだと次の手が無い (#161)
                names = ", ".join(os.path.basename(f) for f in skipped[:5])
                print(f"   入れ物ではありません: {names}"
                      f"{' …' if len(skipped) > 5 else ''}", file=sys.stderr)
            print("   指定した場所に MAP のファイルがありません。"
                  "吸い出したフォルダの MAP/ を指定してください:", file=sys.stderr)
            print("     python3 tools/boku2.py maps 実物/MAP -o OUT/maps", file=sys.stderr)
            return 1
        # 渡した数ではなく**入れ物だった数**で言う。渡した数で言うと、
        # 飛ばしたものまで入れ物に数えてしまう
        print(f"{boxes} 個の入れ物から {total} 個の部品 → {args.out}")
        if skipped:
            names = ", ".join(os.path.basename(f) for f in skipped[:5])
            print(f"→ 入れ物ではありません (飛ばしました): {len(skipped)} 個 "
                  f"{names}{' …' if len(skipped) > 5 else ''}。"
                  f"MAP のファイルのつもりなら、その名前ごと報告してください",
                  file=sys.stderr)
    elif args.cmd == "text":
        glyphs = load_font(args.font)
        # 文字表が ANSI で保存されて潰れていたら、**取り出す前に**言う (#199)。
        # 潰れたまま取り出すと、本文の ♡ などが `?` になったまま TSV に入る
        damaged = ansi_damage(glyphs)
        if damaged:
            print(ansi_damage_note(damaged), file=sys.stderr)
        # 書き写しで 1 行ぶんずれていたら、**取り出す前に**言う (#229)。
        # ずれたまま取り出すと、下の「文字表で全部読めました」まで通ってしまう
        for line in font_trouble(args.font):
            print(line, file=sys.stderr)
        rows = []
        given = expand_patterns(args.files)
        files = expand_inputs(given)
        empty = []
        for f in files:
            got = text_rows(f, glyphs, args.keep_voice)
            if not got:
                empty.append(f)        # 読めたはずの形なのに 1 行も出なかった
            rows += got
        if not rows:
            # 0 行の TSV を黙って作ると、開くまで何も起きていないことに気づけない。
            # 素人が踏むのは「unpack / maps を先に回していない」か「場所違い」(#77)
            print(f"→ 文言が 1 行も見つかりませんでした (見たファイル {len(files)} 個)")
            if not files:
                print("   指定した場所に読めるファイルがありません。"
                    "先に unpack (索引の切り分け) と maps (入れ物の切り分け) を回してください:")
                print("     python3 tools/boku2.py unpack 実物/BOKU2.IDX 実物/BOKU2.IMG OUT/")
                print("     python3 tools/boku2.py maps 実物/MAP -o OUT/maps")
                print("   そのうえで OUT を指定します: python3 tools/boku2.py text OUT -f font.txt -o all.tsv")
            else:
                print("   ファイルはありましたが、どれも .msg / 入れ物の部品として読めませんでした。"
                    "python3 tools/boku2.py check 実物/ で、どの段で外れているかを診てください")
            if args.out:
                with open(args.out, "w", encoding="utf-8-sig", newline="\n") as fo:
                    write_tsv(rows, fo)
                print(f"   (見出しだけの {args.out} は作ってあります)")
            return 1
        if args.out:
            # BOM 付き UTF-8: Excel でそのまま開いても日本語が化けない
            with open(args.out, "w", encoding="utf-8-sig", newline="\n") as fo:
                escaped = write_tsv(rows, fo)
            if glyphs:
                note = ""
            elif args.font:
                # -f を渡したのに空だった。「文字表なし」だと渡していないように読める
                note = f" (文字表 {args.font} から読めた字が 0 なので、番号のまま)"
            else:
                note = " (文字表なし: 番号のまま。-f font.txt を付けると日本語になります)"
            print(f"{len(rows)} 行 → {args.out}" + note)
            if escaped:
                # **記号に変えたことを言う。** 言わないと、実機の制御コードだと
                # 思われる (`<BR>` の仲間に見える) (#201)
                print(f"   本文にタブか改行があった {escaped} 行を "
                      f"{' / '.join(TSV_ESCAPES_SHOWN)} に置き換えました "
                      "(TSV の列がずれるため。読み直すと元に戻ります)")
        else:
            write_tsv(rows, sys.stdout)
        if glyphs:
            # 文字表が短い / 抜けがあると本文に [番号] が残る。どの番号か数えて知らせる
            missing = [u for u in used_codes(files) if u >= len(glyphs) or glyphs[u] is None]
            used = used_codes(files)
            if missing:
                print(f"文字表に無い番号: {len(missing)} 種 (例: {' '.join(str(u) for u in missing[:12])}"
                      f"{' …' if len(missing) > 12 else ''})。本文ではこの番号が [番号] のまま残っています。"
                      f"フォント画像のこの番号の文字を文字表に足してください", file=sys.stderr)
            elif not used:
                # 0 種で「全部読めました」と言うと、**文字表を一度も引いていない**のに
                # 仕上がった印 (docs/10 の 55 分の行) が出る。Shift-JIS だけの範囲
                # (保存画面の一部) を 1 ファイルだけ渡すとこうなる (#124)
                print("文字番号を使っている行がありませんでした。文字表は試せていません "
                      "(この範囲は Shift-JIS です)", file=sys.stderr)
            else:
                print(f"文字表で全部読めました (使われている番号 {len(used)} 種)", file=sys.stderr)
        # **出なかったものを言う。** 上の行は文字表の話しかしていないのに、
        # 仕事が終わったように読める (#160)。足りない疑いは必ず添える
        if empty:
            names = ", ".join(os.path.basename(f) for f in empty[:5])
            print(f"→ 読めるはずの形なのに 1 行も出なかったファイル {len(empty)} 個 "
                  f"({names}{' …' if len(empty) > 5 else ''})。"
                  f"その名前ごと報告してください", file=sys.stderr)
            # **ここは 0 で終わらない** (#177)。読めるはずの形なのに 1 行も出て
            # いないのは、**本文が落ちている**ということ。docs/10 の手順 6 は
            # 3 つの命令を続けて打つので、0 を返すと次に進んでしまう。#159 で
            # 31 行のうち 12 行しか出ていなかったときと同じ落ち方が、終了コードに残っていた。
            #
            # ほかの → はそのまま 0 のまま。「入れ物ではありません (飛ばしました)」は
            # ごみを飛ばしただけで仕事は済んでいるし (#162)、「名前が付かなかった」も
            # ファイルは全部出ている。**出るはずのものが出ていない**ときだけ赤にする。
            #
            # さらに「件数 0 と書いてある .msg」は、**正直に空**なだけで落ちてはいない
            # (#160 の hollow.msg)。件数が 1 以上と書いてあるのに 1 行も出ないものだけ、
            # 本文が落ちたと見なす
            if not all(honestly_empty(f) for f in empty):
                return 1
        missed = unpicked_nearby(given)
        if missed:
            print(unpicked_note(missed), file=sys.stderr)
    elif args.cmd == "used":
        given = expand_patterns(args.files)
        used = used_codes(given)
        if not used:
            # 0 種を「この番号だけ書き出せばよい」と言うと、**書き出す番号が無い**のに
            # 手順が進んだように読める。読めるファイルが無かっただけなので、そう言う (#123)
            print("→ 使われている文字番号が 1 つも見つかりませんでした", file=sys.stderr)
            print("   指定したファイルが .msg として読めていません。"
                  "先に unpack / maps を回すか、boku2.py check で診てください", file=sys.stderr)
            return 1
        print(" ".join(str(u) for u in used))
        # **`text` にあって `used` に無かった見張り** (#232)。ファイルを並べて渡すと
        # 深い所の `.msg` と入れ物が丸ごと落ちる。練習データでは 68 種が 15 種になり、
        # いちばん大きい番号も 165 が 87 になった。それでも今までは
        # 「この番号だけ書き出せば本文は読める」と言い切っていた —— **文字表を
        # 作る手順はこの数を見ている**ので、そのまま足りない文字表ができあがる
        missed = unpicked_nearby(given)
        print(f"# {len(used)} 種 (最大 {used[-1]})。"
              + ("**渡したファイルの中だけ**の数です"
                 if missed else "フォント画像のこの番号だけ書き出せば本文は読める"),
              file=sys.stderr)
        if missed:
            print(unpicked_note(missed), file=sys.stderr)
            print("   このままだと書き写す番号が足りません。いちばん大きい番号も変わるので、"
                  "**文字表の 2 枚目が要るかどうかも決まりません**", file=sys.stderr)
        else:
            print(glyph_range_note(used[-1]).strip(), file=sys.stderr)
    elif args.cmd == "table":
        glyphs = load_font(args.font) or []
        mapping = glyph_table_mapping(glyphs)
        scrp.save_table(args.out, mapping,
                        "僕の夏休み 2 の文字表 (tools/boku2.py table)\n"
                        "文字番号は 2 バイトのリトルエンディアン。0080 終わり / 0180 改行 / 0280 待ち")
        print(f"{len(mapping)} 件 → {args.out}  (例: python3 tools/hexdump.py system.msg --table {args.out})")
    elif args.cmd == "fontlist":
        glyphs = load_font(args.font) or []
        # 校正の文字表に渡す前に、書き写しのずれを言う (#229)。ここを通った一覧が
        # proofread の `--font-chars` になるので、ずれたまま渡すと校正ごと外れる
        for line in font_trouble(args.font):
            print(line, file=sys.stderr)
        # 全角の空白 (U+3000) を落とさないこと。フォントには入っている (僕の夏休み 2 では
        # 0 番) ので、落とすと本文の空白が「フォントに無い文字」として誤って指摘される (#89)
        lines = ["# フォント画像の並び (tools/boku2.py fontlist)"] + [g for g in glyphs if g]
        text = "\n".join(lines) + "\n"
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fo:
                fo.write(text)
            print(f"{len(lines) - 1} 字 → {args.out}")
        else:
            sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
