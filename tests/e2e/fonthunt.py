"""文字表の続きの候補を、**画面でも同じ順に**挙げるか (#212).

#211 で「1 枚目と同じ幅の候補を先に挙げる」に直した。一括処理 (`boku2.py`) は
単体の検査で確かめたが、**画面側は「ソースに同じ字がある」ことしか見ていなかった** ——
並べ方そのものは一度も動かしていない。この一式は #131 でも #205 でも、
ソースの grep で済ませた所から穴が出ている。

材料も無かった。練習データのフォントは 1 枚のファイルに 2 枚入っているだけで、
**ほかのファイルに候補が並ぶ形が無い**。そこでここで作る:

* `system/bk_font.tms` … 1 枚目 (46 行)。幅はこれが基準
* `bg/back0〜5.tm2` … おとり 6 件。「1 行 23 字の幅で割り切れる」枠には入るが**幅が違う**
* `system/bk_font2.tms` … 本命。**1 枚目と同じ幅**で、残りが入る大きさ

見るのは 2 つ:

1. 画面の「報告用の要約」で、**本命がおとりより先に**、`← 1 枚目と同じ幅` 付きで出る
2. その候補の行が、`boku2.py check` と **1 行ずつ同じ** (片側だけ直すと食い違う)
"""
import asyncio
import os
import re
import struct
import subprocess
import sys
import tempfile

from playwright.async_api import async_playwright

from common import REPO, launch

sys.path.insert(0, os.path.join(REPO, "tools"))
import boku2                                                  # noqa: E402
import make_tim2                                              # noqa: E402

#: おとりの幅。1 行 23 字の枠 (506〜527 ドット) には入るが、1 枚目と同じではない
DECOY_COLS, DECOY_CELL = 47, 11
DECOYS = 6


def build(folder: str) -> None:
    page1, _ = make_tim2.font_sheet(rows=46, cols=boku2.FONT_COLS, cell=boku2.FONT_CELL)
    page2, _ = make_tim2.font_sheet(rows=26, cols=boku2.FONT_COLS, cell=boku2.FONT_CELL)
    decoy, _ = make_tim2.font_sheet(rows=60, cols=DECOY_COLS, cell=DECOY_CELL)
    head = lambda blob: b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + blob

    files = [("system/bk_font.tms", head(page1))]
    files += [(f"bg/back{i}.tm2", decoy) for i in range(DECOYS)]
    files += [("system/bk_font2.tms", head(page2))]

    data, recs = bytearray(), []
    for _name, blob in files:
        recs.append((len(data) // 2048, len(blob)))
        data += blob + bytes((-len(blob)) % 2048)             # セクタ境界に揃える
    idx = bytearray(b"DFI\0" + struct.pack("<III", len(files), 0, 0))
    for sector, length in recs:
        idx += struct.pack("<HHIII", 0, 0, 0, sector, length)
    for name, _blob in files:
        idx += name.encode() + b"\0"
    with open(os.path.join(folder, "BOKU2.IDX"), "wb") as fh:
        fh.write(bytes(idx))
    with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
        fh.write(bytes(data))


def cli_lines(folder: str) -> list:
    res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                          "check", folder], capture_output=True, text=True, cwd=REPO)
    out = res.stdout + res.stderr
    return [ln.rstrip() for ln in out.splitlines() if ln.strip().startswith(("・", "続きが入って"))
            or "続きが入っていそうな画像" in ln]


async def main() -> int:
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        build(tmp)
        cli = cli_lines(tmp)
        print("CLI:")
        for ln in cli:
            print("   ", ln.strip())
        if not any("bk_font2.tms" in ln for ln in cli):
            errors.append("材料が弱い: 一括処理でも本命が候補に出ていない")

        async with async_playwright() as p:
            browser = await launch(p)
            page = await browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await page.goto("file://" + REPO + "/web/index.html")
            await page.set_input_files("#fileinput", [os.path.join(tmp, "BOKU2.IDX"),
                                                      os.path.join(tmp, "BOKU2.IMG")])
            await page.wait_for_selector("#shell:not([hidden])")
            await page.click('[data-tab="index"]')
            opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
            await page.select_option("#idxsrc", [o for o in opts if o.endswith("BOKU2.IDX")][0])
            await page.select_option("#idxdata", [o for o in opts if o.endswith("BOKU2.IMG")][0])
            await page.click("#idxrun")
            await page.wait_for_selector("#idxpreview button.btn.primary")
            await page.click("#idxreport")
            await page.wait_for_function(
                "document.querySelector('#idxreporttext').value.includes('== 結果')", timeout=30000)
            report = await page.input_value("#idxreporttext")
            await browser.close()

    screen = [ln.rstrip() for ln in report.splitlines()
              if ln.strip().startswith("・") or "続きが入っていそうな画像" in ln]
    print("画面:")
    for ln in screen:
        print("   ", ln.strip())

    named = [ln for ln in screen if "・" in ln]
    if not named:
        errors.append("画面が候補を 1 件も挙げていない")
    else:
        if "bk_font2.tms" not in named[0]:
            errors.append(f"画面で本命が先頭に来ていない: {named[0].strip()!r}")
        if "1 枚目と同じ幅" not in named[0]:
            errors.append(f"画面が「1 枚目と同じ幅」と言っていない: {named[0].strip()!r}")
        if not any("back" in ln for ln in named):
            errors.append("画面が幅の違う候補を 1 件も残していない")

    # **1 行ずつ同じであること。** 名前の書き方だけは画面 (末端の名前) と
    # 一括処理 (フォルダ付きの道筋) で違うので、そこは末端で見比べる
    trim = lambda ln: re.sub(r"・[^ ]*/", "・", ln.strip())
    if [trim(ln) for ln in cli] != [trim(ln) for ln in screen]:
        errors.append("一括処理と画面で候補の行が違う:\n  CLI  : "
                      + " | ".join(trim(ln) for ln in cli)
                      + "\n  画面 : " + " | ".join(trim(ln) for ln in screen))

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
