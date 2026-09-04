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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = 17          # フォント画像の 1 行の文字数 (公開ソースの asm_notes: char % 17)
CELL = 23          # 1 文字のドット数 (0x17)

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
    """u32 件数 + 位置表 (stride 刻み) + 本文。空の項目は位置 0."""
    tab = 4 + len(entries) * stride
    head = struct.pack("<I", len(entries))
    body, p = b"", tab
    for e in entries:
        head += struct.pack("<I", p if e else 0) + b"\0" * (stride - 4)
        body += struct.pack(f"<{len(e)}H", *e)
        p += len(e) * 2
    return head + body


def build_tables(tables: list[list[list[int]]]) -> bytes:
    """u32 表の数 + 12 バイトの項目 + 各表 (4 バイト刻みの .msg)."""
    head = 4 + len(tables) * 12
    bodies = [build_msg(t, 4) for t in tables]
    out, p, data = struct.pack("<I", len(tables)), head, b""
    for i, b in enumerate(bodies):
        out += struct.pack("<IHHHH", 0x0000_0001, len(b), 100 + i, p, 0)
        data += b
        p += len(b)
    return out + data


def build_map(parts: list[bytes | None]) -> bytes:
    """u32 項目数 + (u32 位置, u32 長さ)。部品は 16 バイト揃え。None は空."""
    n = len(parts)
    head_len = ((4 + n * 8 + 15) // 16) * 16
    out, data, off = struct.pack("<I", n), b"", head_len
    for part in parts:
        if part is None:
            out += struct.pack("<II", 0, 0)
            continue
        padded = part + b"\0" * ((16 - len(part) % 16) % 16)
        out += struct.pack("<II", off, len(part))
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
    answer["system"] = [(f"system:{i}", t) for i, t in enumerate(MENU)]
    answer["namemsg"] = [(f"namemsg:{i}", t) for i, t in enumerate(NAMES)]

    font_tim2, _ = make_tim2.font_sheet(rows=(len(glyphs) + COLS - 1) // COLS, cols=COLS, cell=CELL)
    tms = b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + font_tim2
    photo = [make_tim2.build_tim2(8, 8, 5, [(i + k) % 4 for i in range(64)],
                                  [(0, 0, 0, 255), (255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)] + [(0, 0, 0, 0)] * 252,
                                  clut_type=3) for k in range(8)]
    tree = [
        (True, 1, "/", None),
        (True, 1, "00diary", None),
    ] + [(False, 0 if i == 7 else 1, f"nik{i:03d}.tm2", photo[i]) for i in range(8)] + [
        (True, 1, "system", None),
        (False, 1, "bk_font.tms", tms),
        (False, 1, "system.msg", menu),
        (True, 0, "namemsg", None),
        (False, 0, "namemsg.msg", names),
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
        answer[stem] = [(f"1:{ti}-{li}", t) for ti, table in enumerate(tables) for li, t in enumerate(table)]

    with open(os.path.join(out_dir, "font.txt"), "w", encoding="utf-8") as fh:
        for r in range(0, len(glyphs), COLS):
            fh.write("".join(glyphs[r:r + COLS]) + "\n")
    with open(os.path.join(out_dir, "answer.tsv"), "w", encoding="utf-8") as fh:
        fh.write("id\toriginal\n")
        for rows in answer.values():
            for rid, text in rows:
                fh.write(f"{rid}\t{text}\n")
    return answer


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(REPO, "work", "BOKU2SAMPLE"))
    args = ap.parse_args()
    answer = build_sample(args.out)
    n = sum(len(v) for v in answer.values())
    print(f"{args.out}: BOKU2.IDX / BOKU2.IMG / MAP/*.BIN / font.txt / answer.tsv ({n} 行)")
    print("次: docs/10-僕夏2の手順.md の手順を、このフォルダで最後まで試せます")


if __name__ == "__main__":
    main()
