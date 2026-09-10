#!/usr/bin/env python3
"""僕の夏休み 2 と同じ「形」の練習用データを組み立てる (中身は自作の架空テキスト)。

    python3 tools/make_boku2_sample.py            # work/BOKU2SAMPLE/ に一式を作る

作るもの (docs/10 の手順を、実物なしで最後まで通すため):

    BOKU2.IDX / BOKU2.IMG   索引 "DFI" と本体。system/system.msg などが入っている
    MAP/M_A01000.BIN …      マップの入れ物。1 番に会話 (表が複数、音声つき)
    bk_font.tms             フォント画像 (TIM2 に 0x80 の前置き)。模様で代用
    font.txt                フォントの並び (= 文字表)。実物では自分で書き出すもの
    answer.tsv              取り出せるはずの全文 (答え合わせ用)

形式は英語化パッチ (Hilltop Works) の公開ソースで確認したもので、文章は
この練習のために書いた架空のもの。市販ソフトのデータは含まない。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_tim2  # noqa: E402
import scrp  # noqa: E402  (cli_main = 全部の道具で同じエラー表示にする)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = 23          # フォント画像の 1 行の文字数 (公開ソース reprint.py の N_COLUMNS = 23。asm_notes の「17」は 0x17)
CELL = 22          # 文字の刻み (0x16)。実機は 0x17 = 23 ドットの枠を 22 刻みで描く (asm_notes.txt)

# ---------- 文字表 (フォントの並び) ----------

def glyph_table() -> list[str]:
    """フォント画像の左上から順に並ぶ文字。実物では画像を見て自分で書き出す."""
    kana = "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをんゔ"
    kata = "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴ"
    return list("　、。ー！？…「」") + list(kana) + list(kata) + list("０１２３４５６７８９")


def encode(text: str, glyphs: list[str]) -> list[int]:
    """校正ツールの書き方 (<BR> / <WAIT:xx> / <VOICE:n>) の文章を 2 バイトの並びにする."""
    if text.startswith("<VOICE:") and text.endswith(">"):
        digits = text[7:-1]
        assert len(digits) == 8 and digits.isdigit()
        return [ord(digits[i]) | (ord(digits[i + 1]) << 8) for i in range(0, 8, 2)]
    codes: list[int] = []
    i = 0
    while i < len(text):
        if text.startswith("<BR>", i):
            codes.append(0x8001); i += 4
        elif text.startswith("<WAIT:", i):
            end = text.index(">", i)
            codes += [0x8002, int(text[i + 6:end], 16)]; i = end + 1
        elif text.startswith("<BREAK>", i):
            codes.append(0x8002); i += 7      # 引数の無いページ送り (turi_info.msg などの形)
        else:
            ch = text[i]
            if ch not in glyphs:
                raise ValueError(f"フォントに無い文字: {ch!r}")
            codes.append(glyphs.index(ch)); i += 1
    codes.append(0x8000)
    if len(codes) % 2:
        codes.append(0xCDCD)            # 4 バイト揃えの詰め物
    return codes


# ---------- 各形式の組み立て ----------

def build_msg(entries: list[list[int]], stride: int) -> bytes:
    """u32 件数 + 位置表 (stride 刻み) + 本文。空の項目は位置 0.

    8 バイト刻みのときの後ろ 4 バイトは、その項目のバイト長 (公開ソースの書き出し側で確認)。
    ここを 0 のままにしていると、実物と違う練習データになる (#71)。
    """
    tab = 4 + len(entries) * stride
    head = struct.pack("<I", len(entries))
    body, p = b"", tab
    for e in entries:
        size = len(e) * 2
        head += struct.pack("<I", p if e else 0)
        if stride >= 8:
            head += struct.pack("<I", size if e else 0) + b"\0" * (stride - 8)
        else:
            head += b"\0" * (stride - 4)
        body += struct.pack(f"<{len(e)}H", *e)
        p += size
    return head + body


def build_tables(tables: list[list[list[int]]]) -> bytes:
    """u32 表の数 + 12 バイトの項目 + 各表 (4 バイト刻みの .msg)."""
    head = 4 + len(tables) * 12
    bodies = [build_msg(t, 4) for t in tables]
    out, p, data = struct.pack("<I", len(tables)), head, b""
    for i, b in enumerate(bodies):
        out += struct.pack("<IHHI", 0x0000_0001, len(b), 100 + i, p)     # 位置は u32 (MSG_notes.txt)
        data += b
        p += len(b)
    return out + data


def build_map(parts: list[bytes | None], rec: int = 8) -> bytes:
    """u32 項目数 + (u32 位置, u32 長さ [, u32 予備])。部品は 16 バイト揃え。None は空.
    rec=8 がマップなどの普通の形、rec=12 が日記・保存画面の形."""
    n = len(parts)
    head_len = ((4 + n * rec + 15) // 16) * 16
    out, data, off = struct.pack("<I", n), b"", head_len
    for part in parts:
        if part is None:
            out += b"\0" * rec
            continue
        padded = part + b"\0" * ((16 - len(part) % 16) % 16)
        out += struct.pack("<II", off, len(part)) + b"\0" * (rec - 8)
        data += padded
        off += len(padded)
    return out + b"\0" * (head_len - len(out)) + data


def build_dfi(tree: list[tuple[bool, int, str, bytes | None]]) -> tuple[bytes, bytes, dict[str, bytes]]:
    """tree: (フォルダか, まだ続くか, 名前, 中身) の並び → (BOKU2.IDX, BOKU2.IMG, path→中身)."""
    recs, img, want = [], b"", {}
    stack: list[tuple[str, int]] = []
    for is_dir, more, name, data in tree:
        if is_dir:
            recs.append((1, more, 0, 0))
            stack.append(("" if name == "/" else name, more))
            continue
        lba = len(img) // 2048
        recs.append((0, more, lba, len(data)))
        img += data + b"\0" * ((2048 - len(data) % 2048) % 2048)
        want["/".join([d for d, _ in stack if d] + [name])] = data
        if more == 0:
            d = stack.pop()
            while d and d[1] == 0 and len(stack) > 1:
                d = stack.pop()
    idx = b"DFI\0" + struct.pack("<I", 0x100) + b"\0" * 8
    noise = 0x8130                     # +4 は名前の位置ではない (減っていく値を真似る)
    for kind, more, lba, size in recs:
        idx += struct.pack("<HHIII", kind, more, noise, lba, size)
        noise = noise - 7 if noise > 0x100 else 0x8130
    idx += b"".join(name.encode() + b"\0" for _, _, name, _ in tree)
    return idx, img, want


# ---------- 練習用の一式 ----------

MENU = ["はじめから", "つづきから", "せってい", "おわる"]
NAMES = ["ぼく", "おかあさん", "しずか"]
CONFIG = ["おんりょう", "しんどう", "がめん"]           # 深いフォルダ (system/submenu/msg/config/) の例
# 0x8002 が引数の無いページ送りになるメニュー (公開ソースの ALT_NEWLINE_FILES の 1 つ)。
# 待ち時間として読むと <BREAK> の次の字を飛ばしてしまう、その確認用
ITEM_INFO = ["あみ<BREAK>むしをつかまえる", "つりざお<BREAK>さかなをつる"]
# 魚の説明: fish_on_mem.bin の 11〜16 番の部品がさらに入れ物で、その 2 番が説明文
# (公開ソース UNPACK.py の IMG_MAP_FILES)。入れ物の入れ子の確認用
FISH = ["フナ<BR>ぬまにいる", "コイ<BR>かわにいる"]
DIARY = ["きょうは、", "をした。", "たのしかった。"]     # 日記の雛形: 見出しの無い並び (diary.bin の 0 番)
MAPS = {
    "M_A01000": [
        ["<VOICE:00010001>", "きょうはうみにいくんだ。<BR>いっしょにいこうよ。<WAIT:0A>",
         "<VOICE:00010002>", "そうだね、いこう。"],
        ["あさごはんはたべたの？", "うん、たべたよ。<WAIT:05>"],
    ],
    "M_A02000": [
        ["カブトムシがいる！", "しずかにつかまえよう…<BR>そっと、そっと。"],
    ],
}


def build_sample(out_dir: str) -> dict[str, list[tuple[str, str]]]:
    """一式を out_dir に書き、答え (ファイルごとの id と本文) を返す."""
    glyphs = glyph_table()
    answer: dict[str, list[tuple[str, str]]] = {}
    os.makedirs(os.path.join(out_dir, "MAP"), exist_ok=True)

    menu = build_msg([encode(t, glyphs) for t in MENU], 8)
    names = build_msg([encode(t, glyphs) for t in NAMES], 8)
    config = build_msg([encode(t, glyphs) for t in CONFIG], 8)
    answer["system"] = [(f"system:{i}", t) for i, t in enumerate(MENU)]
    answer["namemsg"] = [(f"namemsg:{i}", t) for i, t in enumerate(NAMES)]
    answer["config"] = [(f"config:{i}", t) for i, t in enumerate(CONFIG)]
    item_info = build_msg([encode(t, glyphs) for t in ITEM_INFO], 8)
    answer["item_info"] = [(f"item_info:{i}", t) for i, t in enumerate(ITEM_INFO)]
    # 日記の入れ物 (12 バイト刻み): 0 番が見出しの無い並び、1 番が画像
    raw = b"".join(struct.pack(f"<{len(c)}H", *c) for c in (encode(t, glyphs) for t in DIARY))
    diary_img = make_tim2.build_tim2(8, 8, 5, [1] * 64,
                                     [(0, 0, 0, 255), (255, 255, 255, 255)] + [(0, 0, 0, 0)] * 254, clut_type=3)
    diary = build_map([raw, diary_img], rec=12)
    answer["diary"] = [(f"diary#0:{i}", t) for i, t in enumerate(DIARY)]
    # 入れ子の入れ物: 外側の 1 番が内側の入れ物で、その 2 番が説明文 (.msg 4 バイト刻み)
    fish_msg = build_msg([encode(t, glyphs) for t in FISH], 4)
    inner = build_map([b"\x22" * 32, b"\x33" * 48, fish_msg])
    fish_on_mem = build_map([b"\x11" * 40, inner, None])
    answer["fish_on_mem"] = [(f"fish_on_mem#1#2:{i}", t) for i, t in enumerate(FISH)]

    font_tim2, _ = make_tim2.font_sheet(rows=(len(glyphs) + COLS - 1) // COLS, cols=COLS, cell=CELL)
    tms = b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + font_tim2
    photo = [make_tim2.build_tim2(8, 8, 5, [(i + k) % 4 for i in range(64)],
                                  [(0, 0, 0, 255), (255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)] + [(0, 0, 0, 0)] * 252,
                                  clut_type=3) for k in range(8)]
    tree = [
        (True, 1, "/", None),
        (False, 1, "diary.bin", diary),
        (False, 1, "fish_on_mem.bin", fish_on_mem),
        (True, 1, "00diary", None),
    ] + [(False, 0 if i == 7 else 1, f"nik{i:03d}.tm2", photo[i]) for i in range(8)] + [
        (True, 1, "system", None),
        (False, 1, "bk_font.tms", tms),
        (False, 1, "system.msg", menu),
        (False, 1, "item_info.msg", item_info),
        (True, 1, "namemsg", None),
        (False, 0, "namemsg.msg", names),
        # 4 段目 system/submenu/msg/config/config.msg。閉じ方は、こちらの stack 規則と
        # 公開ソースの flag 規則の両方で同じ道筋になる形にしてある (#56): 1 つのファイルで
        # 閉じるのは最大 2 段 (続く 0 のフォルダ + その親)、残りは段ごとに閉じる
        (True, 1, "submenu", None),
        (True, 1, "msg", None),
        (True, 0, "config", None),                  # msg の最後の項目
        (False, 0, "config.msg", config),           # config と msg を閉じて submenu に戻る
        (False, 0, "sub_readme.bin", b"\0" * 32),   # submenu を閉じて system に戻る
        (False, 0, "sys_end.bin", b"\0" * 32),      # system を閉じて根に戻る
        (False, 0, "readme.bin", b"\0" * 64),
    ]
    idx, img, _ = build_dfi(tree)
    with open(os.path.join(out_dir, "BOKU2.IDX"), "wb") as fh:
        fh.write(idx)
    with open(os.path.join(out_dir, "BOKU2.IMG"), "wb") as fh:
        fh.write(img)

    for stem, tables in MAPS.items():
        talk = build_tables([[encode(t, glyphs) for t in table] for table in tables])
        script = b"\x06\x00\x32\x00\x00\x00" + b"\x03\x00\x2d\x00" * 8      # 命令列らしきもの (会話ではない)
        with open(os.path.join(out_dir, "MAP", stem + ".BIN"), "wb") as fh:
            fh.write(build_map([script, talk, None]))
        answer[stem] = [(f"{stem}:{ti}-{li}", t) for ti, table in enumerate(tables) for li, t in enumerate(table)]

    with open(os.path.join(out_dir, "font.txt"), "w", encoding="utf-8") as fh:
        for r in range(0, len(glyphs), COLS):
            fh.write("".join(glyphs[r:r + COLS]) + "\n")
    with open(os.path.join(out_dir, "answer.tsv"), "w", encoding="utf-8") as fh:
        fh.write("id\toriginal\n")
        for rows in answer.values():
            for rid, text in rows:
                fh.write(f"{rid}\t{text}\n")
    return answer


# 診断 (boku2.py check) の読み方を練習するための壊し方。実物で起き得る外れ方を 1 つずつ再現する
DAMAGE = {
    "idx":  "索引の先頭 4 バイトを壊す (DFI ではなくなる → 索引が読めない)",
    "name": "索引の名前の置き場を壊す (名前が付かないファイルが多い)",
    "msg":  "system.msg の先頭を壊す (.msg が読めない)",
    "font": "bk_font.tms の TIM2 の目印を壊す (フォントが TIM2 として読めない)",
    "map":  "MAP の 1 つを壊す (入れ物として読めない)",
}


def damage(out_dir: str, kind: str) -> str:
    """out_dir の練習データを kind の壊し方で壊し、何をしたかを返す (check の練習用)."""
    idx_path = os.path.join(out_dir, "BOKU2.IDX")
    img_path = os.path.join(out_dir, "BOKU2.IMG")
    if kind == "idx":
        with open(idx_path, "r+b") as fh:
            fh.write(b"XXXX")
        return "BOKU2.IDX の先頭 4 バイトを XXXX にした"
    with open(idx_path, "rb") as fh:
        idx = fh.read()
    if kind == "name":
        rec_end = 16
        while rec_end + 16 <= len(idx) and (idx[rec_end] | (idx[rec_end + 1] << 8)) in (0, 1):
            rec_end += 16
        with open(idx_path, "r+b") as fh:
            fh.seek(rec_end)
            fh.write(b"\xff" * 64)
        return f"BOKU2.IDX の名前の置き場 (0x{rec_end:X} から 64 バイト) を FF で埋めた"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import boku2
    entries = boku2.read_dfi(idx, os.path.getsize(img_path))
    if kind in ("msg", "font"):
        want = "system/system.msg" if kind == "msg" else "system/bk_font.tms"
        e = next(x for x in entries if x["path"] == want)
        with open(img_path, "r+b") as fh:
            fh.seek(e["at"] + (0 if kind == "msg" else 0x80))
            fh.write(b"\xee" * 16)
        return f"BOKU2.IMG の {want} の先頭 16 バイト{'' if kind == 'msg' else ' (TIM2 の位置)'}を EE で埋めた"
    if kind == "map":
        name = sorted(os.listdir(os.path.join(out_dir, "MAP")))[0]
        with open(os.path.join(out_dir, "MAP", name), "r+b") as fh:
            fh.write(b"\xee" * 16)
        return f"MAP/{name} の先頭 16 バイトを EE で埋めた"
    raise ValueError(kind)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")      # Windows の cp932 コンソール/リダイレクトで落ちない
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(REPO, "work", "BOKU2SAMPLE"))
    ap.add_argument("--break", dest="damage", choices=sorted(DAMAGE),
                    help="わざと壊す (診断の読み方の練習用): " + " / ".join(f"{k}={v}" for k, v in DAMAGE.items()))
    args = ap.parse_args()
    answer = build_sample(args.out)
    n = sum(len(v) for v in answer.values())
    print(f"{args.out}: BOKU2.IDX / BOKU2.IMG / MAP/*.BIN / font.txt / answer.tsv ({n} 行)")
    if args.damage:
        print("壊した: " + damage(args.out, args.damage))
        print(f"次: python3 tools/boku2.py check {args.out} で、→ の行を読む練習 (exercises 課題 9)")
    else:
        print("次: docs/10-僕夏2の手順.md の手順を、このフォルダで最後まで試せます")


if __name__ == "__main__":
    scrp.cli_main(main)
