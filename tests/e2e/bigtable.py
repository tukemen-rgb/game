"""位置表が**先頭 4KB に収まらない**入れ物を、黙って `bin` にしないこと (#282).

画面が中身を見るのは各ファイルの**先頭 4KB だけ**です。入れ物かどうかは
「件数 + 位置表」を読んで決めるので、刻み 8 なら枠 511 個で頭打ちになります。
それを超えたファイルは、前は黙って `null` を返して「種類が分からなかったもの」
(`bin`) に落ちていました。**「入れ物ではない」ではなく「見ていない」**なので、
要約でその件数を言います。

あわせて、同じ要約の締めくくりも見ます (#283)。そこには長らく
「圧縮らしい N 件 (packed) の中にテキストがある見込みです」と書いてありました。
**#4 で取り消した読み**です —— 公開ソース一式に伸張処理が無く、この作品の
本文は圧縮されていません。DFI (= この作品) と分かって切り分けたときは、
そう言わせないこと。

ここで見るのは 3 つ:

* 枠 600 個の入れ物を切り分けたとき、要約が「見ていません」と言うこと
* 枠 300 個 (4KB に収まる) のほうは今までどおり入れ物として読めること
  —— 「見ていません」と言うだけで何も読めなくなったのでは意味が無い
* `packed` と見たファイルがあっても、**この作品では**そこを探せと言わないこと
"""
import asyncio
import os
import struct
import sys
import tempfile

from playwright.async_api import async_playwright

from common import REPO, launch

#: 刻み 8 の位置表は (4096 - 4) / 8 = 511 個で先頭 4KB に収まらなくなる
FITS = 300
OVER = 600

#: 索引の候補として拾われる件数まで詰め物で足す
PADDING = 18


def container(count: int, stride: int = 8) -> bytes:
    """件数 + 位置表 + 部品、の入れ物 (16 バイト境界まで詰めてから部品)."""
    table_end = 4 + count * stride
    first = -(-table_end // 16) * 16
    b = bytearray(first + count * 16)
    struct.pack_into("<I", b, 0, count)
    off = first
    for i in range(count):
        struct.pack_into("<I", b, 4 + i * stride, off)
        struct.pack_into("<I", b, 8 + i * stride, 16)
        b[off:off + 16] = bytes([0x82, 0xA0] * 8)      # 日本語らしいバイト
        off += 16
    return bytes(b)


def packed_looking(size: int = 4096) -> bytes:
    """先頭 u32 が「伸張後の大きさ」に見えるファイル (`packed` と見当が付く)."""
    b = bytearray(size)
    struct.pack_into("<I", b, 0, size * 3)
    for i in range(4, size):
        b[i] = (i * 37 + 11) & 0xFF            # 入れ物にも文字にも見えない並び
    return bytes(b)


def build(folder: str) -> None:
    """索引と本体を書く。1 件目は 4KB に収まる入れ物、2 件目は収まらない入れ物.

    索引の形は練習データと同じものを使う (`make_boku2_sample.build_dfi`)。
    ここで見たいのは索引の読み方ではなく、**切り分けたあとの見当**なので。
    """
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import make_boku2_sample

    # **件数が少ないと索引の候補として拾われない** (総当たりの足切り)。
    # ここで見たいのは索引ではないので、詰め物のファイルで数を足しておく
    tree = [(True, 1, "/", None),
            (False, 1, "fits.bin", container(FITS)),
            (False, 1, "over.bin", container(OVER)),
            (False, 1, "packedish.bin", packed_looking())]
    tree += [(False, 0 if i == PADDING - 1 else 1, f"pad{i:02d}.bin",
              bytes([0x41 + i]) * 256) for i in range(PADDING)]
    idx, img, _want = make_boku2_sample.build_dfi(tree)
    with open(os.path.join(folder, "BOKU2.IDX"), "wb") as fh:
        fh.write(idx)
    with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
        fh.write(img)


async def main() -> int:
    errors = []
    async with async_playwright() as p:
        b = await launch(p)
        with tempfile.TemporaryDirectory() as tmp:
            build(tmp)
            page = await b.new_page()
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console",
                    lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
            await page.goto("file://" + REPO + "/web/index.html")
            await page.set_input_files("#fileinput", [os.path.join(tmp, "BOKU2.IDX"),
                                                      os.path.join(tmp, "BOKU2.IMG")])
            await page.wait_for_selector("#shell:not([hidden])")
            await page.click('[data-tab="index"]')
            opts = await page.eval_on_selector_all("#idxsrc option",
                                                   "els => els.map((e) => e.value)")
            await page.select_option("#idxsrc",
                                     [o for o in opts if o.endswith("BOKU2.IDX")][0])
            await page.select_option("#idxdata",
                                     [o for o in opts if o.endswith("BOKU2.IMG")][0])
            await page.click("#idxrun")
            print("  idxnote:", (await page.text_content("#idxnote") or "").strip()[:160])
            await page.wait_for_selector("#idxpreview button.btn.primary")
            await page.click("#idxpreview button.btn.primary")
            await page.wait_for_function(
                "document.querySelector('#capnote').textContent.includes('切り分けました')",
                timeout=30000)
            cap = await page.text_content("#capnote")
            print("  capnote:", (cap or "").strip()[:300])
            # 収まらなかった 1 件を「見ていない」と言うこと
            if "入れ物かどうかを**見ていません**" not in (cap or ""):
                errors.append("表が 4KB に収まらないファイルを黙って見捨てている")
            if "うち 1 件" not in (cap or ""):
                errors.append(f"見ていない件数が 1 件になっていない: {cap!r}")
            # **取り消した読みを出さないこと** (#283)。DFI = この作品
            if "(packed) の中にテキストがある見込みです" in (cap or ""):
                errors.append("#4 で取り消した「packed を探せ」を画面が言っている")
            if "圧縮されていません" not in (cap or ""):
                errors.append(f"この作品の本文が圧縮でないことを言っていない: {cap!r}")
            # **何も読めなくなっていないこと。** 収まるほうは入れ物として読める
            kinds = await page.eval_on_selector_all(
                "#tree .filerow .nm", "els => els.map((e) => e.textContent)")
            print("  names:", kinds)
            titles = await page.eval_on_selector_all(
                "#tree .filerow", "els => els.map((e) => e.title)")
            if not any("入れ物らしい" in (t or "") for t in titles):
                errors.append(f"4KB に収まる入れ物まで読めなくなっている: {titles}")
            await page.close()
        await b.close()
    for e in errors:
        print("  NG:", e)
    print("RESULT", "OK" if not errors else "NG")
    return 0 if not errors else 1


sys.exit(asyncio.run(main()))
