"""「探さなかった」を画面が言うことを確かめる (#148).

構造探査台でいちばん強い誤検出よけは、docs/07 の言う **1 段目 — そもそも見ない**。
圧縮や乱数と分類された区画は、文字列の走査そのものをしない。**効きすぎるほど効く**
ので、丸ごと圧縮のファイルでは 1 件も出ない。

ところがその跡が画面のどこにも出ず、結果は「該当する文字列はありません。」の
1 行だけだった。**道具が壊れているのと見分けが付かない。** 素人が実データで最初に
出会う画面がこれで、しかも「文字列が無い」は事実として間違っている
(16 進タブで見れば読める並びはいくらでもある)。

読ませるのは練習用イメージの `MOVIE.PSS` (docs/07 の診断表で
「圧縮または暗号化されたデータ」)。比べるために `SYSTEM.CNF` (ASCII のテキスト)
でも見て、**普通のファイルでは余計なことを言わない**ことを確かめる。
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

DOC = os.path.join(REPO, "docs", "07-構造探査台.md")


def doc_says_it_is_shown() -> bool:
    """docs/07 が「飛ばしたことを画面に出す」と約束していること."""
    with open(DOC, encoding="utf-8") as fh:
        doc = fh.read()
    return "飛ばしたことは画面に出します" in doc


async def look(page, name: str) -> tuple:
    """そのファイルの文字列タブを開いて (注意書き, 表の中身) を返す."""
    await select_file(page, name, name)
    await page.click('[data-tab="strings"]')
    await page.wait_for_timeout(600)
    note = await page.eval_on_selector(
        "#strskip", "el => el.hidden ? '' : el.innerText.trim()")
    body = (await page.text_content("#strbody")) or ""
    return note, body.strip()


async def main() -> int:
    errors = []
    if not doc_says_it_is_shown():
        print("docs/07 が「飛ばしたことは画面に出します」と書いていない (約束が消えた)")
        print("RESULT NG")
        return 1

    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [os.path.join(WORK, "RINFOLT.iso")])
        await page.wait_for_selector("#shell:not([hidden])")

        # 1. 丸ごと圧縮のファイル: 何割を見ていないかを言うこと
        note, body = await look(page, "MOVIE.PSS")
        print(f"  MOVIE.PSS の注意書き: {note!r}")
        print(f"  MOVIE.PSS の表: {body[:80]!r}")
        if not note:
            errors.append("MOVIE.PSS で「探さなかった」の注意書きが出ない "
                          "(黙って 0 件は、壊れているのと見分けが付かない)")
        else:
            m = re.search(r"(\d+)% は", note)
            if not m:
                errors.append(f"注意書きに割合が入っていない: {note!r}")
            elif int(m.group(1)) < 50:
                errors.append(f"飛ばした割合が {m.group(1)}% (丸ごと圧縮のはずが低すぎる)")
            for word in ("文字列を探していません", "16 進"):
                if word not in note:
                    errors.append(f"注意書きに「{word}」が無い: {note!r}")
        # 0 件で終わるなら、その行も理由に触れること
        if "該当する文字列はありません" in body and "上の理由" not in body:
            errors.append(f"0 件の行が理由に触れていない: {body[:80]!r}")
        # 「100%」と言いながら表に並んでいたら嘘 (#148 で実際に出た。四捨五入のせい)
        if "100% は" in note and "該当する文字列はありません" not in body:
            errors.append(f"「100% 探していません」と言いながら表に並んでいる: {body[:80]!r}")

        # 2. 普通のテキスト: 余計な注意書きを出さないこと
        note2, body2 = await look(page, "SYSTEM.CNF")
        print(f"  SYSTEM.CNF の注意書き: {note2!r}")
        if note2:
            errors.append(f"ASCII のテキストで飛ばしたと言っている: {note2!r}")
        if "該当する文字列はありません" in body2:
            errors.append("SYSTEM.CNF から文字列が 1 件も出ていない (読み取り方が壊れた)")

        await browser.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
