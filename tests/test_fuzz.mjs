/* ブラウザ側の各読み取り (web/app.js) が、壊れたデータで例外を投げないことを、
 * 乱数の種を固定して何通りも確かめる (tools 側の TestDamagedData と同じ考え)。
 *
 * 画面では、読み取りが例外で落ちると「何も出ない」だけで、利用者には理由が分からない。
 * だから読めないときは null / 空を返すこと (投げないこと) を、ここで固定する。 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const block = (name) => {
  const s = src.indexOf(`/* @extract-start ${name} */`);
  const e = src.indexOf(`/* @extract-end ${name} */`);
  if (s < 0 || e < 0) { console.error(`app.js に ${name} マーカーが無い`); process.exit(2); }
  return src.slice(s, e);
};
const u32le = (b, p) => (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
const u16le = (b, p) => b[p] | (b[p + 1] << 8);
const ascii = (bytes) => { let s = ""; for (const b of bytes) s += b >= 0x20 && b < 0x7F ? String.fromCharCode(b) : " "; return s; };
const SJIS_LEAD = (b) => (b >= 0x81 && b <= 0x9F) || (b >= 0xE0 && b <= 0xEF);
const SJIS_TRAIL = (b) => b >= 0x40 && b <= 0xFC && b !== 0x7F;

const msg = new Function("u32le", "u16le", block("bokumsg")
  + "\nreturn { parseBokuMsg, detectBokuMsg, bokuMsgText, parseBokuMsgTables, parseBokuMap, bokuMsgTsv, bokuMsgUsed, parseBokuMsgRaw, parseSjisList };")(u32le, u16le);
const named = new Function("u32le", "ascii", block("named-index")
  + "\nreturn { readDfi, analyzeNamedIndex, namedEntries };")(u32le, ascii);
const tim2 = new Function("u32le", "u16le", block("tim2")
  + "\nreturn { findTim2, parseTim2, decodeTim2 };")(u32le, u16le);
const sniff = new Function(block("sniff") + "\nreturn { sniffKind };")();
const lzss = new Function("SJIS_LEAD", "SJIS_TRAIL", block("lzss") + "\nreturn { lzssScan };")(SJIS_LEAD, SJIS_TRAIL);

const fail = (m) => { console.error("NG: " + m); process.exit(1); };
const sjisDecode = (bytes) => new TextDecoder("shift_jis").decode(bytes);
const glyphs = Array.from("あいうえおかきくけこ");

/* ---------- 正しい形の入力を組み立てる (test_bokumsg.mjs / test_index.mjs と同じ規則) ---------- */
function buildMsg(entries, stride) {
  const n = entries.length, tab = 4 + n * stride;
  const bodies = entries.map((codes) => { const b = new Uint8Array(codes.length * 2); codes.forEach((c, i) => { b[i * 2] = c & 255; b[i * 2 + 1] = c >> 8; }); return b; });
  const buf = new Uint8Array(tab + bodies.reduce((a, b) => a + b.length, 0));
  const dv = new DataView(buf.buffer);
  dv.setUint32(0, n, true);
  let p = tab;
  entries.forEach((codes, i) => { dv.setUint32(4 + i * stride, codes.length ? p : 0, true); buf.set(bodies[i], p); p += bodies[i].length; });
  return buf;
}
function buildTables(tables) {
  const head = 4 + tables.length * 12, bodies = tables.map((t) => buildMsg(t, 4));
  const buf = new Uint8Array(head + bodies.reduce((a, b) => a + b.length, 0));
  const dv = new DataView(buf.buffer);
  dv.setUint32(0, tables.length, true);
  let p = head;
  bodies.forEach((body, i) => { dv.setUint32(4 + i * 12, 0xDEAD, true); dv.setUint16(8 + i * 12, body.length, true); dv.setUint16(10 + i * 12, 100 + i, true); dv.setUint32(12 + i * 12, p, true); buf.set(body, p); p += body.length; });
  return buf;
}
function buildMap(parts, rec) {
  const n = parts.length, headLen = Math.ceil((4 + n * rec) / 16) * 16;
  let total = headLen;
  const offs = parts.map((p) => { if (!p) return 0; const o = total; total += Math.ceil(p.length / 16) * 16; return o; });
  const buf = new Uint8Array(total);
  const dv = new DataView(buf.buffer);
  dv.setUint32(0, n, true);
  parts.forEach((p, i) => { if (!p) return; dv.setUint32(4 + i * rec, offs[i], true); dv.setUint32(8 + i * rec, p.length, true); buf.set(p, offs[i]); });
  return buf;
}
function buildDfi(dirCount, filesPerDir) {
  const recs = [{ dir: true, more: 1, name: "/" }];
  let lba = 16;
  for (let d = 0; d < dirCount; d++) {
    recs.push({ dir: true, more: d === dirCount - 1 ? 0 : 1, name: `dir${d}` });
    for (let i = 0; i < filesPerDir; i++) {
      const size = 1000 + i * 37;
      recs.push({ dir: false, more: i === filesPerDir - 1 ? 0 : 1, name: `f${i}.msg`, lba, size });
      lba += Math.ceil(size / 2048);
    }
  }
  const recEnd = 16 + recs.length * 16;
  const nameBuf = [];
  for (const r of recs) { for (const ch of r.name) nameBuf.push(ch.charCodeAt(0)); nameBuf.push(0); }
  const buf = new Uint8Array(recEnd + nameBuf.length);
  const dv = new DataView(buf.buffer);
  buf.set([0x44, 0x46, 0x49, 0x00], 0);
  dv.setUint32(4, 0x100, true);
  recs.forEach((r, k) => {
    const p = 16 + k * 16;
    dv.setUint16(p, r.dir ? 1 : 0, true); dv.setUint16(p + 2, r.more, true);
    dv.setUint32(p + 4, 0x8130 - k * 7, true);
    dv.setUint32(p + 8, r.dir ? 0 : r.lba, true); dv.setUint32(p + 12, r.dir ? 0 : r.size, true);
  });
  buf.set(nameBuf, recEnd);
  return { idx: buf, dataSize: lba * 2048 };
}
const entries = [[5, 6, 0x8001, 7, 0x8000], [], [0x8002, 0x12, 9, 0x8000, 0xCDCD], [0, 1, 0x8000], [0x3130, 0x3332, 0x3534, 0x3736]];
const talk = buildTables([entries, [[0, 0x8000]], [[2, 3, 0x8000]]]);
const rawText = Uint8Array.from([5, 0, 6, 0, 0, 0x80, 7, 0, 8, 0, 0, 0x80]);
const sjisList = Uint8Array.from([...new TextEncoder().encode("abc"), 0]);   /* 実際は SJIS だが、壊す元としては何でもよい */
const hexToBytes = (h) => Uint8Array.from(h.match(/../g).map((x) => parseInt(x, 16)));
const tim2Cases = JSON.parse(execFileSync("python3", [path.join(repo, "tools", "make_tim2.py"), "--json"], { encoding: "utf8" }));
const dfi = buildDfi(5, 6);

const seeds = [
  { name: "msg8", data: buildMsg(entries, 8) },
  { name: "msg4", data: buildMsg(entries, 4) },
  { name: "tables", data: talk },
  { name: "map8", data: buildMap([new Uint8Array(40).fill(0x11), talk, null, new Uint8Array(3).fill(0x22)], 8) },
  { name: "map12", data: buildMap([new Uint8Array(40).fill(0x11), null, talk], 12) },
  { name: "raw", data: rawText },
  { name: "sjis", data: sjisList },
  { name: "dfi", data: dfi.idx, dataSize: dfi.dataSize },
  ...tim2Cases.map((c) => ({ name: `tim2:${c.name}`, data: hexToBytes(c.tim2) })),
];

/* ---------- 壊し方 (種を固定した乱数) ---------- */
function rng(seed) { let a = seed >>> 0; return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
const MUTATIONS = {
  truncate: (b, r) => b.slice(0, Math.floor(r() * b.length)),
  flip: (b, r) => { const o = b.slice(); for (let k = 0; k < Math.max(1, o.length >> 6); k++) o[Math.floor(r() * o.length)] ^= 1 + Math.floor(r() * 255); return o; },
  zero: (b, r) => { const o = b.slice(); const a = Math.floor(r() * o.length); o.fill(0, a, a + 1 + Math.floor(r() * 64)); return o; },
  empty: () => new Uint8Array(0),
  garbage_head: (b, r) => { const o = b.slice(); for (let k = 0; k < Math.min(48, o.length); k++) o[k] = Math.floor(r() * 256); return o; },
  huge_count: (b) => { const o = b.slice(); if (o.length >= 4) { o[0] = 0xFF; o[1] = 0xFF; o[2] = 0xFF; o[3] = 0x7F; } return o; },
  random: (b, r) => Uint8Array.from({ length: Math.min(4096, b.length * 2 + 16) }, () => Math.floor(r() * 256)),
};

/* ---------- 全部の読み取りを、投げないことだけ見て呼ぶ ---------- */
function exercise(b, dataSize, label) {
  const calls = {
    detectBokuMsg: () => { const r = msg.detectBokuMsg(b); if (r) for (const it of r.items) { msg.bokuMsgText(it.codes, glyphs); msg.bokuMsgText(it.codes, null, true); } if (r) { msg.bokuMsgTsv(r.items, glyphs); msg.bokuMsgUsed(r.items); } return r; },
    parseBokuMsg8: () => msg.parseBokuMsg(b, 8),
    parseBokuMsg4: () => msg.parseBokuMsg(b, 4),
    parseBokuMsgTables: () => { const r = msg.parseBokuMsgTables(b); if (r) for (const t of r.tables) if (t.msg) for (const it of t.msg.items) msg.bokuMsgText(it.codes, glyphs, true); return r; },
    parseBokuMap: () => msg.parseBokuMap(b),
    parseBokuMsgRaw: () => { const r = msg.parseBokuMsgRaw(b, 4000); if (r) for (const it of r.items || r) if (it && it.codes) msg.bokuMsgText(it.codes, glyphs); return r; },
    parseSjisList: () => msg.parseSjisList(b, sjisDecode),
    readDfi: () => { const r = named.readDfi(b, dataSize); if (r) named.namedEntries(b, r, dataSize, 50); return r; },
    analyzeNamedIndex: () => named.analyzeNamedIndex(b, dataSize),
    tim2: () => { const at = tim2.findTim2(b); if (at == null || at < 0) return null; const t = tim2.parseTim2(b, at); if (t) for (const pic of t.pictures || []) tim2.decodeTim2(b, pic); return t; },
    sniffKind: () => sniff.sniffKind(b.subarray(0, 64), "packed", b.length),
    lzssScan: () => (b.length < 8192 ? lzss.lzssScan(b, 16, 64) : null),
  };
  const got = {};
  for (const [name, fn] of Object.entries(calls)) {
    try { got[name] = fn() != null; }
    catch (err) { fail(`${label}: ${name} が例外を投げた: ${err && err.stack ? err.stack.split("\n").slice(0, 3).join(" | ") : err}`); }
  }
  return got;
}

let cases = 0;
const parsed = {};
for (const seed of seeds) {
  exercise(seed.data, seed.dataSize || seed.data.length * 4, `${seed.name} (壊す前)`);
  for (const [how, mut] of Object.entries(MUTATIONS)) {
    for (let k = 0; k < 6; k++) {
      const r = rng(cases * 7919 + k);
      const b = mut(seed.data, r);
      const got = exercise(b, seed.dataSize || b.length * 4, `${seed.name} を ${how} (${k})`);
      for (const [n, ok] of Object.entries(got)) if (ok) parsed[n] = (parsed[n] || 0) + 1;
      cases++;
    }
  }
}
/* 壊しても読めることがある (切り詰めなど) のは自然。全部読めなくなるのも自然。
   ここで見ているのは「投げない」ことだけだが、読み取りが一度も成功しない種類が
   あれば、呼び方 (引数) が間違っている恐れがあるので知らせる */
for (const n of ["detectBokuMsg", "parseBokuMsgTables", "parseBokuMap", "readDfi", "tim2", "sniffKind"]) {
  if (!parsed[n]) fail(`${n} が一度も読めていない (呼び方が違う?)`);
}
console.log(`OK: ${seeds.length} 種 × ${Object.keys(MUTATIONS).length} 通り × 6 = ${cases} 件の壊れたデータで、どの読み取りも例外を投げなかった`);
