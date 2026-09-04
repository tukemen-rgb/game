/* TIM2 読み取り (web/app.js の tim2 ブロック) の回帰テスト。
 * Python (tools/make_tim2.py --json) が組み立てた各画素形式の画像を、
 * ブラウザ側の読み取りが正しい RGBA に戻せるか突き合わせる。 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const s = src.indexOf("/* @extract-start tim2 */");
const e = src.indexOf("/* @extract-end tim2 */");
if (s < 0 || e < 0) { console.error("app.js に tim2 マーカーが無い"); process.exit(2); }
const u32le = (b, p) => (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
const u16le = (b, p) => b[p] | (b[p + 1] << 8);
const m = new Function("u32le", "u16le",
  src.slice(s, e) + "\nreturn { findTim2, parseTim2, decodeTim2, csm1Index, tim2Clut };")(u32le, u16le);

const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };
const hexToBytes = (h) => Uint8Array.from(h.match(/../g).map((x) => parseInt(x, 16)));

const cases = JSON.parse(execFileSync("python3", [path.join(repo, "tools", "make_tim2.py"), "--json"], { encoding: "utf8" }));
let ok = 0;
for (const c of cases) {
  const b = hexToBytes(c.tim2);
  const at = m.findTim2(b);
  if (at !== 0) fail(`${c.name}: 先頭の TIM2 を見つけられない`);
  const t = m.parseTim2(b, 0);
  if (!t) fail(`${c.name}: 見出しが読めない`);
  const pic = t.pictures[0];
  if (pic.width !== c.w || pic.height !== c.h) fail(`${c.name}: 大きさ ${pic.width}x${pic.height}`);
  const rgba = m.decodeTim2(b, pic);
  if (!rgba) fail(`${c.name}: 画素を戻せない`);
  for (let i = 0; i < c.rgba.length; i++) {
    const want = c.rgba[i];
    for (let k = 0; k < 4; k++) {
      if (Math.abs(rgba[i * 4 + k] - want[k]) > 1) {
        fail(`${c.name}: 画素 ${i} の ${"RGBA"[k]} が ${rgba[i * 4 + k]} (期待 ${want[k]})`);
      }
    }
  }
  ok++;
}

/* CSM1 の並び替え: 8〜15 と 16〜23 が入れ替わる。それ以外はそのまま。往復で戻る */
for (let i = 0; i < 256; i++) {
  const j = m.csm1Index(i);
  if (m.csm1Index(j) !== i) fail(`CSM1 の並び替えが往復しない (${i})`);
  const m8 = i & 0x18;
  if (m8 === 0x08 && j !== i + 8) fail(`CSM1: ${i} → ${j}`);
  if (m8 === 0x10 && j !== i - 8) fail(`CSM1: ${i} → ${j}`);
  if ((m8 === 0 || m8 === 0x18) && j !== i) fail(`CSM1: ${i} → ${j}`);
}

/* .tms のように 0x80 バイトの前置きがあっても見つける */
const inner = hexToBytes(cases[0].tim2);
const tms = new Uint8Array(0x80 + inner.length);
tms.set([0x54, 0x4D, 0x53, 0x00], 0);
tms.set(inner, 0x80);
if (m.findTim2(tms) !== 0x80) fail("0x80 の前置きの後の TIM2 を見つけられない");
const t2 = m.parseTim2(tms, 0x80);
if (!t2 || t2.pictures[0].width !== cases[0].w) fail("前置き付きの見出しが読めない");

/* TIM2 でないものを読まない。壊れた見出しで落ちない */
if (m.findTim2(new TextEncoder().encode("これは画像ではありません。".repeat(10))) !== -1) fail("文字列を TIM2 と誤認した");
const broken = hexToBytes(cases[0].tim2).slice(0, 40);
if (m.parseTim2(broken, 0) !== null) fail("途中で切れた TIM2 を読んだことにした");

console.log(`OK  TIM2 ${ok}/${cases.length} 形式 (32/24/16bit, 8bit CSM1/直線, 4bit 128 バイト揃え) · CSM1 往復 · 前置き 0x80`);
