"""社長に渡す単体の HTML (tools/build_web.py の出力) が、そのまま開いて動くこと.

web/index.html を直接開く他の検査と違い、ここは組み立てた 1 ファイルを開く。
組み立て (連結・埋め込み) が壊れていれば、他の検査が通ってもここで落ちる。
- 「練習用のイメージを読む」が出て、押すと一覧が出る
- 外部への通信が一度も起きない (file: 以外の要求が無い)
- 主要な画面 (診断 / 索引 / 既知の形式 / 調査メモ) が空でない
"""
import asyncio, os, subprocess, sys
from playwright.async_api import async_playwright

from common import REPO, WORK, launch
OUT = os.path.join(WORK, "構造探査台_test.html")

async def main():
    subprocess.run([sys.executable, os.path.join(REPO, "tools", "build_web.py"), "--embed-sample", "-o", OUT],
                   cwd=REPO, check=True, capture_output=True)
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: requests.append(r.url) if not r.url.startswith(("file:", "data:", "blob:")) else None)
        await page.goto("file://" + OUT)
        await page.wait_for_selector("#sample:not([hidden])")
        await page.click("#sample")
        await page.wait_for_selector("#shell:not([hidden])")
        await page.wait_for_function("document.querySelectorAll('#tree .filerow').length > 0", timeout=30000)
        names = await page.eval_on_selector_all("#tree .filerow .nm", "els => els.map(e => e.textContent)")
        diag = await page.text_content("#diagbox")
        await page.click('[data-tab="report"]')
        await page.click("#repmake")
        await page.wait_for_timeout(300)
        report = await page.input_value("#reptext")
        await page.click('[data-tab="format"]')
        has_msg = await page.is_visible("#msgparse")
        loaded = await page.text_content("#loaded")
        print("names:", names[:8]); print("diag head:", (diag or "")[:120].replace("\n", " "))
        print("report head:", report[:120].replace("\n", " ")); print("loaded:", loaded)
        print("external requests:", requests); print("errors:", errors)
        await b.close()
        ok = (len(names) >= 3 and diag and len(diag) > 50 and len(report) > 100 and has_msg
              and "RINFOLT" in (loaded or "") and not requests and not errors)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)

asyncio.run(main())
