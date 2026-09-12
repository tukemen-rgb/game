"""文字表の下書き (フォント画像の形から候補の字を当てる) を、ブラウザで実際に測る.

前半: ブラウザで描いた「字の並んだ画像」を作り、位置と大きさをずらしてから当てさせて
正解率を見る。実物のフォントは書体が違うので、ここで出る率は上限であって保証ではない。
後半: 練習データのフォント画像で下書きの操作が最後まで通り、人が書いた文字表を
下書きが踏まないことを確かめる (練習データの字は模様なので、当たるかどうかは見ない)。
"""

import asyncio
import sys

from playwright.async_api import async_playwright

from common import REPO, WORK, launch, select_file

# 当てさせる字。仮名を中心に、数字と記号も混ぜる
CHARS = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもアイウエオカキクケコ0123456789。、"

MEASURE = """(chars => {
  const CHARS = Array.from(chars);
  const CW = 22, CH = 22, COLS = 23;
  const rows = Math.ceil(CHARS.length / COLS);
  const cv = document.createElement("canvas");
  cv.width = CW * COLS; cv.height = CH * rows;
  const g = cv.getContext("2d", { willReadFrequently: true });
  g.fillStyle = "#000"; g.fillRect(0, 0, cv.width, cv.height);
  g.fillStyle = "#fff"; g.textAlign = "center"; g.textBaseline = "middle";
  CHARS.forEach((c, n) => {
    const x = (n % COLS) * CW, y = Math.floor(n / COLS) * CH;
    /* 実物は書体も置き方も候補と違う。小さめに、右上へずらして描いて、そのぶんを模す */
    g.font = Math.floor(CH * 0.74) + "px sans-serif";
    g.fillText(c, x + CW / 2 + 1, y + CH / 2 - 1);
  });
  const rgba = g.getImageData(0, 0, cv.width, cv.height).data;
  const pol = { alpha: false, invert: false };
  const cells = CHARS.map((c, n) => {
    const x = (n % COLS) * CW, y = Math.floor(n / COLS) * CH;
    return { n, feat: inkFeature(glyphCellInk(rgba, cv.width, x, y, CW, CH, pol), CW, CH) };
  });
  const cands = glyphCandidateFeatures(GLYPH_CANDIDATES, CW, CH);
  const raw = draftGlyphMatches(cells, cands, {});
  const shapeOnly = raw.filter((d) => CHARS[d.n] === d.ch).length;
  /* 並び順で直す。区間を外へ伸ばすかどうかは画像で確かめる */
  const featCache = new Map(cands.map((k) => [k.ch, k.feat]));
  const cellFeat = new Map(cells.map((c) => [c.n, c.feat]));
  const similar = (n, c) => {
    if (!featCache.has(c)) {
      const f = glyphCandidateFeatures([c], CW, CH);
      featCache.set(c, f.length ? f[0].feat : null);
    }
    return glyphFeatureScore(cellFeat.get(n), featCache.get(c));
  };
  const ordered = applyGlyphOrder(raw, { similar });
  const got = ordered.draft;
  let right = 0;
  const wrong = [];
  for (const d of got) {
    if (CHARS[d.n] === d.ch) right++;
    else wrong.push(`${d.n}:${CHARS[d.n]}->${d.ch}`);
  }
  const ns = new Set(got.map((d) => d.n)), chs = new Set(got.map((d) => d.ch));
  return { total: CHARS.length, matched: got.length, right, wrong, shapeOnly,
           orderFixed: ordered.fixed.length,
           uniqueN: ns.size === got.length, uniqueCh: chs.size === got.length,
           cands: cands.length };
})"""


# 漢字は既定の候補に入っていない。画面の欄に貼ったときだけ当たること (#72)
KANJI = """(chars => {
  const CHARS = Array.from(chars);
  const CW = 22, CH = 22, COLS = 8;
  const rows = Math.ceil(CHARS.length / COLS);
  const cv = document.createElement("canvas");
  cv.width = CW * COLS; cv.height = CH * rows;
  const g = cv.getContext("2d", { willReadFrequently: true });
  g.fillStyle = "#000"; g.fillRect(0, 0, cv.width, cv.height);
  g.fillStyle = "#fff"; g.textAlign = "center"; g.textBaseline = "middle";
  CHARS.forEach((c, n) => {
    const x = (n % COLS) * CW, y = Math.floor(n / COLS) * CH;
    g.font = Math.floor(CH * 0.74) + "px sans-serif";
    g.fillText(c, x + CW / 2, y + CH / 2);
  });
  const rgba = g.getImageData(0, 0, cv.width, cv.height).data;
  const pol = { alpha: false, invert: false };
  const cells = CHARS.map((c, n) => {
    const x = (n % COLS) * CW, y = Math.floor(n / COLS) * CH;
    return { n, feat: inkFeature(glyphCellInk(rgba, cv.width, x, y, CW, CH, pol), CW, CH) };
  });
  const hit = (candText) => {
    const cands = glyphCandidateFeatures(candText, CW, CH);
    const got = draftGlyphMatches(cells, cands, {});
    return got.filter((d) => CHARS[d.n] === d.ch).length;
  };
  const extra = glyphExtraCandidates(chars, GLYPH_CANDIDATES);
  return { total: CHARS.length, without: hit(GLYPH_CANDIDATES),
           with: hit(GLYPH_CANDIDATES + extra), extra: Array.from(extra).length };
})"""


def doc_says_the_same(shape_only: int, right: int, total: int) -> list[str]:
    """docs/10 の「形だけで 46/57、並び順の補正を入れて 53/57」を測り直す (#102).

    率そのものは書体しだいで動くので、**動いたら文書を書き換える**のが正しい。
    落ちたときにどう直せばよいかまで言う。
    """
    import re

    with open(REPO + "/docs/10-僕夏2の手順.md", encoding="utf-8") as fh:
        doc = fh.read()
    said = re.findall(r"形だけで (\d+)/(\d+)、並び順の補正を入れて (\d+)/(\d+)", doc)
    if len(said) != 1:
        return [f"docs/10 の実測の書き方が見つからない ({len(said)} 件)"]
    a, at, b, bt = (int(x) for x in said[0])
    now = f"形だけで {shape_only}/{total}、並び順の補正を入れて {right}/{total}"
    if (a, at, b, bt) != (shape_only, total, right, total):
        return [f"docs/10 の実測が今と違う。docs/10 をこう書き換える → 「{now}」"]
    return []


async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto("file://" + REPO + "/web/index.html")

        # 1. 当たり具合を測る
        r = await page.evaluate(MEASURE, CHARS)
        rate = r["right"] / max(1, r["total"])
        print("draft:", {k: r[k] for k in ("total", "matched", "right", "shapeOnly", "orderFixed", "cands")},
              f"rate={rate:.2f}")
        print("wrong:", " ".join(r["wrong"][:20]))
        bad = []
        # 書体が変われば率は動く。仕組みが働いていることを見る線として 7 割を置く
        # (#70 の実測は 53/57 = 0.93。形だけの #69 は 46/57 = 0.81 だった)
        if rate < 0.7:
            bad.append(f"正解率が低い: {rate:.2f}")
        # docs/10 はこの率を「実測」として数字で書いている。緩い下限だけ見ていると、
        # 率が動いた日に**文書の数字だけがもっともらしく古くなる** (#102)。
        # 社長はその数字を目印に「合っているか」を判断するので、ここで突き合わせる。
        bad += doc_says_the_same(r["shapeOnly"], r["right"], r["total"])
        # 並び順で直す規則が、形だけより悪くしていないこと
        if r["right"] < r["shapeOnly"]:
            bad.append(f"並び順で直して悪くなった: {r['shapeOnly']} -> {r['right']}")
        if not r["orderFixed"]:
            bad.append("並び順で直した字が 1 つも無い")
        if r["matched"] < r["total"] * 0.8:
            bad.append(f"当てられたマスが少ない: {r['matched']}/{r['total']}")
        if not r["uniqueN"] or not r["uniqueCh"]:
            bad.append("同じ番号か同じ字を 2 回使った")
        if r["cands"] < 200:
            bad.append(f"候補が少ない: {r['cands']}")

        # 1.5 漢字は貼ったときだけ当たる (#72)
        k = await page.evaluate(KANJI, "日月火水木金土山")
        print("kanji:", k)
        if k["extra"] != k["total"]:
            bad.append(f"貼った漢字の数が合わない: {k['extra']}/{k['total']}")
        if k["with"] <= k["without"]:
            bad.append(f"漢字を貼っても当たりが増えない: {k['without']} -> {k['with']}")
        if k["with"] < k["total"] * 0.5:
            bad.append(f"貼った漢字の当たりが少ない: {k['with']}/{k['total']}")

        # 2. 練習データのフォント画像で、操作が最後まで通ること
        await page.set_input_files("#fileinput", [WORK + "/BOKU2SAMPLE/BOKU2.IDX",
                                                  WORK + "/BOKU2SAMPLE/BOKU2.IMG"])
        await page.wait_for_selector("#shell:not([hidden])")
        await page.click('[data-tab="index"]')
        await page.click("#idxrun")
        await page.wait_for_selector("#idxpreview button.btn.primary")
        await page.click("#idxpreview button.btn.primary")
        await page.wait_for_function(
            "document.querySelector('#capnote').textContent.includes('切り分けました')", timeout=30000)
        await select_file(page, "font", "bk_font.tms")
        await page.click('[data-tab="format"]')
        await page.wait_for_selector("#formatbox canvas")
        # 人が先に書いた分は踏まないこと。候補に足す欄も使う
        await page.fill("#msgglyphs", "0=あ\n1=い\n")
        await page.fill("#msgcands", "日月火水")
        await page.click("#tim2draft")
        await page.wait_for_timeout(2000)
        table = await page.input_value("#msgglyphs")
        hint = await page.text_content("#formatbox .hint")
        print("hint:", hint[:160])
        print("table head:", table.replace("\n", " ")[:80])
        lines = [ln for ln in table.split("\n") if ln.strip()]
        if not table.startswith("0=あ\n1=い"):
            bad.append(f"人が書いた文字表を踏んだ: {table[:40]!r}")
        if any("=" not in ln for ln in lines):
            bad.append("番号=文字 の形になっていない行がある")
        if "必ず目で確かめてください" not in hint:
            bad.append("下書きだと分かる断りが出ていない")
        if "うち貼った字 4" not in hint or "貼った候補も混ぜてあります" not in hint:
            bad.append(f"貼った候補の数が知らせに出ていない: {hint[:200]!r}")

        await b.close()
        ok = not bad and not errors
        if bad:
            print("bad:", bad)
        if errors:
            print("errors:", errors)
        print("RESULT", "OK" if ok else "NG")
        sys.exit(0 if ok else 1)


asyncio.run(main())
