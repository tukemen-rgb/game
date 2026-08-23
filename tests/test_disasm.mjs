/* MIPS 逆アセンブラの回帰テスト。web/app.js から該当部分だけ取り出して Node で動かす。
 *
 * 三段構えです。
 *   1. capstone の答え (tests/mips_cases.json) と 1 命令ずつ突き合わせる
 *   2. オペランドの書き方を、手で書いた期待値で固定する
 *   3. 練習用の本体プログラム work/BOOT.ELF を端から端まで読ませる
 *
 * 自分で書いた逆アセンブラは、自分では正しさを確かめられません。
 * 外の実装と突き合わせるのが唯一まともな検証方法です。
 */
import fs from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const start = src.indexOf("/* @extract-start mips */");
const end = src.indexOf("/* @extract-end mips */");
if (start < 0 || end < 0) {
  console.error("app.js に @extract-start/@extract-end mips がありません");
  process.exit(2);
}

/* app.js の外にある小道具だけを渡して、逆アセンブラ部分を切り出す */
const hex = (n, pad = 0) => n.toString(16).toUpperCase().padStart(pad, "0");
const u32le = (b, p) => (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
const SJIS_LEAD = (b) => (b >= 0x81 && b <= 0x9F) || (b >= 0xE0 && b <= 0xEF);
const SJIS_TRAIL = (b) => b >= 0x40 && b <= 0xFC && b !== 0x7F;
const DECODERS = { sjis: new TextDecoder("shift_jis"), utf8: new TextDecoder("utf-8") };
const ascii = (bytes) => {
  let s = "";
  for (const b of bytes) s += b >= 0x20 && b < 0x7F ? String.fromCharCode(b) : " ";
  return s;
};

const mips = new Function("hex", "u32le", "SJIS_LEAD", "SJIS_TRAIL", "DECODERS", "ascii",
  src.slice(start, end) +
  "\nreturn { decodeMips, readElf, vaddrToOffset, stringAt, disassemble };")(
  hex, u32le, SJIS_LEAD, SJIS_TRAIL, DECODERS, ascii);

const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };

/* ---------- 1. capstone との突き合わせ ---------- */

/* capstone は MIPS32 として読むので、R5900 独自のオペコードだけは答えが違って
   当たり前です。lq / sq (128 ビット転送) と lqc2 / sqc2 (VU0 との転送) の 4 つ。 */
const R5900_ONLY = new Set([0x1E, 0x1F, 0x36, 0x3E]);

const golden = JSON.parse(fs.readFileSync(path.join(repo, "tests", "mips_cases.json"), "utf8"));
let matched = 0, notImpl = 0, r5900 = 0;
const wrong = [];
for (const [word, want] of golden.cases) {
  const w = word >>> 0;
  const got = mips.decodeMips(w, golden.addr);
  if (got.mn === want) { matched++; continue; }
  if (R5900_ONLY.has(w >>> 26)) { r5900++; continue; }
  /* 知らない命令を ".word" と出すのは正しい態度。嘘の命令名を出すのが駄目 */
  if (got.mn === ".word") { notImpl++; continue; }
  wrong.push(`0x${hex(w, 8)}  capstone=${want}  こちら=${got.mn}`);
}
if (wrong.length) {
  for (const line of wrong.slice(0, 20)) console.error("  " + line);
  fail(`capstone と食い違う命令が ${wrong.length} 件`);
}
if (matched < golden.cases.length * 0.8) {
  fail(`一致が ${matched} 件しかない (全 ${golden.cases.length} 件)`);
}

/* ---------- 2. オペランドの書き方 ---------- */

const OPERANDS = [
  [0x27BDFFE0, "addiu", "$sp, $sp, -0x20"],
  [0xAFBF001C, "sw", "$ra, 0x1C($sp)"],
  [0x8FBF001C, "lw", "$ra, 0x1C($sp)"],
  [0x3C040010, "lui", "$a0, 0x10"],
  [0x24840414, "addiu", "$a0, $a0, 0x414"],
  [0x0C040040, "jal", "0x00100100"],
  [0x00000000, "nop", ""],
  [0x00408021, "move", "$s0, $v0"],
  [0x03E00008, "jr", "$ra"],
  [0x90880000, "lbu", "$t0, 0($a0)"],
  [0x11000005, "beqz", "$t0, 0x00100018"],
  [0x14400005, "bnez", "$v0, 0x00100018"],
  [0x10000005, "b", "0x00100018"],
  [0x08040061, "j", "0x00100184"],
  [0x0004102A, "slt", "$v0, $zero, $a0"],
  [0x00641821, "addu", "$v1, $v1, $a0"],
  [0x00042080, "sll", "$a0, $a0, 2"],
  [0x34840100, "ori", "$a0, $a0, 0x100"],
  [0x0064001A, "div", "$v1, $a0"],
  [0x0000000D, "break", "0x0"],
  [0x0080000D, "break", "0x20000"],
  [0x00A00034, "teq", "$a1, $zero"],
  [0x40046000, "mfc0", "$a0, $12"],
  [0x44840000, "mtc1", "$a0, $f0"],
  [0x46000021, "cvt.d.s", "$f0, $f0"],
  [0x46020000, "add.s", "$f0, $f0, $f2"],
  [0x4600003C, "c.lt.s", "$f0, $f0"],
  [0x70641018, "mult1", "$v1, $a0"],
];
for (const [word, mn, ops] of OPERANDS) {
  const got = mips.decodeMips(word >>> 0, 0x00100000);
  if (got.mn !== mn) fail(`0x${hex(word, 8)} のニーモニックが ${got.mn} (期待 ${mn})`);
  if (got.ops !== ops) fail(`0x${hex(word, 8)} (${mn}) のオペランドが「${got.ops}」(期待「${ops}」)`);
}

/* ---------- 3. 本体プログラムを端から端まで ---------- */

const elfPath = path.join(repo, "work", "BOOT.ELF");
if (!fs.existsSync(elfPath)) {
  console.error("work/BOOT.ELF がありません。python3 tools/make_elf.py を先に実行してください");
  process.exit(2);
}
const buf = new Uint8Array(fs.readFileSync(elfPath));
const elf = mips.readElf(buf);
if (!elf) fail("BOOT.ELF を ELF として読めない");
if (elf.machine !== 8) fail(`e_machine が ${elf.machine} (期待 8 = MIPS)`);
if (elf.entry !== 0x00100000) fail(`入口が 0x${hex(elf.entry, 8)} (期待 0x00100000)`);
const loads = elf.segments.filter((s) => s.type === 1);
if (loads.length !== 1) fail(`PT_LOAD が ${loads.length} 個 (期待 1)`);
if (mips.vaddrToOffset(elf, 0x00100000) !== 0x1000) fail("番地 → ファイル位置の対応が違う");
if (mips.vaddrToOffset(elf, 0x00000000) !== -1) fail("範囲外の番地に位置を返している");

const names = elf.sections.map((s) => s.name);
for (const want of [".text", ".rodata"]) {
  if (!names.includes(want)) fail(`セクション ${want} が読めていない (${names.join(",")})`);
}

/* 入口から読んで、lui + addiu の組から文字列が復元できていること */
const lines = mips.disassemble(buf, elf, elf.entry, 32);
if (lines.length !== 32) fail(`32 命令読めていない (${lines.length} 命令)`);
if (lines[0].mn !== "addiu" || lines[0].ops !== "$sp, $sp, -0x20") {
  fail(`入口の 1 命令目が ${lines[0].mn} ${lines[0].ops}`);
}
const notes = lines.filter((l) => l.note).map((l) => l.note);
const wantNotes = [
  '→ 0x00100414  "cdrom0:\\BOKU2.IDX;1"',
  '→ 0x00100400  "cdrom0:\\BOKU2.IMG;1"',
];
for (const w of wantNotes) {
  if (!notes.includes(w)) fail(`文字列の注記が出ていない: ${w}\n  出たのは ${JSON.stringify(notes)}`);
}
/* 呼び出し先が拾えていること (クリックで飛べる先) */
const calls = lines.filter((l) => l.kind === "call").map((l) => l.target);
if (!calls.includes(0x00100100)) fail(`jal の行き先が拾えていない (${calls.map((t) => hex(t, 8))})`);

/* 参照されていない文字列を、参照されていると言わないこと */
const all = mips.disassemble(buf, elf, elf.entry, 0x400 / 4);
const seen = all.filter((l) => l.note && l.note.includes('"'))
  .map((l) => l.note.slice(l.note.indexOf('"')));
if (seen.some((s) => s.includes("MAP/NATSU00.PAK") || s.includes("BGM/TITLE.VAG"))) {
  fail("どこからも参照されていない文字列を参照済みとして出している");
}
if (new Set(seen).size !== 4) {
  fail(`参照している文字列が ${new Set(seen).size} 種類 (期待 4)`);
}

console.log(`OK  capstone と一致 ${matched} / 未実装 ${notImpl} / R5900 独自 ${r5900}`
  + `  ·  オペランド ${OPERANDS.length} 件  ·  BOOT.ELF の文字列参照 4 件`);
