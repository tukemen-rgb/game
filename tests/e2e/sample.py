"""docs/10 の「画面で確かめる」を練習データで通す."""
import asyncio, os, sys
from playwright.async_api import async_playwright

from common import REPO, WORK, launch
S = os.path.join(REPO, "work", "BOKU2SAMPLE")

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [os.path.join(S, "BOKU2.IDX"), os.path.join(S, "BOKU2.IMG"),
                                                  os.path.join(S, "MAP", "M_A01000.BIN")])
        await page.wait_for_selector("#shell:not([hidden])")
        # 2. 索引
        await page.click('[data-tab="index"]')
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        await page.select_option("#idxsrc", [o for o in opts if o.endswith("BOKU2.IDX")][0])
        await page.select_option("#idxdata", [o for o in opts if o.endswith("BOKU2.IMG")][0])
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary")
        note = await page.text_content("#idxnote")
        # 3. 切り分け
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function("document.querySelector('#capnote').textContent.includes('切り分けました')", timeout=30000)
        names = await page.eval_on_selector_all("#tree .filerow .nm", "els => els.map(e => e.textContent)")
        dirs = await page.eval_on_selector_all("#tree .dir", "els => els.map(e => e.textContent)")
        # 4. system.msg を読む (文字表なし)
        await page.fill("#treeq", "system.msg")
        await page.click("#tree .filerow:has(.nm:text-is('system.msg'))")
        await page.wait_for_timeout(300)
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note_msg = await page.text_content("#msgnote")
        # 5. フォント画像
        await page.fill("#treeq", "font")
        await page.click("#tree .filerow:has(.nm:text-is('bk_font.tms'))")
        await page.wait_for_timeout(300)
        await page.click('[data-tab="format"]')
        await page.wait_for_selector("#formatbox canvas")
        fmt = await page.text_content("#formatbox")
        # 6. 文字表を貼る → 日本語になる
        font = open(os.path.join(S, "font.txt"), encoding="utf-8").read()
        await page.fill("#treeq", "system.msg")
        await page.click("#tree .filerow:has(.nm:text-is('system.msg'))")
        await page.wait_for_timeout(300)
        await page.click('[data-tab="format"]')
        await page.fill("#msgglyphs", font)
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        menu = await page.eval_on_selector_all("#msgbox tbody td:nth-child(4)", "els => els.map(e => e.textContent)")
        # 7. マップの入れ物 → 1.bin → 会話
        await page.fill("#treeq", "M_A01000")
        await page.click("#tree .filerow:has(.nm:text-is('M_A01000.BIN'))")
        await page.wait_for_timeout(300)
        await page.click('[data-tab="format"]')
        await page.click("#mapsplit")
        await page.wait_for_function("document.querySelector('#capnote').textContent.includes('マップの入れ物として')", timeout=20000)
        await page.fill("#treeq", "1.bin")
        await page.click("#tree .filerow:has(.nm:text-is('1.bin'))")
        await page.wait_for_timeout(300)
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        talk = await page.eval_on_selector_all("#msgbox tbody td:nth-child(4)", "els => els.map(e => e.textContent)")
        print("idxnote:", note); print("dirs:", dirs); print("names:", names[:12])
        print("msg note:", note_msg); print("font:", "TIM2" in fmt, "位置 0x80" in fmt)
        print("menu:", menu); print("talk:", talk); print("errors:", errors)
        await b.close()
        ok = ("DFI" in note and "/BOKU2.IMG/system/" in dirs and "system.msg" in names and "bk_font.tms" in names
              and "4 件" in note_msg and "位置 0x80" in fmt
              and menu == ["はじめから{END}", "つづきから{END}", "せってい{END}", "おわる{END}"]
              and talk[0] == "{VOICE 00010001}" and talk[1] == "きょうはうみにいくんだ。\nいっしょにいこうよ。{WAIT 10}{END}"
              and not errors)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)

asyncio.run(main())
