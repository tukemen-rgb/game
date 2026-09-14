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


async def check_pointer_claim(page) -> list:
    """docs/07 の「確度『高』はちょうど 2 件」を、ポインタ表タブで確かめる (#145).

    この主張は**この道具でいちばん価値がある機能**の成績表なのに、
    タブを開く検査すら #144 まで無かった。数と、その 2 つが
    `SCRIPT.BIN` / `MSG_ENC.BIN` のものであることを見る。

    期待値は docs/07 から読み取る。数が変わったら「こう書き換える」と言う。
    """
    import os

    with open(DOC, encoding="utf-8") as fh:
        doc = fh.read()
    m = re.search(r"練習用イメージ \((\d+)KB\) を丸ごと読ませると、"
                  r"\*\*確度「高」はちょうど (\d+) 件\*\*", doc)
    if not m:
        return ["docs/07 のポインタ表の主張が読み取れない (書き方が変わった)"]
    said_kb, said_high = int(m.group(1)), int(m.group(2))

    iso = os.path.join(WORK, "RINFOLT.iso")
    real_kb = round(os.path.getsize(iso) / 1024)
    out = []
    if real_kb != said_kb:
        out.append(f"docs/07 の大きさが今と違う。{said_kb}KB → {real_kb}KB に書き換える")

    # 主張は**イメージ全体**を読ませたときの話。ファイルを 1 つ選んだままだと
    # その中だけを走査するので、木の先頭の「(イメージ全体)」に戻してから開く
    await select_file(page, "", "(イメージ全体)")
    await page.click('[data-tab="pointers"]')
    await page.wait_for_timeout(3000)
    rows = await page.eval_on_selector_all(
        "#tab-pointers table tr",
        "els => els.map(e => Array.from(e.cells || []).map(c => c.innerText.trim()))")
    rows = [r for r in rows if r]
    if len(rows) < 5:
        return out + [f"ポインタ表タブの行が {len(rows)} 本 (読み取り方が壊れた)"]
    high = [r for r in rows if r[-1] == "高"]
    if len(high) != said_high:
        out.append(f"確度「高」が {len(high)} 件 (docs/07 は {said_high} 件)。"
                   f"docs/07 をこう書き換える → 「確度「高」はちょうど {len(high)} 件」")

    # docs/07 は「SCRIPT.BIN と MSG_ENC.BIN のポインタ表だけ」と名指しする。
    # ISO のどこにその 2 つが入っているかを自分で探して、基準がそこを指すか見る
    with open(iso, "rb") as fh:
        data = fh.read()
    starts, at = [], 0
    while True:
        at = data.find(b"SCRP", at)
        if at < 0:
            break
        starts.append(at)
        at += 1
    if len(starts) != 2:
        return out + [f"ISO の中の SCRP が {len(starts)} 個 (題材が変わった)"]
    bases = {int(re.search(r"0x([0-9A-Fa-f]+)", r[3]).group(1), 16)
             for r in high if re.search(r"0x([0-9A-Fa-f]+)", r[3])}
    if bases != set(starts):
        out.append(f"確度「高」の基準が {sorted(hex(b) for b in bases)} で、"
                   f"SCRP の位置 {sorted(hex(s) for s in starts)} と違う")
    print(f"  ポインタ表: 行 {len(rows)} / 確度「高」 {len(high)} 件 "
          f"{sorted(hex(b) for b in bases)} / ISO {real_kb}KB")
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
            # 判定を言い切っておいて「根拠 0 件」と続けないこと (#112)。
            # 内訳の証拠が「2 種類以上のとき」しか出ず、100% 1 種類のファイル
            # (BGM.ADP は波形 100%、PAD.DAT はゼロ埋め 100%) で、いちばん強い証拠が
            # 消えていた。理由を述べた直後に「決め手が無い」と出るのは矛盾に読める。
            m = re.search(r"そう判断した根拠 \((\d+)\)", box)
            if not m:
                errors.append(f"{name}: 根拠の見出しが出ていない")
            elif m.group(1) == "0":
                errors.append(f"{name}: 「{diagnosis}」と言い切ったのに根拠が 0 件")
            if "決め手になる手がかりが見つかりませんでした" in box:
                errors.append(f"{name}: 判定を出しているのに「決め手が無い」と言っている")
            print(f"  {name}: {diagnosis} (根拠 {m.group(1) if m else '?'} 件)")

        if checked < 7:
            errors.append(f"確かめた行が {checked} 行しかない (素通りの疑い)")
        errors += await check_pointer_claim(page)
        await b.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    sys.exit(0 if not errors else 1)


asyncio.run(main())
