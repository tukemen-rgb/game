"""**画面の要約も、両側にある判定を全部呼ぶか**を機械で数える (#237).

#236 で一括処理の側 (`boku2.py check`) に同じ見張りを付けた —— 対になっている
判定を機械で拾い、練習データと `--break` の全部を通して、**実際に呼ばれたもの**を
数える。落ちたら「通る材料を足す」か「通らない理由を書く」しかない形にした。

画面の側は、そのとき**間接的にしか押さえていなかった** (`broken` の全行突き合わせ、
`save` / `fontpage` / `unseen`)。#234・#235 で 2 度踏んだ穴 —— 判定の関数は両側で
1 字まで揃えているのに、**呼ばれるかどうか**は誰も見ていない —— は、片側だけ
数えても半分しか塞がらない。ここで残り半分を数える。

やり方: ヘッドレスの中で、`web/app.js` の関数を**包んでから**要約を作らせる。
古い形の JavaScript なので、頭に書いた `function 名前` はそのまま `window.名前` に
なり、中からの呼び出しも包んだほうを通る。

一覧は作らない。`tools/boku2.py` の `def 名前` と `web/app.js` の `function 名前` が
snake / camel で対応するものを機械で拾い、呼ばれなかったものだけを下に書く。
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch

SAMPLE = os.path.join(WORK, "BOKU2SAMPLE")
OUT = os.path.join(WORK, "REACH")

#: 画面の要約からは呼ばれないと分かっているもの と、**その理由**。
#: 理由の書けるものだけをここに置く (一括処理側の NOT_FROM_CHECK と同じ決まり)。
#:
#: いまは **空**。19 件すべてが要約までの道で呼ばれる。一括処理側で逃がして
#: いる `block_stats` も、画面では**ファイルの性質を一覧に出す**ので通る ——
#: 同じ関数でも、両側で通り方が違うという記録でもある (#237)
NOT_FROM_REPORT: dict = {}


def paired_names() -> list:
    """両側にある関数の名前 (画面側の呼び方) を機械で拾う."""
    with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
        py = set(re.findall(r"^def ([a-z_][a-z0-9_]*)\(", fh.read(), re.M))
    with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
        js = set(re.findall(r"^function ([A-Za-z][A-Za-z0-9_]*)\(", fh.read(), re.M))
    out = set()
    for name in py:
        head, *rest = name.split("_")
        camel = head + "".join(w.capitalize() for w in rest)
        for cand in (camel, "boku" + camel[0].upper() + camel[1:]):
            if cand in js:
                out.add(cand)
    return sorted(out)


def build_samples() -> list:
    """練習データと、壊し方ごとの吸い出しを作る (`--break` の一覧を正にする)."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    try:
        import make_boku2_sample
    finally:
        sys.path.remove(os.path.join(REPO, "tools"))
    made = []
    for kind in [None] + sorted(make_boku2_sample.DAMAGE):
        folder = os.path.join(OUT, kind or "ok")
        make_boku2_sample.build_sample(folder)
        if kind:
            make_boku2_sample.damage(folder, kind)
        made.append((kind or "ok", folder))
    return made


WRAP = """(names) => {
  window.__seen = [];
  for (const n of names) {
    const f = window[n];
    if (typeof f !== "function") continue;
    window[n] = function (...a) {
      if (!window.__seen.includes(n)) window.__seen.push(n);
      return f.apply(this, a);
    };
  }
}"""


async def seen_for(folder: str, names: list, glyphs: str | None, errors: list) -> set:
    """その吸い出しで要約を作り、**呼ばれた関数**の名前を返す."""
    files = [os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG")]
    mapdir = os.path.join(folder, "MAP")
    if os.path.isdir(mapdir):
        files += [os.path.join(mapdir, n) for n in sorted(os.listdir(mapdir))]
    async with async_playwright() as p:
        browser = await launch(p)
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        await page.goto("file://" + REPO + "/web/index.html")
        # **読み込む前に包む。** 索引を読むときに呼ばれる分も数えたい
        await page.evaluate(WRAP, names)
        await page.set_input_files("#fileinput", files)
        await page.wait_for_selector("#shell:not([hidden])", timeout=30000)
        if glyphs is not None:
            await page.click('[data-tab="format"]')
            await page.fill("#msgglyphs", glyphs)
        await page.click('[data-tab="index"]')
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        idx = [o for o in opts if o.endswith("BOKU2.IDX")]
        img = [o for o in opts if o.endswith("BOKU2.IMG")]
        if idx and img:
            await page.select_option("#idxsrc", idx[0])
            await page.select_option("#idxdata", img[0])
        await page.click("#idxrun")
        try:
            await page.wait_for_selector("#idxpreview button.btn.primary", timeout=8000)
            await page.click("#idxpreview button.btn.primary")
            await page.wait_for_timeout(600)
        except Exception:
            pass                      # 索引が読めない壊し方 (idx) ではここまで来ない
        await page.click("#idxreport")
        await page.wait_for_timeout(2500)
        got = set(await page.evaluate("window.__seen"))
        await browser.close()
    return got


async def main():
    errors = []
    names = paired_names()
    # **0 件で緑にしない。** 拾い方が壊れたら「全部通っている」に化ける
    if len(names) < 15:
        print(f"両側にある関数を {len(names)} 件しか拾えない (拾い方が壊れた): {names}")
        print("RESULT NG")
        return 1
    print(f"  両側にある判定: {len(names)} 件")

    with open(os.path.join(SAMPLE, "font.txt"), encoding="utf-8") as fh:
        rows = [ln for ln in fh.read().replace("\r", "").split("\n") if ln]
    broken_table = list(rows)
    broken_table[1] = broken_table[1][:-1]        # 2 行目だけ 1 字少ない
    broken_table[-1] = broken_table[-1] + "??"    # ANSI で潰れた跡

    seen: set = set()
    for kind, folder in build_samples():
        # 文字表は無事な吸い出しのときだけ貼る (壊した文字表の枝もここで通る)
        table = "\n".join(broken_table) if kind == "ok" else None
        got = await seen_for(folder, names, table, errors)
        print(f"  {kind:9} 呼ばれた {len(got)} 件")
        seen |= got

    missed = sorted(set(names) - seen - set(NOT_FROM_REPORT))
    if missed:
        errors.append("画面の要約で一度も呼ばれない両側の判定があります。"
                      "通る材料を足すか、NOT_FROM_REPORT に理由を書いてください: "
                      + ", ".join(missed))
    for name, why in NOT_FROM_REPORT.items():
        if name not in names:
            errors.append(f"NOT_FROM_REPORT に両側の関数でない名前がある: {name}")
        elif name in seen:
            errors.append(f"{name} は要約から呼ばれるようになりました。"
                          f"NOT_FROM_REPORT から外してください (理由: {why[:40]}…)")

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
