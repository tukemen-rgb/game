"""docs/07 が「このファイルはこう診断される」と書いている表を、画面で確かめる (#111).

docs/07 の冒頭にある

    | ファイル | 診断 |

は、素人が構造探査台を最初に触ったときに**見えるはずのもの**の一覧。練習用の ISO を
読ませて、8 行が 1 行ずつそのとおりに出るかを見る。#110 で docs/06 に同じことをした。

期待値は docs/07 の表から読み取るので、表を書き換えれば検査がそのまま追いかける。
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

DOC = os.path.join(REPO, "docs", "07-構造探査台.md")


def doc_table():
    """docs/07 の「ファイル | 診断」の表を dict で返す."""
    with open(DOC, encoding="utf-8") as fh:
        doc = fh.read()
    if "| ファイル | 診断 |" not in doc:
        return {}
    body = doc.split("| ファイル | 診断 |", 1)[1].split("\n\n", 1)[0]
    out = {}
    for line in body.split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0].startswith("`"):
            out[cells[0].strip("`")] = cells[1]
    return out


async def main():
    errors = []
    want = doc_table()
    if len(want) < 8:
        print(f"docs/07 の表から {len(want)} 行しか読めていない (作りが変わった)")
        print("RESULT NG")
        sys.exit(1)

    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [os.path.join(WORK, "RINFOLT.iso")])
        await page.wait_for_selector("#shell:not([hidden])")
        names = await page.eval_on_selector_all("#tree .filerow .nm", "e => e.map(x => x.textContent)")
        print(f"docs/07 の表 {len(want)} 行 / ISO の中身 {len(names)} 件")

        checked = 0
        for name, diagnosis in want.items():
            if name.endswith(".iso"):
                # イメージそのものは一覧に「(イメージ全体)」として出る
                continue
            if name not in names:
                errors.append(f"{name}: docs/07 が挙げているファイルが ISO に無い")
                continue
            await select_file(page, name, name)
            await page.click('[data-tab="diag"]')
            await page.wait_for_timeout(400)
            box = re.sub(r"\s+", " ", (await page.text_content("#diagbox")) or "")
            if f"これは {diagnosis} です" not in box:
                errors.append(f"{name}: docs/07 は「{diagnosis}」と書いているが、"
                              f"画面は {box[:90]!r}")
            else:
                checked += 1
            print(f"  {name}: {diagnosis}")

        if checked < 7:
            errors.append(f"確かめた行が {checked} 行しかない (素通りの疑い)")
        await b.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    sys.exit(0 if not errors else 1)


asyncio.run(main())
