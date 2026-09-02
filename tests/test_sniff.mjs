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

console.log(`OK  魔法数 ${magicCases.length + 1} 種 · 圧縮の見当 6 件 · 性質からの見当 ${Object.keys(byClass).length + 2} 件 · 集計`);
