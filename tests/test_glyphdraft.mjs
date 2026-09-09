/* 文字表の下書き (web/app.js の形合わせ) の回帰テスト。
 *
 * ブラウザの描画は使わず、合成したインクの形だけで確かめる。ここで見るのは
 * 「大きさと位置が違っても同じ形なら似ていると出るか」「同じ候補を 2 つのマスに
 * 使い回さないか」「人が書いた文字表を下書きが踏まないか」の 3 点。 */
import fs from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const s = src.indexOf("/* @extract-start bokumsg */");
const e = src.indexOf("/* @extract-end bokumsg */");
if (s < 0 || e < 0) { console.error("app.js に bokumsg マーカーが無い"); process.exit(2); }
const u32le = (b, p) => (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
const u16le = (b, p) => b[p] | (b[p + 1] << 8);
const m = new Function("u32le", "u16le",
  src.slice(s, e) + "\nreturn { glyphInkPolarity, glyphCellInk, inkFeature, glyphFeatureScore,"
  + " draftGlyphMatches, mergeGlyphDraft, parseGlyphTable, GLYPH_CANDIDATES, applyGlyphOrder, GLYPH_SEQUENCES };")(u32le, u16le);

const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };

/** 絵文字で書いた形をインクの升目にする ("#" が字、"." が地) */
function cellOf(rowsText) {
  const rows = rowsText.trim().split("\n").map((r) => r.trim());
  const h = rows.length, w = rows[0].length;
  const cell = new Float32Array(w * h);
  rows.forEach((r, y) => {
    if (r.length !== w) fail(`形の幅がそろっていない: ${r}`);
    for (let x = 0; x < w; x++) cell[y * w + x] = r[x] === "#" ? 1 : 0;
  });
  return { cell, w, h };
}
const featOf = (txt) => { const c = cellOf(txt); return m.inkFeature(c.cell, c.w, c.h); };

/* ---- 形の似ぐあい ---- */
{
  const l4 = featOf(`
    #...
    #...
    #...
    ####`);
  if (!l4) fail("形があるのに特徴が作れない");
  let sum = 0;
  for (const v of l4) sum += v;
  if (Math.abs(sum - 1) > 1e-6) fail(`特徴の合計が 1 でない: ${sum}`);
  if (m.glyphFeatureScore(l4, l4) < 0.999) fail("同じ形どうしが 1 にならない");

  /* 同じ「L」を倍の大きさで、しかもマスの隅に描く。外枠で正規化するので似ていると出る */
  const l8 = featOf(`
    ##......
    ##......
    ##......
    ##......
    ##......
    ##......
    ########
    ########`);
  const sc = m.glyphFeatureScore(l4, l8);
  if (sc < 0.8) fail(`大きさと位置が違う同じ形の似ぐあいが低い: ${sc.toFixed(3)}`);

  /* 別の形 (横棒だけ) は、はっきり低く出ること */
  const bar = featOf(`
    ....
    ####
    ####
    ....`);
  const sc2 = m.glyphFeatureScore(l4, bar);
  if (sc2 >= sc) fail(`違う形が同じ形より似ていると出た: ${sc2.toFixed(3)} >= ${sc.toFixed(3)}`);

  /* 重なりが無ければ 0 */
  const a = new Float32Array([1, 0, 0, 0]), b = new Float32Array([0, 0, 0, 1]);
  if (m.glyphFeatureScore(a, b) !== 0) fail("重ならない形が 0 にならない");
  if (m.glyphFeatureScore(a, null) !== 0) fail("片方が無いときに 0 にならない");
  if (m.glyphFeatureScore(a, new Float32Array(9)) !== 0) fail("大きさ違いを比べてしまった");
}

/* ---- 空きマスは当てにいかない ---- */
{
  const blank = cellOf(`
    ....
    ....
    ....
    ....`);
  if (m.inkFeature(blank.cell, blank.w, blank.h) !== null) fail("空きマスから特徴を作ってしまった");
  const speck = new Float32Array(16);
  speck[5] = 1;                                     /* 点 1 つ (インクが薄すぎる) */
  if (m.inkFeature(speck, 4, 4) !== null) fail("ごみのような点を字と見なした");
}

/* ---- 画像のどちらが字かの判定 ---- */
{
  const mk = (fn) => { const a = new Uint8ClampedArray(4 * 100); for (let i = 0; i < 100; i++) fn(a, i * 4); return a; };
  const clear = mk((a, p) => { a[p + 3] = 0; });                       /* 全部透明 */
  if (!m.glyphInkPolarity(clear).alpha) fail("透明の多い画像で透明度を使わなかった");
  const white = mk((a, p) => { a[p] = a[p + 1] = a[p + 2] = 255; a[p + 3] = 255; });
  const pw = m.glyphInkPolarity(white);
  if (pw.alpha || !pw.invert) fail("明るい地で暗い方を字と見なさなかった");
  const black = mk((a, p) => { a[p + 3] = 255; });
  const pb = m.glyphInkPolarity(black);
  if (pb.alpha || pb.invert) fail("暗い地で明るい方を字と見なさなかった");
}

/* ---- マスの切り出し (画像の中の 1 文字ぶん) ---- */
{
  const w = 8, h = 4;
  const rgba = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) { rgba[i * 4 + 3] = 255; }
  /* 右半分の 4×4 だけを白くする */
  for (let y = 0; y < 4; y++) for (let x = 4; x < 8; x++) {
    const p = (y * w + x) * 4;
    rgba[p] = rgba[p + 1] = rgba[p + 2] = 255;
  }
  const pol = { alpha: false, invert: false };
  const left = m.glyphCellInk(rgba, w, 0, 0, 4, 4, pol);
  const right = m.glyphCellInk(rgba, w, 4, 0, 4, 4, pol);
  if (left.some((v) => v > 0.01)) fail("左のマスに右の絵が混ざった");
  if (right.some((v) => v < 0.99)) fail("右のマスを取り出せていない");
}

/* ---- 1 対 1 の割り当て ---- */
{
  const l = featOf(`
    #...
    #...
    #...
    ####`);
  const bar = featOf(`
    ....
    ####
    ####
    ....`);
  const cells = [{ n: 3, feat: l }, { n: 7, feat: bar }, { n: 9, feat: null }];
  const cands = [{ ch: "L", feat: l }, { ch: "-", feat: bar }];
  const got = m.draftGlyphMatches(cells, cands, { minScore: 0.5 });
  if (got.length !== 2) fail(`割り当ての数が ${got.length}`);
  if (got[0].n !== 3 || got[0].ch !== "L") fail(`番号順に並んでいない: ${JSON.stringify(got)}`);
  if (got[1].n !== 7 || got[1].ch !== "-") fail(`似ている方に付かなかった: ${JSON.stringify(got)}`);

  /* 同じ形のマスが 2 つあっても、候補 1 つを使い回さない */
  const two = m.draftGlyphMatches([{ n: 1, feat: l }, { n: 2, feat: l }], [{ ch: "L", feat: l }], {});
  if (two.length !== 1) fail(`候補を使い回した: ${JSON.stringify(two)}`);

  /* 似ていなければ何も出さない (当てずっぽうを書かない) */
  const none = m.draftGlyphMatches([{ n: 1, feat: l }], [{ ch: "-", feat: bar }], { minScore: 0.9 });
  if (none.length !== 0) fail("似ていない候補を書き込んだ");

  /* 2 番目との差 (margin)。点数そのものは当たり外れの目安にならないので、
     画面で「まず疑う所」を挙げるのはこの差の小さい順にする (docs/11 第 10 節) */
  const lDot = featOf(`
    #..#
    #...
    #...
    ####`);
  const clear = m.draftGlyphMatches([{ n: 1, feat: l }], [{ ch: "L", feat: l }, { ch: "-", feat: bar }], {});
  const close = m.draftGlyphMatches([{ n: 1, feat: l }], [{ ch: "L", feat: l }, { ch: "l", feat: lDot }], {});
  if (clear[0].margin === undefined) fail("2 番目との差が返っていない");
  if (!(clear[0].margin > close[0].margin)) {
    fail(`紛らわしい方の差が小さくならない: ${clear[0].margin.toFixed(3)} vs ${close[0].margin.toFixed(3)}`);
  }
  if (close[0].ch !== "L") fail("紛らわしくても 1 番似ている方に付くこと");
}

/* ---- 人が書いた文字表を踏まない ---- */
{
  const existing = "12=あ\n13=い\n";
  const r = m.mergeGlyphDraft(existing, [{ n: 12, ch: "ぬ" }, { n: 14, ch: "う" }]);
  if (r.added !== 1) fail(`足した数が ${r.added}`);
  const map = m.parseGlyphTable(r.text);
  if (map[12] !== "あ") fail("人が書いた字を下書きが上書きした");
  if (map[13] !== "い") fail("人が書いた字が消えた");
  if (map[14] !== "う") fail("下書きが入っていない");
  if (!/^12=あ\n13=い\n14=う\n$/.test(r.text)) fail(`書き出しの形が違う: ${JSON.stringify(r.text)}`);

  /* 番号を書かない並びの文字表 (先頭から順) からでも、番号=文字 の形にそろう */
  const r2 = m.mergeGlyphDraft("あい", [{ n: 2, ch: "う" }]);
  if (m.parseGlyphTable(r2.text)[1] !== "い" || m.parseGlyphTable(r2.text)[2] !== "う") fail("並びの文字表を取り込めていない");

  /* 空からでも作れる */
  const r3 = m.mergeGlyphDraft("", [{ n: 0, ch: "か" }]);
  if (r3.added !== 1 || m.parseGlyphTable(r3.text)[0] !== "か") fail("空の文字表に下書きを入れられない");
}

/* ---- 候補の一覧 ---- */
{
  const c = m.GLYPH_CANDIDATES;
  for (const ch of ["あ", "ン", "0", "。", "ー"]) {
    if (!c.includes(ch)) fail(`候補に ${ch} が無い`);
  }
  if (new Set(Array.from(c)).size !== Array.from(c).length) fail("候補に同じ字が 2 回入っている");
}

/* ---- 並び順で直す ---- */
{
  const d = (n, ch) => ({ n, ch, score: 0.7, margin: 0.05 });
  /* 続いたマスが続いた字に当たっていれば、間の外れを並びで直す */
  const r = m.applyGlyphOrder([d(10, "あ"), d(11, "い"), d(12, "ぬ"), d(13, "え"), d(14, "お")]);
  const got = new Map(r.draft.map((x) => [x.n, x.ch]));
  if (got.get(12) !== "う") fail(`並びで直せていない: ${got.get(12)}`);
  if (r.fixed.length !== 1 || r.fixed[0].from !== "ぬ" || r.fixed[0].to !== "う") {
    fail(`直した記録が違う: ${JSON.stringify(r.fixed)}`);
  }
  if (!r.draft.find((x) => x.n === 12).byOrder) fail("並びで直した印が付いていない");

  /* 支持が 4 つ未満なら何もしない */
  const few = m.applyGlyphOrder([d(10, "あ"), d(11, "い"), d(12, "ぬ")]);
  if (few.fixed.length) fail("支持が足りないのに直した");

  /* まばらな一致 (広い区間に 4 つだけ) も当てにしない */
  const sparse = m.applyGlyphOrder([d(0, "あ"), d(10, "か"), d(20, "た"), d(30, "は")]);
  if (sparse.fixed.length) fail("まばらな一致で区間を埋めた");

  /* 並びが五十音でないフォントでは、支持が集まらず何も起きない */
  const shuffled = m.applyGlyphOrder([d(0, "ん"), d(1, "あ"), d(2, "そ"), d(3, "き"), d(4, "ぬ")]);
  if (shuffled.fixed.length) fail("並びが違うのに直してしまった");
}

/* ---- 区間を外へ伸ばすのは、画像の裏付けがあるときだけ ---- */
{
  const d = (n, ch) => ({ n, ch, score: 0.7, margin: 0.05 });
  const base = [d(0, "あ"), d(1, "い"), d(2, "う"), d(3, "え"), d(4, "ぬ")];
  /* 物差しが無ければ伸ばさない (4 番は区間の外) */
  const noEye = m.applyGlyphOrder(base.map((x) => ({ ...x })));
  if (noEye.fixed.length) fail("裏付けが無いのに区間を伸ばした");

  /* 似ていると言う物差しなら伸ばす */
  const yes = m.applyGlyphOrder(base.map((x) => ({ ...x })), { similar: (n, c) => (c === "お" ? 0.95 : 1) });
  if (!yes.fixed.length || yes.draft.find((x) => x.n === 4).ch !== "お") {
    fail(`裏付けがあるのに伸ばさなかった: ${JSON.stringify(yes.fixed)}`);
  }

  /* 似ていないと言う物差しなら伸ばさない (仮名の並びが数字のマスに食い込むのを止める) */
  const no = m.applyGlyphOrder(base.map((x) => ({ ...x })), { similar: (n, c) => (c === "お" ? 0.3 : 1) });
  if (no.fixed.length) fail("似ていないのに区間を伸ばした");
}

/* ---- 並びで直した字が、他のマスに二重に残らない ---- */
{
  const d = (n, ch) => ({ n, ch, score: 0.7, margin: 0.05 });
  const r = m.applyGlyphOrder([d(10, "あ"), d(11, "い"), d(12, "ぬ"), d(13, "え"), d(14, "お"), d(40, "う")]);
  const us = r.draft.filter((x) => x.ch === "う");
  if (us.length !== 1 || us[0].n !== 12) fail(`同じ字が 2 つ残った: ${JSON.stringify(us)}`);
}

/* ---- 並びの一覧 ---- */
{
  if (!m.GLYPH_SEQUENCES.some((s) => s.startsWith("あいうえお"))) fail("五十音の並びが無い");
  for (const seq of m.GLYPH_SEQUENCES) {
    if (new Set(Array.from(seq)).size !== Array.from(seq).length) fail(`並びに同じ字が 2 回: ${seq}`);
  }
}

console.log("OK  文字表の下書き: 形の似ぐあい / 空きマス / 地と字の判定 / マスの切り出し / 1 対 1 の割り当て / 人の書いた分を踏まない / 並び順で直す / 裏付けのある区間だけ伸ばす");
