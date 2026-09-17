"""docs/10 の「報告するとき」が挙げている行が、本当に出てくるかを確かめる (#208).

あの節は **社長が最初に送ってくる 1 通**の中身を決めている。「この行が決め手に
なる」と書いてある行が実際には出ないと、素人は無い行を探して時間を溶かすか、
決め手の無い報告を送ってくる。どちらもこちらからは直せない。

ところが #207 まで、この節には検査が **1 つも無かった**。「困ったとき」の表と
「見る 1 点」の表は tests/run_tests.py が歩いているのに、いちばん大事なここだけ
抜けていた (`grep 本文あり tests/` が 0 件だった)。

引用の相手は 2 つに分かれる —— `boku2.py check` が出す行と、**画面にしか出ない**
行 (報告用の要約・`.msg として読む` の要約・釦の名前)。組み上がった形は画面でしか
見られないので (#131)、ここで両方をいっぺんに見る。

節の引用を拾って歩くので、**節に行を足せばこの検査が勝手に追いかける**。
"""
import asyncio
import os
import re
import subprocess
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, doc_shape, launch, select_file

HOWTO = os.path.join(REPO, "docs", "10-僕夏2の手順.md")
SAMPLE = os.path.join(WORK, "BOKU2SAMPLE")


def quoted() -> list:
    """「報告するとき」の節から、**出力の行**の引用を拾う.

    バッククォートはファイル名 (`font.txt`) や記号 (`→`) にも使うので、
    **空白を含むもの**だけを行と見なす。打つコマンドは行ではないので除く。
    """
    with open(HOWTO, encoding="utf-8") as fh:
        doc = fh.read()
    sec = doc.split("## 報告するとき", 1)[1].split("\n## ", 1)[0]
    flat = re.sub(r"\n\s+", " ", sec)                     # 折り返した引用をつなぐ
    return [q for q in re.findall(r"`([^`]+)`", flat)
            if " " in q and not q.startswith("python3")]


def cli_check() -> str:
    res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                          "check", SAMPLE], capture_output=True, text=True, cwd=REPO)
    return res.stdout + res.stderr


async def main():
    quotes = quoted()
    errors = []
    # **0 件で緑にしない。** 拾い方が壊れたら「全部当たった」に化ける
    if len(quotes) < 6:
        print(f"引用を {len(quotes)} 個しか拾えていない (拾い方が壊れた): {quotes}")
        print("RESULT NG")
        return 1

    haystack = {"check の出力": cli_check()}
    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [
            os.path.join(SAMPLE, "BOKU2.IDX"), os.path.join(SAMPLE, "BOKU2.IMG"),
            os.path.join(SAMPLE, "MAP", "M_A01000.BIN")])
        await page.wait_for_selector("#shell:not([hidden])")
        await page.click('[data-tab="index"]')
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        await page.select_option("#idxsrc", [o for o in opts if o.endswith("BOKU2.IDX")][0])
        await page.select_option("#idxdata", [o for o in opts if o.endswith("BOKU2.IMG")][0])
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary")

        # 1. 報告用の要約 (docs/10 が「または画面の『報告用の要約』」と書いている相手)
        await page.click("#idxreport")
        await page.wait_for_function(
            "document.querySelector('#idxreporttext').value.includes('== 結果')", timeout=20000)
        haystack["報告用の要約"] = await page.input_value("#idxreporttext")

        # 2. `.msg として読む` の要約 1 行 (文字表を貼った状態。docs/10 の 2 番)
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function(
            "document.querySelector('#capnote').textContent.includes('切り分けました')", timeout=30000)
        await select_file(page, "system.msg", "system.msg")
        await page.click('[data-tab="format"]')
        with open(os.path.join(SAMPLE, "font.txt"), encoding="utf-8") as fh:
            await page.fill("#msgglyphs", fh.read())
        await page.click("#msgparse")
        await page.wait_for_timeout(300)
        haystack[".msg の要約"] = await page.text_content("#msgnote")

        # 3. 釦の名前などは、描かれた画面の文字で引ける
        haystack["画面の文字"] = await page.text_content("body")
        await browser.close()

    for q in quotes:
        rx = doc_shape(q)
        found = [name for name, text in haystack.items() if re.search(rx, text or "")]
        print(f"  {'OK ' if found else 'NG '} 「{q}」 {'← ' + ' / '.join(found) if found else ''}")
        if not found:
            errors.append(f"docs/10「報告するとき」が挙げている「{q}」が、"
                          "check の出力にも画面にも出ない")

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
