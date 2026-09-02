/* ブラウザ側 LZSS の回帰テスト。web/app.js から切り出して Node で動かし、
 * Python (tools/lzss.py) が圧縮したデータを正しく伸張できるか突き合わせる。
 * 実行前に: python3 tests/gen_lzss_cases.py で tests/lzss_cases.json を作る。 */
import fs from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const s = src.indexOf("/* @extract-start lzss */");
const e = src.indexOf("/* @extract-end lzss */");
if (s < 0 || e < 0) { console.error("app.js に lzss マーカーが無い"); process.exit(2); }
const SJIS_LEAD = (b) => (b >= 0x81 && b <= 0x9F) || (b >= 0xE0 && b <= 0xEF);
const SJIS_TRAIL = (b) => b >= 0x40 && b <= 0xFC && b !== 0x7F;
const m = new Function("SJIS_LEAD", "SJIS_TRAIL",
  src.slice(s, e) + "\nreturn { lzssDecompress, lzssTextScore, lzssScan };")(SJIS_LEAD, SJIS_TRAIL);

const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };
const casesPath = path.join(repo, "tests", "lzss_cases.json");
if (!fs.existsSync(casesPath)) {
  console.log("skip: tests/lzss_cases.json が無い (python3 tests/gen_lzss_cases.py で作成)");
  process.exit(0);
}
const cases = JSON.parse(fs.readFileSync(casesPath, "utf8"));
let ok = 0;
for (const { packed, orig } of cases) {
  const got = m.lzssDecompress(Uint8Array.from(packed), 0);
  if (got.length === orig.length && orig.every((v, i) => v === got[i])) ok++;
  else fail(`Python の圧縮を伸張できない (期待 ${orig.length} / 実際 ${got.length})`);
}
/* ゼロ埋めをテキストと誤判定しないこと */
if (m.lzssTextScore(new Uint8Array(4096).fill(0x20)) !== 0) fail("空白の羅列をテキストと判定した");
console.log(`OK  Python の圧縮を JS が伸張 ${ok}/${cases.length} 一致`);
