"""課題 9 の主張「壊れたデータで、ブラウザの要約も CLI の check と同じ → の行を出す」を確かめる.

make_boku2_sample.damage() で 5 通りに壊した練習データを読ませ、索引タブの
「報告用の要約」に、tests/run_tests.py の TestDamageDrill と同じ行が出ること。
`idx` (索引の先頭を壊す) だけは索引として読めないので、「DFI: 期待どおり」が出ないことを見る。
"""
import asyncio, os, sys
from playwright.async_api import async_playwright

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools"))
import make_boku2_sample  # noqa: E402
from common import REPO, WORK, launch  # noqa: E402

EXPECT = {
    "name": "名前が付かないファイルが多い",
    "msg": "読めない .msg の例: system/system.msg",
    "font": "TIM2 として読めません",
    "map": "入れ物として読めないファイルの例",
}

async def run_kind(b, kind, errors):
    folder = os.path.join(WORK, f"BROKEN_{kind}")
    make_boku2_sample.build_sample(folder)
    note = make_boku2_sample.damage(folder, kind)
    page = await b.new_page()
    page.on("pageerror", lambda e: errors.append(f"{kind}: {e}"))
    await page.goto("file://" + REPO + "/web/index.html")
    await page.set_input_files("#fileinput", [os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG"),
                                              os.path.join(folder, "MAP", "M_A01000.BIN")])
    await page.wait_for_selector("#shell:not([hidden])")
    await page.click('[data-tab="index"]')
    opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
    await page.select_option("#idxsrc", [o for o in opts if o.endswith("BOKU2.IDX")][0])
    await page.select_option("#idxdata", [o for o in opts if o.endswith("BOKU2.IMG")][0])
    await page.click("#idxrun")
    await page.wait_for_timeout(600)
    idxnote = await page.text_content("#idxnote")
    report = ""
    if await page.is_visible("#idxreport"):
        await page.click("#idxreport")
        try:
            await page.wait_for_function("document.querySelector('#idxreporttext').value.includes('== 結果')", timeout=20000)
        except Exception:
            pass
        report = await page.input_value("#idxreporttext")
    await page.close()
    print(f"[{kind}] {note}")
    print(f"  idxnote: {idxnote}")
    print("  report:", report.replace("\n", " | ")[:300])
    if kind == "idx":
        if "DFI: 期待どおり" in report:
            errors.append("idx: 壊した索引を DFI と読んだ")
        # 索引が読めないときこそ報告する材料が要る。要約は作れて、
        # この先を診ていないことまで書いてあること (#73、CLI の check と同じ)
        if not report:
            errors.append("idx: 索引が読めないと要約を作れない (報告する材料が無い)")
        elif ("索引が読めないのでここで止めました" not in report
              or "この先 (本体・.msg・フォント・MAP) は診ていません" not in report):
            errors.append(f"idx: 止めた理由と診ていない範囲が書かれていない: {report[:200]!r}")
        elif "[フォント]" in report or "[MAP]" in report:
            errors.append("idx: 診ていない段の行を出している")
        return
    want = EXPECT[kind]
    if want not in report or "問題なし" in report or "確認事項" not in report:
        errors.append(f"{kind}: 要約に「{want}」と「確認事項 N 件」が無い (または 問題なし になっている)")

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        errors = []
        for kind in ["idx", "name", "msg", "font", "map"]:
            await run_kind(b, kind, errors)
        await b.close()
        print("errors:", errors)
        print("RESULT", "OK" if not errors else "NG")
        sys.exit(0 if not errors else 1)

asyncio.run(main())
