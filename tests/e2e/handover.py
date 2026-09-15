"""docs/09 の「次の一手」を、書いてある順に画面で歩いてみる (#155).

社長が 9/19 に戻ってきて**最初にやること**が、docs/09 の「次の一手」の
1〜5 です。ところが e2e の検査はどれも**機能を 1 つずつ、専用の材料で**
見ていました (`split.py` は PACK.IDX、`msg.py` は自前の小さな `.msg`、
`map.py` は自前の MAP)。**手順を続けて歩いた検査が一つも無い。**

段と段の**受け渡し**で切れていても、どの検査も落ちません。しかもこの手順は
「名前が `system/system.msg` のように出るはず」のように**出る物を名指し**して
いるので、そのまま期待値にできます。

歩くのは練習用の `BOKU2SAMPLE` (組み立て方が分かっているので正解がある)。
最後まで行くと**日本語が出る**ところまで見ます —— そこがこの手順の目的地で、
途中の段がどれか欠けても、そこには着きません。

期待値は docs/09 と `work/BOKU2SAMPLE/answer.tsv` から取ります。
"""
import asyncio
import csv
import os
import re
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

DOC = os.path.join(REPO, "docs", "09-調査ログと引き継ぎ.md")
SAMPLE = os.path.join(WORK, "BOKU2SAMPLE")


def doc_steps() -> str:
    """docs/09 の「次の一手」の本文."""
    with open(DOC, encoding="utf-8") as fh:
        doc = fh.read()
    head = "## 次の一手 (優先順)"
    if head not in doc:
        return ""
    return doc.split(head, 1)[1].split("\n## ", 1)[0]


def plain(text: str) -> str:
    """制御の印を落として、見える日本語だけにする.

    答えの TSV は `<BR>` `<WAIT:0A>`、画面は改行と `{WAIT 10}` `{END}` と、
    **書き方が違うだけ**。そこで突き合わせると、道具が正しくても落ちる。
    比べたいのは本文なので、両方から印を落としてから比べる。
    """
    out = re.sub(r"<[^>]*>|\{[^}]*\}", "", text)
    return re.sub(r"\s+", "", out)


def answers() -> dict:
    """練習データの正解 (id → 本文)."""
    path = os.path.join(SAMPLE, "answer.tsv")
    with open(path, encoding="utf-8") as fh:
        return {row["id"]: row["original"] for row in csv.DictReader(fh, delimiter="\t")}


async def main() -> int:
    errors = []
    steps = doc_steps()
    if not steps:
        print("docs/09 の「次の一手」を読めない (書き方が変わった)")
        print("RESULT NG")
        return 1
    # 手順が名指ししている物を、文書から読み取る
    if "`msg` と入れる" not in steps:
        errors.append("docs/09 の手順 1 が「絞り込みに msg と入れる」でなくなった")
    want_name = re.search(r"名前が\s*\n?\s*`([^`]+\.msg)`", steps)
    if not want_name:
        errors.append("docs/09 の手順 1 から、出るはずの名前を読み取れない")
    if "「既知の形式」→「\\.msg として読む」" not in steps.replace("\\", "") \
            and ".msg として読む" not in steps:
        errors.append("docs/09 の手順 2 が「.msg として読む」でなくなった")
    if "マップの入れ物を切り分ける" not in steps:
        errors.append("docs/09 の手順 4 が「マップの入れ物を切り分ける」でなくなった")
    if errors:
        print("\n".join(errors))
        print("RESULT NG")
        return 1
    target = want_name.group(1)                     # 例: system/system.msg
    want = answers()

    with open(os.path.join(SAMPLE, "font.txt"), encoding="utf-8") as fh:
        font = fh.read()

    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        # 手順 4 の MAP は BOKU2.IMG の中ではなく**別のファイル**として置かれている
        # (実物もそう)。手順どおりに歩くなら、最初にまとめて読ませる
        maps_dir = os.path.join(SAMPLE, "MAP")
        map_files = [os.path.join(maps_dir, n) for n in sorted(os.listdir(maps_dir))]
        await page.set_input_files("#fileinput", [os.path.join(SAMPLE, "BOKU2.IDX"),
                                                  os.path.join(SAMPLE, "BOKU2.IMG")]
                                   + map_files)
        await page.wait_for_selector("#shell:not([hidden])")

        # --- 手順 1: 索引タブで切り分け、絞り込みに msg と入れる ---
        await page.click('[data-tab="index"]')
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary", timeout=30000)
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function(
            "document.querySelector('#capnote').textContent.includes('切り分けました')",
            timeout=30000)
        await page.fill("#treeq", "msg")
        await page.wait_for_timeout(400)
        names = await page.eval_on_selector_all(
            "#tree .filerow .nm", "e => e.map(x => x.textContent.trim())")
        print(f"  手順 1: 絞り込み msg → {len(names)} 件 {names[:6]}")
        leaf = target.rsplit("/", 1)[-1]
        if leaf not in names:
            errors.append(f"手順 1: docs/09 が名指しする {target} が一覧に出ない "
                          f"(出たのは {names[:8]})")
        if not names:
            errors.append("手順 1: 絞り込み msg で 1 件も出ない")

        # --- 手順 2: その .msg を選んで「.msg として読む」 ---
        await select_file(page, "msg", leaf)
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(800)
        box = await page.text_content("#msgbox") or ""
        note = await page.text_content("#msgnote") or ""
        print(f"  手順 2: {note.strip()[:70]}")
        if "件" not in note and "件" not in box:
            errors.append(f"手順 2: 件数が出ない (msgnote={note[:80]!r})")
        # 文字表を貼る前なので、まだ日本語では読めていないはず
        before = box

        # --- 手順 3: フォントから作った文字表を貼ると、日本語で読める ---
        await page.fill("#msgglyphs", font)
        await page.click("#msgparse")
        await page.wait_for_timeout(800)
        after = await page.text_content("#msgbox") or ""
        shown = [v for k, v in want.items() if k.startswith("system:")]
        hit = [t for t in shown if t in after]
        print(f"  手順 3: 文字表を貼ったあと、正解 {len(shown)} 件中 {len(hit)} 件が画面に出た")
        if len(hit) < len(shown):
            missing = [t for t in shown if t not in after][:3]
            errors.append(f"手順 3: 文字表を貼っても読めない本文がある {missing} "
                          f"(画面: {after[:120]!r})")
        if after == before:
            errors.append("手順 3: 文字表を貼っても画面が変わらない")

        # --- 手順 4: MAP の入れ物を切り分けて 1.bin を読む ---
        await page.fill("#treeq", "M_A")
        await page.wait_for_timeout(400)
        maps = await page.eval_on_selector_all(
            "#tree .filerow .nm", "e => e.map(x => x.textContent.trim())")
        if not maps:
            errors.append("手順 4: MAP のファイルが一覧に出ない")
        else:
            await select_file(page, "M_A", maps[0])
            await page.click('[data-tab="format"]')
            await page.click("#mapsplit")
            await page.wait_for_timeout(1200)
            await page.fill("#treeq", "1.bin")
            await page.wait_for_timeout(400)
            parts = await page.eval_on_selector_all(
                "#tree .filerow .nm", "e => e.map(x => x.textContent.trim())")
            print(f"  手順 4: {maps[0]} を切り分け → {parts[:4]}")
            if not any(n.endswith("1.bin") for n in parts):
                errors.append(f"手順 4: 切り分けても 1.bin が出ない (出たのは {parts[:6]})")
            else:
                await select_file(page, "1.bin", [n for n in parts
                                                  if n.endswith("1.bin")][0])
                await page.click('[data-tab="format"]')
                await page.click("#msgparse")
                await page.wait_for_timeout(800)
                talk = await page.text_content("#msgbox") or ""
                stem = maps[0].rsplit(".", 1)[0]
                mapwant = [v for k, v in want.items() if k.startswith(stem)]
                if not mapwant:
                    errors.append(f"手順 4: {stem} の正解が answer.tsv に無い")
                else:
                    got = [t for t in mapwant if plain(t) and plain(t) in plain(talk)]
                    print(f"  手順 4: 会話 {len(mapwant)} 件中 {len(got)} 件が出た")
                    miss = [t for t in mapwant if t not in got]
                    if miss:
                        errors.append(f"手順 4: 読めない会話がある {miss[:2]} "
                                      f"(画面: {talk[-160:]!r})")

        await browser.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
