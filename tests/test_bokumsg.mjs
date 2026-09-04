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
  src.slice(s, e) + "\nreturn { parseBokuMsg, detectBokuMsg, bokuMsgText, parseBokuMsgTables, parseBokuMap };")(u32le, u16le);

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
if (m.parseBokuMap(msg8)) fail(".msg を入れ物と誤認した");
if (m.parseBokuMap(sjis)) fail("テキストを入れ物と誤認した");

console.log("OK  .msg 8 バイト刻み / 4 バイト刻み / 制御コード / 表が複数の会話ファイル / マップの入れ物 / 誤認しない");
