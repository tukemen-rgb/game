"""実物並みの索引 (1951 件) を読ませて、**画面が固まらない**ことを確かめる (#193).

ここまでヘッドレスで使ってきた練習データは 20 件・140 KB で、しかも中身が
「読める本文」として作ってある。実物の `BOKU2.IMG` は **1951 件**で、あいだに
詰め物 (同じバイトの並び) がいくらでも入る。その形を作って読ませたら、
**画面が固まって戻ってこなかった** —— `scanStrings` が開始位置を 1 つずつ
ずらして端まで数え直すので、読めるバイトが続く所で**長さの 2 乗**の手間になる。
実測 120 KB で 36 秒。実物は桁が 3 つ上なので、開いた瞬間に終わる。

#171 と同じ型の見落とし: **練習データが小さすぎて、実物の形を通っていなかった。**

ここで見るのは 2 つ:

* 実物並みの件数・形の索引を読んで、画面が**決めた秒数のうちに**使えるようになる
* 速くするために**本文を捨てていない** —— 同じ材料に本文を混ぜたら、ちゃんと出る
"""
import asyncio
import os
import struct
import sys
import tempfile
import time

from playwright.async_api import async_playwright

from common import REPO, launch

#: 実物の件数 (docs/09 の「1951 ファイルに切り分けられた」)
ENTRIES = 1951

#: ここまでに使えるようになっていること。実測は 0.1 秒なので、
#: 20 秒は「固まっていないか」だけを見る、うんと緩い線
BUDGET = 20.0

#: 本文として埋め込む Shift-JIS の台詞 (速くしたせいで落ちていないかを見る)
SAMPLE_TEXT = "ぼくのなつやすみ"


def build(folder: str, with_text: bool) -> None:
    """実物並みの件数の索引と本体を書く。中身は詰め物だらけにする."""
    data, recs = bytearray(), []
    body_text = SAMPLE_TEXT.encode("shift_jis")
    for i in range(ENTRIES):
        # 詰め物のような並び: 同じバイトが延々と続く区画。値は区画ごとに変える
        body = bytearray(bytes([(i * 7) & 0xFF]) * 2048)
        if with_text and i % 200 == 0:
            body[64:64 + len(body_text)] = body_text
        recs.append(len(data) // 2048)
        data += bytes(body)
    idx = bytearray(b"DFI\0" + struct.pack("<III", ENTRIES, 0, 0))
    for sector in recs:
        idx += struct.pack("<HHIII", 0, 0, 0, sector, 2048)
    for i in range(ENTRIES):
        idx += f"f{i:04d}.bin".encode() + b"\0"
    with open(os.path.join(folder, "BOKU2.IDX"), "wb") as fh:
        fh.write(bytes(idx))
    with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
        fh.write(bytes(data))


async def open_pair(b, folder: str, errors: list) -> tuple:
    """索引と本体を読ませて、画面が使えるようになるまでの秒数を返す."""
    page = await b.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    await page.goto("file://" + REPO + "/web/index.html")
    started = time.time()
    await page.set_input_files("#fileinput", [os.path.join(folder, "BOKU2.IDX"),
                                              os.path.join(folder, "BOKU2.IMG")])
    try:
        await page.wait_for_selector("#shell:not([hidden])", timeout=int(BUDGET * 1000))
    except Exception:
        errors.append(f"{BUDGET:.0f} 秒たっても画面が使えるようにならない "
                      f"(索引 {ENTRIES} 件)。scanStrings の数え直しを疑うこと")
        await page.close()
        return None, None
    return page, time.time() - started


async def main() -> int:
    errors = []
    async with async_playwright() as p:
        b = await launch(p)
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "plain")
            texty = os.path.join(tmp, "texty")
            os.makedirs(plain)
            os.makedirs(texty)
            build(plain, with_text=False)
            build(texty, with_text=True)
            size = os.path.getsize(os.path.join(plain, "BOKU2.IMG"))
            print(f"  索引 {ENTRIES} 件 / 本体 {size:,} バイト")

            page, took = await open_pair(b, plain, errors)
            if page is not None:
                print(f"  詰め物だけ: {took:.1f} 秒で使えるようになった")
                # **速いが何もしていない、を防ぐ。** 実際に索引として読ませる
                await page.click('[data-tab="index"]')
                await page.click("#idxrun")
                try:
                    await page.wait_for_selector("#idxpreview button.btn.primary",
                                                 timeout=int(BUDGET * 1000))
                except Exception:
                    errors.append(f"{BUDGET:.0f} 秒たっても索引の解析が終わらない")
                note = (await page.text_content("#idxnote") or "").strip()
                if "DFI" not in note:
                    errors.append(f"索引として読めていない: {note[:80]!r}")
                else:
                    print(f"  索引: {note[:70]}")
                await page.close()

            # **速くしたせいで本文を落としていないこと。** 同じ材料に台詞を混ぜる
            page, took = await open_pair(b, texty, errors)
            if page is not None:
                print(f"  本文入り: {took:.1f} 秒")
                # **詰め物にはさまれた本文を、速くしたあとも拾えること。**
                # 上限で切る以上、ここが落ちたら速さのために本文を捨てたことになる
                hit = await page.evaluate(
                    """() => {
                         const body = new Uint8Array(8192).fill(0x00);   /* 区画のあいだの詰め物 */
                         /* 「ふくのなつやすみ」を Shift-JIS で真ん中に置く */
                         const sjis = [0x82, 0xd4, 0x82, 0xad, 0x82, 0xcc,
                                       0x82, 0xc8, 0x82, 0xc2, 0x82, 0xe2,
                                       0x82, 0xb7, 0x82, 0xdd];
                         body.set(sjis, 4100);
                         const got = scanStrings(body, 4);
                         return got.filter((s) => s.text.includes("なつやすみ")).length;
                       }""")
                if not hit:
                    errors.append("詰め物にはさまれた本文を拾えなくなっている "
                                  "(上限で切ったせいで落としていないか)")
                await page.close()
        await b.close()

    for e in errors:
        print("  -", e)
    print("RESULT " + ("OK" if not errors else "NG"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
