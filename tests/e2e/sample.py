"""docs/10 の「画面で確かめる」を練習データで通す."""
import asyncio, os, sys
from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file
S = os.path.join(REPO, "work", "BOKU2SAMPLE")

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
        # 2.5 報告用の要約 (boku2.py check と同じ項目)
        await page.click("#idxreport")
        await page.wait_for_function("document.querySelector('#idxreporttext').value.includes('== 結果')", timeout=20000)
        report = await page.input_value("#idxreporttext")
        print("report:", report.replace("\n", " | ")[:400])
        if not ("DFI: 期待どおり" in report and "問題なし" in report and "TIM2 (位置 0x80)" in report
                and "[入れ物] 文言の入れ物: あり diary.bin, fish_on_mem.bin" in report
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
