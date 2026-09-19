"""docs/10 の「画面で確かめる」を練習データで通す."""
import asyncio, os, shutil, sys
from playwright.async_api import async_playwright

from common import REPO, WORK, doc_shape, launch, select_file
S = os.path.join(REPO, "work", "BOKU2SAMPLE")
HOWTO = os.path.join(REPO, "docs", "10-僕夏2の手順.md")


def screen_quotes(after: str) -> list:
    """docs/10 の「1. 画面で確かめる」から、**画面にこう出ると言っている**引用を拾う (#131).

    `tests/run_tests.py` の突き合わせは **ソースを検索する**ので、
    `候補 ${n} 件` + ` (${top.known} 形式として読みました)` のように
    組み立てて出す文字列は見つけられない。組み上がった形は画面でしか見られないので、
    そこだけこちらで持つ。期待値は文書から読み取るので、文書を直せば追いかける。

    かぎ括弧は押すボタンの名前にも使うので、**「…と出れば」「…と出ます」**が
    後ろに付いているものだけを「画面に出る文字」として採る。
    """
    import re

    with open(HOWTO, encoding="utf-8") as fh:
        body = fh.read().split("## 1. 画面で確かめる", 1)[1].split("\nここまでで", 1)[0]
    for step in re.split(r"\n(?=\d+\. )", body):
        if after in step:
            flat = " ".join(step.split())
            return re.findall(r"「([^」]+)」\s*と出(?:れば|ます|る)", flat)
    return []


def screen_quote_missing(shown: str, after: str, where: str) -> list:
    """引用が描かれた文字の中にあるか。無ければ何が出ていたかを添えて返す."""
    import re

    quotes = screen_quotes(after)
    if not quotes:
        return [f"docs/10 の「{after}」の手順から「…と出れば」の引用を拾えない (拾い方が壊れた)"]
    out = []
    for q in quotes:
        # 文書は数を N / K と書く。「候補 1 件」のように実数で書いてある所も数として当てる。
        # 置き換えは共通の doc_shape に任せる (1 文字で立っている N / M / K だけ。
        # ここで素朴に replace("N", …) していたのが #207 と同じ穴だった)
        rx = doc_shape(q).replace(r"1\ 件", r"\d[\d,]* 件")
        if not re.search(rx, shown or ""):
            out.append(f"{where} に docs/10 の「{q}」が出ていない (実際: {(shown or '')[:120]!r})")
    return out

async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")
        await page.set_input_files("#fileinput", [os.path.join(S, "BOKU2.IDX"), os.path.join(S, "BOKU2.IMG"),
                                                  os.path.join(S, "MAP", "M_A01000.BIN")])
        await page.wait_for_selector("#shell:not([hidden])")
        # 2. 索引
        await page.click('[data-tab="index"]')
        opts = await page.eval_on_selector_all("#idxsrc option", "els => els.map(e => e.value)")
        await page.select_option("#idxsrc", [o for o in opts if o.endswith("BOKU2.IDX")][0])
        await page.select_option("#idxdata", [o for o in opts if o.endswith("BOKU2.IMG")][0])
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary")
        note = await page.text_content("#idxnote")
        # docs/10 の手順 2 が「こう出れば正解」と書いている文字列を、**描かれた文字**で確かめる (#131)。
        # app.js の中では `候補 ${n} 件` と ` (${top.known} 形式として読みました)` に分かれていて、
        # ソースを検索しても出てこない。組み上がった形は画面でしか見られない
        errors += screen_quote_missing(note, "解析する", "#idxnote")
        # 2.5 報告用の要約 (boku2.py check と同じ項目)
        await page.click("#idxreport")
        await page.wait_for_function("document.querySelector('#idxreporttext').value.includes('== 結果')", timeout=20000)
        report = await page.input_value("#idxreporttext")
        print("report:", report.replace("\n", " | ")[:400])
        # 締めの言葉は #268 で 2 通りになった (診ていない段があれば「→ の行は
        # ありません。ただし…」)。**確認事項が出ないこと**で見る
        if not ("DFI: 期待どおり" in report and "確認事項" not in report
                and "TIM2 (位置 0x80)" in report
                and "[入れ物] 文言の入れ物: あり diary.bin, saveload.bin, on_mem_event.bin, fish_on_mem.bin" in report
                and "フォルダの規則: 2 通り (stack / flag) で一致" in report
                and "[MAP] 1 件 / 入れ物として読めた 1 件 / 1 番が会話だった 1 件" in report and "はじめから" not in report):
            errors.append("report failed")
        # 文字表をまだ貼っていないので、その旨が出る (boku2.py check の [文字表] と同じ項目)
        if "[文字表] 文字表はまだ貼っていない" not in report:
            errors.append("report should say the glyph table is not pasted yet")
        # 位置表 8 バイト刻みの後ろ 4 バイト (項目のバイト長) の突き合わせ結果 (#71)
        if "位置表の長さの欄: 合う 4 件 / 合わない 0 件" not in report:
            line = next((ln for ln in report.split("\n") if "長さの欄" in ln), "(行が無い)")
            errors.append(f"length-field line: {line!r}")
        # 3. 切り分け
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function("document.querySelector('#capnote').textContent.includes('切り分けました')", timeout=30000)
        names = await page.eval_on_selector_all("#tree .filerow .nm", "els => els.map(e => e.textContent)")
        dirs = await page.eval_on_selector_all("#tree .dir", "els => els.map(e => e.textContent)")
        # 4. system.msg を読む (文字表なし)
        await select_file(page, "system.msg", "system.msg")
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note_msg = await page.text_content("#msgnote")
        # 5. フォント画像
        await select_file(page, "font", "bk_font.tms")
        await page.click('[data-tab="format"]')
        await page.wait_for_selector("#formatbox canvas")
        fmt = await page.text_content("#formatbox")
        # 目盛りの既定値は実機の刻み 22 ドット (asm_notes.txt の *0x16)。23 だと 1 列ごとに 1 ドットずれる
        cw, ch = await page.input_value("#tim2cw"), await page.input_value("#tim2ch")
        if (cw, ch) != ("22", "22"):
            errors.append(f"grid default is {cw}x{ch}, want 22x22")
        # 6. 文字表を貼る → 日本語になる
        font = open(os.path.join(S, "font.txt"), encoding="utf-8").read()
        await select_file(page, "system.msg", "system.msg")
        await page.click('[data-tab="format"]')
        # 6.0 まず**足りない**文字表で貼る。docs/10 の手順 6 が「文字表に無い番号 K 種と出る」と
        #     書いている形を、描かれた文字で確かめる (#131)。足りている表では出ない枝
        await page.fill("#msgglyphs", font[:20])
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        short_note = await page.text_content("#msgnote")
        print("short note:", short_note)
        errors += screen_quote_missing(short_note, "番号 0 から順に", "#msgnote (足りない文字表)")
        await page.fill("#msgglyphs", font)
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        menu = await page.eval_on_selector_all("#msgbox tbody td:nth-child(4)", "els => els.map(e => e.textContent)")
        # 6.2 文字表を貼ったあとに要約を作り直すと、文字表の出来具合が出る (練習データは全部読める)
        await page.evaluate("document.getElementById('idxreport').click()")
        await page.wait_for_function("document.querySelector('#idxreporttext').value.includes('[文字表] 貼ってある文字表')", timeout=20000)
        report2 = await page.input_value("#idxreporttext")
        line = next((ln for ln in report2.split("\n") if ln.startswith("[文字表]")), "")
        print("glyph line:", line)
        if "文字表に無い 0 種。この範囲は全部読める" not in line:
            errors.append(f"report glyph line: {line!r}")
        # 6.3 課題 8 の最後の一手: 「校正用の TSV をコピー」→ 実際に proofread.py にかける。
        #     画面と一括処理の橋渡しで、ここが通らないと課題 8 は終われない (#89)
        await page.click("#msgtsv")
        tsv_text = await page.input_value("#msgtsvtext")
        tsv_path = os.path.join(WORK, "from_browser.tsv")
        with open(tsv_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(tsv_text if tsv_text.endswith("\n") else tsv_text + "\n")
        import subprocess
        proof = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv_path,
             "--font-chars", os.path.join(S, "font.txt")],
            capture_output=True, text=True, cwd=REPO)
        print("proofread rc:", proof.returncode)
        print("proofread out:", proof.stdout[-500:])
        if proof.returncode != 0:
            errors.append(f"copied TSV failed proofread: {proof.stdout[-300:]}{proof.stderr[-300:]}")
        # その作品の文字表を渡しているので、フォントの指摘は出ないはず。
        # 出るなら文字表の読み方が壊れている (#89: 1 行 23 文字の表を先頭 1 字しか読んでいなかった)
        if "フォントに無い文字" in proof.stdout:
            errors.append("font check fired even with the game's own glyph table")
        # 訳文の欄が原文のままなので、比べる検査が動いていないことを言うはず (#88)
        if "原文と見比べる検査は動いていません" not in proof.stdout:
            errors.append("proofread did not say which checks were inert")

        # 6.5 item_info.msg (0x8002 が引数の無いページ送りになるファイル): ファイル名で見分けて {BREAK} と読む
        await select_file(page, "item_info", "item_info.msg")
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note_item = await page.text_content("#msgnote")
        item = await page.eval_on_selector_all("#msgbox tbody td:nth-child(4)", "els => els.map(e => e.textContent)")
        print("item note:", note_item); print("item:", item)
        if "0x8002 はページ送り" not in note_item or item != ["あみ{BREAK}\nむしをつかまえる{END}", "つりざお{BREAK}\nさかなをつる{END}"]:
            errors.append("alt-break file failed")
        # 6.6 画面の TSV と `boku2.py text` の行が同じであること (#105).
        #
        #     #104 で要約の全行をそろえたので、同じやり方を .msg の本文に広げる。
        #     ここを比べていなかった間に 2 つずれていた: id が画面では行番号だけで
        #     (ファイルをまたぐと 0 どうしがぶつかる)、`<BREAK>` を校正側が
        #     知らずに 2 行を 1 行と数えていた。
        import subprocess
        out = os.path.join(WORK, "cli_unpack_105")
        shutil.rmtree(out, ignore_errors=True)
        maps_out = os.path.join(WORK, "cli_maps_106")
        shutil.rmtree(maps_out, ignore_errors=True)
        # 本体側 (unpack → text) と MAP 側 (maps → text) の両方。docs/10 の手順どおり
        for args in (["unpack", os.path.join(S, "BOKU2.IDX"), os.path.join(S, "BOKU2.IMG"), out],
                     ["text", out, "-f", os.path.join(S, "font.txt"),
                      "-o", os.path.join(WORK, "cli_105.tsv")],
                     ["maps", os.path.join(S, "MAP"), "-o", maps_out],
                     ["text", maps_out, "-f", os.path.join(S, "font.txt"),
                      "-o", os.path.join(WORK, "cli_maps_106.tsv")]):
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py")] + args,
                               capture_output=True, text=True, cwd=REPO)
            if r.returncode != 0:
                errors.append(f"boku2 {args[0]} failed: {r.stdout[-200:]}{r.stderr[-200:]}")
        cli_rows = {}
        for name in ("cli_105.tsv", "cli_maps_106.tsv"):
            with open(os.path.join(WORK, name), encoding="utf-8-sig") as fh:
                for line in fh.read().split("\n")[1:]:
                    if line.strip():
                        cli_rows[line.split("\t")[0]] = line
        if len(cli_rows) < 10:
            errors.append(f"CLI の TSV が {len(cli_rows)} 行しかない (比べる材料が無い)")
        compared = 0
        for target in ["system.msg", "item_info.msg", "namemsg.msg", "config.msg"]:
            await select_file(page, target, target)
            await page.click('[data-tab="format"]')
            await page.fill("#msgglyphs", font)
            await page.click("#msgparse")
            await page.wait_for_timeout(200)
            await page.click("#msgtsv")
            rows = [l for l in (await page.input_value("#msgtsvtext")).split("\n")[1:] if l.strip()]
            if not rows:
                errors.append(f"{target}: 画面の TSV が空 (比べていない)")
                continue
            for row in rows:
                key = row.split("\t")[0]
                if key not in cli_rows:
                    errors.append(f"{target}: CLI に無い id: {key}")
                elif cli_rows[key] != row:
                    errors.append(f"{target}: 行が違う\n    画面: {row}\n    CLI : {cli_rows[key]}")
                else:
                    compared += 1
        print(f"TSV 突き合わせ: {compared} 行が画面と CLI で一致")
        if compared < 12:
            errors.append(f"突き合わせた行が {compared} 行しかない (素通りの疑い)")
        async def compare_tsv(label, want_prefix):
            """いま選んでいる部品の TSV を CLI の同じ住所の行と突き合わせる (#106)."""
            await page.click("#msgtsv")
            got = [l for l in (await page.input_value("#msgtsvtext")).split("\n")[1:] if l.strip()]
            if not got:
                errors.append(f"{label}: 画面の TSV が空")
                return 0
            n = 0
            for row in got:
                key = row.split("\t")[0]
                if not key.startswith(want_prefix):
                    errors.append(f"{label}: 住所が {key} (期待は {want_prefix}… )")
                elif key not in cli_rows:
                    errors.append(f"{label}: CLI に無い id: {key}")
                elif cli_rows[key] != row:
                    errors.append(f"{label}: 行が違う\n    画面: {row}\n    CLI : {cli_rows[key]}")
                else:
                    n += 1
            return n

        # 6.7 入れ物の中の入れ物 (fish_on_mem.bin → 1.bin がまた入れ物 → その 2.bin が魚の説明)。
        #     画面では「切り分ける」を 2 回。CLI (#53) の再帰と同じ答えになること
        await select_file(page, "fish_on_mem", "fish_on_mem.bin")
        await page.click('[data-tab="format"]')
        await page.click("#mapsplit")
        await page.wait_for_function("document.querySelector('#capnote').textContent.includes('マップの入れ物として')", timeout=20000)
        await select_file(page, "1.bin", "1.bin")
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        note_inner = await page.text_content("#msgnote")
        await page.click("#mapsplit")
        await page.wait_for_function(
            "[...document.querySelectorAll('#tree .filerow .nm')].some(e => e.textContent === '2.bin')", timeout=20000)
        await select_file(page, "2.bin", "2.bin")
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        fish = await page.eval_on_selector_all("#msgbox tbody td:nth-child(4)", "els => els.map(e => e.textContent)")
        print("inner note:", note_inner); print("fish:", fish)
        if "入れ物です" not in note_inner or fish != ["フナ\nぬまにいる{END}", "コイ\nかわにいる{END}"]:
            errors.append("nested container failed")
        await page.fill("#msgglyphs", font)
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        compared += await compare_tsv("fish_on_mem の部品", "fish_on_mem#1#2:")
        # 7. マップの入れ物 → 1.bin → 会話
        await select_file(page, "M_A01000", "M_A01000.BIN")
        await page.click('[data-tab="format"]')
        await page.click("#mapsplit")
        await page.wait_for_function("document.querySelector('#capnote').textContent.includes('マップの入れ物として')", timeout=20000)
        await page.fill("#treeq", "1.bin")
        # 1.bin は fish_on_mem の部品にもあるので、いちばん後に増えた (マップの) 1.bin を選ぶ
        await page.eval_on_selector_all("#tree .filerow",
                                        "els => els.filter(e => e.querySelector('.nm').textContent === '1.bin').pop().click()")
        # 固定の待ち時間ではなく、選ばれたことを待つ (#73)
        await page.wait_for_function(
            "(() => { const r = document.querySelector('#tree .filerow[aria-current=\"true\"]');"
            " return r && r.querySelector('.nm').textContent === '1.bin'; })()", timeout=20000)
        await page.click('[data-tab="format"]')
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        talk = await page.eval_on_selector_all("#msgbox tbody td:nth-child(4)", "els => els.map(e => e.textContent)")
        # マップの会話も CLI (maps → text) と同じ住所・同じ本文であること (#106)
        await page.fill("#msgglyphs", font)
        await page.click("#msgparse")
        await page.wait_for_timeout(200)
        compared += await compare_tsv("マップの会話", "M_A01000:")
        print(f"TSV 突き合わせ (部品も含む): {compared} 行")
        if compared < 16:
            errors.append(f"突き合わせた行が {compared} 行しかない (素通りの疑い)")
        # MAP を渡さないときは、**「問題なし」で終わらせないこと** (#174)。
        # 物語の会話をまるごと診ていないのに「診た結果、大丈夫」と読める終わり方を
        # していた。CLI 側は tests/run_tests.py が見ているので、ここは画面の分
        p2 = await b.new_page()
        p2.on("pageerror", lambda e: errors.append(str(e)))
        await p2.goto("file://" + REPO + "/web/index.html")
        await p2.set_input_files("#fileinput", [os.path.join(S, "BOKU2.IDX"),
                                                os.path.join(S, "BOKU2.IMG")])
        await p2.wait_for_selector("#shell:not([hidden])")
        await p2.click('[data-tab="index"]')
        await p2.click("#idxrun")
        await p2.wait_for_selector("#idxpreview button.btn.primary")
        await p2.click("#idxreport")
        await p2.wait_for_function(
            "document.querySelector('#idxreporttext').value.includes('== 結果')", timeout=20000)
        nomap = await p2.input_value("#idxreporttext")
        await p2.close()
        print("MAP なしの結果:", nomap.strip().split("\n")[-1][:80])
        if "問題なし" in nomap:
            errors.append("MAP を渡していないのに「問題なし」と言っている")
        if "物語の会話は 1 つも診ていません" not in nomap:
            errors.append(f"何を診ていないのかを言っていない: {nomap[-200:]!r}")

        print("idxnote:", note); print("dirs:", dirs); print("names:", names[:12])
        print("msg note:", note_msg); print("font:", "TIM2" in fmt, "位置 0x80" in fmt)
        print("menu:", menu); print("talk:", talk); print("errors:", errors)
        await b.close()
        ok = ("DFI" in note and "/BOKU2.IMG/system/" in dirs and "system.msg" in names and "bk_font.tms" in names
              and "4 件" in note_msg and "位置 0x80" in fmt
              and menu == ["はじめから{END}", "つづきから{END}", "せってい{END}", "おわる{END}"]
              and talk[0] == "{VOICE 00010001}" and talk[1] == "きょうはうみにいくんだ。\nいっしょにいこうよ。{WAIT 10}{END}"
              and not errors)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)

asyncio.run(main())
