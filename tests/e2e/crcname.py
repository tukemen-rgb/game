"""**名前が読めない吸い出しでも、画面が名前を取り戻す**か (#244).

社長の実物は索引の名前が読めず `#0 #1 …` のままだった (docs/09 の #1・#3)。
名前は `BOKU2.CRC` にも入っていて、そこは索引とは別の場所にあるので、
片方が読めなくてももう片方から出ることがある (#240 で一括処理に
`unpack --names-from-crc` を足した)。

**画面にも同じ道が要る。** 社長が最初に触るのは画面のほうで、そこで
`#0 #1 …` のままなら、`.msg` も入れ物もフォントも名前で拾えず、
この先の段が全部止まる。CLI にだけ逃げ道がある、という形にはしない。

危ないのは「並び順が索引と同じか」が実物でしか決まらないこと。だから
**1 件ずつ検査値で裏を取る** —— その項目の先頭 128 バイトの CRC が、
名前が指す検査値と合ったときだけ名前を使う。ここで見るのは 3 つ:

1. 名前を最後まで潰した吸い出し (`--break allnames`) で、切り分けたあとに
   **本当の名前**が並ぶか
2. 何件当てたかを画面が言うか (黙って名前を変えない)
3. 検査値が合わない表を渡したら、**名前を使わない** (番号のまま)
"""
import asyncio
import os
import struct
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch

OUT = os.path.join(WORK, "CRCNAME")


def build(kind: str) -> str:
    """名前を潰した吸い出しを作る。`kind` が "flip" なら検査値も合わなくする."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    try:
        import make_boku2_sample
    finally:
        sys.path.remove(os.path.join(REPO, "tools"))
    folder = os.path.join(OUT, kind)
    make_boku2_sample.build_sample(folder)
    make_boku2_sample.damage(folder, "allnames")
    if kind == "flip":
        # 検査値を全部反転させる (並び順が合わない形。名前は 1 件も当たらない)
        path = os.path.join(folder, "BOKU2.CRC")
        with open(path, "rb") as fh:
            raw = bytearray(fh.read())
        at = struct.unpack_from("<5I", raw, 0)[3]
        for k in range((len(raw) - at) // 2):
            p = at + k * 2
            struct.pack_into("<H", raw, p, struct.unpack_from("<H", raw, p)[0] ^ 0xFFFF)
        with open(path, "wb") as fh:
            fh.write(raw)
    return folder


async def split_and_read(folder: str, errors: list) -> tuple:
    """その吸い出しを切り分けて、(切り分けの案内, 最初の名前 5 つ) を返す."""
    files = [os.path.join(folder, n) for n in ("BOKU2.IDX", "BOKU2.IMG", "BOKU2.CRC")]
    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", files)
        await page.wait_for_selector("#shell:not([hidden])", timeout=30000)
        await page.click('[data-tab="index"]')
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        await page.select_option("#idxsrc", [o for o in opts if o.endswith("BOKU2.IDX")][0])
        await page.select_option("#idxdata", [o for o in opts if o.endswith("BOKU2.IMG")][0])
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary", timeout=30000)
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function(
            "document.querySelector('#capnote').textContent.includes('切り分けました')",
            timeout=30000)
        note = await page.text_content("#capnote")
        names = await page.evaluate(
            "state.entries.filter((e) => e.kind === 'part').slice(0, 5).map((e) => e.name)")
        await browser.close()
    return note or "", names


async def main():
    errors = []

    note, names = await split_and_read(build("ok"), errors)
    print("  名前を潰した吸い出し:", (note or "")[-120:])
    print("  最初の名前:", names)
    if "検査値ファイルの名前を" not in note:
        errors.append(f"名前を当てたことを言っていない: {note[-150:]!r}")
    if not names or names[0].startswith("#"):
        errors.append(f"名前が番号のままです: {names}")
    if "diary.bin" not in names:
        errors.append(f"本当の名前が出ていない: {names}")

    note2, names2 = await split_and_read(build("flip"), errors)
    print("  検査値が合わない吸い出し:", (note2 or "")[-120:])
    print("  最初の名前:", names2)
    if "検査値ファイルの名前を" in note2:
        errors.append(f"裏が取れないのに名前を当てたと言っている: {note2[-150:]!r}")
    if not names2 or not names2[0].startswith("#"):
        errors.append(f"裏が取れないのに名前を使っている: {names2}")

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
