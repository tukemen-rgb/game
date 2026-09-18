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
import subprocess
import sys
import tempfile

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


def doc_commands(steps: str) -> list:
    """「次の一手」に書いてある `python3 tools/...` を、書いてある順に返す."""
    return re.findall(r"`(python3 tools/[^`]+)`", steps)


#: 画面と CLI で**書き方が違ってよい**行。ここに挙げたものだけが例外で、
#: それ以外は 1 字も違ってはいけない。「差は無視」ではなく「この差だけ許す」
KNOWN_DIFFS = [
    # 見出しは入口の名前が入る (CLI はフォルダ、画面は 2 つのファイル)
    ("== 診断", "== 診断"),
    # 文字表の出どころだけが違う。数字は同じでなければいけない
    ("[文字表] font.txt: ", "[文字表] 貼ってある文字表: "),
]


def compare_reports(cli: str, screen: str) -> list:
    """報告用の要約を、画面と CLI で 1 行ずつ突き合わせる.

    docs/07 の「どちらで作っても報告に使える」を支える検査。
    許すのは `KNOWN_DIFFS` に書いた**出どころの言い換えだけ**で、
    数字や判定が 1 つでも違えば落とす。
    """
    def norm(text: str) -> list:
        return [ln.rstrip() for ln in text.strip().split("\n") if ln.strip()]

    a, b = norm(cli), norm(screen)
    # CLI にだけある「BOKU2.IDX: あり / …」は、フォルダを渡したときの確認行
    a = [ln for ln in a if not ln.startswith("BOKU2.IDX: あり")]
    out = []
    # **何行突き合わせたか**を出す。0 行でも「差が無い」で緑になるので (#134 と同じ形)
    print(f"  手順 0: 報告を {len(a)} 行 (CLI) 対 {len(b)} 行 (画面) で突き合わせる")
    if len(a) < 8:
        return [f"CLI の報告が {len(a)} 行しかない (突き合わせになっていない)"]
    if len(a) != len(b):
        return [f"報告の行数が違う: CLI {len(a)} 行 / 画面 {len(b)} 行\n"
                f"    CLI にだけ: {[x for x in a if x not in b][:3]}\n"
                f"    画面にだけ: {[x for x in b if x not in a][:3]}"]
    for i, (x, y) in enumerate(zip(a, b)):
        if x == y:
            continue
        ok = False
        for pa, pb in KNOWN_DIFFS:
            if x.startswith(pa) and y.startswith(pb):
                # 見出しは前置きだけ、それ以外は前置きを外した残りが一致すること
                ok = (pa == "== 診断") or (x[len(pa):] == y[len(pb):])
                break
        if not ok:
            out.append(f"報告の {i + 1} 行目が画面と CLI で違う\n"
                       f"    CLI : {x!r}\n    画面: {y!r}")
    return out


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
    # **正解が空でも「0 件中 0 件」で緑になる。** 突き合わせる相手の数を先に確かめる
    # (#159 で、答えを空にする壊し方をしたら手順 3 と 6 が素通りした)
    if len(want) < 20:
        print(f"work/BOKU2SAMPLE/answer.tsv の正解が {len(want)} 件しかない "
              f"(突き合わせにならない。make_boku2_sample.py を作り直す)")
        print("RESULT NG")
        return 1

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
        # BOKU2.CRC も渡す (#239)。CLI はフォルダを読むので必ず見つけるため、
        # 渡さないと片側だけ「検査値との突き合わせ」をして行数が合わなくなる
        crc = os.path.join(SAMPLE, "BOKU2.CRC")
        await page.set_input_files("#fileinput", [os.path.join(SAMPLE, "BOKU2.IDX"),
                                                  os.path.join(SAMPLE, "BOKU2.IMG")]
                                   + ([crc] if os.path.exists(crc) else [])
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
        if not shown:
            errors.append("手順 3: 突き合わせる正解が 0 件 (検査になっていない)")
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

        # --- 手順 0: 報告用の要約。画面と CLI が同じ行を出すこと ---
        # docs/07 が「`tools/boku2.py check` と同じ行が出るので、どちらで作っても
        # 報告に使える」と書いている。社長がいちばん最初に打つものなのに、
        # **2 つの出力を突き合わせたことが一度も無かった** (#157)。
        # 文字表は手順 3 で貼ってあるので、CLI に font.txt を渡すのと同じ条件
        await page.click('[data-tab="index"]')
        await page.click("#idxreport")
        await page.wait_for_timeout(3000)
        screen = await page.eval_on_selector("#idxreporttext", "el => el.value") or ""
        cli = subprocess.run([sys.executable, "tools/boku2.py", "check", SAMPLE],
                             capture_output=True, text=True, cwd=REPO)
        if cli.returncode != 0:
            errors.append(f"手順 0: boku2.py check が落ちた ({cli.returncode}) "
                          f"{(cli.stdout + cli.stderr)[-200:]!r}")
        else:
            errors += compare_reports(cli.stdout, screen)

        # --- 手順 5: 校正用の TSV をコピーして proofread.py にかける ---
        # 画面が組み立てた TSV を、そのまま CLI が読めること。
        # **画面と CLI の継ぎ目**はここだけで、切れていても他の検査は落ちない
        await select_file(page, "msg", leaf)
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(800)
        tsv = await page.eval_on_selector("#msgtsvtext", "el => el.value") or ""
        browser_rows = [ln for ln in tsv.strip("\n").split("\n") if ln][1:]
        print(f"  手順 5: 画面の TSV は {len(browser_rows)} 行")
        if len(browser_rows) < 2:
            errors.append(f"手順 5: 画面の TSV が {len(browser_rows)} 行しかない")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "from_browser.tsv")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(tsv)
                # **手順 5 の中だけを読み、書いてある順に全部走らせる** (#186)。
                # 手順 5 は文字表を作る `fontlist` と `proofread` の 2 本立てになった。
                # proofread だけ拾うと、**文字表を作らずに渡す**形になり、
                # #186 で足した「文字表がありません」で落ちる (それが正しい断り方)
                step5 = steps.split("\n5. ", 1)[-1].split("\n6. ", 1)[0]
                cmds = doc_commands(step5)
                fontsrc = os.path.join(tmp, "font.txt")
                with open(fontsrc, "w", encoding="utf-8") as fh:
                    fh.write(font)
                subs = {"そのファイル": path, "font.txt": fontsrc,
                        "font_chars.txt": os.path.join(tmp, "font_chars.txt")}
                if not any("proofread.py" in c for c in cmds):
                    errors.append("docs/09 の手順 5 から proofread の呼び方を読めない")
                for cmd in cmds:
                    argv = [sys.executable if a == "python3" else subs.get(a, a)
                            for a in cmd.split()]
                    res = subprocess.run(argv, capture_output=True, text=True, cwd=REPO)
                    print(f"  手順 5: {' '.join(cmd.split()[1:])} → 終了コード "
                          f"{res.returncode}")
                    if res.returncode != 0:
                        errors.append(f"手順 5: 書いてある通りに打つと落ちる "
                                      f"({cmd}) 終了コード {res.returncode} "
                                      f"{(res.stdout + res.stderr)[-200:]!r}")
                    elif "proofread.py" in cmd:
                        if "行をチェック" not in res.stdout:
                            errors.append(f"手順 5: 校正の結果が出ない {res.stdout[-200:]!r}")
                        # **文字表を渡した以上、フォント検査が動いていること** (#186)
                        elif "文字表なし" in res.stdout:
                            errors.append("手順 5: 文字表を作って渡したのに"
                                          f"フォント検査が動いていない {res.stdout[-300:]!r}")

        # --- 手順 6: 同じ読み方を CLI で一括にかけ、画面と一致すること ---
        # 手順 6 は「画面で確かめた読み方が合っていたら、全部を一括で」と書いてある。
        # **画面と CLI が食い違えば、画面で確かめた意味が無くなる**ので、突き合わせる
        # **手順 6 の中だけを読む** (#186)。節ぜんぶから拾っていたので、手順 5 に
        # `boku2.py fontlist` が 1 本増えた途端に「3 本読めない (4)」で落ちた。
        # 探す範囲を狭めるのは #185 と同じ話
        step6 = steps.split("\n6. ", 1)[-1].split("\n7. ", 1)[0]
        cmds = [c for c in doc_commands(step6) if "boku2.py" in c and "check" not in c]
        if len(cmds) != 3:
            errors.append(f"docs/09 の手順 6 から一括のコマンドを 3 本読めない ({len(cmds)})")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "OUT")
                subs = {
                    "BOKU2.IDX": os.path.join(SAMPLE, "BOKU2.IDX"),
                    "BOKU2.IMG": os.path.join(SAMPLE, "BOKU2.IMG"),
                    "OUT/": out, "OUT": out,
                    "MAP/*.*": os.path.join(SAMPLE, "MAP", "*.*"),
                    "OUT/maps": os.path.join(out, "maps"),
                    # 昔の書き方 (ファイルを列挙する形) も**走れる**ようにしておく。
                    # 走れないと「取りこぼし」ではなく「コマンドが落ちた」になり、
                    # 拾い漏れを見張る検査が働いたのか分からない (#159)
                    "OUT/system/*.msg": os.path.join(out, "system", "*.msg"),
                    "OUT/maps/*/1.bin": os.path.join(out, "maps", "*", "1.bin"),
                    "font.txt": os.path.join(SAMPLE, "font.txt"),
                    "all.tsv": os.path.join(tmp, "all.tsv"),
                }
                bad = False
                for cmd in cmds:
                    argv = [sys.executable if a == "python3" else subs.get(a, a)
                            for a in cmd.split()]
                    # * を含む語は、道具側が展開する約束 (docs/10)。そのまま渡す
                    res = subprocess.run(argv, capture_output=True, text=True, cwd=REPO)
                    if res.returncode != 0:
                        errors.append(f"手順 6: {cmd} が落ちた (終了コード "
                                      f"{res.returncode}) {(res.stdout+res.stderr)[-200:]!r}")
                        bad = True
                        break
                if not bad:
                    with open(subs["all.tsv"], encoding="utf-8") as fh:
                        cli = fh.read().lstrip("\ufeff")
                    cli_rows = {ln.split("\t")[0]: ln for ln in cli.split("\n") if ln}
                    # **正解を全部拾えていること。** 書き方によっては黙って
                    # 取りこぼす (#159: ファイルを列挙すると 31 行中 12 行だけ)。
                    # しかも「文字表で全部読めました」と出るので気づけない
                    missing = [k for k in want if k not in cli_rows]
                    print(f"  手順 6: 正解 {len(want)} 件のうち "
                          f"{len(want) - len(missing)} 件を一括で拾った")
                    if missing:
                        errors.append(f"手順 6: 一括処理が {len(missing)} 件取りこぼした "
                                      f"{missing[:5]}。`text` にはフォルダを渡すこと")
                    stem2 = leaf.rsplit(".", 1)[0]
                    mine = [ln for ln in browser_rows if ln.split("\t")[0].startswith(stem2)]
                    print(f"  手順 6: CLI は {len(cli_rows) - 1} 行。"
                          f"画面の {len(mine)} 行と突き合わせる")
                    for line in mine:
                        rid = line.split("\t")[0]
                        if rid not in cli_rows:
                            errors.append(f"手順 6: 画面にある {rid} が CLI の出力に無い")
                        elif cli_rows[rid] != line:
                            errors.append(f"手順 6: {rid} が画面と CLI で違う\n"
                                          f"    画面: {line!r}\n    CLI : {cli_rows[rid]!r}")

        await browser.close()

    print("errors:", errors)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
