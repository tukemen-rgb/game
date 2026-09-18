"""**診ていない所があるときに、画面が言い切らない**かを確かめる (#234).

社長が画面に渡すのは `BOKU2.IDX` と `BOKU2.IMG` の 2 つだけ、ということが普通に
起きる (`MAP/*.BIN` は本体の外にあるので、ドラッグし忘れると届かない)。すると
**物語の会話を 1 行も読んでいない**まま要約ができる。この作品の本文の大半は
そこにあるので、「いちばん大きい番号は 165 → 2 枚目の画像は要りません」は
ひっくり返り得る。判定を言い切ってはいけない場面。

一括処理 (`boku2.py check`) 側は `tests/run_tests.py` が見ている。ここは
**画面が呼び出し側で断りを渡しているか**を見る —— 判定の関数だけ直しても、
渡す側が黙っていれば意味が無いため。

見るのは 3 つ:

1. MAP を渡さないと、**何を診ていないか**を言う
2. そのとき、頁の判定 (「2 枚目の画像は要りません」) を**出さない**
3. MAP も渡せば、今までどおり判定を出す (断りが出っぱなしにならない)
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch

SAMPLE = os.path.join(WORK, "BOKU2SAMPLE")

VERDICT = "2 枚目の画像は要りません"
CAVEAT = "MAP の会話を診ていない"


async def report_for(files: list, errors: list) -> str:
    """その並びのファイルを読ませて、報告用の要約の中身を返す."""
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
        await page.click("#idxreport")
        await page.wait_for_function(
            "document.querySelector('#idxreporttext').value.includes('== 結果')", timeout=30000)
        text = await page.input_value("#idxreporttext")
        await browser.close()
    return text


async def main():
    errors = []
    idx = os.path.join(SAMPLE, "BOKU2.IDX")
    img = os.path.join(SAMPLE, "BOKU2.IMG")
    maps = [os.path.join(SAMPLE, "MAP", n)
            for n in sorted(os.listdir(os.path.join(SAMPLE, "MAP")))]
    if not maps:
        print("練習データに MAP がありません (材料が弱い)")
        print("RESULT NG")
        return 1

    part = await report_for([idx, img], errors)
    line = next((ln.strip() for ln in part.split("\n") if "使われている文字番号の最大" in ln), "")
    print("  索引と本体だけ:", line[:150])
    if CAVEAT not in line:
        errors.append(f"何を診ていないか言っていない: {line[:150]!r}")
    if VERDICT in line:
        errors.append(f"本文の一部だけで頁の判定を言い切っている: {line[:150]!r}")
    if "ここでは決まりません" not in line:
        errors.append(f"決まらないと言っていない: {line[:150]!r}")

    whole = await report_for([idx, img] + maps, errors)
    line2 = next((ln.strip() for ln in whole.split("\n") if "使われている文字番号の最大" in ln), "")
    print("  MAP も渡した:", line2[:150])
    if CAVEAT in line2:
        errors.append(f"MAP を渡したのに断りが残っている: {line2[:150]!r}")
    if VERDICT not in line2:
        # ここが出ないなら、上の「出さない」は **いつも出ない** だけかもしれない
        errors.append(f"ぜんぶ診たのに判定を出していない (材料が弱い): {line2[:150]!r}")

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
