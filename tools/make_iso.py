#!/usr/bin/env python3
"""練習用のディスクイメージ (ISO9660) を作る.

ゲームディスクの中身が PC からどう見えるのかを、解析する側だけでなく
「作る側」からも 1 度見ておくための道具です。ISO9660 は素朴な形式で、
先頭 32KB を飛ばしたところにボリューム記述子が 1 つあり、そこからディレクトリ
レコードを辿るだけでファイル一覧が取り出せます。

    python3 tools/make_iso.py
    # → work/RINFOLT.iso

中身は疑似ゲームのファイルに、解析の練習向けの性質の違うデータを足したものです。

    SYSTEM.CNF          ASCII テキスト (実機の起動設定ファイルに相当)
    /DATA/SCRIPT.BIN    Shift-JIS のテキスト + ポインタテーブル
    /DATA/MSG_ENC.BIN   独自文字コードのテキスト
    /DATA/FONT.BIN      1bpp のタイル (フォント)
    /DATA/MOVIE.PSS     高エントロピー (圧縮・映像に相当)
    /DATA/BGM.ADP       波形風のデータ
    /DATA/PAD.DAT       ゼロ埋め (詰め物)

ブラウザ側の web/ の解析ツールは、この 7 種類を別の種類として色分けできます。
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTOR = 2048

SYSTEM_CNF = (
    "BOOT2 = cdrom0:\\SLPS_900.99;1\r\n"
    "VER = 1.00\r\n"
    "VMODE = NTSC\r\n"
    "HDDUNITPOWER = NICHDD\r\n"
)


def both_u32(value: int) -> bytes:
    """ISO9660 の「両エンディアン」u32. LE と BE を並べて置く."""
    return struct.pack("<I", value) + struct.pack(">I", value)


def both_u16(value: int) -> bytes:
    return struct.pack("<H", value) + struct.pack(">H", value)


def dir_datetime() -> bytes:
    """ディレクトリレコードの 7 バイト日時. 再現性のため固定値にする."""
    return bytes([102, 1, 1, 0, 0, 0, 0])  # 2002-01-01 00:00:00 GMT


def dec_datetime() -> bytes:
    """ボリューム記述子の 17 バイト日時."""
    return b"2002010100000000" + bytes([0])


def dir_record(name: bytes, lba: int, length: int, is_dir: bool) -> bytes:
    """ディレクトリレコード 1 件. 全体を偶数バイトに揃える."""
    body = bytearray()
    body += b"\x00"                      # レコード長 (後で埋める)
    body += b"\x00"                      # 拡張属性の長さ
    body += both_u32(lba)                # 本体の開始セクタ
    body += both_u32(length)             # 本体のバイト数
    body += dir_datetime()
    body += bytes([0x02 if is_dir else 0x00])   # フラグ (0x02 = ディレクトリ)
    body += b"\x00"                      # ファイルユニットサイズ
    body += b"\x00"                      # インタリーブギャップ
    body += both_u16(1)                  # ボリューム連番
    body += bytes([len(name)])
    body += name
    if len(body) % 2:
        body += b"\x00"
    body[0] = len(body)
    return bytes(body)


def build_directory(entries: list[tuple[bytes, int, int, bool]],
                    self_lba: int, self_len: int,
                    parent_lba: int, parent_len: int) -> bytes:
    """1 セクタぶんのディレクトリを組む. 先頭 2 件は自分と親を指す決まり."""
    out = bytearray()
    out += dir_record(b"\x00", self_lba, self_len, True)      # "." (自分)
    out += dir_record(b"\x01", parent_lba, parent_len, True)  # ".." (親)
    for name, lba, length, is_dir in entries:
        rec = dir_record(name, lba, length, is_dir)
        if len(out) + len(rec) > SECTOR:
            raise scrp.ScrpError("ディレクトリが 1 セクタに収まりません")
        out += rec
    return bytes(out).ljust(SECTOR, b"\x00")


def path_tables(root_lba: int, data_lba: int) -> tuple[bytes, bytes, int]:
    """type-L / type-M パステーブル. 中身は root と DATA の 2 件だけ."""
    def rec(name: bytes, lba: int, parent: int, endian: str) -> bytes:
        pack32 = "<I" if endian == "L" else ">I"
        pack16 = "<H" if endian == "L" else ">H"
        body = bytes([len(name), 0]) + struct.pack(pack32, lba) + struct.pack(pack16, parent) + name
        return body + (b"\x00" if len(body) % 2 else b"")

    tables = {}
    for endian in ("L", "M"):
        t = rec(b"\x00", root_lba, 1, endian) + rec(b"DATA", data_lba, 1, endian)
        tables[endian] = t
    size = len(tables["L"])
    return tables["L"].ljust(SECTOR, b"\x00"), tables["M"].ljust(SECTOR, b"\x00"), size


def primary_volume_descriptor(total_sectors: int, root_record: bytes,
                              path_size: int, lpath_lba: int, mpath_lba: int,
                              volume_id: str) -> bytes:
    pvd = bytearray(b"\x00" * SECTOR)
    pvd[0] = 1                                    # 種別: 基本ボリューム記述子
    pvd[1:6] = b"CD001"                           # 識別子
    pvd[6] = 1                                    # バージョン
    pvd[8:40] = b"PLAYSTATION".ljust(32)          # システム識別子
    pvd[40:72] = volume_id.encode("ascii").ljust(32)
    pvd[80:88] = both_u32(total_sectors)          # ボリューム全体のセクタ数
    pvd[120:124] = both_u16(1)                    # ボリュームセット数
    pvd[124:128] = both_u16(1)                    # ボリューム連番
    pvd[128:132] = both_u16(SECTOR)               # 論理ブロックサイズ
    pvd[132:140] = both_u32(path_size)
    pvd[140:144] = struct.pack("<I", lpath_lba)
    pvd[148:152] = struct.pack(">I", mpath_lba)
    pvd[156:156 + len(root_record)] = root_record  # ルートディレクトリレコード
    pvd[190:318] = b"RINFOLT".ljust(128)
    pvd[318:446] = b"PRACTICE".ljust(128)
    pvd[446:574] = b"PRACTICE".ljust(128)
    pvd[574:702] = b"MAKE_ISO.PY".ljust(128)
    for start in (814, 831, 848, 865):
        pvd[start:start + 17] = dec_datetime()
    pvd[882] = 1                                  # ファイル構造バージョン
    return bytes(pvd)


def terminator() -> bytes:
    t = bytearray(b"\x00" * SECTOR)
    t[0] = 0xFF
    t[1:6] = b"CD001"
    t[6] = 1
    return bytes(t)


# --- 練習用の合成データ ---------------------------------------------------


def pseudo_random(size: int, seed: int = 12345) -> bytes:
    """圧縮済みデータや映像のような、偏りのないバイト列 (再現性のある生成)."""
    out = bytearray(size)
    x = seed
    for i in range(size):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = (x >> 16) & 0xFF
    return bytes(out)


def pseudo_wave(size: int) -> bytes:
    """波形風。隣のバイトと相関があるので圧縮データとは区別できる."""
    out = bytearray(size)
    for i in range(size):
        v = (math.sin(i / 23.0) * 0.6 + math.sin(i / 331.0) * 0.4)
        out[i] = int((v + 1) * 127.5) & 0xFF
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="練習用の ISO9660 イメージを作る")
    ap.add_argument("--workdir", default=os.path.join(REPO, "work"))
    ap.add_argument("-o", "--out", default=os.path.join(REPO, "work", "RINFOLT.iso"))
    ap.add_argument("--volume-id", default="RINFOLT_SENKI")
    args = ap.parse_args()

    need = ["SCRIPT.BIN", "MSG_ENC.BIN", "FONT.BIN"]
    missing = [n for n in need if not os.path.exists(os.path.join(args.workdir, n))]
    if missing:
        raise scrp.ScrpError(
            f"{', '.join(missing)} がありません。先に python3 tools/make_sample.py を実行してください。")

    data_files: list[tuple[str, bytes]] = []
    for name in need:
        with open(os.path.join(args.workdir, name), "rb") as fh:
            data_files.append((name, fh.read()))
    data_files.append(("MOVIE.PSS", pseudo_random(96 * 1024)))
    data_files.append(("BGM.ADP", pseudo_wave(48 * 1024)))
    data_files.append(("PAD.DAT", b"\x00" * (32 * 1024)))

    root_files = [("SYSTEM.CNF", SYSTEM_CNF.encode("ascii"))]

    # --- 配置を決める -----------------------------------------------------
    # 16 セクタのシステム領域 → PVD → 終端子 → パステーブル x2 → ルート → DATA → 本体
    LBA_PVD, LBA_TERM, LBA_LPATH, LBA_MPATH = 16, 17, 18, 19
    LBA_ROOT, LBA_DATA_DIR = 20, 21
    lba = 22
    placed_root, placed_data = [], []
    for name, blob in root_files:
        placed_root.append((name, blob, lba))
        lba += max(1, -(-len(blob) // SECTOR))
    for name, blob in data_files:
        placed_data.append((name, blob, lba))
        lba += max(1, -(-len(blob) // SECTOR))
    total_sectors = lba

    def ident(name: str) -> bytes:
        return (name + ";1").encode("ascii")

    data_dir = build_directory(
        [(ident(n), l, len(b), False) for n, b, l in placed_data],
        LBA_DATA_DIR, SECTOR, LBA_ROOT, SECTOR)
    root_dir = build_directory(
        [(ident(n), l, len(b), False) for n, b, l in placed_root]
        + [(b"DATA", LBA_DATA_DIR, SECTOR, True)],
        LBA_ROOT, SECTOR, LBA_ROOT, SECTOR)

    lpath, mpath, path_size = path_tables(LBA_ROOT, LBA_DATA_DIR)
    root_record = dir_record(b"\x00", LBA_ROOT, SECTOR, True)
    pvd = primary_volume_descriptor(total_sectors, root_record, path_size,
                                    LBA_LPATH, LBA_MPATH, args.volume_id)

    # --- 書き出す ---------------------------------------------------------
    image = bytearray(b"\x00" * (total_sectors * SECTOR))

    def put(lba_at: int, blob: bytes) -> None:
        image[lba_at * SECTOR:lba_at * SECTOR + len(blob)] = blob

    put(LBA_PVD, pvd)
    put(LBA_TERM, terminator())
    put(LBA_LPATH, lpath)
    put(LBA_MPATH, mpath)
    put(LBA_ROOT, root_dir)
    put(LBA_DATA_DIR, data_dir)
    for _, blob, at in placed_root + placed_data:
        put(at, blob)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as fh:
        fh.write(image)

    print(f"{args.out}: {len(image):,} バイト ({total_sectors} セクタ x {SECTOR})")
    print(f"  ボリューム名 {args.volume_id}")
    print(f"  0x{LBA_PVD * SECTOR:X} ボリューム記述子 / 0x{LBA_ROOT * SECTOR:X} ルート")
    for name, blob, at in placed_root:
        print(f"  /{name:<16} LBA {at:<5} 0x{at * SECTOR:07X}  {len(blob):>8,} バイト")
    for name, blob, at in placed_data:
        print(f"  /DATA/{name:<11} LBA {at:<5} 0x{at * SECTOR:07X}  {len(blob):>8,} バイト")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
