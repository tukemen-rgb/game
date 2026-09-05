/* 僕の夏休み 2 の .msg 読み (web/app.js の bokumsg ブロック) の回帰テスト。
 * 形は Hilltop Works の公開ソース (MSG.py) で確認したもの。 */
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
  src.slice(s, e) + "\nreturn { parseBokuMsg, detectBokuMsg, bokuMsgText, parseBokuMsgTables, parseBokuMap, bokuMsgVoice, bokuMsgTsv, bokuMsgUsed, parseGlyphTable, glyphsToHexTable, parseBokuMsgRaw, parseSjisList };")(u32le, u16le);

const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };

/* 位置表の刻みを指定して .msg を組み立てる */
function buildMsg(entries, stride) {
  const n = entries.length;
  const tab = 4 + n * stride;
  const bodies = entries.map((codes) => {
    const b = new Uint8Array(codes.length * 2);
    codes.forEach((c, i) => { b[i * 2] = c & 255; b[i * 2 + 1] = c >> 8; });
    return b;
  });
  const total = tab + bodies.reduce((a, b) => a + b.length, 0);
  const buf = new Uint8Array(total);
  const dv = new DataView(buf.buffer);
  dv.setUint32(0, n, true);
  let p = tab;
  entries.forEach((codes, i) => {
    dv.setUint32(4 + i * stride, codes.length ? p : 0, true);
    if (stride === 8) dv.setUint32(8 + i * stride, 0x1234, true);
    buf.set(bodies[i], p);
    p += bodies[i].length;
  });
  return buf;
}

const glyphs = Array.from("あいうえおかきくけこ");
const entries = [
  [5, 6, 0x8001, 7, 0x8000],
  [],
  [0x8002, 0x12, 9, 0x8000, 0xCDCD],
  [0, 1, 0x8000],
];

/* 1. 8 バイト刻み (IMG 内の .msg) */
const msg8 = buildMsg(entries, 8);
const r8 = m.detectBokuMsg(msg8);
if (!r8) fail("8 バイト刻みを読めない");
if (r8.stride !== 8 || r8.count !== 4) fail(`刻み ${r8.stride} / 件数 ${r8.count}`);
if (r8.items[1].codes.length !== 0) fail("空の項目が空になっていない");
const texts = r8.items.map((it) => m.bokuMsgText(it.codes, glyphs));
if (texts[0] !== "かき\nく{END}") fail(`1 件目の復号が ${JSON.stringify(texts[0])}`);
if (texts[2] !== "{WAIT 18}こ{END}") fail(`3 件目の復号が ${JSON.stringify(texts[2])}`);
if (texts[3] !== "あい{END}") fail(`4 件目の復号が ${JSON.stringify(texts[3])}`);
/* 文字表が無ければ番号のまま */
if (m.bokuMsgText(entries[0], null) !== "[5][6]\n[7]{END}") fail("文字表なしの表示が違う");
/* 文字表が短ければ、はみ出た番号はそのまま */
if (m.bokuMsgText([9, 0x8000], Array.from("あ")) !== "[9]{END}") fail("短い文字表のはみ出しが違う");

/* 2. 4 バイト刻み (マップ内の表) */
const msg4 = buildMsg(entries, 4);
const r4 = m.detectBokuMsg(msg4);
if (!r4) fail("4 バイト刻みを読めない");
if (r4.count !== 4) fail(`4 バイト刻みの件数が ${r4.count}`);
if (m.bokuMsgText(r4.items[0].codes, glyphs) !== "かき\nく{END}") fail("4 バイト刻みの復号が違う");

/* 3. 読めないものを読まない */
const sjis = new TextEncoder().encode("これは普通のテキストです。".repeat(8));
if (m.detectBokuMsg(sjis)) fail("普通のテキストを .msg と誤認した");
const zeros = new Uint8Array(64);
if (m.detectBokuMsg(zeros)) fail("ゼロ埋めを .msg と誤認した");
const tim2 = new Uint8Array(64); tim2.set([0x54, 0x49, 0x4D, 0x32, 4, 0, 1, 0]);
if (m.detectBokuMsg(tim2)) fail("TIM2 を .msg と誤認した");
/* 位置が減る表は弾く */
const bad = msg8.slice();
const dv = new DataView(bad.buffer);
dv.setUint32(4, u32le(bad, 4 + 16), true);
dv.setUint32(4 + 16, 4 + 4 * 8, true);
if (m.parseBokuMsg(bad, 8)) fail("位置が減る表を通した");

/* 4. マップの会話ファイル (表が複数)。表の一覧 12 バイト × T の後に各表 */
function buildTables(tables) {
  const head = 4 + tables.length * 12;
  const bodies = tables.map((t) => buildMsg(t, 4));
  const total = head + bodies.reduce((a, b) => a + b.length, 0);
  const buf = new Uint8Array(total);
  const dv = new DataView(buf.buffer);
  dv.setUint32(0, tables.length, true);
  let p = head;
  bodies.forEach((body, i) => {
    dv.setUint32(4 + i * 12, 0xDEAD, true);          /* 不明 */
    dv.setUint16(8 + i * 12, body.length, true);      /* 表の長さ */
    dv.setUint16(10 + i * 12, 100 + i, true);         /* 番号? */
    dv.setUint16(12 + i * 12, p, true);               /* 表の位置 */
    buf.set(body, p);
    p += body.length;
  });
  return buf;
}
const mt = m.parseBokuMsgTables(buildTables([entries, [[0, 0x8000]], [[2, 3, 0x8000]]]));
if (!mt) fail("表が複数の会話ファイルを読めない");
if (mt.count !== 3 || mt.tables.filter((t) => t.msg).length !== 3) fail(`表の数が ${mt.count}`);
if (m.bokuMsgText(mt.tables[0].msg.items[0].codes, glyphs) !== "かき\nく{END}") fail("表 0 の復号が違う");
if (m.bokuMsgText(mt.tables[2].msg.items[0].codes, glyphs) !== "うえ{END}") fail("表 2 の復号が違う");
if (mt.tables[1].id !== 101) fail("表の番号が読めていない");
/* 単体の .msg を表の一覧と誤認しない、逆も */
if (m.parseBokuMsgTables(msg8)) fail("単体の .msg を表の一覧と誤認した");
if (m.detectBokuMsg(buildTables([entries]))) fail("表の一覧を単体の .msg と誤認した");

/* 5. マップの入れ物: u32 項目数 + (u32 位置, u32 長さ)。部品は 16 バイト揃え */
function buildMap(parts, rec) {
  const n = parts.length;
  const headLen = Math.ceil((4 + n * rec) / 16) * 16;
  let total = headLen;
  const offs = parts.map((p) => { if (!p) return 0; const o = total; total += Math.ceil(p.length / 16) * 16; return o; });
  const buf = new Uint8Array(total);
  const dv = new DataView(buf.buffer);
  dv.setUint32(0, n, true);
  parts.forEach((p, i) => {
    if (!p) return;
    dv.setUint32(4 + i * rec, offs[i], true);
    dv.setUint32(8 + i * rec, p.length, true);
    buf.set(p, offs[i]);
  });
  return buf;
}
const partA = new Uint8Array(40).fill(0x11);
const partText = buildTables([entries]);
const mapBuf = buildMap([partA, partText, null, new Uint8Array(3).fill(0x22)], 8);
const mp = m.parseBokuMap(mapBuf);
if (!mp) fail("マップの入れ物を読めない");
if (mp.count !== 4 || mp.rec !== 8) fail(`入れ物の項目数 ${mp.count} / 刻み ${mp.rec}`);
if (mp.items[2].len !== 0) fail("空の項目が空になっていない");
if (mp.items[1].len !== partText.length) fail("1 番の長さが違う");
const inner = mapBuf.subarray(mp.items[1].at, mp.items[1].at + mp.items[1].len);
if (!m.parseBokuMsgTables(inner)) fail("入れ物の 1 番から会話ファイルを読めない");
const mp12 = m.parseBokuMap(buildMap([partA, partText], 12));
if (!mp12 || mp12.rec !== 12) fail("12 バイト刻みの入れ物を読めない");
/* 後ろの項目が空でも、12 バイト刻みを 8 バイト刻みと誤認しない (部品が多く取れる方を採る) */
const mp12b = m.parseBokuMap(buildMap([partA, null, partText], 12));
if (!mp12b || mp12b.rec !== 12 || mp12b.items.filter((it) => it.len).length !== 2) fail(`空の項目を挟んだ 12 バイト刻みが ${mp12b && mp12b.rec}`);
if (m.parseBokuMap(msg8)) fail(".msg を入れ物と誤認した");
if (m.parseBokuMap(sjis)) fail("テキストを入れ物と誤認した");

/* 6. 音声の項目 (8 桁の数字がそのまま) と、校正ツール向けの TSV */
const voiceCodes = [0x3130, 0x3332, 0x3534, 0x3736];       /* "01234567" をリトルエンディアンで */
if (m.bokuMsgVoice(voiceCodes) !== "01234567") fail("音声の番号が読めない");
if (m.bokuMsgVoice([0x3130, 0x3332]) !== null) fail("4 桁を音声と誤認した");
if (m.bokuMsgVoice([5, 6, 7, 8]) !== null) fail("文字を音声と誤認した");
if (m.bokuMsgText(voiceCodes, glyphs) !== "{VOICE 01234567}") fail("音声の表示が違う");
if (m.bokuMsgText(entries[0], glyphs, true) !== "かき<BR>く") fail(`校正用の書き方が違う: ${m.bokuMsgText(entries[0], glyphs, true)}`);
if (m.bokuMsgText(entries[2], glyphs, true) !== "<WAIT:12>こ") fail(`待ち時間の書き方が違う: ${m.bokuMsgText(entries[2], glyphs, true)}`);
const tsv = m.bokuMsgTsv(r8.items, glyphs);
const lines = tsv.trimEnd().split("\n");
if (lines[0] !== "id\toffset\tsize\toriginal\ttranslation") fail("TSV の見出しが違う");
if (lines.length !== 4) fail(`TSV の行数が ${lines.length} (空の項目は除く)`);
if (lines[1] !== "0\t0x24\t10\tかき<BR>く\tかき<BR>く") fail(`TSV の 1 行目が違う: ${lines[1]}`);
if (lines.some((l) => l.split("\t").length !== 5)) fail("TSV の列数が揃っていない");
/* 音声の番号は TSV に入れない (校正の対象ではない) */
const withVoice = m.bokuMsgTsv([{ i: 0, at: 0x10, codes: voiceCodes }, { i: 1, at: 0x20, codes: entries[0] }], glyphs);
if (withVoice.trimEnd().split("\n").length !== 2) fail("音声の行を TSV に入れてしまった");
/* 7. 使われている文字番号 (制御コード・待ち時間の値・音声は除く) */
const used = m.bokuMsgUsed(r8.items.concat([{ i: 9, at: 0, codes: voiceCodes }]));
if (used.join(",") !== "0,1,5,6,7,9") fail(`使われている番号が ${used.join(",")}`);
/* 8. 文字表の 2 つの書き方: 並び / 番号=文字 の対応表 (使われている番号だけ書ける) */
const seq = m.parseGlyphTable("あいう\nえお");
if (seq.join("") !== "あいうえお") fail("並びの文字表が読めない");
const sparse = m.parseGlyphTable("5=か\n6 き\n7: く\n9＝こ\n\n=x\n");
if (sparse[5] !== "か" || sparse[6] !== "き" || sparse[7] !== "く" || sparse[9] !== "こ") fail("対応表の文字表が読めない");
if (sparse[8] !== undefined || sparse[0] !== undefined) fail("無い番号が空になっていない");
if (m.bokuMsgText(entries[0], sparse) !== "かき\nく{END}") fail("対応表で復号できない");
if (m.bokuMsgText([0, 5, 0x8000], sparse) !== "[0]か{END}") fail("無い番号が [番号] にならない");
/* 9. docs/01 の「16進=文字」テーブルにする (2 バイトのリトルエンディアン) */
const tbl = m.glyphsToHexTable(sparse).trimEnd().split("\n");
if (!tbl.includes("0500=か") || !tbl.includes("0900=こ")) fail(`テーブルの行が違う: ${tbl.slice(0, 3)}`);
if (tbl.some((l) => l.startsWith("0800="))) fail("無い番号をテーブルに入れた");
if (!tbl.includes("0080={END}") || !tbl.includes("0180=<BR>")) fail("制御コードがテーブルに無い");
const big = []; big[300] = "亜";
if (!m.glyphsToHexTable(big).includes("2C01=亜")) fail("256 以上の番号の並びが違う (下位, 上位)");
/* 10. 見出しの無い並び (0x8000 区切り)。日記の雛形・保存画面の文言 */
const rawCodes = [5, 6, 0x8001, 7, 0x8000, 0, 1, 0x8000, 0xCDCD];
const rawBuf = new Uint8Array(rawCodes.length * 2);
rawCodes.forEach((c, i) => { rawBuf[i * 2] = c & 255; rawBuf[i * 2 + 1] = c >> 8; });
const raw = m.parseBokuMsgRaw(rawBuf);
if (!raw || raw.count !== 2) fail(`見出しの無い並びが ${raw && raw.count} 件`);
if (m.bokuMsgText(raw.items[0].codes, glyphs) !== "かき\nく{END}" || m.bokuMsgText(raw.items[1].codes, glyphs) !== "あい{END}") fail("見出しの無い並びの復号が違う");
if (raw.items[1].at !== 10) fail(`2 件目の位置が ${raw.items[1].at}`);
if (m.parseBokuMsgRaw(sjis.subarray(0, sjis.length & ~1))) fail("普通のテキストを見出しの無い並びと誤認した");
if (m.parseBokuMsgRaw(tim2)) fail("TIM2 を見出しの無い並びと誤認した");
if (m.parseBokuMsgRaw(new Uint8Array([5, 0, 6, 0]))) fail("終わりの無い並びを通した");
/* 11. Shift-JIS の並び (0x00 区切り、文字表は不要)。保存画面の入れ物の 2 番 */
{
  const dec = (x) => new TextDecoder("shift_jis", { fatal: true }).decode(x);
  const sjBytes = Buffer.concat([Buffer.from("セーブしますか？", "utf8"), Buffer.from([0])]);  /* UTF-8 ではなく Shift-JIS が要る */
  const sjisOf = (str) => { /* Node には Shift-JIS の encoder が無いので、既知のバイト列を使う */
    const table = { "は": [0x82, 0xcd], "い": [0x82, 0xa2], "え": [0x82, 0xa6], "セ": [0x83, 0x5a], "ー": [0x81, 0x5b], "ブ": [0x83, 0x75], "？": [0x81, 0x48] };
    return Uint8Array.from([].concat(...Array.from(str).map((c) => table[c])));
  };
  const list = new Uint8Array([...sjisOf("セーブ？"), 0, ...sjisOf("はい"), 0, ...sjisOf("いいえ"), 0]);
  const sj = m.parseSjisList(list, dec);
  if (!sj || sj.count !== 3 || sj.items[1].text !== "はい" || sj.items[2].at !== 14) fail(`Shift-JIS の並びが ${sj && sj.items.map((x) => `${x.text}@${x.at}`)}`);
  if (m.parseSjisList(msg8, dec)) fail(".msg を Shift-JIS の並びと誤認した");
  if (m.parseSjisList(new Uint8Array([0x83, 0x5a, 0x83]), dec)) fail("壊れた Shift-JIS を通した");
  if (m.parseSjisList(sjisOf("はい"), dec)) fail("区切りの無い並びを通した");
  void sjBytes;
}
fs.mkdirSync(path.join(repo, "work"), { recursive: true });
fs.writeFileSync(path.join(repo, "work", "MSG_EXPORT.tsv"), tsv);

console.log("OK  .msg 8 バイト刻み / 4 バイト刻み / 制御コード / 表が複数の会話ファイル / マップの入れ物 / 音声 / 校正用 TSV / 誤認しない");
