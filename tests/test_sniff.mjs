/* 名前の無いファイルに中身から見当を付ける sniffKind の回帰テスト。
 * web/app.js の sniff ブロックを切り出して Node で動かす。 */
import fs from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const s = src.indexOf("/* @extract-start sniff */");
const e = src.indexOf("/* @extract-end sniff */");
if (s < 0 || e < 0) { console.error("app.js に sniff マーカーが無い"); process.exit(2); }
const m = new Function(src.slice(s, e) + "\nreturn { sniffKind, sniffSummary, MAGICS, NO_MAGIC_EXTS, packedGuessWorthIt, PACKED_MAX_RATIO };")();

const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };
const bytes = (str, pad = 64) => {
  const out = new Uint8Array(pad);
  for (let i = 0; i < str.length; i++) out[i] = str.charCodeAt(i);
  return out;
};
const u32 = (n, pad = 64) => {
  const out = new Uint8Array(pad);
  out[0] = n & 255; out[1] = (n >>> 8) & 255; out[2] = (n >>> 16) & 255; out[3] = n >>> 24;
  return out;
};

/* 1. 魔法数は確実に当たる (バイトの性質に関わらず) */
const magicCases = [
  ["TIM2", "tm2"], ["VAGp", "vag"], ["RXWS", "rxws"], ["SShd", "sshd"],
  ["\x7FELF", "elf"], ["RIFF", "riff"], ["DFI\0", "dfi"],
];
for (const [magic, ext] of magicCases) {
  for (const cls of ["jp", "high", "zero", "tile"]) {
    const got = m.sniffKind(bytes(magic), cls, 100000);
    if (got.ext !== ext || !got.sure) fail(`${JSON.stringify(magic)} → ${got.ext} (期待 ${ext})`);
  }
}
const pss = new Uint8Array(64); pss[2] = 0x01; pss[3] = 0xBA;
if (m.sniffKind(pss, "high", 1 << 20).ext !== "pss") fail("MPEG PS が当たらない");

/* 2. 先頭 u32 が「伸張後の大きさ」に見えれば packed */
if (m.sniffKind(u32(30000), "tile", 10000).ext !== "packed") fail("伸張後の大きさ 3 倍を packed にしない");
if (m.sniffKind(u32(10001), "tile", 10000).ext !== "packed") fail("ぎりぎり大きい値を packed にしない");
/* 自分より小さい値、大きすぎる値は違う */
if (m.sniffKind(u32(5000), "tile", 10000).ext === "packed") fail("自分より小さい値を packed にした");
if (m.sniffKind(u32(10000 * 33), "tile", 10000).ext === "packed") fail("33 倍を packed にした");
/* 長さが分からなければこの見当は使わない */
if (m.sniffKind(u32(30000), "tile", 0).ext === "packed") fail("長さ不明なのに packed にした");
/* 魔法数が優先される */
if (m.sniffKind(bytes("TIM2"), "high", 10).ext !== "tm2") fail("魔法数より packed を優先した");
/* 2.5 **大きいファイルではこの見当を言わない** (#283)。でたらめな 4 バイトが
   この窓に入る確率は大きさに比例する: 100KB で 0.07%、16MB で 12%、100MB で 76%。
   コイン投げに近くなったら黙る —— 本物の圧縮は「乱数に近い並び」で拾える */
if (!m.packedGuessWorthIt(100 * 1024)) fail("100KB で見当をやめている (偶然は 0.07%)");
if (m.packedGuessWorthIt(100 * 1024 * 1024)) fail("100MB でも見当を言っている (偶然は 76%)");
if (m.packedGuessWorthIt(16 * 1024 * 1024)) fail("16MB でも見当を言っている (偶然は 12%)");
{
  const big = 8 * 1024 * 1024;                 /* 偶然 6% —— 言わない大きさ */
  if (m.sniffKind(u32(big * 3), "tile", big).ext === "packed") {
    fail("偶然のほうが多い大きさで「圧縮らしい」と言った");
  }
  /* **本物の圧縮は取りこぼさない。** 乱数に近い並びなら大きさに関係なく packed */
  if (m.sniffKind(u32(big * 3), "high", big).ext !== "packed") {
    fail("大きいファイルで本物の圧縮まで拾えなくなった");
  }
}

/* 3. 魔法数も大きさも無ければバイトの性質から */
const byClass = { jp: "txt", ascii: "txt", zero: "zero", high: "packed", wave: "wave", tile: "bin" };
for (const [cls, ext] of Object.entries(byClass)) {
  const got = m.sniffKind(u32(1), cls, 10000);
  if (got.ext !== ext || got.sure) fail(`性質 ${cls} → ${got.ext} (期待 ${ext}, sure=false)`);
}
if (m.sniffKind(u32(1), undefined, 10000).ext !== "bin") fail("性質不明を bin にしない");
if (m.sniffKind(new Uint8Array(2), "jp", 10000).ext !== "txt") fail("短すぎる先頭で落ちる");

/* 3.5 入れ物と、前置きの後ろの TIM2 (#153) */
const u32at = (b, p, n) => { b[p] = n & 255; b[p+1] = (n>>>8)&255; b[p+2] = (n>>>16)&255; b[p+3] = n>>>24; };
/** 件数 + (位置, 長さ) の表を持つ入れ物を組み立てる */
const container = (count, stride, pad, size = 4096) => {
  const b = new Uint8Array(size);
  u32at(b, 0, count);
  const tableEnd = 4 + count * stride;
  let off = pad ? Math.ceil(tableEnd / 16) * 16 : tableEnd;
  for (let i = 0; i < count; i++) {
    u32at(b, 4 + i * stride, off);
    if (stride >= 8) u32at(b, 8 + i * stride, 16);
    off += 16;
  }
  return b;
};
/* .msg の形 (詰めなし) は msg、入れ物の形 (16 バイト境界まで詰める) は parts */
if (m.sniffKind(container(4, 8, false), "tile", 4096).ext !== "msg") fail("8 バイト刻みの .msg を見つけない");
if (m.sniffKind(container(6, 4, false), "tile", 4096).ext !== "msg") fail("4 バイト刻みの .msg を見つけない");
if (m.sniffKind(container(2, 12, true), "tile", 4096).ext !== "parts") fail("12 バイト刻みの入れ物を見つけない");
if (m.sniffKind(container(3, 8, true), "tile", 4096).ext !== "parts") fail("8 バイト刻みの入れ物を見つけない");
/* 見当であって確定ではない */
if (m.sniffKind(container(4, 8, false), "tile", 4096).sure) fail("入れ物の見当を sure にした");
/* 空き枠 (位置も長さも 0) があっても見つける */
const withHole = container(3, 8, true);
u32at(withHole, 4 + 2 * 8, 0); u32at(withHole, 8 + 2 * 8, 0);
if (m.sniffKind(withHole, "tile", 4096).ext !== "parts") fail("空き枠のある入れ物を取りこぼす");
/* 1 件目が表の直後でなければ入れ物と見ない (ここが当てはめでない根拠) */
const shifted = container(4, 8, false);
u32at(shifted, 4, 200);
if (m.sniffKind(shifted, "tile", 4096).ext === "msg") fail("1 件目が表の直後でないのに msg にした");
/* 位置が減る / はみ出すものも違う */
const backwards = container(4, 8, false);
u32at(backwards, 4 + 8, 8);
if (m.sniffKind(backwards, "tile", 4096).ext === "msg") fail("位置が減るのに msg にした");
const outside = container(4, 8, false);
u32at(outside, 4 + 3 * 8, 9999);
if (m.sniffKind(outside, "tile", 4096).ext === "msg") fail("ファイル外を指すのに msg にした");
/* 前置きの後ろの TIM2 は見当で拾う。先頭の TIM2 は確定のまま */
const behind = new Uint8Array(256);
behind[0] = 0x54; behind[1] = 0x4D; behind[2] = 0x53;      /* "TMS" 風の前置き */
for (let i = 0; i < 4; i++) behind[0x80 + i] = "TIM2".charCodeAt(i);
const bg = m.sniffKind(behind, "zero", 256);
if (bg.ext !== "tm2" || bg.sure) fail(`前置きの後ろの TIM2 → ${bg.ext} sure=${bg.sure}`);
if (!m.sniffKind(bytes("TIM2"), "tile", 4096).sure) fail("先頭の TIM2 が確定でなくなった");
/* 入れ物のほうが先。中に絵が 1 枚あるだけで「画像」と名づけない (#153) */
const boxWithImage = container(2, 12, true);
for (let i = 0; i < 4; i++) boxWithImage[0x50 + i] = "TIM2".charCodeAt(i);
if (m.sniffKind(boxWithImage, "tile", 4096).ext !== "parts") fail("入れ物より埋まった TIM2 を優先した");

/* 4. 集計は多い順。**確かめたものと当て推量を分けて数える** (#281) */
const sum = m.sniffSummary([
  { ext: "tm2", sure: true }, { ext: "packed", sure: false }, { ext: "tm2", sure: true },
  { ext: "bin", sure: false }, { ext: "tm2", sure: true }, { ext: "packed", sure: false },
]);
if (sum !== "tm2 3 · packed 2 (すべて見当) · bin 1 (すべて見当)") fail(`集計が違う: ${sum}`);
/* 混ざっていたら「うち見当 N」。全部確かめたものなら何も付けない */
const mixed = m.sniffSummary([{ ext: "tm2", sure: true }, { ext: "tm2", sure: false },
                              { ext: "tm2", sure: true }]);
if (mixed !== "tm2 3 (うち見当 1)") fail(`混ざった集計が違う: ${mixed}`);
const allSure = m.sniffSummary([{ ext: "vag", sure: true }, { ext: "vag", sure: true }]);
if (allSure !== "vag 2") fail(`確かめたものに見当と書いた: ${allSure}`);
/* 1 件も確かめられていないときは種類ごとに書かない (同じ断りを何度も言わない)。
   そのときは呼び手の「確かめられたものはありません」の 1 文が受け持つ */
const noneSure = m.sniffSummary([{ ext: "bin", sure: false }, { ext: "txt", sure: false },
                                 { ext: "bin", sure: false }]);
if (noneSure !== "bin 2 · txt 1") fail(`全部見当のときに印を重ねた: ${noneSure}`);

/* 4.5 「確かめようがない」と書く相手が、本当に目印を持たないこと (#281)。
   画面は「.msg にも入れ物にも先頭の目印が無い」と言い切るので、
   その根拠 —— 魔法数の表にその種類が無いこと —— をここで押さえる */
for (const ext of m.NO_MAGIC_EXTS) {
  if (m.MAGICS.some((mg) => mg.ext === ext)) fail(`${ext} は魔法数の表にある (確かめようがある)`);
}
for (const ext of ["msg", "parts"]) {
  if (!m.NO_MAGIC_EXTS.includes(ext)) fail(`${ext} が「目印の無い種類」に入っていない`);
}

/* 5. 表の魔法数は全部 4 バイト以上で重複しない */
const seen = new Set();
for (const mg of m.MAGICS) {
  if (mg.bytes.length < 4) fail(`${mg.ext} の魔法数が短い`);
  const key = mg.bytes.join(",");
  if (seen.has(key)) fail(`${mg.ext} の魔法数が重複`);
  seen.add(key);
}

console.log(`OK  魔法数 ${magicCases.length + 1} 種 · 圧縮の見当 6 件 + 大きさの足切り 5 件 · 性質からの見当 ${Object.keys(byClass).length + 2} 件 · 入れ物と埋まった TIM2 12 件 · 集計 4 件 · 目印の無い種類`);
