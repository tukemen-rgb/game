"""docs/10 の「困ったとき」の最初の 2 行を、**画面で本当に出るか**確かめる (#132).

    | 索引が「候補なし」                | … |
    | `.msg` が「この形では読めませんでした」 | … |

この 2 つは画面だけが出す文言で、これまで **`web/app.js` にその文字列があるか**を
探して済ませていた。#131 で分かったとおり、ソースの検索は「組み立てて出す文言を
見つけられない」だけでなく、「**書いてあるが二度と出ない**」も見分けられない。
どちらもデータが読めなかったときの道なので、実物で最初に踏むのはむしろこちら。

読ませるのは、この場で作る**でたらめなバイト列**。実物のデータは要らない。
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

DOC = os.path.join(REPO, "docs", "10-僕夏2の手順.md")


def doc_quotes() -> dict:
    """「困ったとき」の左列から、索引と .msg の症状の引用を読み取る.

    期待値を文書から取るので、文書を直せばこの検査が追いかける。
    見つからなければ (書き方が変わったら) そう言って落ちる。
    """
    import re

    with open(DOC, encoding="utf-8") as fh:
        table = fh.read().split("## 困ったとき")[1].split("###")[0]
    got = {}
    for line in table.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3:
            continue
        quotes = re.findall(r"「([^」]+)」", cells[1])
        if not quotes:
            continue
        if cells[1].startswith("索引が"):
            got["index"] = quotes[0]
        elif cells[1].startswith("`.msg` が"):
            got["msg"] = quotes[0]
    return got


def junk(path: str, size: int = 4096) -> str:
    """索引にも .msg にも見えないバイト列。0 埋めだと別の道に入るので、値を散らす."""
    with open(path, "wb") as fh:
        fh.write(bytes((i * 7 + 13) & 0xFF for i in range(size)))
    return path


async def main() -> int:
    want = doc_quotes()
    errors = []
    for key in ("index", "msg"):
        if key not in want:
            errors.append(f"docs/10 の「困ったとき」から {key} の引用を拾えない (拾い方が壊れた)")
    if errors:
        print("\n".join(errors))
        print("RESULT NG")
        return 1

    a = junk(os.path.join(WORK, "junk_idx.bin"))
    b = junk(os.path.join(WORK, "junk_img.bin"), 8192)
    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [a, b])
        await page.wait_for_selector("#shell:not([hidden])")

        # 1. 索引として読ませる → 候補なし
        await page.click('[data-tab="index"]')
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        await page.select_option("#idxsrc", [o for o in opts if o.endswith("junk_idx.bin")][0])
        await page.select_option("#idxdata", [o for o in opts if o.endswith("junk_img.bin")][0])
        await page.click("#idxrun")
        await page.wait_for_timeout(400)
        shown = await page.text_content("#idxbody")
        print("idxbody:", (shown or "").strip()[:120])
        if want["index"] not in (shown or ""):
            errors.append(f"索引タブに docs/10 の「{want['index']}」が出ない (実際: {(shown or '')[:120]!r})")
        # 行き止まりで終わらせない、という約束 (app.js の explainIndexMiss)
        hint = await page.text_content("#idxpreview")
        if not (hint or "").strip():
            errors.append("「候補なし」だけで、何を見て諦めたかが出ていない")

        # 2. .msg として読ませる → この形では読めませんでした
        await select_file(page, "junk_img", "junk_img.bin")
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(300)
        note = await page.text_content("#msgnote")
        print("msgnote:", (note or "").strip()[:160])
        if want["msg"] not in (note or ""):
            errors.append(f"既知の形式タブに docs/10 の「{want['msg']}」が出ない "
                          f"(実際: {(note or '')[:160]!r})")
        await browser.close()

    for e in errors:
        print("  -", e)
    print("RESULT " + ("OK" if not errors else "NG"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
