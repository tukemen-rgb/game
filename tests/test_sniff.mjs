/* 名前の無いファイルに中身から見当を付ける sniffKind の回帰テスト。
 * web/app.js の sniff ブロックを切り出して Node で動かす。 */
import fs from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const s = src.indexOf("/* @extract-start sniff */");
const e = src.indexOf("/* @extract-end sniff */");
if (s < 0 || e < 0) { console.error("app.js に sniff マーカーが無い"); process.exit(2); }
const m = new Function(src.slice(s, e) + "\nreturn { sniffKind, sniffSummary, MAGICS };")();

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

/* 4. 集計は多い順 */
const sum = m.sniffSummary([{ ext: "tm2" }, { ext: "packed" }, { ext: "tm2" }, { ext: "bin" }, { ext: "tm2" }, { ext: "packed" }]);
if (sum !== "tm2 3 · packed 2 · bin 1") fail(`集計が違う: ${sum}`);

/* 5. 表の魔法数は全部 4 バイト以上で重複しない */
const seen = new Set();
for (const mg of m.MAGICS) {
  if (mg.bytes.length < 4) fail(`${mg.ext} の魔法数が短い`);
  const key = mg.bytes.join(",");
  if (seen.has(key)) fail(`${mg.ext} の魔法数が重複`);
  seen.add(key);
}

console.log(`OK  魔法数 ${magicCases.length + 1} 種 · 圧縮の見当 6 件 · 性質からの見当 ${Object.keys(byClass).length + 2} 件 · 入れ物と埋まった TIM2 12 件 · 集計`);
