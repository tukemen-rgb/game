"""課題 9 の主張「壊れたデータで、ブラウザの要約も CLI の check と同じ → の行を出す」を確かめる.

make_boku2_sample.damage() で 5 通りに壊した練習データを読ませ、索引タブの
「報告用の要約」に、tests/run_tests.py の TestDamageDrill と同じ行が出ること。
`idx` (索引の先頭を壊す) だけは索引として読めないので、「DFI: 期待どおり」が出ないことを見る。
"""
import asyncio, os, subprocess, sys
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

#: 画面と CLI で違って当たり前の行。**理由の付いたものだけ**を並べる。
#:
#: ここに足すのは「入力そのものが違うから違う」ものに限る。判定や言い回しの
#: 違いをここへ逃がしたら、この検査は意味を失う。
BY_DESIGN = (
    ("== 診断", "CLI はフォルダ名、画面は選んだ 2 ファイル名を書く"),
    ("BOKU2.IDX: あり", "CLI だけがフォルダの中身を確かめる (画面はファイルを選ばせる)"),
    ("[文字表]", "CLI はフォルダの font.txt を読む。画面は貼ってある文字表を見る"),
)


def report_parity(kind, report, errors):
    """画面の要約と `boku2.py check` の出力を、**全部の行**で突き合わせる (#104).

    #100 では → の行だけを比べていた。だから → が 1 本も出ない健全なデータでは
    何も比べておらず、そこに 2 件残っていた: 「最初の名前」が CLI だけ
    フォルダ抜き (docs/10 が 20 分の所で見ろと言っている当のもの) で、
    「見つからない」の並び順も違っていた。

    #99・#100・#103 と 3 回続けて「片側にだけある」を 1 件ずつ見つけていたので、
    ここで一覧にする。違いは**全部**並べて出す。
    """
    folder = os.path.join(WORK, f"BROKEN_{kind}") if kind != "ok" else os.path.join(WORK, "BOKU2SAMPLE")
    res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), "check", folder],
                         capture_output=True, text=True, cwd=REPO)

    def lines(text):
        out = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line or any(line.startswith(p) for p, _ in BY_DESIGN):
                continue
            out.append(line)
        return out

    cli, ui = lines(res.stdout), lines(report)
    # 「一致した」が「1 行も比べていない」でないことを先に確かめる。出力が消えた
    # ときに緑になるのがいちばん危ない (#97)。索引が読めない idx の道でも 4 行は出る
    if len(cli) < 4 or len(ui) < 4:
        errors.append(f"{kind}: 比べる行が少なすぎる (CLI {len(cli)} 行 / 画面 {len(ui)} 行)")
        return
    for line in cli:
        if line not in ui:
            errors.append(f"{kind}: CLI にしかない行: {line[:100]}")
    for line in ui:
        if line not in cli:
            errors.append(f"{kind}: 画面にしかない行: {line[:100]}")


async def run_kind(b, kind, errors):
    # "ok" は壊していない練習データ。→ が 1 本も出ない道でも突き合わせる (#104)
    if kind == "ok":
        folder = os.path.join(WORK, "BOKU2SAMPLE")
        make_boku2_sample.build_sample(folder)
        note = "壊していない練習データ"
    else:
        folder = os.path.join(WORK, f"BROKEN_{kind}")
        make_boku2_sample.build_sample(folder)
        note = make_boku2_sample.damage(folder, kind)
    page = await b.new_page()
    page.on("pageerror", lambda e: errors.append(f"{kind}: {e}"))
    await page.goto("file://" + REPO + "/web/index.html")
    # MAP は**全部**渡す。1 つだけ渡していたので、CLI (フォルダを読む) と
    # [MAP] の件数が食い違い、全部の行を突き合わせられなかった (#104)
    mapdir = os.path.join(folder, "MAP")
    await page.set_input_files("#fileinput",
                               [os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG")]
                               + [os.path.join(mapdir, f) for f in sorted(os.listdir(mapdir))])
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
    if kind == "ok":
        # 壊していないので、両方とも「問題なし」で終わること
        if "問題なし" not in report:
            errors.append(f"ok: 壊していないのに問題ありと言っている: {report[-200:]!r}")
    else:
        want = EXPECT[kind]
        if want not in report or "問題なし" in report or "確認事項" not in report:
            errors.append(f"{kind}: 要約に「{want}」と「確認事項 N 件」が無い (または 問題なし になっている)")
    report_parity(kind, report, errors)

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        errors = []
        for kind in ["ok", "idx", "name", "msg", "font", "map"]:
            await run_kind(b, kind, errors)
        await b.close()
        print("errors:", errors)
        print("RESULT", "OK" if not errors else "NG")
        sys.exit(0 if not errors else 1)

asyncio.run(main())
