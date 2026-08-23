/* 索引解析の回帰テスト。web/app.js から該当部分だけ取り出して Node で動かす。
 * ブラウザを起動せずに、いちばん壊れやすい推定ロジックを固定できる。 */
import fs from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const start = src.indexOf("/* @extract-start index-analyzer */");
const end = src.indexOf("/* @extract-end index-analyzer */");
if (start < 0 || end < 0) {
  console.error("app.js に @extract-start/@extract-end index-analyzer がありません");
  process.exit(2);
}
const u32le = (b, p) => (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
const analyzer = new Function("u32le",
  src.slice(start, end) + "\nreturn { analyzeIndex, indexEntries };")(u32le);

const idx = new Uint8Array(fs.readFileSync(path.join(repo, "work", "PACK.IDX")));
const imgPath = path.join(repo, "work", "PACK.IMG");
const dataSize = fs.statSync(imgPath).size;
const img = new Uint8Array(fs.readFileSync(imgPath));

const cands = analyzer.analyzeIndex(idx, dataSize);
const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };

if (!cands.length) fail("候補が 1 件も出ない");
const best = cands[0];

/* 正解: レコード 16 / ヘッダ 8 / 位置 +0 セクタ単位 / 長さ +4 バイト単位 */
if (best.rec !== 16) fail(`レコード長が ${best.rec} (期待 16)`);
if (best.skip !== 8) fail(`ヘッダが ${best.skip} (期待 8)`);
if (best.field !== 0) fail(`位置の列が +${best.field} (期待 +0)`);
if (best.mult !== 2048) fail(`位置の単位が ${best.mult} (期待 2048)`);
if (!best.size || best.size.field !== 4 || best.size.mult !== 1) {
  fail(`長さの列が ${JSON.stringify(best.size)} (期待 +4 バイト単位)`);
}
if (best.count !== 22) fail(`件数が ${best.count} (期待 22)`);

/* 実際に切り出した中身が正しいこと。先頭は SCRP のはず */
const items = analyzer.indexEntries(idx, best, dataSize, 8);
if (items[0].at !== 0) fail("1 件目の位置が 0 でない");
if (items[0].len !== 2120) fail(`1 件目の長さが ${items[0].len} (期待 2120)`);
const magic = String.fromCharCode(...img.subarray(items[0].at, items[0].at + 4));
if (magic !== "SCRP") fail(`1 件目の中身が ${magic} (期待 SCRP)`);
for (const it of items) {
  if (it.at + it.len > dataSize) fail(`#${it.i} が本体をはみ出している`);
}

/* 索引と関係のないファイルを索引として渡しても、でたらめを返さないこと */
const noise = new Uint8Array(4096);
for (let i = 0; i < noise.length; i++) noise[i] = (i * 37 + 11) & 0xFF;
const bogus = analyzer.analyzeIndex(noise, dataSize);
if (bogus.length && bogus[0].score > best.score) {
  fail("無関係なデータのほうが高得点になっている");
}

console.log(`OK  候補 ${cands.length} 件 / 1 位 = レコード${best.rec} ヘッダ${best.skip} ` +
            `位置+${best.field}(x${best.mult}) 長さ+${best.size.field} ${best.count} 件`);
