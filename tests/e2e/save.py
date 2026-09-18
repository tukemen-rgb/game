"""画面が作ったものを**ファイルに落として、そのまま一括処理に渡せる**か (#209).

#199/#200 で分かったこと: この作品の文字表には `¥` `—` `♡` `︙` が入っていて、
メモ帳や Excel で「ANSI (cp932)」を選んで保存すると、その 4 字が黙って `?` に
化ける。道具は後から気づけるようにしたが、**そもそも手で貼らせるから起きる**。

そこで画面に「ファイルに保存」を付けた。ここで見るのは 4 つ:

1. 落ちたファイルが **UTF-8 のまま**で、cp932 に無い字が生きていること
2. 文字表には **BOM を付けない** (1 文字ずつが番号なので、1 つ増えると全部ずれる)
   / TSV には **BOM を付ける** (Excel 向け。一括処理の書き出しと同じ)
3. 落ちたファイルを**そのまま一括処理に渡せる**こと
   (`boku2.py fontlist` / `proofread.py`)。ここが通らなければ保存する意味がない
4. 保存した文字表の**行幅が揃っていなければ、その場で何行目かを言う**こと (#229)。
   1 行だけ 22 字になると、そこから下の番号が全部ずれる。ずれても全部の番号に
   字は当たるので、保存した瞬間に言わないと、もう誰も気づけない
"""
import asyncio
import os
import subprocess
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

SAMPLE = os.path.join(WORK, "BOKU2SAMPLE")
OUT = os.path.join(WORK, "savecheck")

#: cp932 で書けない字。文字表に混ぜて、保存で消えないことを見る
HARD = "¥—♡︙"


def cp932_cannot_write(text: str) -> list:
    out = []
    for ch in text:
        try:
            ch.encode("cp932")
        except UnicodeEncodeError:
            out.append(ch)
    return out


async def main():
    errors = []
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page(accept_downloads=True)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [
            os.path.join(SAMPLE, "BOKU2.IDX"), os.path.join(SAMPLE, "BOKU2.IMG")])
        await page.wait_for_selector("#shell:not([hidden])")
        await page.click('[data-tab="index"]')
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        await page.select_option("#idxsrc", [o for o in opts if o.endswith("BOKU2.IDX")][0])
        await page.select_option("#idxdata", [o for o in opts if o.endswith("BOKU2.IMG")][0])
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary")
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function(
            "document.querySelector('#capnote').textContent.includes('切り分けました')", timeout=30000)

        # 1. 文字表を貼る (練習データの表の後ろに、cp932 で書けない 4 字を足す)
        await select_file(page, "system.msg", "system.msg")
        await page.click('[data-tab="format"]')
        with open(os.path.join(SAMPLE, "font.txt"), encoding="utf-8") as fh:
            font = fh.read().rstrip("\n")
        await page.fill("#msgglyphs", font + HARD)
        await page.click("#msgparse")
        await page.wait_for_timeout(300)

        # 2. 文字表をファイルに落とす
        async with page.expect_download() as got:
            await page.click("#glyphsave")
        dl = await got.value
        font_out = os.path.join(OUT, "font.txt")
        await dl.save_as(font_out)
        print("glyph file:", dl.suggested_filename, os.path.getsize(font_out), "bytes")
        if dl.suggested_filename != "font.txt":
            errors.append(f"文字表の名前が font.txt でない: {dl.suggested_filename}")

        # 3. 校正用の TSV をファイルに落とす
        await page.click("#msgtsv")
        async with page.expect_download() as got2:
            await page.click("#msgtsvsave")
        dl2 = await got2.value
        tsv_out = os.path.join(OUT, dl2.suggested_filename)
        await dl2.save_as(tsv_out)
        print("tsv file:", dl2.suggested_filename, os.path.getsize(tsv_out), "bytes")
        note = await page.text_content("#glyphsavenote")
        print("note:", note)
        if "UTF-8" not in (note or ""):
            errors.append(f"何で保存したかを言っていない: {note!r}")
        # **無事な表には何も言わないこと** (毎回出たら誰も読まなくなる)
        if "行目" in (note or ""):
            errors.append(f"無事な文字表に書き写しのずれを言っている: {note!r}")

        # 4. **1 行だけ字数が違う文字表**を保存すると、何行目かを言うこと (#229)。
        #    ずれても全部の番号に字は当たるので、ここで言わないと誰も気づけない
        rows = [ln for ln in font.split("\n") if ln]
        widths = {len(ln) for ln in rows[:-1]}
        if len(rows) < 3 or len(widths) != 1:
            errors.append(f"材料が弱い: 練習データの文字表の行幅が揃っていない ({widths})")
        slipped = list(rows)
        slipped[1] = slipped[1][:-1]              # 2 行目だけ 1 字少ない
        await page.fill("#msgglyphs", "\n".join(slipped))
        async with page.expect_download() as got3:
            await page.click("#glyphsave")
        await (await got3.value).save_as(os.path.join(OUT, "font_slipped.txt"))
        note2 = await page.text_content("#glyphsavenote")
        print("note (slipped):", note2)
        for want in ("2 行目", str(len(rows[0]))):
            if want not in (note2 or ""):
                errors.append(f"書き写しのずれで「{want}」を言っていない: {note2!r}")
        await browser.close()

    # --- 落ちた中身を見る ---
    raw = open(font_out, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        errors.append("文字表に BOM が付いている (1 字ぶん番号が全部ずれる)")
    text = raw.decode("utf-8")
    lost = [ch for ch in HARD if ch not in text]
    if lost:
        errors.append(f"cp932 で書けない字が消えている: {lost}")
    if not cp932_cannot_write(HARD):
        errors.append("材料が弱い: 混ぜた字は cp932 でも書けるので、化けようがない")

    raw2 = open(tsv_out, "rb").read()
    if raw2[:3] != b"\xef\xbb\xbf":
        errors.append("TSV に BOM が無い (Excel が UTF-8 と判らず、開いた時点で化ける)")

    # --- そのまま一括処理に渡せるか ---
    chars = os.path.join(OUT, "font_chars.txt")
    r1 = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                         "fontlist", font_out, "-o", chars], capture_output=True, text=True, cwd=REPO)
    print("fontlist rc:", r1.returncode, (r1.stdout + r1.stderr).strip()[:200])
    if r1.returncode != 0:
        errors.append("落とした文字表を fontlist が読めない")
    else:
        got = open(chars, encoding="utf-8-sig").read()
        gone = [ch for ch in HARD if ch not in got]
        if gone:
            errors.append(f"fontlist を通ると字が消える: {gone}")

    r2 = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                         tsv_out, "--font-chars", chars], capture_output=True, text=True, cwd=REPO)
    out2 = r2.stdout + r2.stderr
    print("proofread rc:", r2.returncode, out2.strip()[-300:])
    # 中身の良し悪しではなく、**受け取れたか**を見る。受け取れないなら保存する意味がない
    for bad in ("がありません", "検査する行が 1 行もありません", "ANSI (cp932) で保存"):
        if bad in out2:
            errors.append(f"落とした TSV を proofread が受け取れていない: {bad}")

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
