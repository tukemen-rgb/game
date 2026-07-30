#!/usr/bin/env python3
"""メッセージウィンドウのシミュレータ (HTML) を生成する.

抽出したテキストを「実機の画面でどう見えるか」で確認するための画面です。
FONT.BIN のグリフをそのまま描画するので、フォントに無い文字は本当に □ に
なります。校正で一番効くのは、データ上の文字列と画面の見た目を突き合わせる
作業なので、それを 1 画面でできるようにしたものです。

    python3 tools/make_viewer.py
    # → work/viewer.html をブラウザで開く

できること
----------
* 原文 / 訳文 を切り替えて、同じ画面で見比べる
* Space で送り。<WAIT> で止まり、<CLEAR> でウィンドウが消える
* <COLOR:xx> の色変更、<VAR:00> のプレイヤー名差し込みを反映する
* プレイヤー名を変えると行幅が変わる (変数の長さ問題がその場で見える)
* 1 行 18 文字を超えた文字、3 行を超えた行が画面からはみ出して赤くなる
* 各メッセージの校正チェック結果と、元データの 16 進を並べて表示する

生成物は外部リソースを一切参照しない 1 枚の HTML です。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import proofread
import scrp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLYPH_W = GLYPH_H = 16


def load_names(path: str) -> dict:
    names = {}
    if not os.path.exists(path):
        return names
    with open(path, encoding="utf-8") as fh:
        fh.readline()
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = (line.split("\t") + ["", ""])[:3]
            names[parts[0].upper()] = {"name": parts[1], "role": parts[2]}
    return names


def load_glyph_chars(path: str) -> list[str]:
    chars = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            chars.append(line[0])
    return chars


def findings_json(findings) -> list[dict]:
    return [{"severity": f.severity, "rule": f.rule, "message": f.message,
             "detail": f.detail} for f in findings]


def build_data(args) -> dict:
    rules = proofread.load_rules(args.rules, args.lang)
    glossary = proofread.load_glossary(args.glossary) if os.path.exists(args.glossary) else []
    glyph_chars = load_glyph_chars(args.font_chars)
    font_set = set(glyph_chars)
    names = load_names(args.names)
    rows = scrp.read_tsv(args.tsv)

    with open(args.font, "rb") as fh:
        glyph_bytes = fh.read()
    expected = len(glyph_chars) * GLYPH_W * GLYPH_H // 8
    if len(glyph_bytes) < expected:
        raise scrp.ScrpError(
            f"{args.font}: {len(glyph_bytes)} バイトしかありません "
            f"({len(glyph_chars)} グリフには {expected} バイト必要)")

    hex_by_id: dict[str, str] = {}
    binary_name = None
    if args.binary and os.path.exists(args.binary):
        archive = scrp.read_archive(args.binary)
        binary_name = os.path.basename(args.binary)
        for i in range(archive.count):
            hex_by_id[str(i)] = archive.raw_block(i).hex(" ").upper()

    messages = []
    for row in rows:
        original = row.get("original", "")
        translation = row.get("translation", "")
        base_row = dict(row)
        base_row["translation"] = original
        messages.append({
            "id": row["id"],
            "offset": row.get("offset", ""),
            "size": row.get("size", ""),
            "hex": hex_by_id.get(row["id"], ""),
            "original": original,
            "translation": translation,
            "findings": {
                "original": findings_json(
                    proofread.check_row(base_row, rules, glossary, font_set)),
                "translation": findings_json(
                    proofread.check_row(row, rules, glossary, font_set)),
            },
        })

    return {
        "source": os.path.relpath(args.tsv, REPO),
        "binaryName": binary_name,
        "rules": {
            "lineMaxWidth": int(rules.get("line_max_width", 18)),
            "linesMax": int(rules.get("lines_max", 3)),
        },
        "glyphs": {
            "w": GLYPH_W,
            "h": GLYPH_H,
            "chars": "".join(glyph_chars),
            "bytes": base64.b64encode(glyph_bytes).decode("ascii"),
        },
        "names": names,
        "controls": {name: argc for _, (name, argc) in scrp.CONTROL_CODES.items()},
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

DOC_OPEN = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
"""
DOC_MID = "</head>\n<body>\n"
DOC_CLOSE = "</body>\n</html>\n"

#: <head> に入れる部分 (title と style)
HTML_HEAD = r"""<title>メッセージウィンドウ検査台 — リィンフォルト戦記</title>
<style>
:root {
  color-scheme: light dark;
  --ground: #eceef2;
  --surface: #ffffff;
  --surface-2: #f4f6f8;
  --line: #d2d9df;
  --line-soft: #e4e9ed;
  --text: #151a1f;
  --text-2: #5b666f;
  --text-3: #8a949c;
  --accent: #1f6f80;
  --accent-ink: #ffffff;
  --accent-soft: #dceaee;
  --err: #ac3c29;
  --err-soft: #f6e2de;
  --warn: #855d0d;
  --warn-soft: #f6eeda;
  --ok: #2d6641;
  --ok-soft: #e0eee5;
  --font-ui: "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic", YuGothic,
             "Noto Sans JP", IPAGothic, Meiryo, system-ui, sans-serif;
  --font-mono: ui-monospace, "SF Mono", SFMono-Regular, "Cascadia Mono",
               "Roboto Mono", Menlo, Consolas, monospace;
  --rail-l: 268px;
  --rail-r: 336px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground: #0d1116;
    --surface: #141a20;
    --surface-2: #1a2229;
    --line: #27313a;
    --line-soft: #1e262d;
    --text: #dfe5ea;
    --text-2: #8d99a3;
    --text-3: #6b7681;
    --accent: #5fc0d1;
    --accent-ink: #06222a;
    --accent-soft: #13323b;
    --err: #e0806c;
    --err-soft: #35211d;
    --warn: #d3a44a;
    --warn-soft: #322718;
    --ok: #6fb98a;
    --ok-soft: #1a2c22;
  }
}
:root[data-theme="dark"] {
  --ground: #0d1116;
  --surface: #141a20;
  --surface-2: #1a2229;
  --line: #27313a;
  --line-soft: #1e262d;
  --text: #dfe5ea;
  --text-2: #8d99a3;
  --text-3: #6b7681;
  --accent: #5fc0d1;
  --accent-ink: #06222a;
  --accent-soft: #13323b;
  --err: #e0806c;
  --err-soft: #35211d;
  --warn: #d3a44a;
  --warn-soft: #322718;
  --ok: #6fb98a;
  --ok-soft: #1a2c22;
}
:root[data-theme="light"] {
  --ground: #eceef2;
  --surface: #ffffff;
  --surface-2: #f4f6f8;
  --line: #d2d9df;
  --line-soft: #e4e9ed;
  --text: #151a1f;
  --text-2: #5b666f;
  --text-3: #8a949c;
  --accent: #1f6f80;
  --accent-ink: #ffffff;
  --accent-soft: #dceaee;
  --err: #ac3c29;
  --err-soft: #f6e2de;
  --warn: #855d0d;
  --warn-soft: #f6eeda;
  --ok: #2d6641;
  --ok-soft: #e0eee5;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--text);
  font-family: var(--font-ui);
  font-size: 14px;
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}
h1, h2, h3 { margin: 0; font-weight: 600; text-wrap: balance; }
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* ---------- 見出し ---------- */
.topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px 20px;
  padding: 14px 20px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}
.topbar h1 { font-size: 15px; letter-spacing: .02em; }
.topbar .src {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-3);
}
.stats { display: flex; gap: 18px; margin-left: auto; }
.stat { display: flex; align-items: baseline; gap: 6px; }
.stat b {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 16px;
  font-weight: 600;
}
.stat span {
  font-size: 10.5px;
  letter-spacing: .09em;
  color: var(--text-3);
}
.stat.is-err b { color: var(--err); }
.stat.is-warn b { color: var(--warn); }

/* ---------- 全体レイアウト ---------- */
.shell {
  display: grid;
  grid-template-columns: var(--rail-l) minmax(0, 1fr) var(--rail-r);
  gap: 1px;
  background: var(--line);
  min-height: calc(100vh - 52px);
}
.rail, .stage { background: var(--ground); min-width: 0; }

/* ---------- 左レール: メッセージ一覧 ---------- */
.rail-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line-soft);
  background: var(--surface);
}
.rail-head h2 { font-size: 11px; letter-spacing: .1em; color: var(--text-2); }
.filters { display: flex; gap: 4px; margin-left: auto; }
.chipbtn {
  border: 1px solid var(--line);
  background: transparent;
  border-radius: 999px;
  padding: 2px 9px;
  font-size: 11px;
  cursor: pointer;
  color: var(--text-2);
}
.chipbtn[aria-pressed="true"] {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-ink);
}
.msglist {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: calc(100vh - 100px);
  overflow-y: auto;
}
.msglist li { border-bottom: 1px solid var(--line-soft); }
.msgrow {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 14px;
  background: none;
  border: 0;
  border-left: 3px solid transparent;
  text-align: left;
  cursor: pointer;
  font-size: 12.5px;
}
.msgrow:hover { background: var(--surface-2); }
.msgrow[aria-current="true"] {
  background: var(--surface);
  border-left-color: var(--accent);
}
.msgrow .n {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 11px;
  color: var(--text-3);
}
.msgrow .t {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  display: inline-block;
}
.dot.err { background: var(--err); }
.dot.warn { background: var(--warn); }
.dot.ok { background: transparent; border: 1px solid var(--line); }

/* ---------- 中央: 画面ステージ ---------- */
.stage { padding: 20px; display: flex; flex-direction: column; gap: 16px; }
.screen-wrap {
  align-self: center;
  padding: 12px;
  background: #05080d;
  border: 1px solid var(--line);
  border-radius: 3px;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.04);
  max-width: 100%;
  overflow-x: auto;
}
canvas#screen {
  display: block;
  image-rendering: pixelated;
  max-width: 100%;
}
.transport {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 13px;
  border: 1px solid var(--line);
  background: var(--surface);
  border-radius: 3px;
  cursor: pointer;
  font-size: 13px;
}
.btn:hover { border-color: var(--accent); }
.btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-ink);
  font-weight: 600;
}
.btn kbd {
  font-family: var(--font-mono);
  font-size: 10.5px;
  opacity: .75;
  border: 1px solid currentColor;
  border-radius: 2px;
  padding: 0 3px;
}
.pageind {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--text-2);
  min-width: 84px;
  text-align: center;
}

/* ---------- 操作盤 ---------- */
.panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 3px;
}
.panel > h3 {
  font-size: 10.5px;
  letter-spacing: .1em;
  color: var(--text-2);
  padding: 9px 14px;
  border-bottom: 1px solid var(--line-soft);
}
.panel > .body { padding: 12px 14px; display: grid; gap: 12px; }
.controls {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 12px 20px;
  align-items: center;
}
.field { display: grid; gap: 4px; }
.field label {
  font-size: 10.5px;
  letter-spacing: .07em;
  color: var(--text-3);
}
.field .row { display: flex; align-items: center; gap: 8px; }
input[type="text"] {
  font: inherit;
  font-family: var(--font-ui);
  width: 100%;
  padding: 5px 8px;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: var(--surface-2);
  color: var(--text);
}
input[type="range"] { width: 100%; accent-color: var(--accent); }
.segmented { display: inline-flex; border: 1px solid var(--line); border-radius: 3px; overflow: hidden; }
.segmented button {
  border: 0;
  background: var(--surface);
  padding: 5px 12px;
  font-size: 12.5px;
  cursor: pointer;
  color: var(--text-2);
}
.segmented button + button { border-left: 1px solid var(--line); }
.segmented button[aria-pressed="true"] {
  background: var(--accent);
  color: var(--accent-ink);
  font-weight: 600;
}
.toggles { display: flex; flex-wrap: wrap; gap: 6px; }
.mono-num {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--text-2);
  min-width: 5.6em;
  white-space: nowrap;
}

/* ---------- 行幅メーター ---------- */
.meter { display: grid; gap: 6px; }
.meter .lineitem {
  display: grid;
  grid-template-columns: 3.4em minmax(0, 1fr) 5.2em;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
}
.meter .lbl { color: var(--text-3); font-family: var(--font-mono); }
.bar {
  position: relative;
  height: 9px;
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
  border-radius: 2px;
  overflow: hidden;
}
.bar i { position: absolute; inset: 0 auto 0 0; background: var(--accent); }
.bar i.over { background: var(--err); }
.bar .limit {
  position: absolute;
  top: -2px; bottom: -2px;
  width: 1px;
  background: var(--text-3);
}
.meter .val {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}
.meter .val.over { color: var(--err); font-weight: 600; }

/* ---------- 右レール: 検査 ---------- */
.rail-r { display: flex; flex-direction: column; }
.inspect { padding: 14px; display: grid; gap: 14px; align-content: start; overflow-y: auto; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; font-size: 12px; }
.kv dt { color: var(--text-3); font-size: 10.5px; letter-spacing: .06em; }
.kv dd {
  margin: 0;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.tagtext {
  font-size: 13px;
  line-height: 2;
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
  border-radius: 3px;
  padding: 9px 11px;
  overflow-wrap: anywhere;
}
.tagtext .tag {
  font-family: var(--font-mono);
  font-size: 10.5px;
  padding: 1px 4px;
  border-radius: 2px;
  background: var(--accent-soft);
  color: var(--accent);
  white-space: nowrap;
}
.tagtext .tag.ph { background: var(--warn-soft); color: var(--warn); font-weight: 600; }
.tagtext .miss {
  color: var(--err);
  background: var(--err-soft);
  border-bottom: 2px solid var(--err);
}
.findings { display: grid; gap: 8px; }
.finding {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 3px 9px;
  padding: 9px 11px;
  border: 1px solid var(--line-soft);
  border-left-width: 3px;
  border-radius: 3px;
  background: var(--surface);
  font-size: 12px;
}
.finding.ERROR { border-left-color: var(--err); }
.finding.WARN { border-left-color: var(--warn); }
.finding .sev {
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: .08em;
  padding: 1px 5px;
  border-radius: 2px;
  align-self: start;
  margin-top: 3px;
}
.finding.ERROR .sev { background: var(--err-soft); color: var(--err); }
.finding.WARN .sev { background: var(--warn-soft); color: var(--warn); }
.finding .rule {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  grid-column: 2;
}
.finding .msg { grid-column: 2; line-height: 1.55; }
.finding .det { grid-column: 2; font-size: 11px; color: var(--text-2); line-height: 1.5; }
.clean {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 3px;
  background: var(--ok-soft);
  color: var(--ok);
  font-size: 12.5px;
}
.hex {
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 1.85;
  color: var(--text-2);
  background: var(--surface-2);
  border: 1px solid var(--line-soft);
  border-radius: 3px;
  padding: 8px 10px;
  overflow-wrap: anywhere;
}
.note {
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.65;
}
.legend { display: grid; gap: 5px; font-size: 11.5px; color: var(--text-2); }
.legend div { display: flex; align-items: center; gap: 8px; }
.swatch { width: 22px; height: 10px; border-radius: 2px; flex: none; }

@media (prefers-reduced-motion: reduce) {
  * { animation-duration: 0s !important; transition-duration: 0s !important; }
}
@media (max-width: 1180px) {
  .shell { grid-template-columns: minmax(0, 1fr); }
  .msglist { max-height: 320px; }
  .rail-l { order: 2; }
  .stage { order: 1; }
  .rail-r { order: 3; }
}
</style>
"""

#: <body> に入れる部分
HTML_BODY = r"""<header class="topbar">
  <h1>メッセージウィンドウ検査台</h1>
  <span class="src" id="srcline"></span>
  <div class="stats" id="stats"></div>
</header>

<div class="shell">
  <section class="rail rail-l" aria-label="メッセージ一覧">
    <div class="rail-head">
      <h2>メッセージ</h2>
      <div class="filters" role="group" aria-label="表示する行の絞り込み">
        <button class="chipbtn" data-filter="all" aria-pressed="true">全件</button>
        <button class="chipbtn" data-filter="ERROR" aria-pressed="false">ERROR</button>
        <button class="chipbtn" data-filter="WARN" aria-pressed="false">WARN</button>
      </div>
    </div>
    <ul class="msglist" id="msglist"></ul>
  </section>

  <main class="stage">
    <div class="screen-wrap">
      <canvas id="screen" width="640" height="480"
              aria-label="ゲーム画面のメッセージウィンドウ (内容は右の検査パネルに文字で表示されます)"></canvas>
    </div>

    <div class="transport">
      <button class="btn" id="prev" title="前のメッセージ">◀ 前のメッセージ</button>
      <button class="btn primary" id="advance">送り <kbd>Space</kbd></button>
      <span class="pageind" id="pageind">1 / 1 ページ</span>
      <button class="btn" id="next" title="次のメッセージ">次のメッセージ ▶</button>
      <button class="btn" id="replay">頭から <kbd>R</kbd></button>
    </div>

    <div class="panel">
      <h3>表示条件</h3>
      <div class="body">
        <div class="controls">
          <div class="field">
            <label for="colsel">画面に出す列</label>
            <div class="segmented" role="group" id="colsel">
              <button data-col="original" aria-pressed="false">原文</button>
              <button data-col="translation" aria-pressed="true">訳文</button>
            </div>
          </div>
          <div class="field">
            <label for="pname">プレイヤー名 (&lt;VAR:00&gt; に入る文字列)</label>
            <input type="text" id="pname" value="アレン" maxlength="12"
                   autocomplete="off" spellcheck="false">
          </div>
          <div class="field">
            <label for="speed">文字送りの速さ</label>
            <div class="row">
              <input type="range" id="speed" min="0" max="120" step="10" value="40">
              <span class="mono-num" id="speedval">40字/秒</span>
            </div>
          </div>
          <div class="field">
            <label for="zoom">拡大率</label>
            <div class="row">
              <input type="range" id="zoom" min="1" max="3" step="1" value="2">
              <span class="mono-num" id="zoomval">×2</span>
            </div>
          </div>
        </div>
        <div class="toggles">
          <button class="chipbtn" id="tgrid" aria-pressed="false">文字マスを表示</button>
          <button class="chipbtn" id="tsafe" aria-pressed="true">枠の上限を表示</button>
          <button class="chipbtn" id="tvar" aria-pressed="true">変数の差し込みを色分け</button>
        </div>
      </div>
    </div>

    <div class="panel">
      <h3>行ごとの幅 — 現在のページ</h3>
      <div class="body">
        <div class="meter" id="meter"></div>
        <p class="note" id="meternote"></p>
      </div>
    </div>
  </main>

  <section class="rail rail-r" aria-label="検査パネル">
    <div class="rail-head"><h2>検査</h2></div>
    <div class="inspect">
      <dl class="kv" id="meta"></dl>
      <div>
        <div class="rail-head" style="background:none;border:0;padding:0 0 6px">
          <h2>タグ入りテキスト</h2>
        </div>
        <div class="tagtext" id="tagtext"></div>
        <p class="note" id="misslist"></p>
      </div>
      <div>
        <div class="rail-head" style="background:none;border:0;padding:0 0 6px">
          <h2>校正チェック</h2>
        </div>
        <div class="findings" id="findings"></div>
      </div>
      <div id="hexbox">
        <div class="rail-head" style="background:none;border:0;padding:0 0 6px">
          <h2>元データ (原文) の 16 進</h2>
        </div>
        <div class="hex" id="hex"></div>
      </div>
      <div class="legend">
        <div><span class="swatch" style="background:#f4f7ff"></span>本文</div>
        <div><span class="swatch" style="background:#8fd6ff"></span>変数の差し込み</div>
        <div><span class="swatch" style="background:#ff6a52"></span>枠から出た文字・表示できない文字</div>
      </div>
      <p class="note">
        話者名は別テーブル (<code>data/names.tsv</code>) にあるので、ウィンドウ上には
        ゲーム本体のフォントではなくシステムのフォントで描いています。実機では
        名前用のフォントが別に用意されていることが多い部分です。
      </p>
    </div>
  </section>
</div>

<script id="viewer-data" type="application/json">/*__DATA__*/</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("viewer-data").textContent);

/* ---------- 画面の寸法 (論理ピクセル) ---------- */
const CELL = DATA.glyphs.w;              /* 全角 1 文字 = 16px */
const LEAD = 4;                          /* 行間 */
const LINE_H = CELL + LEAD;
const COLS = DATA.rules.lineMaxWidth;    /* 1 行 18 文字 */
const ROWS = DATA.rules.linesMax;        /* 1 ページ 3 行 */
const PAD = 8;
const WIN_W = COLS * CELL + PAD * 2;
const WIN_H = ROWS * LINE_H - LEAD + PAD * 2;
/* 枠の外に 2 文字ぶんの余白を取る。18 文字を越えた文字が枠から出て、
   さらに画面の端で切れる様子まで見えるようにするため */
const SCREEN_W = WIN_W + CELL * 4;
const SCREEN_H = 240;
const WIN_X = (SCREEN_W - WIN_W) >> 1;
const WIN_Y = SCREEN_H - WIN_H - 10;
const TEXT_X = WIN_X + PAD;
const TEXT_Y = WIN_Y + PAD;

const INK = "#f4f7ff";
const INK_VAR = "#8fd6ff";
const INK_BAD = "#ff6a52";
const COLOR_TABLE = { "00": INK, "01": "#ffe680", "02": "#9ff0a8", "03": "#ff9dc0" };

/* ---------- グリフ ---------- */
const GB = Uint8Array.from(atob(DATA.glyphs.bytes), c => c.charCodeAt(0));
const GLYPH_BYTES = DATA.glyphs.w * DATA.glyphs.h / 8;
const CHAR_INDEX = new Map();
[...DATA.glyphs.chars].forEach((ch, i) => { if (!CHAR_INDEX.has(ch)) CHAR_INDEX.set(ch, i); });
const glyphCache = new Map();

function glyphFor(ch, color) {
  const idx = CHAR_INDEX.has(ch) ? CHAR_INDEX.get(ch) : -1;
  const key = idx + "|" + color;
  let cv = glyphCache.get(key);
  if (cv) return cv;
  cv = document.createElement("canvas");
  cv.width = DATA.glyphs.w;
  cv.height = DATA.glyphs.h;
  const g = cv.getContext("2d");
  g.fillStyle = color;
  if (idx < 0) {
    /* フォントに無い文字。実機と同じく □ になる */
    g.fillRect(2, 2, CELL - 4, 1);
    g.fillRect(2, CELL - 3, CELL - 4, 1);
    g.fillRect(2, 2, 1, CELL - 4);
    g.fillRect(CELL - 3, 2, 1, CELL - 4);
  } else {
    const base = idx * GLYPH_BYTES;
    for (let y = 0; y < DATA.glyphs.h; y++) {
      const bits = (GB[base + y * 2] << 8) | GB[base + y * 2 + 1];
      for (let x = 0; x < DATA.glyphs.w; x++) {
        if ((bits >> (DATA.glyphs.w - 1 - x)) & 1) g.fillRect(x, y, 1, 1);
      }
    }
  }
  glyphCache.set(key, cv);
  return cv;
}

const hasGlyph = ch => CHAR_INDEX.has(ch);

/* ---------- テキストの解釈 ---------- */
const TAG_RE = /<([A-Z]+)(?::([0-9A-Fa-f]{2}))?>/g;

function tokenize(text) {
  const out = [];
  let i = 0, m;
  TAG_RE.lastIndex = 0;
  while ((m = TAG_RE.exec(text))) {
    for (const ch of text.slice(i, m.index)) out.push({ t: "ch", v: ch });
    if (m[1] === "LT") out.push({ t: "ch", v: "<" });
    else out.push({ t: "tag", name: m[1], arg: m[2] || null });
    i = m.index + m[0].length;
  }
  for (const ch of text.slice(i)) out.push({ t: "ch", v: ch });
  return out;
}

function countPages(tokens) {
  let pages = 1, seenCharSince = false;
  for (const tk of tokens) {
    if (tk.t === "ch") { seenCharSince = true; continue; }
    if (tk.name === "WAIT" || tk.name === "CLEAR") {
      if (seenCharSince) { pages++; seenCharSince = false; }
    }
  }
  return seenCharSince ? pages : Math.max(1, pages - 1);
}

/* ---------- 状態 ---------- */
const ui = {
  index: 0,
  column: "translation",
  filter: "all",
  playerName: "アレン",
  speed: 40,
  zoom: 2,
  grid: false,
  safe: true,
  varTint: true,
};
let st = null;
let blinkOn = true;

function currentMessage() { return DATA.messages[ui.index]; }
function currentText() { return currentMessage()[ui.column] || ""; }
function currentFindings() { return currentMessage().findings[ui.column] || []; }

function resetPlayback() {
  const tokens = tokenize(currentText());
  st = {
    tokens,
    i: 0,
    lines: [[]],
    color: INK,
    speaker: null,
    waiting: false,
    done: false,
    page: 1,
    pages: countPages(tokens),
    acc: 0,
    last: performance.now(),
  };
  if (ui.speed === 0) runToStop();
}

function pushChar(ch, color, isVar) {
  st.lines[st.lines.length - 1].push({ ch, color, isVar });
}

/** 1 単位進める。文字を 1 つ置いたときだけ true を返す (文字送りの刻み) */
function tick() {
  if (st.waiting || st.done) return false;
  if (st.i >= st.tokens.length) { st.done = true; return false; }
  const tk = st.tokens[st.i++];
  if (tk.t === "ch") { pushChar(tk.v, st.color, false); return true; }
  switch (tk.name) {
    case "BR": st.lines.push([]); return false;
    case "CLEAR": st.lines = [[]]; st.page = Math.min(st.page + 1, st.pages); return false;
    case "WAIT": st.waiting = true; return false;
    case "COLOR": st.color = COLOR_TABLE[tk.arg] || INK; return false;
    case "NAME": st.speaker = tk.arg; return false;
    case "VAR": {
      for (const ch of ui.playerName) pushChar(ch, ui.varTint ? INK_VAR : st.color, true);
      return false;
    }
    default: return false;
  }
}

function runToStop() {
  let guard = 20000;
  while (!st.waiting && !st.done && guard-- > 0) tick();
}

function advance() {
  if (!st) return;
  if (!st.waiting && !st.done) { runToStop(); draw(); return; }  /* 送り: 残りを一気に出す */
  if (st.waiting) {
    st.waiting = false;
    st.lines = [[]];
    st.page = Math.min(st.page + 1, st.pages);
    if (ui.speed === 0) runToStop();
    draw();
    return;
  }
  select(Math.min(ui.index + 1, DATA.messages.length - 1));
}

/* ---------- 描画 ---------- */
const cv = document.getElementById("screen");
const ctx = cv.getContext("2d");
let backdrop = null;

function buildBackdrop() {
  const b = document.createElement("canvas");
  b.width = SCREEN_W;
  b.height = SCREEN_H;
  const g = b.getContext("2d");
  const sky = g.createLinearGradient(0, 0, 0, SCREEN_H);
  sky.addColorStop(0, "#0a1526");
  sky.addColorStop(0.55, "#16304b");
  sky.addColorStop(1, "#0b1a2b");
  g.fillStyle = sky;
  g.fillRect(0, 0, SCREEN_W, SCREEN_H);

  /* 星。乱数は使わず決め打ちの式で散らす */
  g.fillStyle = "rgba(210,230,255,.55)";
  for (let i = 0; i < 90; i++) {
    const x = (i * 61 + 13) % SCREEN_W;
    const y = (i * 29 + 7) % 110;
    if ((i * 7) % 5 === 0) g.fillRect(x, y, 1, 1);
  }
  /* 遠景の山 */
  g.fillStyle = "#0a1420";
  g.beginPath();
  g.moveTo(0, 150);
  const peaks = [[40, 108], [86, 132], [140, 96], [196, 126], [250, 104], [300, 134], [SCREEN_W, 118]];
  for (const [x, y] of peaks) g.lineTo(x, y);
  g.lineTo(SCREEN_W, SCREEN_H);
  g.lineTo(0, SCREEN_H);
  g.closePath();
  g.fill();
  /* 地面 */
  g.fillStyle = "#070f18";
  g.fillRect(0, 150, SCREEN_W, SCREEN_H - 150);
  return b;
}

function drawWindow(g) {
  g.fillStyle = "rgba(8,18,44,.9)";
  g.fillRect(WIN_X, WIN_Y, WIN_W, WIN_H);
  g.strokeStyle = "#7fa8cf";
  g.lineWidth = 1;
  g.strokeRect(WIN_X + 0.5, WIN_Y + 0.5, WIN_W - 1, WIN_H - 1);
  g.strokeStyle = "#2b4c78";
  g.strokeRect(WIN_X + 2.5, WIN_Y + 2.5, WIN_W - 5, WIN_H - 5);
}

function drawGuides(g) {
  if (ui.grid) {
    g.fillStyle = "rgba(140,190,230,.16)";
    for (let c = 1; c < COLS; c++) g.fillRect(TEXT_X + c * CELL, TEXT_Y, 1, ROWS * LINE_H - LEAD);
    for (let r = 1; r < ROWS; r++) g.fillRect(TEXT_X, TEXT_Y + r * LINE_H - LEAD / 2, COLS * CELL, 1);
  }
  if (ui.safe) {
    const right = TEXT_X + COLS * CELL;              /* 18 文字目の右端 */
    const bottom = TEXT_Y + ROWS * LINE_H - LEAD;    /* 3 行目の下端 */
    g.fillStyle = "rgba(255,106,82,.3)";
    g.fillRect(right, TEXT_Y, 1, bottom - TEXT_Y);
    g.fillRect(TEXT_X, bottom, right - TEXT_X, 1);
    g.fillStyle = "rgba(255,106,82,.85)";            /* 上限のかぎ括弧 */
    g.fillRect(right - 3, TEXT_Y, 4, 1);
    g.fillRect(right, TEXT_Y, 1, 4);
    g.fillRect(right - 3, bottom, 4, 1);
    g.fillRect(right, bottom - 3, 1, 4);
    g.fillRect(TEXT_X, bottom, 4, 1);
    g.fillRect(TEXT_X, bottom - 3, 1, 4);
  }
}

function drawSpeaker(g) {
  if (!st.speaker) return;
  const info = DATA.names[st.speaker];
  const label = info ? info.name : "<NAME:" + st.speaker + ">";
  g.font = "600 11px " + getComputedStyle(document.body).fontFamily;
  const w = Math.ceil(g.measureText(label).width) + 14;
  const h = 17;
  const x = WIN_X, y = WIN_Y - h - 2;
  g.fillStyle = "rgba(8,18,44,.92)";
  g.fillRect(x, y, w, h);
  g.strokeStyle = "#7fa8cf";
  g.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  g.fillStyle = "#cfe6ff";
  g.textBaseline = "middle";
  g.fillText(label, x + 7, y + h / 2 + 0.5);
}

function drawText(g) {
  st.lines.forEach((cells, li) => {
    const rowOver = li >= ROWS;
    const y = TEXT_Y + li * LINE_H;
    cells.forEach((cell, ci) => {
      const colOver = ci >= COLS;
      const missing = !hasGlyph(cell.ch);
      const color = (rowOver || colOver || missing) ? INK_BAD : cell.color;
      g.drawImage(glyphFor(cell.ch, color), TEXT_X + ci * CELL, y);
    });
  });
}

function drawPrompt(g) {
  if (!st.waiting || !blinkOn) return;
  const x = WIN_X + WIN_W - 14, y = WIN_Y + WIN_H - 10;
  g.fillStyle = INK;
  for (let i = 0; i < 4; i++) g.fillRect(x + i, y + i, 8 - i * 2, 1);
}

function draw() {
  if (!backdrop) backdrop = buildBackdrop();
  const z = ui.zoom;
  cv.width = SCREEN_W * z;
  cv.height = SCREEN_H * z;
  cv.style.width = SCREEN_W * z + "px";
  ctx.setTransform(z, 0, 0, z, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, SCREEN_W, SCREEN_H);
  ctx.drawImage(backdrop, 0, 0);
  drawWindow(ctx);
  drawGuides(ctx);
  drawSpeaker(ctx);
  drawText(ctx);
  drawPrompt(ctx);
  renderMeter();
  document.getElementById("pageind").textContent = st.page + " / " + st.pages + " ページ";
}

/* ---------- 行幅メーター ---------- */
function renderMeter() {
  const box = document.getElementById("meter");
  box.textContent = "";
  const lines = st.lines;
  const shown = Math.max(lines.length, 1);
  for (let li = 0; li < shown; li++) {
    const n = (lines[li] || []).length;
    const over = n > COLS || li >= ROWS;
    const item = document.createElement("div");
    item.className = "lineitem";
    const lbl = document.createElement("span");
    lbl.className = "lbl";
    lbl.textContent = (li + 1) + "行";
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = Math.min(100, (n / COLS) * 100) + "%";
    if (over) fill.classList.add("over");
    const limit = document.createElement("span");
    limit.className = "limit";
    limit.style.left = "100%";
    bar.append(fill, limit);
    const val = document.createElement("span");
    val.className = "val" + (over ? " over" : "");
    val.textContent = n + " / " + COLS;
    item.append(lbl, bar, val);
    box.append(item);
  }
  const note = document.getElementById("meternote");
  const rowsOver = Math.max(0, lines.length - ROWS);
  const parts = [];
  if (lines.some(l => l.length > COLS)) parts.push("枠の右端を越えた文字は画面の外で切れます。");
  if (rowsOver) parts.push(ROWS + " 行を越えた " + rowsOver + " 行は枠の下に出ています。");
  const varCount = lines.flat().filter(c => c.isVar).length;
  if (varCount) {
    parts.push("うち " + varCount + " 文字はプレイヤー名の差し込みです。名前の長さで行幅が変わります。");
  }
  note.textContent = parts.join(" ") ||
    "仕様は 1 行 " + COLS + " 文字・1 ページ " + ROWS + " 行。いまは収まっています。";
}

/* ---------- 右レール ---------- */
function renderInspector() {
  const m = currentMessage();
  const text = currentText();

  const meta = document.getElementById("meta");
  meta.textContent = "";
  const rows = [
    ["ID", m.id],
    ["オフセット", m.offset || "—"],
    ["元のサイズ", m.size ? m.size + " バイト" : "—"],
    ["表示文字数", [...text.replace(TAG_RE, "")].length + " 文字"],
  ];
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    meta.append(dt, dd);
  }

  /* タグ入りテキスト。フォントに無い文字には印を付ける */
  const box = document.getElementById("tagtext");
  box.textContent = "";
  const missing = new Set();
  let i = 0, mm;
  TAG_RE.lastIndex = 0;
  const emitPlain = s => {
    for (const ch of s) {
      const el = document.createElement("span");
      el.textContent = ch;
      if (!hasGlyph(ch)) {
        el.className = "miss";
        el.title = "フォントに無い文字";
        missing.add(ch);
      }
      box.append(el);
    }
  };
  while ((mm = TAG_RE.exec(text))) {
    emitPlain(text.slice(i, mm.index));
    const el = document.createElement("span");
    el.className = "tag" + (mm[1] === "VAR" || mm[1] === "NAME" ? " ph" : "");
    el.textContent = mm[0];
    box.append(el);
    i = mm.index + mm[0].length;
  }
  emitPlain(text.slice(i));

  document.getElementById("misslist").textContent = missing.size
    ? "フォントに無い文字: " + [...missing].join(" ") + " — 実機では □ になります。"
    : "";

  const fbox = document.getElementById("findings");
  fbox.textContent = "";
  const fs = currentFindings();
  if (!fs.length) {
    const ok = document.createElement("p");
    ok.className = "clean";
    ok.textContent = "指摘なし。仕様の範囲に収まっています。";
    fbox.append(ok);
  }
  for (const f of fs) {
    const el = document.createElement("div");
    el.className = "finding " + f.severity;
    const sev = document.createElement("span");
    sev.className = "sev";
    sev.textContent = f.severity;
    const rule = document.createElement("span");
    rule.className = "rule";
    rule.textContent = f.rule;
    const msg = document.createElement("span");
    msg.className = "msg";
    msg.textContent = f.message;
    el.append(sev, rule, msg);
    if (f.detail) {
      const det = document.createElement("span");
      det.className = "det";
      det.textContent = f.detail;
      el.append(det);
    }
    fbox.append(el);
  }

  const hexbox = document.getElementById("hexbox");
  if (m.hex) {
    hexbox.hidden = false;
    document.getElementById("hex").textContent = m.hex;
  } else {
    hexbox.hidden = true;
  }
}

/* ---------- 左レール ---------- */
function worstOf(list) {
  if (list.some(f => f.severity === "ERROR")) return "err";
  if (list.some(f => f.severity === "WARN")) return "warn";
  return "ok";
}

function renderList() {
  const ul = document.getElementById("msglist");
  ul.textContent = "";
  DATA.messages.forEach((m, idx) => {
    const sev = worstOf(m.findings[ui.column] || []);
    if (ui.filter === "ERROR" && sev !== "err") return;
    if (ui.filter === "WARN" && sev !== "warn") return;
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "msgrow";
    btn.type = "button";
    if (idx === ui.index) btn.setAttribute("aria-current", "true");
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = m.id;
    const t = document.createElement("span");
    t.className = "t";
    const plain = (m[ui.column] || "").replace(TAG_RE, " ").trim();
    t.textContent = plain || "(空)";
    const d = document.createElement("span");
    d.className = "dot " + sev;
    btn.append(n, t, d);
    btn.addEventListener("click", () => select(idx));
    li.append(btn);
    ul.append(li);
  });
}

function renderStats() {
  const list = DATA.messages;
  const counts = { ERROR: 0, WARN: 0 };
  let clean = 0;
  for (const m of list) {
    const fs = m.findings[ui.column] || [];
    for (const f of fs) if (counts[f.severity] !== undefined) counts[f.severity]++;
    if (!fs.length) clean++;
  }
  const box = document.getElementById("stats");
  box.textContent = "";
  const items = [
    ["メッセージ", list.length, ""],
    ["ERROR", counts.ERROR, "is-err"],
    ["WARN", counts.WARN, "is-warn"],
    ["指摘なし", clean, ""],
  ];
  for (const [label, value, cls] of items) {
    const el = document.createElement("div");
    el.className = "stat " + cls;
    const b = document.createElement("b");
    b.textContent = value;
    const s = document.createElement("span");
    s.textContent = label;
    el.append(b, s);
    box.append(el);
  }
  document.getElementById("srcline").textContent =
    DATA.source + (DATA.binaryName ? "  ·  " + DATA.binaryName : "") +
    "  ·  1行" + COLS + "文字 / 1ページ" + ROWS + "行";
}

function select(idx) {
  ui.index = idx;
  resetPlayback();
  renderList();
  renderInspector();
  draw();
}

/* ---------- 操作 ---------- */
document.getElementById("advance").addEventListener("click", advance);
document.getElementById("prev").addEventListener("click", () => select(Math.max(0, ui.index - 1)));
document.getElementById("next").addEventListener("click",
  () => select(Math.min(DATA.messages.length - 1, ui.index + 1)));
document.getElementById("replay").addEventListener("click", () => { resetPlayback(); draw(); });

for (const btn of document.querySelectorAll("#colsel button")) {
  btn.addEventListener("click", () => {
    ui.column = btn.dataset.col;
    for (const b of document.querySelectorAll("#colsel button")) {
      b.setAttribute("aria-pressed", String(b === btn));
    }
    renderStats();
    select(ui.index);
  });
}
for (const btn of document.querySelectorAll(".filters .chipbtn")) {
  btn.addEventListener("click", () => {
    ui.filter = btn.dataset.filter;
    for (const b of document.querySelectorAll(".filters .chipbtn")) {
      b.setAttribute("aria-pressed", String(b === btn));
    }
    renderList();
  });
}
const pname = document.getElementById("pname");
pname.addEventListener("input", () => {
  ui.playerName = pname.value || "　";
  resetPlayback();
  draw();
});
const speed = document.getElementById("speed");
speed.addEventListener("input", () => {
  ui.speed = Number(speed.value);
  document.getElementById("speedval").textContent =
    ui.speed === 0 ? "一括表示" : ui.speed + "字/秒";
  resetPlayback();
  draw();
});
const zoom = document.getElementById("zoom");
zoom.addEventListener("input", () => {
  ui.zoom = Number(zoom.value);
  document.getElementById("zoomval").textContent = "×" + ui.zoom;
  draw();
});
for (const [id, key] of [["tgrid", "grid"], ["tsafe", "safe"], ["tvar", "varTint"]]) {
  const btn = document.getElementById(id);
  btn.addEventListener("click", () => {
    ui[key] = !ui[key];
    btn.setAttribute("aria-pressed", String(ui[key]));
    if (key === "varTint") { resetPlayback(); }
    draw();
  });
}
document.addEventListener("keydown", ev => {
  if (ev.target instanceof HTMLInputElement) return;
  if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); advance(); }
  else if (ev.key === "ArrowLeft") { ev.preventDefault(); select(Math.max(0, ui.index - 1)); }
  else if (ev.key === "ArrowRight") {
    ev.preventDefault();
    select(Math.min(DATA.messages.length - 1, ui.index + 1));
  } else if (ev.key === "r" || ev.key === "R") { resetPlayback(); draw(); }
  else if (ev.key === "t" || ev.key === "T") {
    const other = ui.column === "translation" ? "original" : "translation";
    document.querySelector('#colsel button[data-col="' + other + '"]').click();
  }
});

/* ---------- 進行ループ ---------- */
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
function loop(now) {
  let needDraw = false;
  if (st && ui.speed > 0 && !st.waiting && !st.done) {
    const dt = now - st.last;
    st.last = now;
    st.acc += (dt / 1000) * ui.speed;
    let guard = 400;
    while (st.acc >= 1 && !st.waiting && !st.done && guard-- > 0) {
      if (tick()) st.acc -= 1;
      needDraw = true;
    }
  } else if (st) {
    st.last = now;
  }
  const nextBlink = reduced ? true : Math.floor(now / 480) % 2 === 0;
  if (nextBlink !== blinkOn) { blinkOn = nextBlink; if (st && st.waiting) needDraw = true; }
  if (needDraw) draw();
  requestAnimationFrame(loop);
}

renderStats();
select(0);
requestAnimationFrame(loop);
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="メッセージウィンドウのシミュレータを生成する")
    ap.add_argument("--tsv", default=os.path.join(REPO, "exercises", "qa_target.tsv"),
                    help="表示する TSV (既定: 課題用の訳文)")
    ap.add_argument("--binary", default=os.path.join(REPO, "work", "SCRIPT.BIN"),
                    help="16 進表示に使う .BIN (無ければ省略される)")
    ap.add_argument("--font", default=os.path.join(REPO, "work", "FONT.BIN"))
    ap.add_argument("--font-chars", default=os.path.join(REPO, "data", "font_chars.txt"))
    ap.add_argument("--names", default=os.path.join(REPO, "data", "names.tsv"))
    ap.add_argument("--rules", default=os.path.join(REPO, "data", "rules.json"))
    ap.add_argument("--glossary", default=os.path.join(REPO, "data", "glossary.tsv"))
    ap.add_argument("--lang", default="ja")
    ap.add_argument("-o", "--out", default=os.path.join(REPO, "work", "viewer.html"))
    ap.add_argument("--fragment", action="store_true",
                    help="<html> を付けず本文だけを出す (ページに埋め込む場合)")
    args = ap.parse_args()

    if not os.path.exists(args.font):
        raise scrp.ScrpError(
            f"{args.font} がありません。先に python3 tools/make_sample.py を実行してください。")

    data = build_data(args)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    body = HTML_BODY.replace("/*__DATA__*/", payload)
    if args.fragment:
        html = HTML_HEAD + body
    else:
        html = DOC_OPEN + HTML_HEAD + DOC_MID + body + DOC_CLOSE

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    errors = sum(1 for m in data["messages"] for f in m["findings"]["translation"]
                 if f["severity"] == "ERROR")
    print(f"{args.out}: {len(html):,} バイト")
    print(f"  メッセージ {len(data['messages'])} 件 / グリフ {len(data['glyphs']['chars'])} 字 / "
          f"訳文の ERROR {errors} 件")
    print(f"  題材: {data['source']}")
    print("  ブラウザで開いてください (外部への通信は一切しません)")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
