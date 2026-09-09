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
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp   # open_text (メモ帳/Excel のどの保存形式でも読む) と .tbl の書き出し

SECTOR = 2048


# ---------- 索引 (DFI) ----------

def read_dfi(idx: bytes, data_size: int, rule: str = "stack") -> list[dict]:
    """レコードを歩いて {path, at, len} の一覧にする。ブラウザ側 readDfi と同じ規則.

    フォルダの閉じ方 (rule):
      "stack": ファイルの「続く」が 0 なら自分のフォルダを閉じ、閉じたフォルダの「続く」も 0 なら
               親も閉じる (何段でも)。こちらの既定
      "flag":  公開ソース UNPACK.py の規則。「続く」が 0 のフォルダを見たら旗を立て、次に
               「続く」が 0 のファイルで 1 段 (旗が立っていれば 2 段) 閉じる
    実物では両者が一致するはず (check が突き合わせる)。違えば規則の理解が足りていない."""
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


def safe_parts(path: str) -> list[str]:
    """索引の名前をそのままフォルダ名に使うと、'..' や '\\' で出力先の外に書いてしまう。
    索引は信用しない: 区切りを揃え、上に戻る部品と空の部品を落とし、危ない文字は _ にする."""
    parts = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        parts.append("".join(c if c.isalnum() or c in "._-~#()+" else "_" for c in part))
    return parts or ["_"]


def unpack(idx_path: str, img_path: str, out_dir: str) -> int:
    with open(idx_path, "rb") as fh:
        idx = fh.read()
    size = os.path.getsize(img_path)
    entries = read_dfi(idx, size)
    dupes = sum(1 for e in entries if "~" in os.path.basename(e["path"]))
    if dupes:
        print(f"注意: 同じ名前が {dupes} 件あり ~2 を付けて区別しました。"
              "フォルダの入れ子の規則が実物と違うかもしれません", file=sys.stderr)
    with open(img_path, "rb") as img:
        for e in entries:
            dest = os.path.join(out_dir, *safe_parts(e["path"]))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            img.seek(e["at"])
            with open(dest, "wb") as fo:
                fo.write(img.read(e["len"]))
    return len(entries)


# ---------- マップの入れ物 ----------

def parse_map(b: bytes) -> list[dict] | None:
    got = parse_map_rec(b)
    return got[1] if got else None


def parse_map_rec(b: bytes) -> tuple[int, list[dict]] | None:
    """入れ物を読み、(項目の刻み, 部品の一覧) を返す。刻み 8 が普通、12 は日記・保存画面など."""
    if len(b) < 16:
        return None
    n = struct.unpack_from("<I", b, 0)[0]
    if not 1 <= n <= 64:
        return None
    best = None
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
            # 12 バイト刻みの入れ物は 8 バイト刻みとしても「読めて」しまうことがある
            # (後ろの項目が空のとき)。部品が多く取れる方を採る。同じなら 8
            filled = sum(1 for it in items if it["len"])
            if best is None or filled > best[0]:
                best = (filled, rec, items)
    return (best[1], best[2]) if best else None


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
        msg = parse_msg(b[off:off + size], 4) if size >= 8 else None
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
    msg = parse_msg(b, 8) or parse_msg(b, 4) or (parse_raw(b) if allow_raw else None)
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
TEXT_CONTAINERS = {"diary.bin", "saveload.bin", "on_mem_event.bin", "fish_on_mem.bin"}


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


def tim2_info(b: bytes) -> dict | None:
    """TIM2 の見出しだけ読む (ブラウザ側 parseTim2 の要点)。.tms の 0x80 前置きも見る."""
    for at in (0, 0x80, 0x10, 0x20, 0x40):
        if b[at:at + 4] == b"TIM2":
            fmt, count = b[at + 5], struct.unpack_from("<H", b, at + 6)[0]
            p = at + (0x80 if fmt else 0x10)
            if p + 24 > len(b):
                return {"at": at, "format": fmt, "count": count}
            clut_colors = struct.unpack_from("<H", b, p + 14)[0]
            clut_type, image_type = b[p + 18], b[p + 19]
            w, h = struct.unpack_from("<HH", b, p + 20)
            return {"at": at, "format": fmt, "count": count, "width": w, "height": h,
                    "image_type": image_type, "clut_type": clut_type, "clut_colors": clut_colors}
    return None


def stopped_here(problems: int, why: str) -> str:
    """途中で止めたときの締めの行。この先を診ていないことまで書く.

    素人が最初に踏むのは「フォルダ違い」と「ファイル違い」で、そこで締めの行が
    出ないと、道具が落ちたのか診た結果なのかが分からない (#73)。
    """
    return (f"\n== 結果: 確認事項 {problems} 件 (上の → の行)。{why}ここで止めました"
            "。この先 (本体・.msg・フォント・MAP) は診ていません。この出力ごと報告してください")


def check(folder: str, out=sys.stdout) -> int:
    """吸い出したフォルダを一通り診て、報告用の要約を出す (ゲームの本文は出さない).

    docs/10 の手順に入る前に走らせる。ここで外れた所が、次の手がかりになる。"""
    def say(s=""):
        out.write(s + "\n")

    problems = 0
    idx_path = next((os.path.join(folder, n) for n in os.listdir(folder) if n.lower() == "boku2.idx"), None)
    img_path = next((os.path.join(folder, n) for n in os.listdir(folder) if n.lower() == "boku2.img"), None)
    map_dir = next((os.path.join(folder, n) for n in os.listdir(folder) if n.lower() == "map"), None)
    say(f"== 診断: {folder}")
    say(f"BOKU2.IDX: {'あり' if idx_path else '無い'} / BOKU2.IMG: {'あり' if img_path else '無い'} / MAP/: {'あり' if map_dir else '無い'}")
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
    first_names = []
    q = rec_end
    while len(first_names) < 6 and q < len(idx):
        e = idx.find(b"\0", q)
        if e < 0:
            break
        first_names.append(idx[q:e].decode("ascii", "replace"))
        q = e + 1
    say(f"レコード {rec_count} 件 (名前の置き場は 0x{rec_end:X} から) / ファイル {len(entries)} 件 / 名前が付いた {named} 件"
        + (f" / 同じ名前 {dupes} 件" if dupes else ""))
    say(f"最初の名前: {' / '.join(first_names)}")
    say(f"[本体] {img_size:,} バイト / 索引が指す合計 {used:,} バイト ({100 * used / max(1, img_size):.1f}%)")
    if named < len(entries) * 0.9:
        problems += 1
        say("→ 名前が付かないファイルが多い。名前の置き場 (上の 0x…) 付近の 64 バイトを報告してください")
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
    else:
        say("フォルダの規則: 2 通り (stack / flag) で一致")
    msgs = [e for e in entries if e["path"].lower().endswith(".msg")]
    fonts = [e for e in entries if "font" in os.path.basename(e["path"]).lower()]
    say(f".msg: {len(msgs)} 件 (例: {', '.join(os.path.basename(e['path']) for e in msgs[:4])})")
    found = sorted({os.path.basename(e["path"]).lower() for e in entries} & TEXT_CONTAINERS)
    missing = sorted(TEXT_CONTAINERS - set(found))
    say(f"[入れ物] 文言の入れ物: あり {', '.join(found) or 'なし'}"
        + (f" / 見つからない {', '.join(missing)}" if missing else ""))

    used_here: set[int] = set()
    with open(img_path, "rb") as img:
        ok_msg, first_bad = 0, None
        len_ok, len_ng = 0, 0            # 8 バイト刻みの後ろ 4 バイト (項目のバイト長) が合うか
        for e in msgs[:50]:
            img.seek(e["at"])
            b = img.read(e["len"])
            info: dict = {}
            if parse_msg(b, 8, info) or parse_msg(b, 4) or parse_tables(b) or parse_raw(b) or parse_sjis_list(b):
                ok_msg += 1
                if info.get("len_field") == "ok":
                    len_ok += 1
                elif info.get("len_field") == "ng":
                    len_ng += 1
                # 文字表の確認用に、使われている番号も拾っておく (文字表なしの復号は [番号] の形)
                for _, _, _, text in text_rows_bytes(b, e["path"], None, alt=is_alt_break(e["path"])):
                    used_here.update(int(m) for m in re.findall(r"\[(\d+)\]", text))
            elif first_bad is None:
                first_bad = (e, b[:16])
        if msgs:
            say(f"  先頭 {min(50, len(msgs))} 件のうち読めた形: {ok_msg} 件")
            if len_ok or len_ng:
                say(f"  位置表の長さの欄: 合う {len_ok} 件 / 合わない {len_ng} 件"
                    + ("" if not len_ng else " (合わない分は位置だけで読んでいる。この行ごと報告)"))
            if first_bad:
                problems += 1
                e, head = first_bad
                say(f"→ 読めない .msg の例: {e['path']} 先頭 16 バイト {head.hex(' ').upper()}")
        # 文字表 (font.txt) がこのフォルダにあれば、その出来具合も診る (docs/10 の手順 3 の途中経過)
        font_txt = next((os.path.join(folder, n) for n in os.listdir(folder) if n.lower() == "font.txt"), None)
        if font_txt:
            glyphs = load_font(font_txt) or []
            missing = sorted(u for u in used_here if u >= len(glyphs) or glyphs[u] is None)
            say(f"[文字表] font.txt: {sum(1 for g in glyphs if g)} 字 / 上の .msg で使われている番号 {len(used_here)} 種のうち"
                f"文字表に無い {len(missing)} 種"
                + (f" (例: {' '.join(str(u) for u in missing[:10])}{' …' if len(missing) > 10 else ''})。"
                   "フォント画像のこの番号を書き足す (docs/10 の手順 3)" if missing else "。この範囲は全部読める"))
        else:
            say("[文字表] font.txt はまだ無い (作ったらこのフォルダに置くと、ここで出来具合を確かめられる)")
        for e in fonts[:3]:
            img.seek(e["at"])
            info = tim2_info(img.read(min(e["len"], 0x100)))
            if info:
                say(f"[フォント] {e['path']}: TIM2 (位置 0x{info['at']:X}) "
                    + (f"{info.get('width')}×{info.get('height')} 画素の種類 {info.get('image_type')} パレット {info.get('clut_colors')} 色" if "width" in info else ""))
            else:
                problems += 1
                img.seek(e["at"])
                say(f"→ [フォント] {e['path']} は TIM2 として読めません。先頭 16 バイト {img.read(16).hex(' ').upper()}")

    if map_dir:
        files = sorted(os.listdir(map_dir))
        ok_map, ok_talk, lines, bad_examples = 0, 0, 0, []
        for name in files:
            p = os.path.join(map_dir, name)
            if not os.path.isfile(p):
                continue
            with open(p, "rb") as fh:
                b = fh.read()
            items = parse_map(b)
            if not items:
                bad_examples.append((name, b[:16].hex(" ").upper()))
                continue
            ok_map += 1
            one = next((it for it in items if it["i"] == 1 and it["len"]), None)
            if one:
                tables = parse_tables(b[one["at"]:one["at"] + one["len"]])
                if tables:
                    ok_talk += 1
                    lines += sum(1 for t in tables if t["msg"] for it in t["msg"] if it["codes"])
        say(f"\n[MAP] {len(files)} 件 / 入れ物として読めた {ok_map} 件 / 1 番が会話だった {ok_talk} 件 / 会話 {lines:,} 行")
        if bad_examples:
            problems += 1
            say("→ 入れ物として読めないファイルの例 (名前: 先頭 16 バイト):")
            for name, head in bad_examples[:5]:
                say(f"   {name}: {head}")
    say("\n== 結果: " + ("問題なし。docs/10 の手順へ" if not problems else f"確認事項 {problems} 件 (上の → の行)。この出力ごと報告してください"))
    return 1 if problems else 0


def write_tsv(rows, out) -> None:
    out.write("id\toffset\tsize\toriginal\ttranslation\n")
    for rid, off, size, text in rows:
        text = text.replace("\t", " ")
        out.write(f"{rid}\t0x{off:X}\t{size}\t{text}\t{text}\n")


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
    except (OSError, ValueError, struct.error) as exc:
        # 途中で止まった理由を 1 行で。Python の長い追跡表示は初めての人には読めない
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


def run(args) -> int:
    if args.cmd == "check":
        return check(args.folder)

    if args.cmd == "unpack":
        n = unpack(args.idx, args.img, args.out)
        print(f"{n} 個に切り分けました → {args.out}")
    elif args.cmd == "maps":
        files = expand_patterns(args.files, folder_files=True)
        total = 0
        for f in files:
            stem = os.path.splitext(os.path.basename(f))[0]
            total += split_map(f, os.path.join(args.out, stem))
        print(f"{len(files)} 個の入れ物から {total} 個の部品 → {args.out}")
    elif args.cmd == "text":
        glyphs = load_font(args.font)
        rows = []
        files = expand_inputs(expand_patterns(args.files))
        for f in files:
            rows += text_rows(f, glyphs, args.keep_voice)
        if args.out:
            # BOM 付き UTF-8: Excel でそのまま開いても日本語が化けない
            with open(args.out, "w", encoding="utf-8-sig", newline="\n") as fo:
                write_tsv(rows, fo)
            print(f"{len(rows)} 行 → {args.out}" + ("" if glyphs else " (文字表なし: 番号のまま)"))
        else:
            write_tsv(rows, sys.stdout)
        if glyphs:
            # 文字表が短い / 抜けがあると本文に [番号] が残る。どの番号か数えて知らせる
            missing = [u for u in used_codes(files) if u >= len(glyphs) or glyphs[u] is None]
            if missing:
                print(f"文字表に無い番号: {len(missing)} 種 (例: {' '.join(str(u) for u in missing[:12])}"
                      f"{' …' if len(missing) > 12 else ''})。本文ではこの番号が [番号] のまま残っています。"
                      f"フォント画像のこの番号の文字を文字表に足してください", file=sys.stderr)
            else:
                print(f"文字表で全部読めました (使われている番号 {len(used_codes(files))} 種)", file=sys.stderr)
    elif args.cmd == "used":
        used = used_codes(expand_patterns(args.files))
        print(" ".join(str(u) for u in used))
        print(f"# {len(used)} 種 (最大 {used[-1] if used else 0})。フォント画像のこの番号だけ書き出せば本文は読める", file=sys.stderr)
    elif args.cmd == "table":
        glyphs = load_font(args.font) or []
        mapping = glyph_table_mapping(glyphs)
        scrp.save_table(args.out, mapping,
                        "僕の夏休み 2 の文字表 (tools/boku2.py table)\n"
                        "文字番号は 2 バイトのリトルエンディアン。0080 終わり / 0180 改行 / 0280 待ち")
        print(f"{len(mapping)} 件 → {args.out}  (例: python3 tools/hexdump.py system.msg --table {args.out})")
    elif args.cmd == "fontlist":
        glyphs = load_font(args.font) or []
        lines = ["# フォント画像の並び (tools/boku2.py fontlist)"] + [g for g in glyphs if g and g.strip()]
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
