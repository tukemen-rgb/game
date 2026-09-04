#!/usr/bin/env python3
"""僕の夏休み 2 (PS2 / SCPS-15026) のデータを一括で取り出す。

ブラウザの構造探査台と同じ読み方 (web/app.js の named-index / bokumsg ブロック) を
Python にしたもの。画面で 1 つずつ確かめた後、全部をまとめて処理するのに使う。

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
import os
import struct
import sys

SECTOR = 2048


# ---------- 索引 (DFI) ----------

def read_dfi(idx: bytes, data_size: int) -> list[dict]:
    """レコードを歩いて {path, at, len} の一覧にする。ブラウザ側 readDfi と同じ規則."""
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
    for k in range(rec_count):
        p = 16 + k * 16
        is_dir = (idx[p] | (idx[p + 1] << 8)) == 1
        more = idx[p + 2] | (idx[p + 3] << 8)
        name = names[k] if k < len(names) else ""
        if is_dir:
            stack.append(("" if name == "/" else name, more))
            continue
        lba, length = struct.unpack_from("<II", idx, p + 8)
        at = lba * SECTOR
        base = name or f"#{len(entries)}"
        path = "/".join([d for d, _ in stack if d] + [base])
        if length > 0 and at + length <= data_size:
            entries.append({"path": path, "at": at, "len": length})
        if more == 0:
            d = stack.pop() if stack else None
            while d and d[1] == 0 and len(stack) > 1:
                d = stack.pop()
    return entries


def unpack(idx_path: str, img_path: str, out_dir: str) -> int:
    with open(idx_path, "rb") as fh:
        idx = fh.read()
    size = os.path.getsize(img_path)
    entries = read_dfi(idx, size)
    with open(img_path, "rb") as img:
        for e in entries:
            dest = os.path.join(out_dir, *e["path"].split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            img.seek(e["at"])
            with open(dest, "wb") as fo:
                fo.write(img.read(e["len"]))
    return len(entries)


# ---------- マップの入れ物 ----------

def parse_map(b: bytes) -> list[dict] | None:
    if len(b) < 16:
        return None
    n = struct.unpack_from("<I", b, 0)[0]
    if not 1 <= n <= 64:
        return None
    for rec in (8, 12):
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
            return items
    return None


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

def parse_msg(b: bytes, stride: int) -> list[dict] | None:
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
    items = []
    for i, s in enumerate(starts):
        if not s:
            items.append({"i": i, "at": 0, "codes": []})
            continue
        end = next((t for t in starts[i + 1:] if t), len(b))
        codes = list(struct.unpack_from(f"<{(end - s) // 2}H", b, s))
        items.append({"i": i, "at": s, "codes": codes})
    return items


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
        size, ident, off = struct.unpack_from("<HHH", b, 4 + i * 12 + 4)
        if off < head or off + size > len(b) or off < prev:
            return None
        prev = off
        msg = parse_msg(b[off:off + size], 4) if size >= 8 else None
        tables.append({"i": i, "off": off, "size": size, "id": ident, "msg": msg})
    if not any(x["msg"] for x in tables):
        return None
    return tables


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


def decode(codes: list[int], glyphs: list[str] | None, tags: bool = True) -> str:
    """ブラウザ側 bokuMsgText と同じ。tags=True で校正ツールの書き方 (<BR> / <WAIT:xx>)."""
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
        elif c == 0x8002:
            n = codes[i + 1] if i + 1 < len(codes) else 0
            out.append(f"<WAIT:{n:02X}>" if tags else "{WAIT %d}" % n)
            i += 1
        elif c == 0xCDCD:
            pass
        elif c >= 0x8000:
            out.append(f"<{c:04X}>" if tags else "{%04X}" % c)
        elif glyphs and c < len(glyphs):
            out.append(glyphs[c])
        else:
            out.append(f"[{c}]")
        i += 1
    return "".join(out)


def load_font(path: str | None) -> list[str] | None:
    """フォント画像を左上から書き出したテキスト。改行は無視する (ブラウザと同じ)."""
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return list(fh.read().replace("\r", "").replace("\n", ""))


def text_rows(path: str, glyphs: list[str] | None, keep_voice: bool = False) -> list[tuple[str, int, int, str]]:
    """1 ファイルから (id, offset, size, text) の行を作る。入れ物 / 表の一覧 / 単体を自動で見分ける.

    音声の番号 (8 桁の数字) の項目は文章ではないので、既定では省く (校正の対象にならない)."""
    with open(path, "rb") as fh:
        b = fh.read()
    stem = os.path.splitext(os.path.basename(path))[0]
    rows = []
    items_map = parse_map(b)
    base_off = 0
    if items_map:
        one = next((it for it in items_map if it["i"] == 1 and it["len"]), None)
        if not one:
            return rows
        base_off = one["at"]
        b = b[one["at"]:one["at"] + one["len"]]
    tables = parse_tables(b)
    if tables:
        for tb in tables:
            if not tb["msg"]:
                continue
            for it in tb["msg"]:
                if it["codes"] and (keep_voice or not voice_id(it["codes"])):
                    rows.append((f"{stem}:{tb['i']}-{it['i']}", base_off + tb["off"] + it["at"],
                                 len(it["codes"]) * 2, decode(it["codes"], glyphs)))
        return rows
    msg = parse_msg(b, 8) or parse_msg(b, 4)
    if msg:
        for it in msg:
            if it["codes"] and (keep_voice or not voice_id(it["codes"])):
                rows.append((f"{stem}:{it['i']}", base_off + it["at"], len(it["codes"]) * 2,
                             decode(it["codes"], glyphs)))
    return rows


def write_tsv(rows, out) -> None:
    out.write("id\toffset\tsize\toriginal\ttranslation\n")
    for rid, off, size, text in rows:
        text = text.replace("\t", " ")
        out.write(f"{rid}\t0x{off:X}\t{size}\t{text}\t{text}\n")


# ---------- 入口 ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("unpack", help="索引で本体を切り分ける")
    p.add_argument("idx"); p.add_argument("img"); p.add_argument("out")
    p = sub.add_parser("maps", help="マップの入れ物を部品にする")
    p.add_argument("files", nargs="+"); p.add_argument("-o", "--out", required=True)
    p = sub.add_parser("text", help="会話を TSV にする")
    p.add_argument("files", nargs="+"); p.add_argument("-f", "--font"); p.add_argument("-o", "--out")
    p.add_argument("--keep-voice", action="store_true", help="音声の番号の項目も残す")
    p = sub.add_parser("fontlist", help="フォントの並びを校正ツールのフォント一覧にする")
    p.add_argument("font"); p.add_argument("-o", "--out")
    args = ap.parse_args(argv)

    if args.cmd == "unpack":
        n = unpack(args.idx, args.img, args.out)
        print(f"{n} 個に切り分けました → {args.out}")
    elif args.cmd == "maps":
        total = 0
        for f in args.files:
            stem = os.path.splitext(os.path.basename(f))[0]
            total += split_map(f, os.path.join(args.out, stem))
        print(f"{len(args.files)} 個の入れ物から {total} 個の部品 → {args.out}")
    elif args.cmd == "text":
        glyphs = load_font(args.font)
        rows = []
        for f in args.files:
            rows += text_rows(f, glyphs, args.keep_voice)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fo:
                write_tsv(rows, fo)
            print(f"{len(rows)} 行 → {args.out}" + ("" if glyphs else " (文字表なし: 番号のまま)"))
        else:
            write_tsv(rows, sys.stdout)
    elif args.cmd == "fontlist":
        glyphs = load_font(args.font) or []
        lines = ["# フォント画像の並び (tools/boku2.py fontlist)"] + [g for g in glyphs if g.strip()]
        text = "\n".join(lines) + "\n"
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fo:
                fo.write(text)
            print(f"{len(lines) - 1} 字 → {args.out}")
        else:
            sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
