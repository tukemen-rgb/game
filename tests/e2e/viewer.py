"""docs/06 が「訳文に切り替えるとこう見える」と約束していることを、画面で確かめる.

この画面 (`tools/make_viewer.py` が作る work/viewer.html) には**ブラウザでの検査が
1 つも無かった** (#110)。tests/run_tests.py の TestViewer は埋め込むデータを見ているだけで、
**ページが開くかどうかさえ試していなかった**。docs/06 の中心は

    | id | 訳文に切り替えると | 対応するチェック |

の表で、素人がこの一式で最初に「仕様違反が画面のどこに出るか」を掴む所。
その表が本当かは、開いて見るしかない。

期待値は**docs/06 から読み取る**。表に書いた id と現象を書き換えたら、この検査が
そのまま追いかける (#102 でやった「文書の数字を測り直す」と同じ形)。
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch

DOC = os.path.join(REPO, "docs", "06-画面で確かめる.md")


def doc_table():
    """docs/06 の「何が見えるようになるか」の表を (id, 現象, チェック名) で返す."""
    with open(DOC, encoding="utf-8") as fh:
        doc = fh.read()
    body = doc.split("## 何が見えるようになるか", 1)[1].split("\n##", 1)[0]
    rows = []
    for line in body.split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if len(cells) == 3 and cells[0].isdigit():
            rows.append((cells[0], cells[1], cells[2]))
    return rows


def font_chars():
    with open(os.path.join(REPO, "data", "font_chars.txt"), encoding="utf-8") as fh:
        return set(fh.read().replace("\n", ""))


TAG = re.compile(r"<[A-Z]+(?::[0-9A-Fa-f]+)?>")


async def show(page, msg_id, column):
    """その id を選び、原文 / 訳文 を切り替える."""
    await page.evaluate(
        """([id, col]) => {
            ui.column = col;
            ui.filter = "all";
            const i = DATA.messages.findIndex(m => m.id === id);
            if (i < 0) throw new Error("id が無い: " + id);
            select(i);
        }""", [msg_id, column])
    await page.wait_for_timeout(120)


async def main():
    errors = []
    rows = doc_table()
    if len(rows) < 6:
        print(f"docs/06 の表から {len(rows)} 行しか読めていない")
        print("RESULT NG")
        sys.exit(1)
    chars = font_chars()

    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" else None)
        await page.goto("file://" + os.path.join(WORK, "viewer.html"))
        await page.wait_for_selector("#msglist .msgrow")
        # 文字送りの途中で読むと、その時点までの分しか幅に出ない (docs/06 の
        # 「速さ 0 で一括表示」)。最初に 0 にしてから測る
        await page.eval_on_selector("#speed", """e => { e.value = 0;
            e.dispatchEvent(new Event('input', {bubbles: true})); }""")
        await page.wait_for_timeout(200)

        msgs = await page.evaluate("() => DATA.messages.map(m => ({id: m.id, tr: m.translation}))")
        by_id = {m["id"]: m["tr"] for m in msgs}
        print(f"docs/06 の表 {len(rows)} 行を画面で確かめる")

        for msg_id, phenomenon, check in rows:
            if msg_id not in by_id:
                errors.append(f"id {msg_id}: docs/06 が挙げている id が題材に無い")
                continue
            await show(page, msg_id, "translation")
            miss = await page.text_content("#misslist")
            note = await page.text_content("#meternote")
            meter = await page.eval_on_selector_all("#meter .val", "els => els.map(e => e.textContent)")
            over = await page.eval_on_selector_all("#meter .val.over", "els => els.length")
            plain = TAG.sub("", by_id[msg_id])

            if "font" in check:
                # 訳文のうちフォントに無い字が、画面の「フォントに無い文字」に並ぶこと
                want = [c for c in dict.fromkeys(plain) if c not in chars and c not in "　\n"]
                if not want:
                    errors.append(f"id {msg_id}: font の行なのに、無い字が 1 つも無い")
                for c in want:
                    if c not in miss:
                        errors.append(f"id {msg_id}: 「{c}」が画面の無い字の一覧に出ていない: {miss!r}")
                # docs が □ 入りの全文を書いている行は、その通りに崩れること
                quoted = re.search(r"「([^」]*□[^」]*)」", phenomenon)
                if quoted:
                    boxed = "".join("□" if ch not in chars else ch for ch in plain)
                    if boxed != quoted.group(1):
                        errors.append(f"id {msg_id}: docs/06 は「{quoted.group(1)}」"
                                      f"と書いているが、実際は「{boxed}」")
            if "line_count" in check:
                if "行は枠の下に出ています" not in note:
                    errors.append(f"id {msg_id}: 行数の超過が画面に出ていない: {note!r}")
                if not over:
                    errors.append(f"id {msg_id}: 超過の印が付いた行が無い ({meter})")
            if "line_width" in check:
                if "枠の右端を越えた" not in note:
                    errors.append(f"id {msg_id}: 行幅の超過が画面に出ていない: {note!r}")
                if not over:
                    errors.append(f"id {msg_id}: 超過の印が付いた行が無い ({meter})")
            if "placeholder" in check:
                # 差し込みが無いので、名前の欄を変えても画面は動かない
                shown = await page.evaluate("() => st.lines.map(l => l.map(c => c.ch).join(''))")
                if not any("あなた" in s for s in shown):
                    errors.append(f"id {msg_id}: 訳文に「あなた」が出ていない: {shown}")
                varn = await page.evaluate("() => st.lines.flat().filter(c => c.isVar).length")
                if varn:
                    errors.append(f"id {msg_id}: 差し込みが消えている訳文なのに "
                                  f"{varn} 文字が差し込み扱い")
            print(f"  id {msg_id} ({check}): 無い字={miss[:40]!r} 行={meter} 超過={over}")

        # docs/06「変数の長さを試す」: 6 文字の名前で 20 / 18 になり、超過すること
        await show(page, "2", "original")
        await page.fill("#pname", "ながいなまえ")
        await page.wait_for_timeout(200)
        first = await page.eval_on_selector("#meter .val", "e => e.textContent")
        over1 = await page.eval_on_selector_all("#meter .val.over", "els => els.length")
        print(f"  変数の長さ: 1 行目 {first} / 超過 {over1} 行")
        with open(DOC, encoding="utf-8") as fh:
            doc = fh.read()
        m = re.search(r"タグの状態では (\d+) 文字ですが、(\d+) 文字の名前が入ると (\d+) 文字になり", doc)
        if not m:
            errors.append("docs/06 の「変数の長さを試す」の書き方が読み取れない")
        else:
            _base, namelen, total = (int(x) for x in m.groups())
            if len("ながいなまえ") != namelen:
                errors.append(f"docs/06 は {namelen} 文字の名前と書いている (試したのは 6 文字)")
            if first.split("/")[0].strip() != str(total):
                errors.append(f"docs/06 は {total} 文字になると書いているが、画面は {first}")
            if not over1:
                errors.append("上限を越えているのに超過の印が付いていない")

        await b.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    sys.exit(0 if not errors else 1)


asyncio.run(main())
