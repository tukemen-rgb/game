/* 文字の番号振りの列数が、判っている 1 行 23 字と合うかの検査 (#166)。
 *
 * 「文字の番号を重ねる」は **画像の幅 ÷ 刻み** で列数を決めている。docs/09 の
 * 手順 3 も「列数は画像の幅から自動で決まる」と書いてある。実物の
 * `bk_font.tms` は幅 512 ドットなので 512/22 = 23 で合う——**合う画像なら**。
 *
 * この作品には 1024 ドット幅の画像が普通にある (公開ソースの書き出しを見ると
 * `config.tm2` `insect_base.tm2` `bumper.tm2` がそう)。それを渡すと 1 行 46 字で
 * 番号が振られ、文字表は丸ごとずれる。ところが **出てくる字は日本語のまま** なので、
 * 目では気づけない。#158 でフォントの升目を間違えたときと同じ型の壊れ方。
 *
 * ここで見るのは 4 つ:
 *
 *   1. 実物と練習データの幅では、何も言わない (要らない警告を出さない)
 *   2. 合わない幅では、**実際に何字になるか** と **本当は何字か** の両方を言う
 *   3. 広すぎる側と狭すぎる側で、言うことが違う (直し方が違うので)
 *   4. **1 枚で文字表がまかなえるか** (#167)。font.txt は 1656 字あるのに、
 *      bk_font.tms の頁は 1058 マスしかない。残り 598 字は別の画像にある
 */
import fs from "node:fs";
import path from "node:path";

const repo = path.resolve(import.meta.dirname, "..");
const src = fs.readFileSync(path.join(repo, "web", "app.js"), "utf8");
const s = src.indexOf("/* @extract-start tim2 */");
const e = src.indexOf("/* @extract-end tim2 */");
if (s < 0 || e < 0) { console.error("app.js に tim2 マーカーが無い"); process.exit(2); }

/* 定数は塊の外にあるので、app.js の字から読む。**写さない**——写すと
   app.js が変わっても検査だけが古い値で緑になる (#137) */
const constOf = (name) => {
  const m = src.match(new RegExp(`^const ${name} = (\\d+);`, "m"));
  if (!m) { console.error(`app.js に ${name} の定義が無い`); process.exit(2); }
  return Number(m[1]);
};
const FONT_COLS = constOf("FONT_COLS");
const FONT_CELL = constOf("FONT_CELL");
const u32le = (b, p) => (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
const u16le = (b, p) => b[p] | (b[p + 1] << 8);
const m = new Function("u32le", "u16le", "FONT_COLS", "FONT_CELL",
  src.slice(s, e) + "\nreturn { fontGridMismatch };")(u32le, u16le, FONT_COLS, FONT_CELL);

const fail = (msg) => { console.error("NG: " + msg); process.exit(1); };
/** 画面が列数を出すのと同じ式 (app.js の draw / draftBtn と同じ) */
const colsFor = (width, cell = FONT_CELL, ox = 0) => Math.max(1, Math.floor((width - ox) / cell));

let checked = 0;

/* ---- 1. 黙っているべき幅 ---- */
/* 512 = 実物の bk_font.tms (公開ソースの書き出し font1.png / font2.png が 512 幅)。
   506 = 練習データ。528 は 24 列に届く手前で、まだ 23 のままのはず */
for (const width of [506, 512, 527]) {
  const cols = colsFor(width);
  if (cols !== FONT_COLS) fail(`幅 ${width} で列数が ${cols} (前提が崩れた)`);
  const note = m.fontGridMismatch(cols, width, FONT_CELL);
  if (note !== "") fail(`幅 ${width} は 1 行 ${FONT_COLS} 字になるのに文句を言っている: ${note}`);
  checked++;
}

/* ---- 2. 広すぎる幅 ---- */
for (const width of [1024, 640, 768]) {
  const cols = colsFor(width);
  if (cols === FONT_COLS) fail(`幅 ${width} で列数が ${FONT_COLS} になった (検査にならない)`);
  const note = m.fontGridMismatch(cols, width, FONT_CELL);
  if (!note) fail(`幅 ${width} (1 行 ${cols} 字) で何も言わない`);
  /* **実際に何字になるか** と **本当は何字か** の両方が要る。
     片方だけだと、読んだ人が自分の画像の話だと分からない */
  for (const must of [String(cols), String(FONT_COLS), String(width), String(FONT_CELL)]) {
    if (!note.includes(must)) fail(`幅 ${width} の説明に ${must} が出てこない: ${note}`);
  }
  if (!note.includes("ずれ")) fail(`幅 ${width} の説明が、何が起きるかを言っていない: ${note}`);
  checked++;
}

/* ---- 3. 狭すぎる側とは言うことが違う ---- */
{
  const wide = m.fontGridMismatch(colsFor(1024), 1024, FONT_CELL);
  const narrow = m.fontGridMismatch(colsFor(220), 220, FONT_CELL);
  if (!narrow) fail("狭すぎる幅で何も言わない");
  if (wide === narrow) fail("広すぎる側と狭すぎる側で同じことを言っている (直し方が違う)");
  if (!wide.includes("2 枚分")) fail(`広すぎる側が、2 枚並びの見込みを言っていない: ${wide}`);
  if (!narrow.includes(`幅 ${FONT_COLS * FONT_CELL} ドットが要ります`)) {
    fail(`狭すぎる側が、要る幅を言っていない: ${narrow}`);
  }
  checked += 2;
}

/* ---- 4. 刻みの欄を変えたときも見ている ---- */
{
  /* 実物の幅のままでも、刻みに枠の 23 を打ち込むと 512/23 = 22 列になる。
     #51 で実際に踏んだ取り違え (刻み 22 と枠 23) が、そのまま画面で起きる形 */
  const cell = FONT_COLS;            /* 描く枠のほう。刻みと 1 ドット違う */
  const cols = colsFor(512, cell);
  if (cols === FONT_COLS) fail(`刻みに枠の ${cell} を入れても列数が変わらない (検査にならない)`);
  const note = m.fontGridMismatch(cols, 512, cell);
  if (!note) fail(`刻みを ${cell} にしたら 1 行 ${cols} 字になるのに、何も言わない`);
  if (!note.includes(String(cell))) fail(`打ち込んだ刻み ${cell} を言っていない: ${note}`);
  checked++;
}


/* ---- 5. 1 枚で文字表がまかなえるか (#167) ---- */
{
  const FONT_GLYPHS = constOf("FONT_GLYPHS");
  const m2 = new Function("u32le", "u16le", "FONT_COLS", "FONT_CELL", "FONT_GLYPHS",
    src.slice(s, e) + "\nreturn { fontPageShortfall };")(u32le, u16le, FONT_COLS, FONT_CELL, FONT_GLYPHS);
  /* 公開ソースの実物: font.txt は 72 行 × 23 = 1656 字。ところが bk_font.tms の頁は
     512×1024 ドットで 23 × 46 = 1058 マスしかない。差の 598 は向こうの font2.txt の
     字数とちょうど一致する —— **文字表は 2 枚に分かれている** */
  const page1 = Math.floor(512 / FONT_CELL) * Math.floor(1024 / FONT_CELL);
  if (page1 !== 1058) fail(`実物の頁が ${page1} マス (前提が崩れた)`);
  if (FONT_GLYPHS - page1 !== 598) fail(`足りない字数が ${FONT_GLYPHS - page1} (font2.txt は 598 字)`);

  /* 足りている画像には何も言わない */
  if (m2.fontPageShortfall(FONT_GLYPHS, -1) !== "") fail("ちょうど足りる画像に文句を言っている");
  if (m2.fontPageShortfall(FONT_GLYPHS + 100, 5) !== "") fail("余る画像に文句を言っている");
  checked += 2;

  /* 実物の頁: 足りない字数と、全体の字数の両方を言う */
  const note = m2.fontPageShortfall(page1, -1);
  if (!note) fail("実物の頁 (1058 マス) で何も言わない");
  for (const must of [String(page1), String(FONT_GLYPHS), "598"]) {
    if (!note.includes(must)) fail(`説明に ${must} が出てこない: ${note}`);
  }
  checked++;

  /* **本文がこの画像を越える番号を使っているとき**は、書き写しの間違いではないと言う。
     ここを言わないと、社長は 1058 字を書き写したあと自分の写しを疑う */
  const over = m2.fontPageShortfall(page1, 1600);
  if (!over.includes("1600")) fail(`本文が使う最大の番号を言っていない: ${over}`);
  if (!over.includes(String(page1 - 1))) fail(`この画像の番号の上限を言っていない: ${over}`);
  if (!over.includes("書き写しの間違いではありません")) {
    fail(`原因が書き写しでないことを言っていない: ${over}`);
  }
  if (over === note) fail("本文が越えていても、越えていなくても同じことを言っている");
  checked += 2;
}


/* ---- 6. 1 つのファイルから TIM2 を全部探す (#168) ---- */
{
  const m3 = new Function("u32le", "u16le", "FONT_COLS", "FONT_CELL",
    src.slice(s, e) + "\nreturn { tim2Pages, findTim2, parseTim2 };")(u32le, u16le, FONT_COLS, FONT_CELL);
  const { execFileSync } = await import("node:child_process");
  /* 練習データを作る道具で、1 行 23 字の頁を 2 枚こしらえる。**道具に作らせる**ので、
     TIM2 の組み立て方が変わってもこの検査は本物を見続ける */
  const mk = (rows) => {
    const hex = execFileSync("python3", ["-c", `
import sys, binascii
sys.path.insert(0, "tools")
import make_tim2, boku2
t, _ = make_tim2.font_sheet(rows=${rows}, cols=boku2.FONT_COLS, cell=boku2.FONT_CELL)
sys.stdout.write(binascii.hexlify(t).decode())
`], { encoding: "utf8", cwd: repo });
    return Uint8Array.from(hex.match(/../g).map((x) => parseInt(x, 16)));
  };
  const head = new Uint8Array(0x80);
  head.set([0x54, 0x4D, 0x53, 0x00, 0x80, 0, 0, 0]);   /* "TMS\0" + 前置き 0x80 */
  const p1 = mk(2), p2 = mk(3);
  const file = new Uint8Array(head.length + p1.length + p2.length);
  file.set(head, 0); file.set(p1, head.length); file.set(p2, head.length + p1.length);

  /* 今までの findTim2 は 1 枚目しか出さない —— それがこの直しの発端 */
  if (m3.findTim2(file) !== 0x80) fail(`findTim2 が ${m3.findTim2(file)} を返した (前提が崩れた)`);
  const pages = m3.tim2Pages(file);
  if (pages.length !== 2) fail(`頁が ${pages.length} 枚しか見つからない (2 枚あるはず)`);
  if (pages[0].at !== 0x80) fail(`1 枚目の位置が ${pages[0].at}`);
  if (pages[1].at !== head.length + p1.length) fail(`2 枚目の位置が ${pages[1].at}`);
  if (pages[0].t.pictures[0].height === pages[1].t.pictures[0].height) {
    fail("2 枚が同じ大きさ (作り分けられていないので検査にならない)");
  }
  checked += 2;

  /* たまたま "TIM2" の 4 バイトが並んだだけのものを拾わないこと。
     拾うと、社長に「ここにもう 1 枚あります」と嘘を教える */
  const junk = new Uint8Array(file.length + 64);
  junk.set(file, 0);
  junk.set([0x54, 0x49, 0x4D, 0x32, 0xFF, 0xFF, 0xFF, 0xFF], file.length + 8);
  if (m3.tim2Pages(junk).length !== 2) fail("見出しの読めない TIM2 もどきを頁に数えている");
  checked++;
}

if (checked < 17) fail(`確かめた場合が ${checked} 通りしかない`);
console.log(`OK (${checked} 通り)`);
