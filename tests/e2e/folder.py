"""**フォルダごと読む**を、ごみが混ざった状態で試す (#162).

docs/10 の手順 1 は「**フォルダごと読む** で吸い出したフォルダを開く」——
社長が構造探査台で**いちばん最初にする操作**です。ところが `#dirinput` を
押す検査が一つもありませんでした。ほかの検査はどれもファイルを 1 つずつ
`#fileinput` に渡しています。

しかも実物のフォルダには **`.DS_Store` (Mac) や `Thumbs.db` (Windows) が
ほぼ必ず混ざります**。#161 で CLI 側はこれで止まっていたことが分かったので、
画面側も同じ目で見ます。

見るのは 3 つ:

1. ごみが混ざっても、索引と本体を**自分で選び当てる**か
2. ごみを選んだとき、**分かったような顔をしない**か
   (1 バイトのファイルに「独自の形式です / 根拠 3 件」と言っていた)
3. 本物のファイルの診断が、ごみのせいで変わっていないか
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

SAMPLE = os.path.join(WORK, "BOKU2SAMPLE")
#: 実物の吸い出しに混ざるごみ。**名前の順で `.DS_Store` が先に来る**のが肝で、
#: 順番に弱い作りだと、いちばん最初に目に入るのがこれになる
JUNK = {".DS_Store": b"\x00\x00\x00\x01Bud1", "Thumbs.db": b"\xff"}


def build(tmp: str) -> str:
    """練習データを写して、ごみを混ぜたフォルダを作る."""
    game = os.path.join(tmp, "GAME")
    os.makedirs(os.path.join(game, "MAP"))
    for name in ("BOKU2.IDX", "BOKU2.IMG"):
        shutil.copy(os.path.join(SAMPLE, name), os.path.join(game, name))
    for name in sorted(os.listdir(os.path.join(SAMPLE, "MAP"))):
        shutil.copy(os.path.join(SAMPLE, "MAP", name), os.path.join(game, "MAP", name))
    for name, body in JUNK.items():
        with open(os.path.join(game, name), "wb") as fh:
            fh.write(body)
    # フォルダの中にも 1 つ置く (Mac は**どのフォルダにも**作る)
    with open(os.path.join(game, "MAP", ".DS_Store"), "wb") as fh:
        fh.write(JUNK[".DS_Store"])
    return game


async def main() -> int:
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        game = build(tmp)
        async with async_playwright() as p:
            browser = await launch(p)
            page = await browser.new_page()
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console: {m.text}")
                    if m.type == "error" else None)
            await page.goto("file://" + REPO + "/web/index.html")
            # **フォルダごと**渡す。ここがこの検査の眼目で、ほかのどの検査も
            # #fileinput にファイルを並べている
            await page.set_input_files("#dirinput", game)
            await page.wait_for_selector("#shell:not([hidden])", timeout=30000)
            await page.wait_for_timeout(1500)

            names = await page.eval_on_selector_all(
                "#tree .filerow .nm", "e => e.map(x => x.textContent.trim())")
            print(f"  一覧: {names}")
            for real in ("BOKU2.IDX", "BOKU2.IMG", "M_A01000.BIN"):
                if real not in names:
                    errors.append(f"本物の {real} が一覧に出ない (ごみに押し出された)")

            # 1. ごみが混ざっても、索引と本体を自分で選び当てるか
            await page.click('[data-tab="index"]')
            await page.wait_for_timeout(600)
            src = await page.eval_on_selector(
                "#idxsrc", "e => e.selectedOptions[0] && e.selectedOptions[0].textContent")
            dat = await page.eval_on_selector(
                "#idxdata", "e => e.selectedOptions[0] && e.selectedOptions[0].textContent")
            print(f"  既定の選択: 索引 {(src or '').strip()} / 本体 {(dat or '').strip()}")
            if "BOKU2.IDX" not in (src or ""):
                errors.append(f"索引にごみを選んでいる: {src!r}")
            if "BOKU2.IMG" not in (dat or ""):
                errors.append(f"本体にごみを選んでいる: {dat!r}")
            await page.click("#idxrun")
            await page.wait_for_timeout(2500)
            note = (await page.text_content("#idxnote")) or ""
            print(f"  解析: {note.strip()[:80]}")
            if "DFI" not in note:
                errors.append(f"ごみが混ざると索引を読めない: {note.strip()[:120]!r}")

            # 2. ごみを選んだとき、分かったような顔をしないこと
            for junk in sorted(JUNK):
                await select_file(page, junk, junk)
                await page.click('[data-tab="diag"]')
                await page.wait_for_timeout(500)
                box = re.sub(r"\s+", " ", (await page.text_content("#diagbox")) or "")
                verdict = re.search(r"これは (.+?) です", box)
                print(f"  {junk}: {verdict.group(1) if verdict else '(判定が読めない)'}")
                if not verdict:
                    errors.append(f"{junk}: 判定の行が出ていない")
                elif "小さすぎて判定できません" not in verdict.group(1):
                    errors.append(f"{junk}: 1 バイト級のごみに「{verdict.group(1)}」と"
                                  f"言い切っている")
                # 根拠の数も見る。**言い切らないのに根拠が 3 件**では筋が通らない
                m = re.search(r"そう判断した根拠 \((\d+)\)", box)
                if m and int(m.group(1)) > 1:
                    errors.append(f"{junk}: 判定できないのに根拠が {m.group(1)} 件")

            # 3. 本物の診断が、ごみのせいで変わっていないこと。
            #    **魔法数を持たない小さめのファイル**も見る。`BOKU2.IDX` は
            #    `DFI\0` で除外されるので、短さの境目を広げても気づけない
            for real in ("BOKU2.IDX", "M_A01000.BIN"):
                await select_file(page, real, real)
                await page.click('[data-tab="diag"]')
                await page.wait_for_timeout(500)
                box = re.sub(r"\s+", " ", (await page.text_content("#diagbox")) or "")
                verdict = re.search(r"これは (.+?) です", box)
                print(f"  {real}: {verdict.group(1) if verdict else '(読めない)'}")
                if not verdict:
                    errors.append(f"{real}: 判定の行が出ていない")
                elif "小さすぎ" in verdict.group(1):
                    errors.append(f"本物の {real} まで「小さすぎ」になった "
                                  f"(短さの境目が広すぎる): {box[:120]!r}")

            await browser.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
