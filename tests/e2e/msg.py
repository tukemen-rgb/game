import asyncio, os, struct, sys
from playwright.async_api import async_playwright

from common import REPO, WORK, launch
MSG = os.path.join(WORK, "system.msg")

# 8 バイト刻みの .msg を作る (Hilltop の MSG.py で確認した形)
entries = [[5, 6, 0x8001, 7, 0x8000], [], [0x8002, 0x12, 9, 0x8000, 0xCDCD]]
tab = 4 + len(entries) * 8
body = b"".join(struct.pack("<%dH" % len(e), *e) for e in entries)
buf = struct.pack("<I", len(entries))
p = tab
for e in entries:
    buf += struct.pack("<II", p if e else 0, 0x1234)
    p += len(e) * 2
buf += body
open(MSG, "wb").write(buf)

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [MSG])
        await page.wait_for_selector("#shell:not([hidden])")
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note1 = await page.text_content("#msgnote")
        rows1 = await page.eval_on_selector_all("#msgbox tbody tr", "els => els.map(e => e.textContent)")
        await page.fill("#msgglyphs", "あいうえお\nかきくけこ")
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note2 = await page.text_content("#msgnote")
        rows2 = await page.eval_on_selector_all("#msgbox tbody td:nth-child(4)", "els => els.map(e => e.textContent)")
        await page.click("#msgtsv")
        await page.wait_for_timeout(100)
        tsv = await page.input_value("#msgtsvtext")
        print("tsv:", repr(tsv))
        # 文字表は端末に覚えておく → 読み直しても残る
        await page.reload()
        await page.wait_for_selector("#msgglyphs", state="attached")
        kept = await page.input_value("#msgglyphs")
        print("kept glyphs:", repr(kept))
        if not tsv.startswith("id\toffset\tsize\toriginal\ttranslation\n0\t0x1C\t10\tかき<BR>く\tかき<BR>く") or kept != "あいうえお\nかきくけこ":
            errors.append("tsv or glyph persistence failed")
        print("note1:", note1); print("rows1:", rows1)
        print("note2:", note2); print("rows2:", rows2)
        print("errors:", errors)
        await b.close()
        ok = ("3 件" in note1 and len(rows1) == 2 and rows2 == ["かき\nく{END}", "{WAIT 18}こ{END}"] and not errors)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)

asyncio.run(main())
