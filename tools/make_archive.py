#!/usr/bin/env python3
"""「索引ファイル + データ本体」の練習用ペアを作る.

市販の PS2 ゲームで最も多いアーカイブの形です。実物では

    BOKU2.IDX     56 KB      索引
    BOKU2.IMG    453 MB      データ本体

のように、小さな索引ファイルと巨大な本体が対になっています。索引の中身は
ゲームごとに違いますが、実際に使われている形はそれほど多くありません。
ここでは市販タイトルでよく見る形を再現します。

    ヘッダ  8 バイト   u32 件数 / u32 予備
    レコード 16 バイト  u32 位置 (セクタ単位) / u32 長さ (バイト) /
                        u32 種別 / u32 名前のハッシュ
    本体              各エントリを 2048 バイト境界に並べる

「位置がセクタ単位」というのがポイントで、バイト単位だと思って読むと
まったく違う場所を指します。構造探査台の索引解析は、この単位も含めて
総当たりで当てにいきます。

    python3 tools/make_archive.py
    # → work/PACK.IDX と work/PACK.IMG
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import make_iso
import scrp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTOR = 2048


def name_hash(name: str) -> int:
    """よくある単純なハッシュ。索引に名前そのものが入らない形の再現."""
    h = 0
    for ch in name.encode("ascii", "replace"):
        h = (h * 31 + ch) & 0xFFFFFFFF
    return h


def build_entries(workdir: str) -> list[tuple[str, int, bytes]]:
    """(名前, 種別, 中身) の一覧を作る. 性質の違うデータを混ぜる."""
    def read(name: str) -> bytes:
        with open(os.path.join(workdir, name), "rb") as fh:
            return fh.read()

    script = read("SCRIPT.BIN")
    msg = read("MSG_ENC.BIN")
    font = read("FONT.BIN")

    entries: list[tuple[str, int, bytes]] = []
    for i in range(6):
        entries.append((f"SCRIPT{i:02d}.BIN", 1, script))
        entries.append((f"MSG{i:02d}.BIN", 2, msg))
    entries.append(("FONT16.BIN", 3, font))
    entries.append(("FONT16B.BIN", 3, font))
    for i in range(4):
        entries.append((f"TEX{i:02d}.DAT", 4, make_iso.pseudo_random(24 * 1024, seed=7000 + i)))
    for i in range(3):
        entries.append((f"SE{i:02d}.ADP", 5, make_iso.pseudo_wave(16 * 1024)))
    entries.append(("PAD.DAT", 0, b"\x00" * 4096))
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description="索引ファイルとデータ本体の対を作る")
    ap.add_argument("--workdir", default=os.path.join(REPO, "work"))
    ap.add_argument("--name", default="PACK")
    args = ap.parse_args()

    need = ["SCRIPT.BIN", "MSG_ENC.BIN", "FONT.BIN"]
    missing = [n for n in need if not os.path.exists(os.path.join(args.workdir, n))]
    if missing:
        raise scrp.ScrpError(
            f"{', '.join(missing)} がありません。先に python3 tools/make_sample.py を実行してください。")

    entries = build_entries(args.workdir)

    body = bytearray()
    records = []
    for name, kind, blob in entries:
        lba = len(body) // SECTOR
        records.append((lba, len(blob), kind, name_hash(name)))
        body += blob
        pad = (-len(body)) % SECTOR       # 次のセクタ境界まで詰める
        body += b"\x00" * pad

    idx = bytearray()
    idx += struct.pack("<II", len(records), 0)
    for lba, size, kind, h in records:
        idx += struct.pack("<IIII", lba, size, kind, h)

    idx_path = os.path.join(args.workdir, args.name + ".IDX")
    img_path = os.path.join(args.workdir, args.name + ".IMG")
    with open(idx_path, "wb") as fh:
        fh.write(idx)
    with open(img_path, "wb") as fh:
        fh.write(body)

    print(f"{os.path.relpath(idx_path, REPO)}  {len(idx):,} バイト "
          f"(ヘッダ 8 + {len(records)} 件 x 16)")
    print(f"{os.path.relpath(img_path, REPO)}  {len(body):,} バイト "
          f"({len(body) // SECTOR} セクタ)")
    print()
    print("  正解: レコード 16 バイト / 先頭 8 バイトを飛ばす / "
          "位置は +0 のセクタ単位 / 長さは +4 のバイト単位")
    print("  先頭 3 件:")
    for i, (lba, size, kind, _) in enumerate(records[:3]):
        print(f"    #{i}  セクタ {lba:5} (0x{lba * SECTOR:07X})  {size:7,} バイト  種別 {kind}")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
