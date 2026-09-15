"""フォント画像の **2 枚目** を、画面で選んで番号どおりに読めるか (#169).

#167 で「文字表は 1 枚に収まらない」と分かり、#168 で「続きはここにあります」と
言えるようにした。ところが **言われた所を画面で開けるか** は、通しで一度も
試していなかった。しかも #168 で足した切り替えの釦には、番号を **0 から振り直す**
という壊れ方がある。2 枚目を 0 から番号付けすると、社長が書き出した文字表は
丸ごとずれる —— 出てくる字は日本語のままなので、目では気づけない (#158 と同じ型)。

練習データ (`make_boku2_sample.py`) は #169 からフォントを 2 枚に分けてある。
ここで見るのは 4 つ:

1. `bk_font.tms` を開くと、**切り替えの釦が 2 つ以上**出るか
2. 1 枚目の左上が **0** 番か
3. 2 枚目に切り替えると、左上が **1 枚目のマス数** から始まるか (0 に戻らないか)
4. 2 枚目の案内が、**前の頁の続きだと言う**か
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

SAMPLE = os.path.join(WORK, "BOKU2SAMPLE")


def font_pages() -> list[dict]:
    """練習データの `bk_font.tms` に入っている頁を、一括処理側の道具で数える."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    try:
        import boku2
    finally:
        sys.path.remove(os.path.join(REPO, "tools"))
    with open(os.path.join(SAMPLE, "BOKU2.IDX"), "rb") as fh:
        idx = fh.read()
    img = os.path.join(SAMPLE, "BOKU2.IMG")
    entry = next(e for e in boku2.read_dfi(idx, os.path.getsize(img))
                 if e["path"].endswith("bk_font.tms"))
    with open(img, "rb") as fh:
        fh.seek(entry["at"])
        blob = fh.read(entry["len"])
    return [{**p, "cells": boku2.font_page_cells(p)} for p in boku2.tim2_pages(blob)]


async def main() -> int:
    errors = []
    pages = font_pages()
    print(f"  練習データの頁: {[(hex(p['at']), p['width'], p['height'], p['cells']) for p in pages]}")
    if len(pages) < 2:
        print("  練習データのフォントが 1 枚しかない (make_boku2_sample.py が 2 枚に分けるはず)")
        print("RESULT NG")
        return 1

    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [os.path.join(SAMPLE, "BOKU2.IDX"),
                                                  os.path.join(SAMPLE, "BOKU2.IMG")])
        await page.wait_for_selector("#shell:not([hidden])", timeout=30000)

        # 索引を切り分けてから、フォントを選ぶ (docs/10 の手順どおり)
        await page.click('[data-tab="index"]')
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary", timeout=30000)
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function(
            "document.querySelector('#capnote').textContent.includes('切り分けました')",
            timeout=30000)
        await select_file(page, "font", "bk_font.tms")
        await page.click('[data-tab="format"]')
        await page.wait_for_selector("#formatbox canvas", timeout=30000)

        # 1. 切り替えの釦
        chips = await page.eval_on_selector_all(
            "#tim2pages .chipbtn", "e => e.map(x => x.textContent.trim())")
        print(f"  切り替えの釦: {chips}")
        if len(chips) < len(pages):
            errors.append(f"頁が {len(pages)} 枚あるのに釦が {len(chips)} 個")

        async def first_label() -> int | None:
            """いま出ている目盛りの、左上のマスの番号を案内文から読む."""
            notes = await page.eval_on_selector_all(
                ".tim2body .hint, .tim2body .warnbar",
                "e => e.map(x => x.textContent)")
            note = re.sub(r"\s+", " ", " ".join(notes))
            m = re.search(r"左上の (\d+) から順に", note)
            if m is None:
                print(f"    (案内文: {note[:200]!r})")
            return int(m.group(1)) if m else None

        # 2. 1 枚目は 0 から
        await page.click("#tim2grid")
        await page.wait_for_timeout(800)
        head = await first_label()
        print(f"  1 枚目の左上: {head}")
        if head != 0:
            errors.append(f"1 枚目の左上が {head} 番 (0 のはず)")

        # 3. 2 枚目は 1 枚目のマス数から
        if len(chips) >= 2:
            await page.click("#tim2pages .chipbtn:nth-child(2)")
            await page.wait_for_timeout(800)
            if await page.get_attribute("#tim2grid", "aria-pressed") != "true":
                await page.click("#tim2grid")
                await page.wait_for_timeout(800)
            head2 = await first_label()
            want = pages[0]["cells"]
            print(f"  2 枚目の左上: {head2} (1 枚目のマス数 {want})")
            if head2 == 0:
                errors.append("2 枚目の番号が 0 に戻っている "
                              "(このまま書き出すと文字表が丸ごとずれる)")
            elif head2 != want:
                errors.append(f"2 枚目の左上が {head2} 番 (1 枚目のマス数 {want} のはず)")

            # 4. 続きだと言っているか
            note = re.sub(r"\s+", " ", (await page.text_content(".tim2body .hint")) or "")
            if "前の頁の続き" not in note:
                errors.append(f"2 枚目が前の頁の続きだと言っていない: {note[:120]!r}")

        await browser.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
