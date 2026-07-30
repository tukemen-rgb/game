/* 構造探査台 — ブラウザだけでゲームデータの構造を推定して可視化する.
 *
 * 読み込みはすべて File API で行い、どこにも送信しません。大きなイメージは
 * 全部をメモリに載せず、必要なところだけ slice して読みます。
 *
 * 構成
 *   1. 小道具            バイト列と数値の扱い
 *   2. ISO9660           ボリューム記述子からファイル一覧を取り出す
 *   3. バイトの性質       エントロピー・種類の判定・周期
 *   4. 構造の推定         文字列の並び / ポインタテーブル
 *   5. 文字コード         Shift-JIS / UTF-8 / 独自テーブル / 相対検索
 *   6. 状態
 *   7. 受け入れ (ドロップ)
 *   8. ファイル一覧と検出結果
 *   9. 全体マップ
 *  10. タブ (16進 / 文字列 / ポインタ表 / タイル / 相対検索 / 既知の形式)
 */
"use strict";

/* ======================= 1. 小道具 ======================= */

const SECTOR = 2048;
/** 深い解析をかける上限。これを超えるファイルは先頭だけを見る */
const ANALYZE_CAP = 24 * 1024 * 1024;
const MAP_CELLS = 1800;

const $ = (id) => document.getElementById(id);
const hex = (n, pad = 0) => n.toString(16).toUpperCase().padStart(pad, "0");
const hx = (n) => "0x" + hex(n);
const fmtBytes = (n) => n.toLocaleString("ja-JP") + " バイト";

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function parseOffset(text, fallback = 0) {
  const s = String(text).trim().replace(/^0x/i, "");
  const v = parseInt(s, 16);
  return Number.isFinite(v) && v >= 0 ? v : fallback;
}

async function readRange(file, offset, length) {
  const end = Math.min(file.size, offset + length);
  if (end <= offset) return new Uint8Array(0);
  return new Uint8Array(await file.slice(offset, end).arrayBuffer());
}

const u32le = (b, p) => (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
const u32be = (b, p) => ((b[p] << 24) | (b[p + 1] << 16) | (b[p + 2] << 8) | b[p + 3]) >>> 0;
const u16le = (b, p) => b[p] | (b[p + 1] << 8);
const u16be = (b, p) => (b[p] << 8) | b[p + 1];

function ascii(bytes) {
  let s = "";
  for (const b of bytes) s += b >= 0x20 && b < 0x7F ? String.fromCharCode(b) : " ";
  return s;
}

/* ======================= 2. ISO9660 ======================= */
/* 先頭 32KB のシステム領域のあと、セクタ 16 に基本ボリューム記述子が置かれ、
 * その中のルートディレクトリレコードから木を辿れる、という単純な形式です。 */

async function readIso(file) {
  if (file.size < 17 * SECTOR) return null;
  const pvd = await readRange(file, 16 * SECTOR, SECTOR);
  if (!(pvd[0] === 1 && pvd[1] === 0x43 && pvd[2] === 0x44 && pvd[3] === 0x30 &&
        pvd[4] === 0x30 && pvd[5] === 0x31)) return null;   // "CD001"

  const blockSize = u16le(pvd, 128) || SECTOR;
  const volumeId = ascii(pvd.subarray(40, 72)).trim();
  const totalSectors = u32le(pvd, 80);
  const root = pvd.subarray(156, 190);
  const rootLba = u32le(root, 2);
  const rootLen = u32le(root, 10);

  const entries = [];
  const seen = new Set();

  async function walk(lba, length, prefix, depth) {
    if (depth > 8 || seen.has(lba)) return;
    seen.add(lba);
    const dir = await readRange(file, lba * blockSize, length);
    let i = 0;
    const subdirs = [];
    while (i < dir.length) {
      const recLen = dir[i];
      if (recLen === 0) {                 // セクタの残りは詰め物。次のセクタへ
        i = (Math.floor(i / blockSize) + 1) * blockSize;
        if (i >= dir.length) break;
        continue;
      }
      const rec = dir.subarray(i, i + recLen);
      const extent = u32le(rec, 2);
      const size = u32le(rec, 10);
      const flags = rec[25];
      const nameLen = rec[32];
      const rawName = rec.subarray(33, 33 + nameLen);
      i += recLen;
      if (nameLen === 1 && (rawName[0] === 0 || rawName[0] === 1)) continue;  // "." ".."
      const name = ascii(rawName).trim().replace(/;\d+$/, "");
      if (flags & 0x02) {
        subdirs.push([extent, size, prefix + name + "/"]);
      } else {
        entries.push({
          path: prefix + name, name, offset: extent * blockSize, size,
          lba: extent, kind: "file",
        });
      }
    }
    for (const [lba2, len2, pre2] of subdirs) await walk(lba2, len2, pre2, depth + 1);
  }

  await walk(rootLba, rootLen, "/", 0);
  return { volumeId, blockSize, totalSectors, entries };
}

/* ======================= 3. バイトの性質 ======================= */

const CLASSES = {
  zero:  { label: "ゼロ埋め",   css: "--cls-zero" },
  ascii: { label: "ASCII テキスト", css: "--cls-ascii" },
  jp:    { label: "日本語テキスト", css: "--cls-jp" },
  tile:  { label: "タイル・コードなど", css: "--cls-tile" },
  wave:  { label: "波形 (音声など)", css: "--cls-wave" },
  high:  { label: "高エントロピー (圧縮・映像)", css: "--cls-high" },
};

const SJIS_LEAD = (b) => (b >= 0x81 && b <= 0x9F) || (b >= 0xE0 && b <= 0xEF);
const SJIS_TRAIL = (b) => b >= 0x40 && b <= 0xFC && b !== 0x7F;

function blockStats(whole) {
  if (!whole.length) return null;
  /* 末尾のゼロはセクタの詰め物なので除いて数える。ディスクイメージでは
     「80 バイトのテキスト + 1968 バイトの詰め物」が普通にあり、
     そのまま平均を取ると何もかもゼロ埋めに見えてしまう */
  let end = whole.length;
  while (end > 0 && whole[end - 1] === 0) end--;
  const padRatio = (whole.length - end) / whole.length;
  if (end < 16) return { n: whole.length, entropy: 0, zeroRatio: 1, printRatio: 0, pairRatio: 0, meanDiff: 0, padRatio };
  const b = whole.subarray(0, end);
  const n = b.length;
  const hist = new Uint32Array(256);
  let zeros = 0, printable = 0, diffSum = 0;
  for (let i = 0; i < n; i++) {
    const v = b[i];
    hist[v]++;
    if (v === 0) zeros++;
    if ((v >= 0x20 && v < 0x7F) || v === 0x0A || v === 0x0D || v === 0x09) printable++;
    if (i) diffSum += Math.abs(v - b[i - 1]);
  }
  let entropy = 0;
  for (let v = 0; v < 256; v++) {
    if (!hist[v]) continue;
    const p = hist[v] / n;
    entropy -= p * Math.log2(p);
  }
  let pairs = 0;
  for (let i = 0; i + 1 < n; i++) {
    if (SJIS_LEAD(b[i]) && SJIS_TRAIL(b[i + 1])) { pairs++; i++; }
  }
  return {
    n, entropy, padRatio,
    zeroRatio: zeros / n,
    printRatio: printable / n,
    pairRatio: (pairs * 2) / n,
    meanDiff: n > 1 ? diffSum / (n - 1) : 0,
  };
}

/**
 * 種類を決める。
 *
 * エントロピーのしきい値を固定値にしてはいけません。256 種類の値を n 個しか
 * 標本にしていないと、完全な乱数でもエントロピーは log2(256)=8 に届かず、
 * n=256 なら 7.3 程度にしかなりません。標本数から「乱数だったときの期待値」を
 * 出して、それと比べます。
 */
function classifyStats(s) {
  if (!s) return "zero";
  if (s.zeroRatio > 0.92) return "zero";
  if (s.pairRatio > 0.45) return "jp";
  if (s.printRatio > 0.85) return "ascii";
  /* 隣のバイトとの差が小さいものは波形。乱数の平均差は 85 前後になるので混ざらない */
  if (s.entropy > 4.5 && s.meanDiff < 24) return "wave";
  const expectedRandom = 8 - 255 / (2 * s.n * Math.LN2);
  if (s.entropy > Math.max(6, expectedRandom - 0.3)) return "high";
  return "tile";
}

/** 何バイト周期でデータが繰り返しているかの候補を返す (タイルの大きさ推定) */
function guessPeriods(b, candidates = [2, 4, 8, 16, 24, 32, 48, 64, 96, 128, 256]) {
  const n = Math.min(b.length, 64 * 1024);
  const scores = [];
  for (const lag of candidates) {
    if (n <= lag * 4) continue;
    let sum = 0, count = 0;
    for (let i = 0; i + lag < n; i += 1) { sum += Math.abs(b[i] - b[i + lag]); count++; }
    scores.push({ lag, score: sum / count });
  }
  scores.sort((a, b2) => a.score - b2.score);
  return scores;
}

/* ======================= 4. 構造の推定 ======================= */

/** よく使われる文字の範囲。乱数がたまたま Shift-JIS に見えるだけの並びを弾く */
function commonJp(ch) {
  const c = ch.codePointAt(0);
  return (c >= 0x3000 && c <= 0x303F) || (c >= 0x3041 && c <= 0x309F) ||
         (c >= 0x30A0 && c <= 0x30FF) || (c >= 0x4E00 && c <= 0x9FFF) ||
         (c >= 0xFF01 && c <= 0xFF60) || c === 0x2026 || c === 0x2015 ||
         c === 0x266A || c === 0x2192 || (c >= 0x2018 && c <= 0x201D);
}

/** 2 バイト文字のうち、よく使われる範囲に入っている割合 */
function jpPlausibility(text) {
  let wide = 0, good = 0;
  for (const ch of text) {
    if (ch.codePointAt(0) < 0x80) continue;
    wide++;
    if (commonJp(ch)) good++;
  }
  return wide ? good / wide : 0;
}

/** Shift-JIS / ASCII として読める並びを探す */
function scanStrings(b, minChars) {
  const out = [];
  const n = b.length;
  let i = 0;
  while (i < n) {
    let j = i, chars = 0, jp = 0, asciiCount = 0;
    while (j < n) {
      const c = b[j];
      if (j + 1 < n && SJIS_LEAD(c) && SJIS_TRAIL(b[j + 1])) { j += 2; chars++; jp++; continue; }
      if ((c >= 0x20 && c < 0x7F) || c === 0x0A || c === 0x0D) { j++; chars++; asciiCount++; continue; }
      if (c >= 0xA1 && c <= 0xDF) { j++; chars++; continue; }   // 半角カナ (証拠にはしない)
      break;
    }
    const enough = chars >= minChars && (jp >= 3 || asciiCount >= minChars);
    if (enough) {
      const slice = b.subarray(i, j);
      let text;
      try { text = new TextDecoder("shift_jis").decode(slice); }
      catch { text = ascii(slice); }
      /* 割り当てのないコードは U+FFFD になる。乱数がたまたま Shift-JIS の
         バイト対に見えるだけの並びは、これでほぼ全部落ちる */
      if (jp >= 3 && (text.includes("\uFFFD") || jpPlausibility(text) < 0.6)) { i++; continue; }
      out.push({
        off: i, byteLen: j - i, chars,
        kind: jp >= 3 ? "jp" : "ascii",
        text: text.replace(/[\r\n\t]/g, " "),
      });
      i = j;
    } else {
      i++;
    }
  }
  return out;
}

/** UTF-8 として読める並びを探す (非 ASCII を 2 文字以上含むものだけ) */
function scanUtf8(b, minChars) {
  const out = [];
  const n = b.length;
  let i = 0;
  while (i < n) {
    let j = i, chars = 0, wide = 0;
    while (j < n) {
      const c = b[j];
      if (c >= 0x20 && c < 0x7F) { j++; chars++; continue; }
      let len = 0;
      if (c >= 0xC2 && c <= 0xDF) len = 2;
      else if (c >= 0xE0 && c <= 0xEF) len = 3;
      else if (c >= 0xF0 && c <= 0xF4) len = 4;
      if (!len || j + len > n) break;
      let ok = true;
      for (let k = 1; k < len; k++) if ((b[j + k] & 0xC0) !== 0x80) { ok = false; break; }
      if (!ok) break;
      j += len; chars++; wide++;
    }
    if (chars >= minChars && wide >= 2) {
      const slice = b.subarray(i, j);
      out.push({
        off: i, byteLen: j - i, chars, kind: "utf8",
        text: new TextDecoder("utf-8").decode(slice).replace(/[\r\n\t]/g, " "),
      });
      i = j;
    } else {
      i++;
    }
  }
  return out;
}

/**
 * 候補に根拠を付けて確度を決める。
 *
 * 手がかりは 2 つ。
 *   1. 表の直後から本文が始まっている (1 件目の行き先 ≒ 表の終わり)
 *   2. 各行き先の直前が区切りバイト (0x00 / 0xFF) になっている
 *      ── レコードが終端付きなら必ずそうなる。これがいちばん強い証拠で、
 *      たまたま単調増加している波形データなどはここで落ちる。
 */
function scoreTable(b, t) {
  const read = t.stride === 4 ? u32le : u16le;
  const n = Math.min(t.count, 64);
  let checked = 0, terminated = 0;
  for (let i = 0; i < n; i++) {
    const target = t.base + read(b, t.off + i * t.stride);
    if (target <= 0 || target >= b.length) continue;
    if (b[target] === 0x00) continue;          // 記録が終端バイトで始まることはない
    checked++;
    const prevByte = b[target - 1];
    if (prevByte === 0x00 || prevByte === 0xFF) terminated++;
  }
  const termRatio = checked ? terminated / checked : 0;
  const evidence = [];
  if (t.gap <= 64) evidence.push("表の直後から本文");
  if (termRatio >= 0.7) evidence.push("行き先の直前が区切りバイト");
  const confidence = evidence.length >= 2 ? "high" : evidence.length === 1 ? "mid" : "low";
  return Object.assign({}, t, { termRatio, evidence, confidence });
}

/**
 * ポインタテーブルの候補を探す。
 *
 * 見ているのは「値が減らずに増えていく」「すべてファイル内を指す」「1 件ずつの
 * 増分が現実的」という 3 点。加えて、表の直後から本文が始まっていれば確度を上げる
 * ── 人が 16 進を眺めて判断しているのと同じ手がかりです。
 */
function findPointerTables(b, fileSize, opt = {}) {
  const out = [];
  for (const stride of [4, 2]) {
    /* 16 ビットの表は条件を厳しくする。値の範囲が狭いぶん、意味のないデータでも
       単調増加が偶然できてしまうので、件数と増分の両方で絞る */
    const minCount = opt.minCount || (stride === 4 ? 8 : 12);
    const maxDelta = opt.maxDelta || (stride === 4 ? 0x4000 : 0x800);
    const read = stride === 4 ? u32le : u16le;
    const limit = stride === 4 ? fileSize : Math.min(fileSize, 0x10000);
    for (let phase = 0; phase < stride; phase += stride === 4 ? 4 : 2) {
      let start = -1, prev = -1, count = 0, zeroDeltas = 0, first = 0;
      /**
       * 見つけた単調増加の並びについて、
       *   ・表の一部でない先頭を捨てた版 (3 通り)
       *   ・基準の値 (4 通り: 0 / 表の直後 / 1 件目や 2 件目が表の直後を指すように合わせた値)
       * を組み合わせて総当たりし、いちばん根拠の多い読み方を 1 つだけ採用します。
       * 基準を決め打ちにしないのが肝で、イメージの中に埋まったファイルの
       * ポインタ (ファイル先頭からの相対値) もこれで拾えます。
       */
      const flush = () => {
        if (count < minCount || zeroDeltas > count * 0.3 || prev <= first) {
          start = -1; count = 0; zeroDeltas = 0; return;
        }
        const tableEnd = start + count * stride;   /* 先頭を捨てても変わらない */
        const values = [];
        for (let i = 0; i < count; i++) values.push(read(b, start + i * stride));

        const variants = [{ off: start, values }];
        if (count - 1 >= minCount) {
          variants.push({ off: start + stride, values: values.slice(1) });
        }
        let drop = 0;
        while (drop < count && values[drop] < tableEnd) drop++;
        if (drop > 1 && count - drop >= minCount) {
          variants.push({ off: start + drop * stride, values: values.slice(drop) });
        }

        const cands = [];
        for (const v of variants) {
          const bases = new Set([0, tableEnd, tableEnd - v.values[0]]);
          if (v.values.length > 1) bases.add(tableEnd - v.values[1]);
          for (const base of bases) {
            if (base < 0) continue;
            if (base + v.values[v.values.length - 1] >= b.length) continue;
            cands.push(scoreTable(b, {
              off: v.off, count: v.values.length, stride, base,
              baseKind: base === 0 ? "abs" : "rel",
              first: base + v.values[0], tableEnd,
              gap: Math.abs(base + v.values[0] - tableEnd),
            }));
          }
        }
        cands.sort((a, c) => c.evidence.length - a.evidence.length || c.count - a.count);
        /* 根拠がひとつも無い並びは偶然とみて報告しない */
        if (cands.length && cands[0].evidence.length >= 1) out.push(cands[0]);
        start = -1; count = 0; zeroDeltas = 0;
      };
      for (let p = phase; p + stride <= b.length; p += stride) {
        const v = read(b, p);
        const inRange = v < limit;
        const ok = inRange && (count === 0 || (v >= prev && v - prev <= maxDelta));
        if (!ok) { flush(); if (inRange) { start = p; prev = v; first = v; count = 1; } continue; }
        if (count === 0) { start = p; first = v; }
        else if (v === prev) zeroDeltas++;
        prev = v; count++;
      }
      flush();
    }
  }
  /* 同じ場所を指す重複を落とし、確度と件数で並べる */
  const seen = new Set();
  const uniq = [];
  const rank = { high: 0, mid: 1, low: 2 };
  out.sort((a, c) => (rank[a.confidence] !== rank[c.confidence]
    ? rank[a.confidence] - rank[c.confidence] : c.count - a.count));
  for (const t of out) {
    const key = t.off + ":" + t.stride + ":" + t.baseKind;
    if (seen.has(key)) continue;
    if (uniq.some((u) => u.stride === t.stride && u.baseKind === t.baseKind &&
                          t.off >= u.off && t.off < u.off + u.count * u.stride)) continue;
    seen.add(key);
    uniq.push(t);
    if (uniq.length >= 40) break;
  }
  return uniq;
}


/* ======================= タイル領域の自動検出 =======================
 * 「この位置に絵がありそう」を自動で探す。
 *
 * 手がかりは 2 つだけ。
 *   1. バイトの性質が「タイル・コードなど」に分類される
 *      (圧縮・波形・テキストはここで落ちる)
 *   2. ある間隔だけ離れたバイトが、隣のバイトより似ている
 *      画像は縦に相関があるので、行の長さ = その間隔になる
 *
 * 2 の強さは「間隔をあけた平均差 ÷ 隣どうしの平均差」で測ります。1 より
 * 十分小さければ縦の相関があるということ。練習用データで測ると、フォントは
 * 0.56、独自文字コードのテキストは 0.97 と、はっきり分かれます。
 */

const TILE_WINDOW = 4096;
const TILE_LAGS = [8, 16, 24, 32, 64, 128];
const TILE_RATIO_MAX = 0.82;

/**
 * 間隔をあけたバイトの平均差。
 *
 * ゼロ同士の組は数えません。ゼロ埋めはどの間隔でも完全に一致するので、
 * 入れておくと詰め物のある領域が「周期が強い」と誤判定されます。
 */
function meanGapDiff(win, lag) {
  let sum = 0, count = 0;
  const step = lag === 1 ? 1 : 3;
  for (let i = 0; i + lag < win.length; i += step) {
    const a = win[i], c = win[i + lag];
    if (a === 0 && c === 0) continue;
    sum += Math.abs(a - c);
    count++;
  }
  return count >= 64 ? sum / count : null;
}

function lagRatio(win, lag) {
  if (win.length <= lag * 4) return 9;
  const base = meanGapDiff(win, 1);
  const gap = meanGapDiff(win, lag);
  if (base === null || gap === null || base < 1) return 9;
  return gap / base;
}

/** その窓が絵らしいかを返す (らしくなければ null) */
function tileScore(b, off, len) {
  const win = b.subarray(off, off + len);
  const st = blockStats(win);
  if (!st || classifyStats(st) !== "tile") return null;
  if (st.padRatio > 0.5) return null;      /* ほとんど詰め物の窓は対象外 */
  let best = null;
  for (const lag of TILE_LAGS) {
    const ratio = lagRatio(win, lag);
    if (!best || ratio < best.ratio) best = { lag, ratio };
  }
  if (!best || best.ratio > TILE_RATIO_MAX) return null;
  return { lag: best.lag, ratio: best.ratio, st };
}

/** 1 ドットのビット数と 1 タイルの大きさを見当をつける */
function guessTileShape(b, off, len, bytesPerTile) {
  const win = b.subarray(off, Math.min(b.length, off + Math.min(len, 8192)));
  let flat = 0;
  const seen = new Set();
  for (const v of win) {
    if (v === 0x00 || v === 0xFF) flat++;
    seen.add(v);
  }
  const bpp = flat / win.length > 0.3 ? 1 : (seen.size <= 64 ? 4 : 8);
  const side = Math.sqrt(bytesPerTile * 8 / bpp);
  const choices = [8, 16, 24, 32];
  const fit = choices.reduce((a, c) => Math.abs(c - side) < Math.abs(a - side) ? c : a);
  return { bpp, tw: fit, th: fit };
}

/**
 * 領域の前後を詰める。窓の刻み (4KB) は粗いので、そのままだと絵の手前の
 * 別のデータまで含んでしまい、縮小図の頭が化ける。512 バイトずつ寄せる。
 */
function refineRegion(b, r) {
  const STEP = 512, PROBE = 1024;
  /* 領域そのものの強さを基準にする。固定値だと隣の別データを取り込んでしまう */
  const limit = Math.min(TILE_RATIO_MAX, r.ratio + 0.12);
  const looksTile = (o) => {
    const win = b.subarray(o, Math.min(b.length, o + PROBE));
    return lagRatio(win, r.lag) <= limit;
  };
  let start = r.off;
  let end = r.off + r.len;
  while (start + PROBE < end && !looksTile(start)) start += STEP;
  while (end - PROBE > start && !looksTile(end - PROBE)) end -= STEP;
  r.off = start;
  r.len = Math.max(PROBE, end - start);
}

function findTileRegions(b) {
  const raw = [];
  for (let off = 0; off + 1024 <= b.length; off += TILE_WINDOW) {
    const len = Math.min(TILE_WINDOW, b.length - off);
    const r = tileScore(b, off, len);
    if (r) raw.push({ off, len, lag: r.lag, ratio: r.ratio });
  }
  /* 続いている窓はひとつの領域にまとめる */
  const regions = [];
  for (const h of raw) {
    const last = regions[regions.length - 1];
    if (last && h.off === last.off + last.len && h.lag === last.lag) {
      last.len += h.len;
      last.ratio = Math.min(last.ratio, h.ratio);
    } else {
      regions.push(Object.assign({}, h));
    }
  }
  for (const r of regions) {
    refineRegion(b, r);
    Object.assign(r, guessTileShape(b, r.off, r.len, r.lag));
  }
  return regions.sort((a, c) => a.ratio - c.ratio).slice(0, 24);
}

/** タイルを 1 枚の canvas に並べて描く。一覧の縮小図と「タイル」タブで共用する */
function drawTiles(cv, opt) {
  const { off, bpp, tw, th, cols, zoom, maxTiles } = opt;
  const b = state.buf;
  const bytesPerTile = Math.ceil(tw * th * bpp / 8);
  const avail = Math.max(0, b.length - off);
  const nTiles = Math.min(maxTiles, Math.floor(avail / bytesPerTile));
  const rows = Math.max(1, Math.ceil(nTiles / cols));
  cv.width = cols * (tw + 1) * zoom;
  cv.height = rows * (th + 1) * zoom;
  /* 表示上の大きさは呼び出し側で決める。ここで style を書くと
     一覧側の width:100% を上書きしてしまい、縮小図が横に切れる */
  const g = cv.getContext("2d");
  g.imageSmoothingEnabled = false;
  g.fillStyle = cssColor("--plate-2");
  g.fillRect(0, 0, cv.width, cv.height);

  const img = g.createImageData(tw, th);
  const maxVal = (1 << bpp) - 1;
  const fg = hexToRgb(cssColor("--ink"));
  const bgc = hexToRgb(cssColor("--bg"));
  const tmp = document.createElement("canvas");
  tmp.width = tw; tmp.height = th;
  const tg = tmp.getContext("2d");

  for (let t = 0; t < nTiles; t++) {
    const base = off + t * bytesPerTile;
    let bit = 0;
    for (let y = 0; y < th; y++) {
      for (let x = 0; x < tw; x++) {
        let v = 0;
        for (let k = 0; k < bpp; k++) {
          const byte = b[base + ((bit + k) >> 3)] || 0;
          v = (v << 1) | ((byte >> (7 - ((bit + k) & 7))) & 1);
        }
        bit += bpp;
        const a = v / maxVal;
        const p = (y * tw + x) * 4;
        img.data[p] = Math.round(bgc[0] + (fg[0] - bgc[0]) * a);
        img.data[p + 1] = Math.round(bgc[1] + (fg[1] - bgc[1]) * a);
        img.data[p + 2] = Math.round(bgc[2] + (fg[2] - bgc[2]) * a);
        img.data[p + 3] = 255;
      }
    }
    tg.putImageData(img, 0, 0);
    g.drawImage(tmp, (t % cols) * (tw + 1) * zoom, Math.floor(t / cols) * (th + 1) * zoom,
                tw * zoom, th * zoom);
  }
  return { nTiles, bytesPerTile };
}

/* ======================= 5. 文字コード ======================= */

const DECODERS = {
  sjis: (() => { try { return new TextDecoder("shift_jis"); } catch { return null; } })(),
  utf8: new TextDecoder("utf-8"),
};

/** 16 進表示の右側に出す文字を 1 バイトずつ組み立てる */
function textCells(b, enc, table) {
  const cells = new Array(b.length).fill(null);
  let i = 0;
  while (i < b.length) {
    const c = b[i];
    if (enc === "table" && table) {
      let matched = false;
      for (let len = table.maxLen; len >= 1; len--) {
        if (i + len > b.length) continue;
        let key = "";
        for (let k = 0; k < len; k++) key += hex(b[i + k], 2);
        const ch = table.map.get(key);
        if (ch !== undefined) { cells[i] = { ch, span: len }; i += len; matched = true; break; }
      }
      if (matched) continue;
      cells[i] = { ch: ".", span: 1, dim: true }; i++;
      continue;
    }
    if (enc === "sjis" && i + 1 < b.length && SJIS_LEAD(c) && SJIS_TRAIL(b[i + 1])) {
      const ch = DECODERS.sjis ? DECODERS.sjis.decode(b.subarray(i, i + 2)) : "・";
      cells[i] = { ch, span: 2 }; i += 2; continue;
    }
    if (enc === "utf8") {
      let len = 0;
      if (c >= 0xC2 && c <= 0xDF) len = 2;
      else if (c >= 0xE0 && c <= 0xEF) len = 3;
      else if (c >= 0xF0 && c <= 0xF4) len = 4;
      if (len && i + len <= b.length) {
        let ok = true;
        for (let k = 1; k < len; k++) if ((b[i + k] & 0xC0) !== 0x80) { ok = false; break; }
        if (ok) { cells[i] = { ch: DECODERS.utf8.decode(b.subarray(i, i + len)), span: len }; i += len; continue; }
      }
    }
    if (c >= 0x20 && c < 0x7F) cells[i] = { ch: String.fromCharCode(c), span: 1 };
    else if (enc === "sjis" && c >= 0xA1 && c <= 0xDF) {
      cells[i] = { ch: DECODERS.sjis ? DECODERS.sjis.decode(b.subarray(i, i + 1)) : "・", span: 1 };
    } else cells[i] = { ch: ".", span: 1, dim: true };
    i++;
  }
  return cells;
}

function parseTable(text) {
  const map = new Map();
  let maxLen = 1;
  for (const raw of text.split("\n")) {
    const line = raw.split("#")[0].trim();
    if (!line) continue;
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const code = line.slice(0, eq).trim().toUpperCase();
    const ch = line.slice(eq + 1);
    if (!/^[0-9A-F]+$/.test(code) || code.length % 2 || !ch) continue;
    map.set(code, ch);
    maxLen = Math.max(maxLen, code.length / 2);
  }
  return map.size ? { map, maxLen } : null;
}

const HIRAGANA = Array.from({ length: 0x3094 - 0x3041 }, (_, i) => String.fromCharCode(0x3041 + i));
const KATAKANA = Array.from({ length: 0x30F4 - 0x30A1 }, (_, i) => String.fromCharCode(0x30A1 + i));
const SMALL_KANA = new Set([..."ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ"]);

function kanaOrder(name) {
  const all = HIRAGANA.concat(KATAKANA);
  return name === "nosmall" ? all.filter((c) => !SMALL_KANA.has(c)) : all;
}

function relativeSearch(b, query, order, width, endian) {
  const index = new Map(order.map((c, i) => [c, i]));
  const unknown = [...query].filter((c) => !index.has(c));
  if (unknown.length) throw new Error("仮定した並びに無い文字: " + unknown.join(""));
  if ([...query].length < 3) throw new Error("検索語は 3 文字以上にしてください");
  const pos = [...query].map((c) => index.get(c));
  const diffs = pos.slice(1).map((v, i) => v - pos[i]);
  const modulus = width === 1 ? 256 : 65536;
  const read = width === 1 ? (bb, p) => bb[p] : (endian === "le" ? u16le : u16be);
  const span = width * pos.length;
  const hits = [];
  for (let i = 0; i + span <= b.length; i++) {
    let prev = read(b, i), ok = true;
    for (let k = 0; k < diffs.length; k++) {
      const cur = read(b, i + width * (k + 1));
      if (((cur - prev) % modulus + modulus) % modulus !== ((diffs[k] % modulus) + modulus) % modulus) { ok = false; break; }
      prev = cur;
    }
    if (ok) hits.push({ off: i, code: read(b, i, width, endian) });
    if (hits.length >= 200) break;
  }
  return { diffs, hits };
}

function derivedTable(code, query, order, width, endian) {
  const index = new Map(order.map((c, i) => [c, i]));
  const modulus = width === 1 ? 256 : 65536;
  const origin = ((code - index.get([...query][0])) % modulus + modulus) % modulus;
  const map = new Map();
  order.forEach((ch, i) => {
    const v = (origin + i) % modulus;
    map.set(width === 1 ? hex(v, 2) : hex(v, 4), ch);
  });
  return { map, maxLen: width, origin };
}


/* ======================= ヒーローのバイトマップ =======================
 * 受け入れ画面の背景は、この道具が出す絵そのもの。埋め込んである練習用
 * イメージを本当に分類して描く (無ければ似せた模様にする)。列ごとに
 * 同じ種類が並ぶので、分光プレートのように読める。
 */

let heroCells = null;
let heroRaf = null;

function heroClasses() {
  if (typeof window.SAMPLE_ISO === "string" && window.SAMPLE_ISO.length) {
    try {
      const raw = atob(window.SAMPLE_ISO);
      const bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      const block = 192;
      const out = [];
      for (let s = 0; s < bytes.length; s += block) {
        out.push(classifyStats(blockStats(bytes.subarray(s, s + block))));
      }
      if (out.length > 40) return out;
    } catch (err) { /* 落ちたら下の模様にする */ }
  }
  const plan = [["zero", 70], ["ascii", 12], ["jp", 110], ["tile", 190], ["wave", 130],
                ["high", 300], ["zero", 34], ["jp", 54], ["tile", 84], ["high", 120], ["zero", 48]];
  const out = [];
  let x = 20260730;
  for (const [kind, n] of plan) {
    for (let i = 0; i < n; i++) {
      x = (x * 1103515245 + 12345) & 0x7FFFFFFF;
      out.push((x >> 20) % 17 === 0 ? "tile" : kind);
    }
  }
  return out;
}

function startHero() {
  const cv = $("hero");
  if (!cv || !cv.parentElement) return;
  if (!heroCells) heroCells = heroClasses();
  const host = cv.parentElement;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let t0 = null;
  const render = (now) => {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) { heroRaf = requestAnimationFrame(render); return; }
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); }
    if (cv.height !== Math.round(h * dpr)) { cv.height = Math.round(h * dpr); }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const S = 9;
    const cols = Math.ceil(w / S), rows = Math.ceil(h / S);
    if (t0 === null) t0 = now;
    const prog = reduced ? 1 : Math.min(1, (now - t0) / 1100);
    const eased = 1 - Math.pow(1 - prog, 3);
    const colors = {};
    for (const k of Object.keys(CLASSES)) colors[k] = cssColor(CLASSES[k].css);
    const revealed = Math.ceil(cols * eased);
    for (let c = 0; c < revealed; c++) {
      for (let r = 0; r < rows; r++) {
        const idx = (c * rows + r) % heroCells.length;
        g.fillStyle = colors[heroCells[idx]] || colors.tile;
        g.fillRect(c * S, r * S, S - 1, S - 1);
      }
    }
    heroRaf = prog < 1 ? requestAnimationFrame(render) : null;
  };
  if (heroRaf) cancelAnimationFrame(heroRaf);
  heroRaf = requestAnimationFrame(render);
}

window.addEventListener("resize", () => { if (!$("intake").hidden) startHero(); });

/* ======================= 6. 状態 ======================= */

const state = {
  file: null,
  iso: null,
  entries: [],
  current: null,
  buf: new Uint8Array(0),
  truncated: false,
  analysis: null,
  map: null,
  table: null,
  strings: [],
  strFilter: "all",
  pointers: [],
  hexOff: 0,
  tab: "hex",
  tiles: [],        /* 自動検出した絵の領域 */
  marks: [],        /* マップに重ねる印 (見つけた構造の位置) */
  lit: -1,          /* 強調中の印 */
  showMarks: true,
};

/* ======================= 7. 受け入れ ======================= */

const dropzone = $("dropzone");
const fileinput = $("fileinput");

["dragenter", "dragover"].forEach((ev) =>
  document.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("hot"); }));
["dragleave", "drop"].forEach((ev) =>
  document.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("hot"); }));
document.addEventListener("drop", (e) => {
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) openFile(f);
});
$("pick").addEventListener("click", () => fileinput.click());
fileinput.addEventListener("change", () => { if (fileinput.files[0]) openFile(fileinput.files[0]); });
$("reopen").addEventListener("click", () => {
  $("shell").hidden = true;
  $("intake").hidden = false;
  $("loaded").hidden = true;
  $("readout").hidden = true;
  startHero();
});

/* ページに練習用イメージが埋め込まれている場合はボタンを出す
   (build_web.py が window.SAMPLE_ISO を差し込む) */
if (typeof window.SAMPLE_ISO === "string" && window.SAMPLE_ISO.length) {
  const btn = $("sample");
  btn.hidden = false;
  btn.addEventListener("click", () => {
    const raw = atob(window.SAMPLE_ISO);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    openFile(new File([bytes], window.SAMPLE_ISO_NAME || "SAMPLE.iso"));
  });
}

async function openFile(file) {
  state.file = file;
  state.iso = await readIso(file);
  state.entries = [];
  if (state.iso) {
    state.entries.push({
      path: "(イメージ全体)", name: file.name, offset: 0, size: file.size, kind: "image",
    });
    state.entries.push(...state.iso.entries);
  } else {
    state.entries.push({ path: file.name, name: file.name, offset: 0, size: file.size, kind: "file" });
  }

  $("intake").hidden = true;
  $("shell").hidden = false;
  const loaded = $("loaded");
  loaded.hidden = false;
  loaded.textContent = "";
  const info = [
    [file.name, "ファイル"],
    [fmtSize(file.size), "サイズ"],
    [state.iso ? "ISO9660" : "単体", "形式"],
  ];
  if (state.iso) info.push([state.iso.volumeId || "(名前なし)", "ボリューム"]);
  for (const [value, label] of info) {
    const el = document.createElement("span");
    const b = document.createElement("b");
    b.textContent = value;
    const i = document.createElement("i");
    i.textContent = label;
    el.append(b, i);
    loaded.append(el);
  }
  $("readout").hidden = false;
  $("ro-file").textContent = file.name;

  renderTree();
  /* まずイメージ全体の地図を見せる。単体ファイルならそのファイル */
  await selectEntry(state.entries[0]);
  await classifyEntries();
}

/* ======================= 8. ファイル一覧と検出結果 ======================= */

function cssColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

function renderTree() {
  const ul = $("tree");
  ul.textContent = "";
  let lastDir = null;
  for (const entry of state.entries) {
    const dir = entry.kind === "image" ? null : entry.path.slice(0, entry.path.lastIndexOf("/") + 1);
    if (dir && dir !== lastDir) {
      const li = document.createElement("li");
      const d = document.createElement("div");
      d.className = "dir";
      d.textContent = dir;
      li.append(d);
      ul.append(li);
      lastDir = dir;
    }
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "filerow";
    btn.type = "button";
    if (state.current === entry) btn.setAttribute("aria-current", "true");
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = entry.cls ? cssColor(CLASSES[entry.cls].css) : "transparent";
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = entry.kind === "image" ? entry.path : entry.name;
    const sz = document.createElement("span");
    sz.className = "sz";
    sz.textContent = fmtSize(entry.size);
    btn.append(sw, nm, sz);
    btn.addEventListener("click", () => selectEntry(entry));
    li.append(btn);
    ul.append(li);
  }
  $("treecount").textContent = state.iso
    ? state.iso.entries.length + " ファイル" : "1 ファイル";
}

/** ファイル一覧に色の帯を出すため、各ファイルの先頭 4KB だけ見て分類する */
async function classifyEntries() {
  const targets = state.entries.filter((e) => e.kind !== "image" && !e.cls).slice(0, 300);
  for (const entry of targets) {
    const head = await readRange(state.file, entry.offset, Math.min(entry.size, 4096));
    entry.cls = classifyStats(blockStats(head));
  }
  if (targets.length) renderTree();
}

/**
 * 検出結果をマップ上の位置 (マス番号) に直す。
 * 左のレールに出しているものと同じ集合を印にするので、一覧と地図が対応する。
 */
function buildMarks() {
  const block = state.map ? state.map.blockSize : 1;
  const marks = [];
  for (const t of railPointers()) {
    marks.push({
      kind: "ptr", from: Math.floor(t.off / block),
      to: Math.floor((t.tableEnd - 1) / block),
      label: `${t.stride === 4 ? "PTR32" : "PTR16"} ${hx(t.off)} · ${t.count} 件`,
      off: t.off,
    });
  }
  for (const r of state.tiles.slice(0, 8)) {
    marks.push({
      kind: "tile", from: Math.floor(r.off / block),
      to: Math.floor((r.off + r.len - 1) / block),
      label: `絵 ${hx(r.off)} · ${r.bpp}bpp ${r.tw}x${r.th}`,
      off: r.off,
    });
  }
  for (const str of railStrings()) {
    marks.push({
      kind: "text", from: Math.floor(str.off / block),
      to: Math.floor((str.off + str.byteLen - 1) / block),
      label: `TEXT ${hx(str.off)} · ${str.text.slice(0, 24)}`,
      off: str.off,
    });
  }
  state.marks = marks;
}

function railPointers() {
  const high = state.pointers.filter((t) => t.confidence === "high");
  return (high.length ? high : state.pointers.filter((t) => t.confidence === "mid")).slice(0, 8);
}

function railStrings() {
  return state.strings.filter((str) => str.kind !== "ascii").slice(0, 12);
}

function renderFindings() {
  const ul = $("findlist");
  ul.textContent = "";
  const items = [];
  for (const t of railPointers()) {
    items.push({
      kind: t.stride === 4 ? "PTR32" : "PTR16",
      at: hx(t.off),
      txt: `${t.count} 件 / 最初の行き先 ${hx(t.first)}`
           + (t.evidence.length ? " / " + t.evidence.join(" / ") : ""),
      go: () => { gotoOffset(t.off); showTab("pointers"); highlightPointer(t); },
    });
  }
  for (const r of state.tiles.slice(0, 8)) {
    items.push({
      kind: "TILE", at: hx(r.off),
      txt: `${fmtSize(r.len)} / ${r.bpp}bpp ${r.tw}x${r.th} / 縦の相関 ${r.ratio.toFixed(2)}`,
      go: () => { showTab("gallery"); openTileRegion(r); },
    });
  }
  for (const str of railStrings()) {
    items.push({
      kind: "TEXT", at: hx(str.off), txt: str.text.slice(0, 60),
      go: () => { gotoOffset(str.off); showTab("hex"); },
    });
  }
  if (!items.length) {
    const li = document.createElement("li");
    li.innerHTML = '<p class="empty">構造の候補は見つかりませんでした。圧縮されているか、'
      + 'ここには構造が無いのかもしれません。</p>';
    ul.append(li);
  }
  items.forEach((it, i) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "findrow";
    btn.type = "button";
    btn.style.setProperty("--i", String(i));
    const k = document.createElement("span");
    k.className = "kind";
    k.textContent = it.kind;
    const a = document.createElement("span");
    a.className = "at";
    a.textContent = it.at;
    const t = document.createElement("span");
    t.className = "txt";
    t.textContent = it.txt;
    btn.append(k, a, t);
    btn.addEventListener("click", it.go);
    /* 一覧の行と地図の印を対応させる。どこにあるものなのかが目で追える */
    btn.addEventListener("mouseenter", () => { state.lit = i; drawMap(); });
    btn.addEventListener("focus", () => { state.lit = i; drawMap(); });
    btn.addEventListener("mouseleave", () => { state.lit = -1; drawMap(); });
    btn.addEventListener("blur", () => { state.lit = -1; drawMap(); });
    li.append(btn);
    ul.append(li);
  });
  const high = state.pointers.filter((t) => t.confidence === "high").length;
  $("findcount").textContent = `表 ${high} · 絵 ${state.tiles.length} · JP `
    + state.strings.filter((s) => s.kind !== "ascii").length;
}

/* ======================= 9. 全体マップ ======================= */

async function buildMap(entry) {
  const size = entry.size;
  const blockSize = Math.max(64, Math.ceil(size / MAP_CELLS / 64) * 64);
  const nBlocks = Math.max(1, Math.ceil(size / blockSize));
  const sampleLen = Math.min(blockSize, 4096);
  const cells = new Array(nBlocks);

  if (size <= ANALYZE_CAP) {
    /* 全部読めているので、標本ではなくブロック全体で判定する。
       ブロックが小さいと統計は粗くなるが、しきい値を標本数から出しているので
       「乱数なのにタイル扱い」程度のずれで済む */
    const all = state.buf;
    for (let i = 0; i < nBlocks; i++) {
      const s = i * blockSize;
      cells[i] = classifyStats(blockStats(all.subarray(s, Math.min(all.length, s + blockSize))));
    }
  } else {
    /* 大きいイメージは全部読まず、各ブロックの先頭だけを拾う */
    const BATCH = 48;
    for (let i = 0; i < nBlocks; i += BATCH) {
      const jobs = [];
      for (let j = i; j < Math.min(nBlocks, i + BATCH); j++) {
        jobs.push(readRange(state.file, entry.offset + j * blockSize, sampleLen));
      }
      const got = await Promise.all(jobs);
      got.forEach((bytes, k) => { cells[i + k] = classifyStats(blockStats(bytes)); });
    }
  }
  return { blockSize, nBlocks, cells, size };
}

const CELL_PX = 11;
let mapGeom = null;
let mapHover = -1;
let mapReveal = 1;      /* 0→1 で左から順に出す掃引 */

function drawMap() {
  const cv = $("map");
  const m = state.map;
  if (!m) return;
  const cssW = cv.clientWidth || 900;
  const cols = Math.max(8, Math.floor(cssW / CELL_PX));
  const rows = Math.ceil(m.nBlocks / cols);
  const h = Math.max(CELL_PX, rows * CELL_PX);
  const dpr = window.devicePixelRatio || 1;
  cv.height = h * dpr;
  cv.width = cssW * dpr;
  cv.style.height = h + "px";
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, h);

  const colors = {};
  for (const k of Object.keys(CLASSES)) colors[k] = cssColor(CLASSES[k].css);
  const shown = Math.ceil(m.nBlocks * mapReveal);

  /* 一覧をなぞっている間は、その構造の範囲だけを残して他を沈める。
     どこにあるものなのかが一目で分かる */
  const lit = state.showMarks && state.lit >= 0 ? state.marks[state.lit] : null;
  for (let i = 0; i < shown; i++) {
    const x = (i % cols) * CELL_PX;
    const y = Math.floor(i / cols) * CELL_PX;
    g.globalAlpha = lit ? (i >= lit.from && i <= lit.to ? 1 : 0.28) : 1;
    g.fillStyle = colors[m.cells[i]] || colors.tile;
    g.fillRect(x, y, CELL_PX - 1, CELL_PX - 1);
  }
  g.globalAlpha = 1;

  /* 見つけた構造の位置に印を重ねる。一覧と地図をつなぐ */
  if (state.showMarks) {
    const seal = cssColor("--seal");
    state.marks.forEach((mk, i) => {
      const strong = i === state.lit;
      g.fillStyle = seal;
      g.globalAlpha = strong ? 1 : (mk.kind === "text" ? 0.6 : 0.9);
      const thick = mk.kind === "ptr" ? 3 : (mk.kind === "tile" ? 3 : 2);
      for (let c = mk.from; c <= mk.to && c < m.nBlocks; c++) {
        const x = (c % cols) * CELL_PX;
        const y = Math.floor(c / cols) * CELL_PX;
        g.fillRect(x, y + CELL_PX - 1 - thick, CELL_PX - 1, thick);
      }
      if (strong) {
        g.strokeStyle = seal;
        g.lineWidth = 1;
        for (let c = mk.from; c <= mk.to && c < m.nBlocks; c++) {
          const x = (c % cols) * CELL_PX;
          const y = Math.floor(c / cols) * CELL_PX;
          g.strokeRect(x - 0.5, y - 0.5, CELL_PX, CELL_PX);
        }
      }
      g.globalAlpha = 1;
    });
  }

  /* 走査線。読み込み時に左から流れる */
  if (mapReveal < 1) {
    const front = (shown % cols) * CELL_PX;
    g.fillStyle = cssColor("--seal");
    g.fillRect(front, 0, 1, h);
  }

  /* 十字線。押す前にどの行・列を見ているかが分かる */
  if (mapHover >= 0 && mapHover < m.nBlocks) {
    const hx2 = (mapHover % cols) * CELL_PX;
    const hy = Math.floor(mapHover / cols) * CELL_PX;
    g.fillStyle = cssColor("--seal");
    g.globalAlpha = 0.22;
    g.fillRect(0, hy, cssW, CELL_PX - 1);
    g.fillRect(hx2, 0, CELL_PX - 1, h);
    g.globalAlpha = 1;
    g.strokeStyle = cssColor("--seal");
    g.lineWidth = 1;
    g.strokeRect(hx2 - 0.5, hy - 0.5, CELL_PX, CELL_PX);
  }

  mapGeom = { cols, rows, cssW };
  $("mapnote").textContent =
    `1 マス ${fmtSize(m.blockSize)} · ${m.nBlocks.toLocaleString("ja-JP")} マス`;
  drawRuler(cols, rows);
}

/** 行の先頭オフセットを縦に並べる (計測器の目盛りにあたる) */
function drawRuler(cols, rows) {
  const box = $("mapruler");
  const m = state.map;
  box.textContent = "";
  box.style.gridTemplateRows = `repeat(${rows}, ${CELL_PX}px)`;
  const every = rows > 24 ? Math.ceil(rows / 24) : 1;
  for (let r = 0; r < rows; r++) {
    const el = document.createElement("div");
    el.style.lineHeight = CELL_PX + "px";
    el.textContent = r % every === 0 ? hex(r * cols * m.blockSize, 6) : "";
    box.append(el);
  }
}

function animateMap() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { mapReveal = 1; drawMap(); return; }
  mapReveal = 0;
  const t0 = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - t0) / 420);
    mapReveal = 1 - Math.pow(1 - t, 3);
    drawMap();
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function mapIndexFromEvent(ev) {
  if (!mapGeom || !state.map) return -1;
  const cv = $("map");
  const r = cv.getBoundingClientRect();
  const col = Math.floor((ev.clientX - r.left) / CELL_PX);
  const row = Math.floor((ev.clientY - r.top) / CELL_PX);
  if (col < 0 || col >= mapGeom.cols || row < 0) return -1;
  const idx = row * mapGeom.cols + col;
  return idx < state.map.nBlocks ? idx : -1;
}

$("map").addEventListener("mousemove", (ev) => {
  const idx = mapIndexFromEvent(ev);
  const tip = $("maptip");
  if (idx === mapHover) return;
  mapHover = idx;
  if (idx < 0) {
    tip.textContent = "マスにカーソルを合わせると位置と種類が出ます。";
    drawMap();
    return;
  }
  const off = idx * state.map.blockSize;
  const label = CLASSES[state.map.cells[idx]].label;
  tip.innerHTML = "";
  const b = document.createElement("b");
  b.textContent = hx(off);
  tip.append(b, document.createTextNode(`  ${fmtSize(off)} 付近  ·  ${label}`));
  $("ro-off").textContent = hx(off);
  $("ro-cls").textContent = label;

  /* 印の上なら、その構造が何かを出して一覧側も光らせる */
  const under = state.marks.findIndex((mk) => idx >= mk.from && idx <= mk.to);
  state.lit = under;
  const rows = $("findlist").querySelectorAll(".findrow");
  rows.forEach((el, i) => el.classList.toggle("lit", i === under));
  if (under >= 0) {
    tip.append(document.createTextNode("  ·  "));
    const s2 = document.createElement("b");
    s2.textContent = state.marks[under].label;
    tip.append(s2);
  }
  drawMap();
});
$("map").addEventListener("mouseleave", () => { mapHover = -1; drawMap(); });
$("map").addEventListener("click", (ev) => {
  const idx = mapIndexFromEvent(ev);
  if (idx < 0) return;
  gotoOffset(idx * state.map.blockSize);
  showTab("hex");
});
window.addEventListener("resize", () => drawMap());
$("tmarks").addEventListener("click", () => {
  state.showMarks = !state.showMarks;
  $("tmarks").setAttribute("aria-pressed", String(state.showMarks));
  drawMap();
});
$("tsweep").addEventListener("click", () => animateMap());

function renderLegend() {
  const box = $("maplegend");
  box.textContent = "";
  for (const [key, def] of Object.entries(CLASSES)) {
    const el = document.createElement("span");
    const i = document.createElement("i");
    i.style.background = cssColor(def.css);
    el.append(i, document.createTextNode(def.label));
    box.append(el);
  }
  const mk = document.createElement("span");
  mk.className = "mark-key push";
  const bar = document.createElement("i");
  mk.append(bar, document.createTextNode("見つけた構造の位置"));
  box.append(mk);
}

/* ======================= 10. 選択と解析 ======================= */

async function selectEntry(entry) {
  state.current = entry;
  state.hexOff = 0;
  const readLen = Math.min(entry.size, ANALYZE_CAP);
  state.truncated = entry.size > ANALYZE_CAP;
  state.buf = await readRange(state.file, entry.offset, readLen);

  $("capnote").textContent = state.truncated
    ? `このファイルは大きいので、詳しい解析は先頭 ${fmtSize(ANALYZE_CAP)} までに限っています。`
    : "";

  state.strings = scanStrings(state.buf, parseInt($("strmin").value, 10) || 6)
    .concat(scanUtf8(state.buf, parseInt($("strmin").value, 10) || 6))
    .sort((a, b) => a.off - b.off);
  state.pointers = findPointerTables(state.buf, entry.size);
  state.tiles = findTileRegions(state.buf);
  entry.cls = classifyStats(blockStats(state.buf.subarray(0, 4096)));

  /* 先頭がゼロ埋めのことが多いので、最初に意味のある位置へ寄せておく */
  const firstHigh = state.pointers.find((t) => t.confidence === "high");
  const firstText = state.strings.find((s) => s.kind !== "ascii") || state.strings[0];
  let head = 0;
  while (head < state.buf.length && state.buf[head] === 0) head++;
  state.hexOff = Math.min(
    firstHigh ? firstHigh.off : Infinity,
    firstText ? firstText.off : Infinity,
    head < state.buf.length ? head : 0);
  if (!Number.isFinite(state.hexOff)) state.hexOff = 0;
  $("hexoff").value = hex(state.hexOff);

  state.map = await buildMap(entry);
  state.lit = -1;
  buildMarks();
  renderLegend();
  animateMap();
  renderTree();
  renderFindings();
  renderHex();
  renderStrings();
  renderPointers();
  renderTiles();
  renderFormat();
  updateReadout();
}

function updateReadout() {
  const entry = state.current;
  if (!entry) return;
  $("ro-file").textContent = entry.kind === "image" ? entry.path : entry.path;
  $("ro-off").textContent = hx(state.hexOff);
  $("ro-cls").textContent = entry.cls ? CLASSES[entry.cls].label : "—";
  const high = state.pointers.filter((t) => t.confidence === "high").length;
  const jp = state.strings.filter((s) => s.kind !== "ascii").length;
  countUp($("ro-found"), high, jp);
}

/** 検出数をゼロから数え上げる。見つかった感じを出すための一手間 */
function countUp(el, high, jp) {
  const write = (a, b) => { el.textContent = `ポインタ表 ${a} · 日本語 ${b}`; };
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || jp === 0) {
    write(high, jp);
    return;
  }
  const t0 = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - t0) / 520);
    const e = 1 - Math.pow(1 - t, 3);
    write(Math.round(high * e), Math.round(jp * e));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function gotoOffset(off) {
  state.hexOff = Math.max(0, Math.min(off, Math.max(0, state.buf.length - 1)));
  $("hexoff").value = hex(state.hexOff);
  renderHex();
  updateReadout();
}

/* ---------- タブ ---------- */
function showTab(name) {
  state.tab = name;
  for (const btn of $("tabs").querySelectorAll("button")) {
    btn.setAttribute("aria-selected", String(btn.dataset.tab === name));
  }
  for (const key of ["hex", "strings", "pointers", "gallery", "tiles", "relative", "format"]) {
    $("tab-" + key).hidden = key !== name;
  }
  if (name === "tiles") renderTiles();
  else if (name === "gallery") renderGallery();
  else if (name === "format") renderFormat();
  else if (name === "hex") renderHex();
}
for (const btn of $("tabs").querySelectorAll("button")) {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
}

/* ---------- 16 進 ---------- */
const HEX_ROWS = 24;

function renderHex() {
  const view = $("hexview");
  view.textContent = "";
  const width = parseInt($("hexwidth").value, 10) || 16;
  const enc = $("hexenc").value;
  const start = Math.floor(state.hexOff / width) * width;
  const end = Math.min(state.buf.length, start + width * HEX_ROWS);
  const abs = state.current ? state.current.offset : 0;

  const marks = new Set();
  for (const t of state.pointers) {
    if (t.off >= start && t.off < end) {
      for (let p = t.off; p < Math.min(end, t.off + t.count * t.stride); p++) marks.add(p);
    }
  }

  const frag = document.createDocumentFragment();
  for (let row = start; row < end; row += width) {
    const line = document.createElement("div");
    const off = document.createElement("span");
    off.className = "off";
    off.textContent = hex(row, 8) + "  ";
    line.append(off);
    const stop = Math.min(end, row + width);
    for (let p = row; p < stop; p++) {
      const s = document.createElement("span");
      s.className = "b" + (state.buf[p] === 0 ? " zero" : "") + (marks.has(p) ? " mark" : "");
      s.textContent = hex(state.buf[p], 2) + " ";
      line.append(s);
    }
    for (let p = stop; p < row + width; p++) line.append(document.createTextNode("   "));
    line.append(document.createTextNode(" "));
    const cells = textCells(state.buf.subarray(row, stop), enc, state.table);
    for (let k = 0; k < cells.length; k++) {
      const c = cells[k];
      if (!c) continue;
      const s = document.createElement("span");
      s.className = c.dim ? "off" : (c.span > 1 ? "txt" : "b");
      s.textContent = c.ch;
      line.append(s);
    }
    frag.append(line);
  }
  view.append(frag);
  $("hexpos").textContent =
    `${hx(start)} 〜 ${hx(Math.max(start, end - 1))} / 全体 ${fmtBytes(state.buf.length)}`
    + (abs ? `  (イメージ上では ${hx(abs + start)})` : "");
}

$("hexoff").addEventListener("change", () => gotoOffset(parseOffset($("hexoff").value)));
$("hexenc").addEventListener("change", renderHex);
$("hexwidth").addEventListener("change", renderHex);
$("hexprev").addEventListener("click", () => {
  const width = parseInt($("hexwidth").value, 10) || 16;
  gotoOffset(Math.max(0, state.hexOff - width * HEX_ROWS));
});
$("hexnext").addEventListener("click", () => {
  const width = parseInt($("hexwidth").value, 10) || 16;
  gotoOffset(state.hexOff + width * HEX_ROWS);
});

/* ---------- 文字列 ---------- */
function renderStrings() {
  const body = $("strbody");
  body.textContent = "";
  const rows = state.strings.filter((s) =>
    state.strFilter === "all" ? true
      : state.strFilter === "ascii" ? s.kind === "ascii" : s.kind !== "ascii");
  for (const s of rows.slice(0, 600)) {
    const tr = document.createElement("tr");
    const kindLabel = s.kind === "ascii" ? "ASCII" : s.kind === "utf8" ? "UTF-8" : "Shift-JIS";
    for (const [text, cls] of [[hx(s.off), ""], [s.chars + " 字", ""], [kindLabel, ""],
                               [s.text.slice(0, 90), "jp"]]) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = text;
      tr.append(td);
    }
    tr.addEventListener("click", () => { gotoOffset(s.off); showTab("hex"); });
    body.append(tr);
  }
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = "該当する文字列はありません。";
    tr.append(td);
    body.append(tr);
  }
}
for (const btn of document.querySelectorAll("[data-strfilter]")) {
  btn.addEventListener("click", () => {
    state.strFilter = btn.dataset.strfilter;
    for (const b of document.querySelectorAll("[data-strfilter]")) {
      b.setAttribute("aria-pressed", String(b === btn));
    }
    renderStrings();
  });
}
$("strrescan").addEventListener("click", () => {
  const min = parseInt($("strmin").value, 10) || 4;
  state.strings = scanStrings(state.buf, min).concat(scanUtf8(state.buf, min))
    .sort((a, b) => a.off - b.off);
  renderStrings();
  renderFindings();
});

/* ---------- ポインタ表 ---------- */
function renderPointers() {
  const body = $("ptrbody");
  body.textContent = "";
  $("ptrdetail").textContent = "";
  for (const t of state.pointers) {
    const tr = document.createElement("tr");
    const cells = [
      hx(t.off), String(t.count), t.stride * 8 + " ビット",
      t.baseKind === "rel" ? "表の直後 " + hx(t.base) : "ファイル先頭", hx(t.first),
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    const td = document.createElement("td");
    const span = document.createElement("span");
    span.className = "conf " + t.confidence;
    span.textContent = { high: "高", mid: "中", low: "低" }[t.confidence];
    span.title = t.evidence.length ? t.evidence.join(" / ") : "根拠なし";
    td.append(span);
    tr.append(td);
    tr.addEventListener("click", () => { highlightPointer(t); gotoOffset(t.off); });
    body.append(tr);
  }
  if (!state.pointers.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "ポインタテーブルらしい並びは見つかりませんでした。";
    tr.append(td);
    body.append(tr);
  }
}

function highlightPointer(t) {
  for (const tr of $("ptrbody").querySelectorAll("tr")) tr.removeAttribute("aria-current");
  const rows = [...$("ptrbody").querySelectorAll("tr")];
  const idx = state.pointers.indexOf(t);
  if (rows[idx]) rows[idx].setAttribute("aria-current", "true");

  const box = $("ptrdetail");
  box.textContent = "";
  const read = t.stride === 4 ? u32le : u16le;
  const values = [];
  for (let i = 0; i < t.count; i++) values.push(t.base + read(state.buf, t.off + i * t.stride));

  const dl = document.createElement("dl");
  dl.className = "kv";
  const rowsInfo = [
    ["表の位置", `${hx(t.off)} 〜 ${hx(t.tableEnd - 1)}`],
    ["件数", `${t.count} 件 (${t.stride} バイトずつ)`],
    ["行き先の範囲", `${hx(values[0])} 〜 ${hx(values[values.length - 1])}`],
    ["表の直後との差", `${t.gap} バイト`],
    ["行き先の直前が区切りバイト", `${Math.round(t.termRatio * 100)}%`],
    ["根拠", t.evidence.length ? t.evidence.join(" / ") : "なし (偶然の並びかもしれません)"],
    ["1 件あたりの長さ", values.length > 1
      ? `平均 ${Math.round((values[values.length - 1] - values[0]) / (values.length - 1))} バイト` : "—"],
  ];
  for (const [k, v] of rowsInfo) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
  box.append(dl);

  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = "行き先を押すとその位置に飛びます。1 件目の行き先が表の直後なら、"
    + "この並びはポインタテーブルとみて間違いありません。";
  box.append(p);

  const wrap = document.createElement("div");
  wrap.className = "tablewrap";
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>番号</th><th>値</th><th>長さ</th><th>行き先の内容</th></tr></thead>";
  const tb = document.createElement("tbody");
  values.slice(0, 200).forEach((v, i) => {
    const tr = document.createElement("tr");
    const len = i + 1 < values.length ? values[i + 1] - v : "";
    const slice = state.buf.subarray(v, Math.min(state.buf.length, v + 40));
    let preview = "";
    try { preview = new TextDecoder("shift_jis").decode(slice).replace(/[\u0000-\u001F\u007F\uFFFD]/g, "·"); }
    catch { preview = ascii(slice); }
    for (const [text, cls] of [[String(i), ""], [hx(v), ""], [len === "" ? "" : len + " B", ""],
                               [preview, "jp"]]) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = text;
      tr.append(td);
    }
    tr.addEventListener("click", (e) => { e.stopPropagation(); gotoOffset(v); showTab("hex"); });
    tb.append(tr);
  });
  table.append(tb);
  wrap.append(table);
  box.append(wrap);
}

/* ---------- タイル ---------- */
function renderTiles() {
  const cv = $("tiles");
  const bpp = parseInt($("tilebpp").value, 10);
  const tw = parseInt($("tilew").value, 10);
  const th = parseInt($("tileh").value, 10);
  const cols = Math.max(1, parseInt($("tilecols").value, 10) || 24);
  const zoom = parseInt($("tilezoom").value, 10) || 2;
  const off = parseOffset($("tileoff").value, 0);
  const { nTiles, bytesPerTile } =
    drawTiles(cv, { off, bpp, tw, th, cols, zoom, maxTiles: 2048 });
  cv.style.width = cv.width + "px";      /* タイルタブは実寸で見せる */

  const periods = guessPeriods(state.buf.subarray(off, off + 64 * 1024));
  const top = periods.slice(0, 3).map((p) => `${p.lag} バイト`).join(" / ");
  $("tilehint").textContent =
    `${nTiles} タイル (1 タイル ${bytesPerTile} バイト)。`
    + (top ? `  繰り返しの周期が強い順: ${top}` : "");
}
for (const id of ["tileoff", "tilebpp", "tilew", "tileh", "tilecols", "tilezoom"]) {
  $(id).addEventListener("change", renderTiles);
}
$("tileguess").addEventListener("click", () => {
  const off = parseOffset($("tileoff").value, 0);
  const periods = guessPeriods(state.buf.subarray(off, off + 64 * 1024));
  if (!periods.length) return;
  const shape = guessTileShape(state.buf, off, 8192, periods[0].lag);
  $("tilebpp").value = String(shape.bpp);
  $("tilew").value = String(shape.tw);
  $("tileh").value = String(shape.th);
  renderTiles();
});

/* ---------- 見つけた絵 ---------- */
function openTileRegion(r) {
  $("tileoff").value = hex(r.off);
  $("tilebpp").value = String(r.bpp);
  $("tilew").value = String(r.tw);
  $("tileh").value = String(r.th);
  $("tilecols").value = "24";
  showTab("tiles");
  renderTiles();
}

function renderGallery() {
  const box = $("gallerybox");
  box.textContent = "";
  const note = $("gallerynote");
  if (!state.tiles.length) {
    note.textContent = "絵らしい領域は見つかりませんでした。"
      + "圧縮されているか、このファイルには画像が無いのかもしれません。"
      + "「タイル」タブで位置と大きさを手で指定すれば、そこを直接見られます。";
    return;
  }
  note.textContent = `${state.tiles.length} か所。縦の相関が強い順に並べています`
    + "（1 に近いほど根拠が弱い）。押すと「タイル」タブで細かく見られます。";

  state.tiles.forEach((r, i) => {
    const card = document.createElement("button");
    card.className = "gcard";
    card.type = "button";
    card.style.setProperty("--i", String(i));

    const shot = document.createElement("canvas");
    shot.className = "gshot";
    drawTiles(shot, {
      off: r.off, bpp: r.bpp, tw: r.tw, th: r.th,
      cols: 16, zoom: 2, maxTiles: 48,
    });

    const meta = document.createElement("div");
    meta.className = "gmeta";
    const at = document.createElement("b");
    at.textContent = hx(r.off);
    const shape = document.createElement("span");
    shape.textContent = `${r.bpp}bpp ${r.tw}×${r.th}`;
    const size = document.createElement("span");
    size.textContent = fmtSize(r.len);
    const conf = document.createElement("span");
    conf.className = "gconf";
    conf.textContent = `縦の相関 ${r.ratio.toFixed(2)}`;
    meta.append(at, shape, size, conf);

    card.append(shot, meta);
    card.addEventListener("click", () => openTileRegion(r));
    card.addEventListener("mouseenter", () => {
      const idx = state.marks.findIndex((mk) => mk.kind === "tile" && mk.off === r.off);
      state.lit = idx;
      drawMap();
    });
    card.addEventListener("mouseleave", () => { state.lit = -1; drawMap(); });
    box.append(card);
  });
}

function hexToRgb(css) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(css.trim());
  if (!m) return [230, 230, 230];
  return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
}

/* ---------- 相対検索 ---------- */
$("relrun").addEventListener("click", () => {
  const body = $("relbody");
  body.textContent = "";
  const query = $("relq").value.trim();
  const width = parseInt($("relwidth").value, 10);
  const endian = $("relendian").value;
  const order = kanaOrder($("relorder").value);
  let result;
  try {
    result = relativeSearch(state.buf, query, order, width, endian);
  } catch (err) {
    $("reldiff").textContent = String(err.message || err);
    return;
  }
  $("reldiff").textContent =
    `差の並び: ${result.diffs.map((d) => (d >= 0 ? "+" : "") + d).join(", ")}`
    + `  →  ヒット ${result.hits.length} 件`
    + (result.hits.length ? "" : "。--- 小書き文字を除く並びや 2 バイト幅も試してください。");

  for (const hit of result.hits.slice(0, 50)) {
    const table = derivedTable(hit.code, query, order, width, endian);
    const tr = document.createElement("tr");
    const preview = previewWith(state.buf, hit.off, table, width);
    const originCode = width === 1 ? hex(table.origin, 2) : hex(table.origin, 4);
    for (const [text, cls] of [[hx(hit.off), ""],
                               [(width === 1 ? "0x" + hex(hit.code, 2) : "0x" + hex(hit.code, 4)), ""],
                               [`${order[0]} = 0x${originCode}`, ""],
                               [preview, "jp"]]) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = text;
      tr.append(td);
    }
    tr.addEventListener("click", () => {
      const lines = [];
      for (const [code, ch] of table.map) lines.push(code + "=" + ch);
      $("reltable").value = lines.join("\n");
      gotoOffset(hit.off);
    });
    body.append(tr);
  }
});

function previewWith(b, off, table, width) {
  const start = Math.max(0, off - width * 4);
  const end = Math.min(b.length, off + width * 32);
  let out = "";
  let i = start;
  while (i < end) {
    let matched = false;
    for (let len = table.maxLen; len >= 1; len--) {
      let key = "";
      for (let k = 0; k < len; k++) key += hex(b[i + k] || 0, 2);
      const ch = table.map.get(key);
      if (ch !== undefined) { out += ch; i += len; matched = true; break; }
    }
    if (!matched) { out += "・"; i += width; }
  }
  return out;
}

$("reluse").addEventListener("click", () => {
  const table = parseTable($("reltable").value);
  if (!table) { $("reldiff").textContent = "テーブルを読み取れませんでした (16進=文字 の行が必要)"; return; }
  state.table = table;
  $("hexenc").value = "table";
  renderFormat();
  showTab("hex");
});

/* ---------- 既知の形式 ---------- */
function renderFormat() {
  const box = $("formatbox");
  box.textContent = "";
  const b = state.buf;
  const magic = ascii(b.subarray(0, 4));

  const head = document.createElement("dl");
  head.className = "kv";
  const info = [
    ["先頭 4 バイト", [...b.subarray(0, 4)].map((v) => hex(v, 2)).join(" ") + `  ("${magic}")`],
    ["先頭 16 バイト", [...b.subarray(0, 16)].map((v) => hex(v, 2)).join(" ")],
  ];
  for (const [k, v] of info) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    head.append(dt, dd);
  }
  box.append(head);

  if (magic === "SCRP") {
    box.append(renderScrp(b));
    return;
  }
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = "既知の形式には当てはまりませんでした。ポインタ表と文字列のタブで"
    + "構造を推定してください。構造が分かったら、この場所に読み取り処理を足していく"
    + "のが解析ツールの育て方です。";
  box.append(p);
}

/** 練習用フォーマット SCRP を、構造が分かっている場合の見え方の例として読む */
function renderScrp(b) {
  const wrap = document.createElement("div");
  wrap.style.display = "grid";
  wrap.style.gap = "12px";
  const version = u32le(b, 4), count = u32le(b, 8), encId = u32le(b, 12);
  const encName = encId === 0 ? "Shift-JIS" : encId === 1 ? "独自テーブル" : "不明";

  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [k, v] of [["形式", "SCRP (この練習用フォーマット)"], ["バージョン", String(version)],
                        ["メッセージ数", String(count)], ["文字コード", `${encId} (${encName})`],
                        ["ポインタ表", `0x10 〜 ${hx(0x10 + count * 4 - 1)}`]]) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
  wrap.append(dl);

  if (count > 4096) {
    const warn = document.createElement("p");
    warn.className = "warnbar";
    warn.textContent = "メッセージ数が不自然に大きいので、SCRP ではないかもしれません。";
    wrap.append(warn);
    return wrap;
  }

  const CONTROL = { 0xF0: ["BR", 0], 0xF1: ["NAME", 1], 0xF2: ["WAIT", 0],
                    0xF3: ["COLOR", 1], 0xF4: ["VAR", 1], 0xF5: ["CLEAR", 0] };
  const useTable = encId === 1 && state.table;
  const decodeMsg = (start) => {
    let i = start, out = "";
    while (i < b.length) {
      const c = b[i];
      if (c === 0xFF) break;
      if (CONTROL[c]) {
        const [name, argc] = CONTROL[c];
        out += argc ? `<${name}:${hex(b[i + 1], 2)}>` : `<${name}>`;
        i += 1 + argc;
        continue;
      }
      if (useTable) {
        let matched = false;
        for (let len = state.table.maxLen; len >= 1; len--) {
          let key = "";
          for (let k = 0; k < len; k++) key += hex(b[i + k] || 0, 2);
          const ch = state.table.map.get(key);
          if (ch !== undefined) { out += ch; i += len; matched = true; break; }
        }
        if (matched) continue;
        out += "・"; i++;
        continue;
      }
      if (i + 1 < b.length && SJIS_LEAD(c) && SJIS_TRAIL(b[i + 1])) {
        out += DECODERS.sjis ? DECODERS.sjis.decode(b.subarray(i, i + 2)) : "・";
        i += 2;
        continue;
      }
      out += c >= 0x20 && c < 0x7F ? String.fromCharCode(c) : "・";
      i++;
    }
    return { text: out, size: i - start + 1 };
  };

  if (encId === 1 && !state.table) {
    const note = document.createElement("p");
    note.className = "warnbar";
    note.textContent = "独自文字コードのファイルです。相対検索のタブでテーブルを作り、"
      + "「このテーブルを 16 進表示に使う」を押すと、ここに本文が出ます。";
    wrap.append(note);
  }

  const tablewrap = document.createElement("div");
  tablewrap.className = "tablewrap";
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>ID</th><th>ポインタ</th><th>長さ</th><th>本文</th></tr></thead>";
  const tb = document.createElement("tbody");
  for (let i = 0; i < count && 0x10 + i * 4 + 4 <= b.length; i++) {
    const ptr = u32le(b, 0x10 + i * 4);
    if (ptr >= b.length) continue;
    const { text, size } = decodeMsg(ptr);
    const tr = document.createElement("tr");
    for (const [t, cls] of [[String(i), ""], [hx(ptr), ""], [size + " B", ""], [text, "jp"]]) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = t;
      tr.append(td);
    }
    tr.addEventListener("click", () => { gotoOffset(ptr); showTab("hex"); });
    tb.append(tr);
  }
  table.append(tb);
  tablewrap.append(table);
  wrap.append(tablewrap);
  return wrap;
}

/* ======================= 起動 ======================= */
renderLegend();
showTab("hex");
startHero();
