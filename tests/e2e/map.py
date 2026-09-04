import asyncio, os, struct, sys
from playwright.async_api import async_playwright

from common import REPO, WORK, launch
MAPF = os.path.join(WORK, "M_A11000.BIN")

def msg4(entries):
    tab = 4 + len(entries) * 4
    buf = struct.pack("<I", len(entries)); p = tab; body = b""
    for e in entries:
        buf += struct.pack("<I", p if e else 0)
        body += struct.pack("<%dH" % len(e), *e); p += len(e) * 2
    return buf + body

def tables(ts):
    head = 4 + len(ts) * 12
    bodies = [msg4(t) for t in ts]
    buf = struct.pack("<I", len(ts)); p = head; data = b""
    for i, b in enumerate(bodies):
        buf += struct.pack("<IHHHH", 0xDEAD, len(b), 100 + i, p, 0); data += b; p += len(b)
    return buf + data

def mapfile(parts):
    n = len(parts); head = ((4 + n * 8 + 15) // 16) * 16
    buf = struct.pack("<I", n); data = b""; off = head
    for p in parts:
        if p is None: buf += struct.pack("<II", 0, 0); continue
        buf += struct.pack("<II", off, len(p)); pad = p + b"\0" * ((16 - len(p) % 16) % 16); data += pad; off += len(pad)
    buf += b"\0" * (head - len(buf))
    return buf + data

text = tables([[[5, 6, 0x8001, 7, 0x8000]], [[0, 1, 0x8000], []]])
open(MAPF, "wb").write(mapfile([b"\x11" * 40, text, None]))

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [MAPF])
        await page.wait_for_selector("#shell:not([hidden])")
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note0 = await page.text_content("#msgnote")
        await page.click("#mapsplit")
        await page.wait_for_function("document.querySelector('#capnote').textContent.includes('切り分けました')", timeout=20000)
        names = await page.eval_on_selector_all("#tree .filerow .nm", "els => els.map(e => e.textContent)")
        # select 1.bin
        await page.click("#tree .filerow:has(.nm:text-is('1.bin'))")
        await page.wait_for_timeout(300)
        await page.click('[data-tab="format"]')
        await page.fill("#msgglyphs", "あいうえおかきくけこ")
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note1 = await page.text_content("#msgnote")
        rows = await page.eval_on_selector_all("#msgbox tbody tr", "els => els.map(e => [...e.querySelectorAll('td')].map(t => t.textContent))")
        print("note0:", note0); print("names:", names); print("note1:", note1); print("rows:", rows); print("errors:", errors)
        await b.close()
        ok = ("入れ物" in note0 and "1.bin" in names and "表 2 / 2" in note1
              and [r[0] for r in rows] == ["0-0", "1-0"] and rows[0][3] == "かき\nく{END}" and rows[1][3] == "あい{END}" and not errors)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)

asyncio.run(main())
