#!/usr/bin/env python3
"""練習用の疑似ゲームデータを生成する (= ゲーム側のビルド工程).

data/script_source.tsv (開発元のマスターテキスト) から、

    work/SCRIPT.BIN   Shift-JIS 版   … バイナリエディタで日本語が素で見える
    work/MSG_ENC.BIN  独自コード版   … テーブルが分からないと読めない
    work/FONT.BIN     フォント画像   … 1bpp 16x16 のグリフを並べただけのもの
    answers/custom.tbl               … 独自コードの答え (先に自力で挑戦する)
    data/font_chars.txt              … フォントに入っている文字の一覧

を作ります。実機の吸い出しでは data/ 以下は手に入りません。work/ の中だけを
見て中身を復元するのが練習です。

    python3 tools/make_sample.py
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 独自文字コードのレイアウト -------------------------------------------
#
# ひらがな・カタカナは Unicode の並び順そのままで連番に置きます。連番になって
# いるからこそ「相対検索」が効く、という関係を確かめるための配置です。
#
#   0x01-0x53  ひらがな (ぁ〜ん)
#   0x54-0xA6  カタカナ (ァ〜ン)
#   0xA7-0xB0  全角数字 (０-９)
#   0xB1-....  記号
#   0xE0 xx    漢字 (2 バイト。xx が漢字テーブルの番号)
#   0xF0-0xFF  制御コード (scrp.CONTROL_CODES)
HIRAGANA = [chr(c) for c in range(0x3041, 0x3094)]
KATAKANA = [chr(c) for c in range(0x30A1, 0x30F4)]
DIGITS = [chr(c) for c in range(0xFF10, 0xFF1A)]
SYMBOLS = list("　。、「」『』（）！？…ー・：％～→♪")

BASE_HIRAGANA = 0x01
KANJI_LEAD = 0xE0

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-mincho.ttf",
    "C:\\Windows\\Fonts\\msgothic.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
]


def load_source(path: str) -> list[tuple[int, str]]:
    """マスターテキストを [(id, テキスト), ...] で読む."""
    rows: list[tuple[int, str]] = []
    with open(path, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        if header[:2] != ["id", "text"]:
            raise scrp.ScrpError(f"{path}: 見出しは 'id\\ttext' である必要があります")
        for lineno, line in enumerate(fh, 2):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                raise scrp.ScrpError(f"{path}:{lineno}: 列が 2 つではありません")
            rows.append((int(parts[0]), parts[1]))
    for expected, (got, _) in enumerate(rows):
        if got != expected:
            raise scrp.ScrpError(f"{path}: id は 0 から連番で書いてください (期待 {expected}, 実際 {got})")
    return rows


def collect_chars(texts: list[str]) -> list[str]:
    """スクリプト中に出てくる表示文字を、重複なく登場順で集める."""
    seen: list[str] = []
    known = set()
    for text in texts:
        for ch in scrp.strip_tags(text):
            if ch not in known:
                known.add(ch)
                seen.append(ch)
    return seen


def build_table(texts: list[str]) -> tuple[dict[bytes, str], list[str]]:
    """独自文字コードの対応表と、フォントに入れる文字の並びを作る.

    戻り値の 2 番目は「グリフ番号 = リストの添字」になる文字の並びです。
    FONT.BIN のグリフ順と data/font_chars.txt の行順がこれに一致します。
    """
    fixed = HIRAGANA + KATAKANA + DIGITS + SYMBOLS
    fixed_set = set(fixed)
    used = collect_chars(texts)
    kanji = sorted({ch for ch in used if ch not in fixed_set})

    mapping: dict[bytes, str] = {}
    code = BASE_HIRAGANA
    for ch in fixed:
        if code >= min(scrp.CONTROL_CODES) or code == KANJI_LEAD:
            raise scrp.ScrpError("1 バイト領域が足りません。SYMBOLS を減らしてください")
        mapping[bytes([code])] = ch
        code += 1
    if len(kanji) > 256:
        raise scrp.ScrpError(f"漢字が {len(kanji)} 字あり、2 バイト領域 (256) に収まりません")
    for i, ch in enumerate(kanji):
        mapping[bytes([KANJI_LEAD, i])] = ch

    missing = [ch for ch in used if ch not in set(mapping.values())]
    if missing:
        raise scrp.ScrpError(f"テーブルに入れられなかった文字: {''.join(missing)}")
    return mapping, fixed + kanji


def compile_archive(texts: list[str], encoding_id: int, codec: scrp.Codec,
                    pool_duplicates: bool) -> bytes:
    blobs = []
    for i, text in enumerate(texts):
        try:
            blobs.append(scrp.encode_message(text, codec))
        except scrp.ScrpError as exc:
            raise scrp.ScrpError(f"id {i}: {exc}") from exc
    return scrp.build_archive(encoding_id, blobs, pool_duplicates=pool_duplicates)


def render_font(chars: list[str], font_path: str, size: int = 16) -> bytes:
    """1 文字 16x16・1bpp のグリフを並べた FONT.BIN を作る (Pillow が必要)."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_path, size)
    out = bytearray()
    for ch in chars:
        img = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(img)
        # 文字ごとの上下左右のはみ出しを吸収してから中央に置く
        box = draw.textbbox((0, 0), ch, font=font)
        x = (size - (box[2] - box[0])) // 2 - box[0]
        y = (size - (box[3] - box[1])) // 2 - box[1]
        draw.text((x, y), ch, fill=255, font=font)
        pixels = img.load()
        for row in range(size):
            bits = 0
            for col in range(size):
                bits = (bits << 1) | (1 if pixels[col, row] >= 128 else 0)
            out += bits.to_bytes(size // 8, "big")
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="練習用の疑似ゲームデータを生成する")
    ap.add_argument("--source", default=os.path.join(REPO, "data", "script_source.tsv"))
    ap.add_argument("--outdir", default=os.path.join(REPO, "work"))
    ap.add_argument("--answers", default=os.path.join(REPO, "answers"))
    ap.add_argument("--pool-duplicates", action="store_true",
                    help="同一本文をまとめてポインタを共有させる")
    ap.add_argument("--font-path", help="FONT.BIN に使う TrueType フォント")
    ap.add_argument("--no-font", action="store_true", help="FONT.BIN を作らない")
    args = ap.parse_args()

    rows = load_source(args.source)
    texts = [text for _, text in rows]
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.answers, exist_ok=True)

    mapping, glyph_order = build_table(texts)
    table_codec = scrp.TableCodec(mapping)
    charset = set(glyph_order)
    sjis_codec = scrp.SjisCodec(charset)

    sjis_bin = compile_archive(texts, scrp.ENC_SJIS, sjis_codec, args.pool_duplicates)
    custom_bin = compile_archive(texts, scrp.ENC_CUSTOM, table_codec, args.pool_duplicates)

    sjis_path = os.path.join(args.outdir, "SCRIPT.BIN")
    custom_path = os.path.join(args.outdir, "MSG_ENC.BIN")
    with open(sjis_path, "wb") as fh:
        fh.write(sjis_bin)
    with open(custom_path, "wb") as fh:
        fh.write(custom_bin)

    table_path = os.path.join(args.answers, "custom.tbl")
    scrp.save_table(table_path, mapping, header=(
        "work/MSG_ENC.BIN の文字テーブル (答え)\n"
        "まずは tools/relative_search.py で自力で当ててみてください。\n"
        "0xF0-0xFF は制御コードなのでこの表には入っていません。"
    ))

    chars_path = os.path.join(REPO, "data", "font_chars.txt")
    with open(chars_path, "w", encoding="utf-8") as fh:
        fh.write("# FONT.BIN のグリフ順 = この行順 (0 行目 = グリフ 0)\n")
        for ch in glyph_order:
            fh.write(ch + "\n")

    print(f"{os.path.relpath(sjis_path, REPO)}   {len(sjis_bin):6,} バイト  "
          f"({len(texts)} メッセージ, Shift-JIS)")
    print(f"{os.path.relpath(custom_path, REPO)}  {len(custom_bin):6,} バイト  "
          f"({len(texts)} メッセージ, 独自コード)")
    print(f"{os.path.relpath(table_path, REPO)}   {len(mapping)} エントリ "
          f"(1 バイト {sum(1 for k in mapping if len(k) == 1)} / "
          f"2 バイト {sum(1 for k in mapping if len(k) == 2)})")
    print(f"{os.path.relpath(chars_path, REPO)}  {len(glyph_order)} 文字")

    if not args.no_font:
        font_path = args.font_path or next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
        if font_path is None:
            print("日本語フォントが見つからないため FONT.BIN は作りませんでした "
                  "(--font-path で指定できます)")
        else:
            try:
                font_bin = render_font(glyph_order, font_path)
            except ImportError:
                print("Pillow が無いため FONT.BIN は作りませんでした "
                      "(pip install pillow / --no-font)")
            else:
                font_out = os.path.join(args.outdir, "FONT.BIN")
                with open(font_out, "wb") as fh:
                    fh.write(font_bin)
                print(f"{os.path.relpath(font_out, REPO)}     {len(font_bin):6,} バイト  "
                      f"({len(glyph_order)} グリフ x 32 バイト, 16x16 1bpp)")

    ptr_preview = scrp.read_archive(custom_path).pointers[:4]
    print()
    print("ヘッダの読み方の例 (work/MSG_ENC.BIN):")
    print(f"  0x00 マジック   'SCRP'")
    print(f"  0x08 メッセージ数 {len(texts)} (= 0x{len(texts):02X} 00 00 00)")
    print(f"  0x10 ポインタ    " + ", ".join(f"0x{p:X}" for p in ptr_preview) + ", ...")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
