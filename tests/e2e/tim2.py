import asyncio, sys
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
        await page.set_input_files("#fileinput", [REPO + "/work/FONT.TMS"])
        await page.wait_for_selector("#shell:not([hidden])")
        await page.click('[data-tab="format"]')
        await page.wait_for_selector("#formatbox canvas")
        kv = await page.text_content("#formatbox")
        size = await page.evaluate("(() => { const c = document.querySelector('#formatbox canvas'); return [c.width, c.height]; })()")
        # 左上の画素 (枠線 = 色 1) が描かれているか。拡大率 2 なので (1,1)
        px = await page.evaluate("""(() => { const c = document.querySelector('#formatbox canvas');
            const d = c.getContext('2d').getImageData(1, 1, 1, 1).data; return [...d]; })()""")
        await page.click("#formatbox .chipbtn")
        await page.wait_for_timeout(100)
        hint = await page.text_content("#formatbox .hint")
        print("kv:", kv); print("canvas:", size); print("px(1,1):", px); print("hint:", hint); print("errors:", errors)
        await b.close()
        ok = ("391 × 92" in kv and "8bit 索引" in kv and "位置 0x80" in kv and size == [782, 184]
              and px[3] == 255 and px[:3] != [119, 119, 119] and "1 行 17 字" in hint and not errors)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)

asyncio.run(main())
