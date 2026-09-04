import asyncio, os, sys
from playwright.async_api import async_playwright

from common import REPO, WORK, launch

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [REPO + "/work/PACK.IDX", REPO + "/work/PACK.IMG"])
        await page.wait_for_selector("#shell:not([hidden])")
        await page.click('[data-tab="index"]')
        await page.select_option("#idxsrc", label=None, index=None, value=None) if False else None
        # pick idx/data explicitly
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        idx = [o for o in opts if o.endswith("PACK.IDX")][0]
        img = [o for o in opts if o.endswith("PACK.IMG")][0]
        await page.select_option("#idxsrc", idx)
        await page.select_option("#idxdata", img)
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary")
        note = await page.text_content("#idxnote")
        print("idxnote:", note)
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function("document.querySelector('#capnote').textContent.includes('切り分けました')", timeout=30000)
        cap = await page.text_content("#capnote")
        print("capnote:", cap)
        names = await page.eval_on_selector_all("#tree .filerow .nm", "els => els.map(e => e.textContent)")
        print("first names:", names[:6], "count", len(names))
        suffixed = [n for n in names if n.startswith("#") and "." in n]
        print("suffixed:", len(suffixed))
        # filter
        await page.fill("#treeq", "txt")
        await page.wait_for_timeout(100)
        names2 = await page.eval_on_selector_all("#tree .filerow .nm", "els => els.map(e => e.textContent)")
        print("filter txt:", names2[:5], "count", len(names2), "treecount:", await page.text_content("#treecount"))
        await page.fill("#treeq", "zzzz")
        await page.wait_for_timeout(100)
        print("filter none:", await page.text_content("#tree"))
        await page.fill("#treeq", "")
        await page.wait_for_timeout(100)
        names3 = await page.eval_on_selector_all("#tree .filerow .nm", "els => els.map(e => e.textContent)")
        print("filter cleared count:", len(names3), await page.text_content("#treecount"))
        print("errors:", errors)
        await b.close()
        ok = (len(suffixed) > 0 and len(names2) < len(names) and len(names3) == len(names)
              and not errors and "見当" in cap)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)

asyncio.run(main())
