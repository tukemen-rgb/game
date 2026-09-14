"""構造探査台の**全部のタブ**を開いて、壊れていないことを見る (#144).

タブは 11 枚あるのに、ヘッドレスの検査が開いていたのは 4 枚だけだった
(`diag` / `index` / `format` / `report`)。残りの 7 枚
(`hex` / `strings` / `pointers` / `disasm` / `gallery` / `tiles` / `relative`) は
**一度も描かれていなかった**ので、押した瞬間に例外を投げるようになっても
誰も気づかない。素人が最初にやるのは端から順に押してみることなので、
そこが一番無防備だった。

中身の正しさは各タブ担当の検査 (`sample` / `docs07` / `tim2` など) が見る。
ここで見るのは **開くこと**と **何か描かれること**の 2 点だけ。薄く広く。

期待するタブの一覧は `web/index.html` から読み取る。タブを足したら、
この検査が自動でそれも開く (足したのに開かない、が起きない)。
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

from common import REPO, launch

#: タブを押しただけでは中身が入らないもの (材料を選ぶ / ボタンを押すのが先)。
#: **空でもよい**と認める代わりに、なぜよいのかをここに書く。
MAY_BE_EMPTY = {
    "report": "「調査メモを作る」を押してから中身が入る",
}

#: 中身があると認める最低の文字数。見出しだけで終わっていないことを見る
MIN_TEXT = 60


def documented_tabs() -> list:
    with open(os.path.join(REPO, "web", "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    return list(dict.fromkeys(re.findall(r'role="tab" data-tab="(\w+)"', html)))


async def main() -> int:
    errors = []
    tabs = documented_tabs()
    if len(tabs) < 8:
        print(f"index.html からタブを {len(tabs)} 枚しか拾えない (拾い方が壊れた)")
        print("RESULT NG")
        return 1

    iso = os.path.join(REPO, "work", "RINFOLT.iso")
    if not os.path.isfile(iso):
        print(f"練習用の ISO が無い: {iso}")
        print("RESULT NG")
        return 1

    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        seen = []
        page.on("pageerror", lambda e: seen.append(f"pageerror: {e}"))
        page.on("console", lambda m: seen.append(f"console: {m.text}") if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [iso])
        await page.wait_for_selector("#shell:not([hidden])")

        for tab in tabs:
            before = len(seen)
            await page.click(f'[data-tab="{tab}"]')
            await page.wait_for_timeout(400)
            # 押したタブだけが見えていること (切り替えそのものが働いているか)
            shown = await page.eval_on_selector_all(
                ".tabpanel:not([hidden])", "els => els.map(e => e.id)")
            if shown != [f"tab-{tab}"]:
                errors.append(f"{tab}: 表に出ている面が {shown} (1 枚だけのはず)")
            text = await page.eval_on_selector(f"#tab-{tab}", "el => el.innerText.trim()")
            note = "" if len(text) >= MIN_TEXT else f" / 中身 {len(text)} 字"
            print(f"  {tab:9} {len(text):5} 字{note}")
            if len(text) < MIN_TEXT and tab not in MAY_BE_EMPTY:
                errors.append(f"{tab}: 開いても中身がほぼ無い ({len(text)} 字)")
            for line in seen[before:]:
                errors.append(f"{tab}: {line[:120]}")
        await browser.close()

    for e in errors:
        print("  -", e)
    print(f"開いたタブ {len(tabs)} 枚")
    print("RESULT " + ("OK" if not errors else "NG"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
