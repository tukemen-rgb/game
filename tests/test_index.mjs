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

/* ---------- 名前つき索引 (BOKU2.IDX の "DFI" 形式) ---------- */

const nstart = src.indexOf("/* @extract-start named-index */");
const nend = src.indexOf("/* @extract-end named-index */");
if (nstart < 0 || nend < 0) {
  console.error("app.js に @extract-start/@extract-end named-index がありません");
  process.exit(2);
}
const ascii2 = (bytes) => {
  let s = "";
  for (const b of bytes) s += b >= 0x20 && b < 0x7F ? String.fromCharCode(b) : " ";
  return s;
};
const named = new Function("u32le", "ascii",
  src.slice(nstart, nend)
  + "\nreturn { analyzeNamedIndex, namedEntries, isNameAt, scoreNamedLayout, readDfi };")(
  u32le, ascii2);

/* 実物と同じ形の索引を組み立てる。
   ヘッダ 16 バイト ("DFI\0" + 版) / レコード 16 バイト
   (+0 種別, +4 名前の位置, +8 セクタ, +12 長さ) / 末尾に名前の置き場 */
function buildDfi(files, dirs) {
  const recCount = files.length + dirs.length;
  const recEnd = 16 + recCount * 16;
  const names = [];
  let nameOff = recEnd;
  const nameBuf = [];
  for (const n of [...dirs, ...files.map((f) => f.name)]) {
    names.push(nameOff);
    for (const ch of n) nameBuf.push(ch.charCodeAt(0));
    nameBuf.push(0);
    nameOff += n.length + 1;
  }
  const buf = new Uint8Array(recEnd + nameBuf.length);
  const dv = new DataView(buf.buffer);
  buf.set([0x44, 0x46, 0x49, 0x00], 0);          /* "DFI\0" */
  dv.setUint32(4, 0x00000100, true);              /* 版 1.00 */
  let p = 16, ni = 0;
  for (let d = 0; d < dirs.length; d++, ni++) {   /* ディレクトリは位置も長さも 0 */
    dv.setUint32(p, 1, true);
    dv.setUint32(p + 4, names[ni], true);
    p += 16;
  }
  for (const f of files) {
    dv.setUint32(p, 2, true);
    dv.setUint32(p + 4, names[ni++], true);
    dv.setUint32(p + 8, f.lba, true);             /* 位置はセクタ単位 */
    dv.setUint32(p + 12, f.size, true);
    p += 16;
  }
  buf.set(nameBuf, recEnd);
  return buf;
}

const dfiFiles = [];
let lba = 4;
for (let i = 0; i < 40; i++) {
  const size = 3000 + i * 811;
  dfiFiles.push({ name: `nik${String(i).padStart(3, "0")}.tm2`, lba, size });
  lba += Math.ceil(size / 2048);
}
dfiFiles.push({ name: "system.msg", lba, size: 40000 });
lba += Math.ceil(40000 / 2048);
const dfi = buildDfi(dfiFiles, ["00diary", "system", "insect"]);
const dfiDataSize = lba * 2048;

const nc = named.analyzeNamedIndex(dfi, dfiDataSize);
if (!nc.length) fail("名前つき索引の候補が 1 件も出ない");
const nbest = nc[0];
/* 先頭を飛ばす量と列の位置は、丸ごと 1 レコードずらしても同じ読み方になります
   (ヘッダを 1 件目として数えるかどうかの違い)。だから位置そのものではなく、
   **列の並びと単位、そして読み出した結果**を固定します。 */
if (nbest.rec !== 16) fail(`レコード長が ${nbest.rec} (期待 16)`);
if (nbest.atField !== nbest.nameField + 4) fail("名前の次が位置の列になっていない");
if (nbest.lenField !== nbest.atField + 4) fail("位置の次が長さの列になっていない");
if (nbest.atMult !== 2048) fail(`位置の単位が ${nbest.atMult} (期待 2048 = セクタ)`);
if (nbest.lenMult !== 1) fail(`長さの単位が ${nbest.lenMult} (期待 1 = バイト)`);
if (nbest.files !== dfiFiles.length) fail(`件数が ${nbest.files} (期待 ${dfiFiles.length})`);

const nitems = named.namedEntries(dfi, nbest, dfiDataSize, 4096);
if (nitems.length !== dfiFiles.length) fail(`読み出せた件数が ${nitems.length}`);
for (let i = 0; i < dfiFiles.length; i++) {
  if (nitems[i].name !== dfiFiles[i].name) {
    fail(`#${i} の名前が ${nitems[i].name} (期待 ${dfiFiles[i].name})`);
  }
  if (nitems[i].at !== dfiFiles[i].lba * 2048) fail(`#${i} の位置が違う`);
  if (nitems[i].len !== dfiFiles[i].size) fail(`#${i} の長さが違う`);
}
/* ディレクトリ (位置も長さも 0) をファイルとして数えないこと */
if (nitems.some((it) => ["00diary", "system", "insect"].includes(it.name))) {
  fail("ディレクトリをファイルとして拾っている");
}

/* 名前でないものを名前と言わないこと */
const junk = new Uint8Array(4096);
for (let i = 0; i < junk.length; i++) junk[i] = (i * 97 + 13) & 0xFF;
if (named.analyzeNamedIndex(junk, dfiDataSize).length) {
  fail("無関係なデータから名前つき索引を作り出している");
}

console.log(`OK  名前つき索引 = レコード${nbest.rec} ヘッダ${nbest.skip} `
  + `名前+${nbest.nameField} 位置+${nbest.atField}(x${nbest.atMult}) 長さ+${nbest.lenField} `
  + `${nbest.files} 件`);

/* 長さもセクタ単位で持つ索引 (単位を決め打ちすると当たらない形) */
function buildDfiSectorLen(files, dirs) {
  const recCount = files.length + dirs.length;
  const recEnd = 16 + recCount * 16;
  const names = [];
  let nameOff = recEnd;
  const nameBuf = [];
  for (const n of [...dirs, ...files.map((f) => f.name)]) {
    names.push(nameOff);
    for (const ch of n) nameBuf.push(ch.charCodeAt(0));
    nameBuf.push(0);
    nameOff += n.length + 1;
  }
  const buf = new Uint8Array(recEnd + nameBuf.length);
  const dv = new DataView(buf.buffer);
  buf.set([0x44, 0x46, 0x49, 0x00], 0);
  dv.setUint32(4, 0x00000100, true);
  let p = 16, ni = 0;
  for (let d = 0; d < dirs.length; d++, ni++) {
    dv.setUint32(p, 1, true);
    dv.setUint32(p + 4, names[ni], true);
    p += 16;
  }
  for (const f of files) {
    dv.setUint32(p, 2, true);
    dv.setUint32(p + 4, names[ni++], true);
    dv.setUint32(p + 8, f.lba, true);
    dv.setUint32(p + 12, f.sectors, true);      /* 長さもセクタ数 */
    p += 16;
  }
  buf.set(nameBuf, recEnd);
  return buf;
}

const secFiles = [];
let slba = 4;
for (let i = 0; i < 40; i++) {
  const sectors = 2 + (i % 7);
  secFiles.push({ name: `map${String(i).padStart(3, "0")}.bin`, lba: slba, sectors });
  slba += sectors;
}
const dfi2 = buildDfiSectorLen(secFiles, ["data", "map"]);
const nc2 = named.analyzeNamedIndex(dfi2, slba * 2048);
if (!nc2.length) fail("長さがセクタ単位の索引で候補が出ない");
if (nc2[0].lenMult !== 2048) fail(`長さの単位が ${nc2[0].lenMult} (期待 2048 = セクタ)`);
const items2 = named.namedEntries(dfi2, nc2[0], slba * 2048, 4096);
if (items2.length !== secFiles.length) fail(`件数が ${items2.length}`);
for (let i = 0; i < secFiles.length; i++) {
  if (items2[i].name !== secFiles[i].name) fail(`#${i} の名前が ${items2[i].name}`);
  if (items2[i].len !== secFiles[i].sectors * 2048) fail(`#${i} の長さが ${items2[i].len}`);
}
console.log(`OK  長さがセクタ単位の索引も当てられる (${items2.length} 件)`);

/* 名前を持たないレコード (名前の位置が 0) が混ざっていても壊れないこと。
   0 番地には "DFI" があるので、素朴に最小値を取ると判定が吹き飛ぶ */
const poisoned = buildDfi(dfiFiles, ["00diary", "system", "insect"]);
new DataView(poisoned.buffer).setUint32(16 + 4, 0, true);        /* 1 件目の名前を 0 に */
new DataView(poisoned.buffer).setUint32(16 + 5 * 16 + 4, 0, true);
const nc3 = named.analyzeNamedIndex(poisoned, dfiDataSize);
if (!nc3.length) fail("名前を持たないレコードが混ざると候補が出なくなる");
const items3 = named.namedEntries(poisoned, nc3[0], dfiDataSize, 4096);
if (items3.length !== dfiFiles.length) fail(`件数が ${items3.length} (期待 ${dfiFiles.length})`);
if (items3[0].name !== dfiFiles[0].name) fail(`名前がずれている: ${items3[0].name}`);
console.log("OK  名前を持たないレコードが混ざっても読める");

/* ---------- 実物と同じ形 ("DFI") ---------- */

/* 実物の BOKU2.IDX を忠実に真似る。効いてくる癖が 3 つある。
     - ディレクトリとファイルで先頭 4 バイトの形が違う
     - 名前の位置は、走りの中では後ろから前へ下がっていく
     - 同じ作りのデータが並ぶ区間では **長さが数種類しかない**
   3 つ目でいちど正解を落とした (長さの値のばらけ方で弾いていた)。 */
function buildRealDfi(dirCount, fileCount) {
  const recEnd = 16 + (dirCount + fileCount) * 16;
  const nameBuf = [];
  const offs = new Map();
  const push = (n) => {
    offs.set(n, recEnd + nameBuf.length);
    for (const ch of n) nameBuf.push(ch.charCodeAt(0));
    nameBuf.push(0);
  };
  for (let i = 0; i < dirCount; i++) push(`dir${String(i).padStart(4, "0")}`);
  for (let i = 0; i < fileCount; i++) push(String(i).padStart(4, "0"));

  const buf = new Uint8Array(recEnd + nameBuf.length);
  const dv = new DataView(buf.buffer);
  buf.set([0x44, 0x46, 0x49, 0x00], 0);
  dv.setUint32(4, 0x00000100, true);
  let p = 16;
  for (let i = 0; i < dirCount; i++) {
    dv.setUint16(p, 1, true);
    dv.setUint16(p + 2, i, true);
    dv.setUint32(p + 4, offs.get(`dir${String(i).padStart(4, "0")}`), true);
    p += 16;
  }
  const want = [];
  let lba = 16;
  for (let i = 0; i < fileCount; i++) {
    /* 走りの中で名前の位置が下がっていく並べ方 */
    const block = Math.floor(i / 24) * 24;
    const j = block + 24 <= fileCount ? block + (23 - (i % 24)) : i;
    const name = String(j).padStart(4, "0");
    const size = (i % 3) ? 355360 : 401392;         /* 長さは 2 種類だけ */
    dv.setUint16(p, 0, true);
    dv.setUint16(p + 2, 1, true);
    dv.setUint32(p + 4, offs.get(name), true);
    dv.setUint32(p + 8, lba, true);
    dv.setUint32(p + 12, size, true);
    want.push({ name, at: lba * 2048, len: size });
    lba += Math.ceil(size / 2048);
    p += 16;
  }
  buf.set(nameBuf, recEnd);
  return { buf, want, dataSize: lba * 2048 };
}

const real = buildRealDfi(120, 1900);

/* 分かっている形は当てにいかず、そのまま読む */
const dfiCand = named.readDfi(real.buf, real.dataSize);
if (!dfiCand) fail("DFI として読めない");
if (dfiCand.known !== "DFI") fail("既知の形式として印が付いていない");
if (dfiCand.files !== real.want.length) fail(`件数が ${dfiCand.files} (期待 ${real.want.length})`);

const got = named.namedEntries(real.buf, dfiCand, real.dataSize, 4096);
if (got.length !== real.want.length) fail(`読み出した件数が ${got.length}`);
for (let i = 0; i < real.want.length; i++) {
  if (got[i].name !== real.want[i].name) fail(`#${i} の名前が ${got[i].name} (期待 ${real.want[i].name})`);
  if (got[i].at !== real.want[i].at) fail(`#${i} の位置が ${got[i].at}`);
  if (got[i].len !== real.want[i].len) fail(`#${i} の長さが ${got[i].len}`);
}

/* 目印を知らなくても、総当たりで同じ答えにたどり着けること */
const blind = named.analyzeNamedIndex(real.buf, real.dataSize);
if (!blind.length) fail("総当たりで候補が出ない");
const bg = named.namedEntries(real.buf, blind[0], real.dataSize, 4096);
if (bg.length !== real.want.length) fail(`総当たりの件数が ${bg.length}`);
for (let i = 0; i < real.want.length; i++) {
  if (bg[i].name !== real.want[i].name || bg[i].at !== real.want[i].at
      || bg[i].len !== real.want[i].len) {
    fail(`総当たりの #${i} が食い違う: ${bg[i].name} @${bg[i].at} len${bg[i].len}`);
  }
}

/* DFI でないものを DFI として読まないこと */
if (named.readDfi(junk, real.dataSize)) fail("無関係なデータを DFI として読んでいる");

console.log(`OK  DFI をそのまま読める (${got.length} 件) / 総当たりでも同じ答えになる`);

