"""課題 9 の主張「壊れたデータで、ブラウザの要約も CLI の check と同じ → の行を出す」を確かめる.

make_boku2_sample.damage() で壊した練習データを読ませ、索引タブの
「報告用の要約」に、tests/run_tests.py の TestDamageDrill と同じ行が出ること。
`idx` (索引の先頭を壊す) だけは索引として読めないので、「DFI: 期待どおり」が出ないことを見る。

**壊し方の数はここに書かない。** `make_boku2_sample.DAMAGE` から取る (#190)。
数を書くと、壊し方が増えたときに文書だけ古くなる (#185・#189 と同じ)。
"""
import asyncio, os, subprocess, sys
from playwright.async_api import async_playwright

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools"))
import make_boku2_sample  # noqa: E402
from common import REPO, WORK, launch  # noqa: E402

EXPECT = {
    "name": "名前が付かないファイルが多い",
    # 名前が 1 つも付かない吸い出し。**形で探す道** (#172・#173) が通ること。
    # ここが黙って飛ぶと、本文もフォントも診断されないまま「問題なし」になる
    "allnames": "中身の形",
    "msg": "読めない .msg の例: system/system.msg",
    "font": "TIM2 として読めません",
    "map": "入れ物として読めないファイルの例",
    # 索引だけ正しくて中身が空の吸い出し (#218)。画面でも同じ行が出ること
    "empty": "本体の中身がほとんど空です",
    # 文字番号が文字表 1656 字に収まらない形 (#230)。読み方ごと違う合図なので、
    # 画面の要約でも同じ言葉で出ること
    "bignum": "この作品の文字表 1656 字",
    # 切り分けとゲーム自身の検査値が食い違う形 (#239)
    "crc": "検査値が 1 件合いません",
    # 2 か所に書いてある名前が食い違う形 (#241)
    "crcname": "名前が 1 件食い違います",
}

#: 画面と CLI で違って当たり前の行。**理由の付いたものだけ**を並べる。
#:
#: ここに足すのは「入力そのものが違うから違う」ものに限る。判定や言い回しの
#: 違いをここへ逃がしたら、この検査は意味を失う。
BY_DESIGN = (
    ("== 診断", "CLI はフォルダ名、画面は選んだ 2 ファイル名を書く"),
    ("BOKU2.IDX: あり", "CLI だけがフォルダの中身を確かめる (画面はファイルを選ばせる)"),
)

#: **文字表を貼っていないときだけ**、違って当たり前になる行 (#238)。
#: 同じ `font.txt` を貼れば中身は揃うので、貼った回では突き合わせる
BY_DESIGN_NO_TABLE = (
    ("[文字表]", "CLI はフォルダの font.txt を読む。画面は貼っていない"),
    # 上の「[文字表]」の非対称が、そのまま数に出る (#175)。中身が違うのではなく、
    # **見ているものが違うから件数が違う**。締めの行そのものは今までどおり突き合わせる
    ("診ていない段:", "「[文字表]」の非対称が、そのまま診ていない段の数に出る"),
)

#: 貼った回で、**言い方だけ**違う所。中身は同じなので揃えてから比べる (#238)
SAME_THING_SAID_DIFFERENTLY = (("[文字表] 貼ってある文字表:", "[文字表] font.txt:"),)


def report_parity(kind, report, errors, table=False, folder=None):
    """画面の要約と `boku2.py check` の出力を、**全部の行**で突き合わせる (#104).

    #100 では → の行だけを比べていた。だから → が 1 本も出ない健全なデータでは
    何も比べておらず、そこに 2 件残っていた: 「最初の名前」が CLI だけ
    フォルダ抜き (docs/10 が 20 分の所で見ろと言っている当のもの) で、
    「見つからない」の並び順も違っていた。

    #99・#100・#103 と 3 回続けて「片側にだけある」を 1 件ずつ見つけていたので、
    ここで一覧にする。違いは**全部**並べて出す。
    """
    if folder is None:
        folder = os.path.join(WORK, f"BROKEN_{kind}") if kind != "ok" else os.path.join(WORK, "BOKU2SAMPLE")
    res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), "check", folder],
                         capture_output=True, text=True, cwd=REPO)

    skip = BY_DESIGN if table else BY_DESIGN + BY_DESIGN_NO_TABLE

    def lines(text):
        out = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line or any(line.startswith(p) for p, _ in skip):
                continue
            for said, same in SAME_THING_SAID_DIFFERENTLY:
                if line.startswith(said):
                    line = same + line[len(said):]
            out.append(line)
        return out

    cli, ui = lines(res.stdout), lines(report)
    # 「一致した」が「1 行も比べていない」でないことを先に確かめる。出力が消えた
    # ときに緑になるのがいちばん危ない (#97)。索引が読めない idx の道でも 4 行は出る
    if len(cli) < 4 or len(ui) < 4:
        errors.append(f"{kind}: 比べる行が少なすぎる (CLI {len(cli)} 行 / 画面 {len(ui)} 行)")
        return
    what = "文字表を貼って" if table else ""
    for line in cli:
        if line not in ui:
            errors.append(f"{kind}: {what}CLI にしかない行: {line[:100]}")
    for line in ui:
        if line not in cli:
            errors.append(f"{kind}: {what}画面にしかない行: {line[:100]}")
    if table:
        # **貼った回でしか比べられない行**が、本当に比べられていること。
        # 揃え方を間違えて両側から落ちると、0 件どうしで緑になる
        if not any(ln.startswith("[文字表] font.txt:") for ln in cli):
            errors.append(f"{kind}: 貼った回なのに [文字表] の行を比べていない (CLI 側)")
        if not any(ln.startswith("[文字表] font.txt:") for ln in ui):
            errors.append(f"{kind}: 貼った回なのに [文字表] の行を比べていない (画面側)")


#: 文字表も貼って突き合わせる回 (#238)。全部の壊し方でやると倍の時間がかかるので、
#: **文字表の行が別の形になる組み合わせ**だけを選ぶ:
#:
#:   ("ok", "full")      文字表が足りている  → 「この範囲は全部読める」
#:   ("bignum", "full")  1 つだけ足りない    → 「文字表に無い 1 種 (例: …)」
#:   ("ok", "short")     たくさん足りない    → 例の並べ方と「…」の付け方まで比べる
#:
#: 3 つ目が無いと**材料が弱い**: 足りない字が 1 つでは、例を何件まで並べるか
#: (一括処理は 10 件) が両側で違っても差が出ない。実際、最初はそこを見落として
#: 壊しても素通りした
WITH_TABLE = (("ok", "full"), ("bignum", "full"), ("ok", "short"))

#: "short" のときに貼る文字表の字数。一括処理が並べる例の数 (10) より
#: **足りない字がずっと多くなる**ように短くする
SHORT_TABLE_GLYPHS = 3


async def run_kind(b, kind, errors, table=None, folder=None):
    # "ok" は壊していない練習データ。→ が 1 本も出ない道でも突き合わせる (#104)
    if folder is not None:
        note = "文字表を短くした回 (吸い出しはそのまま)"
    elif kind == "ok":
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
    # BOKU2.CRC も渡す (#239)。CLI はフォルダを読むので必ず見つける。渡さないと
    # 片側だけ「検査値との突き合わせ」をしていることになり、全行が揃わない
    crc = os.path.join(folder, "BOKU2.CRC")
    await page.set_input_files("#fileinput",
                               [os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG")]
                               + ([crc] if os.path.exists(crc) else [])
                               + [os.path.join(mapdir, f) for f in sorted(os.listdir(mapdir))])
    await page.wait_for_selector("#shell:not([hidden])")
    if table is not None:
        # **CLI と同じ文字表を貼る** (#238)。貼らないと [文字表] の行が比べられず、
        # そこに出る数 (使われている番号の内訳・足りない字) が両側で食い違っても
        # 誰も気づかない —— #234 で実際に食い違っていた所
        await page.click('[data-tab="format"]')
        await page.fill("#msgglyphs", table)
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
    report_parity(kind, report, errors, table=table is not None, folder=folder)

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        errors = []
        # **壊し方は道具の一覧から取る** (#190)。ここに名前を並べて持っていたので、
        # `--break` に増えても**この検査だけ増えないまま**だった (#189 と同じ型)。
        # `idx` は索引として読めない所で止まるので、待ち受ける行が別扱い
        kinds = sorted(make_boku2_sample.DAMAGE)
        missing = [k for k in kinds if k != "idx" and k not in EXPECT]
        if missing:
            print(f"EXPECT に待ち受ける行が無い壊し方: {missing}")
            print("RESULT NG")
            sys.exit(1)
        for kind in ["ok"] + kinds:
            await run_kind(b, kind, errors)
        # **文字表を貼った回**も突き合わせる (#238)。貼らない回では
        # [文字表] の行が両側から落ちていて、そこに出る数を比べていなかった
        for kind, how in WITH_TABLE:
            folder = os.path.join(WORK, "BOKU2SAMPLE" if kind == "ok" else f"BROKEN_{kind}")
            with open(os.path.join(folder, "font.txt"), encoding="utf-8") as fh:
                table = fh.read()
            if how == "short":
                # 先頭だけ残す (改行は番号に効かないので、そのまま切ってよい)。
                # **一括処理にも同じ短い表を読ませる** —— 別の吸い出しを作って
                # そこの font.txt を短くする (練習データそのものは触らない)
                table = "".join(ch for ch in table if ch not in "\r\n")[:SHORT_TABLE_GLYPHS]
                folder = os.path.join(WORK, "BROKEN_shorttable")
                make_boku2_sample.build_sample(folder)
                with open(os.path.join(folder, "font.txt"), "w", encoding="utf-8") as fh:
                    fh.write(table + "\n")
            else:
                folder = None
            await run_kind(b, kind, errors, table=table, folder=folder)
        await b.close()
        print("errors:", errors)
        print("RESULT", "OK" if not errors else "NG")
        sys.exit(0 if not errors else 1)

asyncio.run(main())
