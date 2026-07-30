#!/usr/bin/env python3
"""抽出した TSV を機械的に校正チェックする (= ローカライズ QA の一次チェック).

    python3 tools/proofread.py exercises/qa_target.tsv

人間が読む前に機械で落とせるものを全部落とすのが目的です。ここで拾うのは
「読めば分かるが見落としやすい」種類の不具合ばかりで、実際の QA でも
チェックリストの上半分はこの手の項目で埋まります。

チェック項目
------------
placeholder   <VAR:xx> <NAME:xx> の欠落・改変        ERROR  変数が消えると実機で崩れる
control       <WAIT> <COLOR:xx> の欠落・増加          WARN   演出が変わる
font          フォントに無い文字 (実機で □ になる)   ERROR
halfwidth     日本語文中の半角文字・半角カナ          ERROR
line_width    1 行の表示幅オーバー                    ERROR  枠からはみ出す
line_count    1 ページの行数オーバー                  ERROR  下の行が切れる
kinsoku       行頭・行末の禁則違反                    WARN
glossary      用語集の誤表記／正式表記の消失          ERROR / WARN
notation      表記ルール違反 (data/rules.json)        設定どおり
empty         訳文が空                                ERROR

<BR> の増減そのものは指摘しません。行の折り返しを直すのは校正の仕事なので、
代わりに line_width / line_count で結果だけを見ます。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEVERITIES = {"ERROR": 0, "WARN": 1, "INFO": 2}
PLACEHOLDER_TAGS = {"NAME", "VAR"}


class Finding:
    def __init__(self, row_id: str, severity: str, rule: str, message: str,
                 detail: str = "", lineno: int | None = None):
        self.row_id = row_id
        self.severity = severity
        self.rule = rule
        self.message = message
        self.detail = detail
        self.lineno = lineno

    def sort_key(self):
        try:
            rid = int(self.row_id)
        except ValueError:
            rid = 1 << 30
        return (rid, SEVERITIES.get(self.severity, 9), self.rule)


def load_rules(path: str, lang: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        rules = json.load(fh)
    if lang not in rules:
        raise scrp.ScrpError(f"{path}: 言語 {lang!r} の設定がありません")
    return rules[lang]


def load_glossary(path: str) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        if header[:2] != ["term", "forbidden"]:
            raise scrp.ScrpError(f"{path}: 見出しは 'term\\tforbidden\\tnote' である必要があります")
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = (line.split("\t") + ["", ""])[:3]
            entries.append({
                "term": parts[0],
                "forbidden": [w for w in parts[1].split("|") if w],
                "note": parts[2],
            })
    return entries


def load_font_chars(path: str) -> set[str]:
    chars = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            chars.add(line[0])
    return chars


def load_names(path: str) -> dict[str, str]:
    names = {}
    with open(path, encoding="utf-8") as fh:
        fh.readline()
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                names[parts[0].upper()] = parts[1]
    return names


def pages_of(text: str) -> list[list[str]]:
    """ページ (ボタン待ち・クリアで区切る) ごとの行リストに割る."""
    pages = []
    for page in re.split(r"<WAIT>|<CLEAR>", text):
        if not scrp.strip_tags(page).strip():
            continue
        pages.append(page.split("<BR>"))
    return pages


def check_row(row: dict, rules: dict, glossary: list[dict],
              font_chars: set[str] | None) -> list[Finding]:
    rid = row.get("id", "?")
    lineno = row.get("_lineno")
    original = row.get("original", "")
    translation = row.get("translation", None)
    out: list[Finding] = []

    def add(severity: str, rule: str, message: str, detail: str = ""):
        out.append(Finding(rid, severity, rule, message, detail, lineno))

    if translation is not None and scrp.strip_tags(original).strip():
        if not translation.strip():
            # 空欄は「原文をそのまま使う」という扱いなので、事故ではなく未着手
            add("WARN", "untranslated", "訳文が空欄です (原文がそのまま入ります)")
            return out
        if not scrp.strip_tags(translation).strip():
            add("ERROR", "empty", "訳文にタグしか残っていません (本文が消えています)", translation)
            return out
    text = scrp.final_text(row)

    # --- タグの照合 -------------------------------------------------------
    before = Counter(f"<{t}{':' + a if a else ''}>" for t, a in scrp.iter_tags(original))
    after = Counter(f"<{t}{':' + a if a else ''}>" for t, a in scrp.iter_tags(text))
    for tag in set(before) | set(after):
        name = tag[1:].split(":")[0].rstrip(">")
        if name in ("BR", "CLEAR", "LT"):
            continue  # 折り返しの調整は校正の裁量
        delta = after[tag] - before[tag]
        if delta == 0:
            continue
        severity, rule = ("ERROR", "placeholder") if name in PLACEHOLDER_TAGS else ("WARN", "control")
        verb = f"{delta} 個増えています" if delta > 0 else f"{-delta} 個減っています"
        add(severity, rule, f"{tag} が原文と一致しません ({verb})",
            f"原文 {before[tag]} 個 / 訳文 {after[tag]} 個")

    visible = scrp.strip_tags(text)

    # --- 半角文字の混在 ---------------------------------------------------
    halfwidth = []
    if not rules.get("allow_halfwidth", False):
        for ch in visible:
            if ch in "　":
                continue
            if unicodedata.east_asian_width(ch) in ("H", "Na", "N") and not ch.isspace():
                halfwidth.append(ch)
    if halfwidth:
        uniq = "".join(dict.fromkeys(halfwidth))
        kana = any("｡" <= ch <= "ﾟ" for ch in uniq)
        add("ERROR", "halfwidth",
            "半角カナが混ざっています。全角に直します" if kana
            else "半角文字が混ざっています。日本語文中は全角に統一します",
            f"該当文字: {uniq}")

    # --- フォントに無い文字 -----------------------------------------------
    if font_chars and rules.get("check_font_table", True):
        missing = [ch for ch in visible
                   if ch not in font_chars
                   and unicodedata.east_asian_width(ch) in "FWA"]
        if missing:
            uniq = "".join(dict.fromkeys(missing))
            add("ERROR", "font",
                f"フォントに無い文字です。実機では □ になります: {uniq}",
                "使える文字は data/font_chars.txt を参照")

    # --- 行の幅と行数 -----------------------------------------------------
    max_width = float(rules.get("line_max_width", 0) or 0)
    lines_max = int(rules.get("lines_max", 0) or 0)
    pages = pages_of(text)
    for p, lines in enumerate(pages):
        if lines_max and len(lines) > lines_max:
            add("ERROR", "line_count",
                f"{p + 1} ページ目が {len(lines)} 行あります (上限 {lines_max} 行)",
                "<BR> を減らすか <CLEAR> でページを分けます")
        for i, line in enumerate(lines):
            width = scrp.display_width(line)
            if max_width and width > max_width:
                add("ERROR", "line_width",
                    f"{p + 1} ページ {i + 1} 行目が {width:g} 文字分です (上限 {max_width:g})",
                    scrp.strip_tags(line))
            stripped = scrp.strip_tags(line)
            if not stripped:
                continue
            if i > 0 and stripped[0] in rules.get("forbid_line_start", ""):
                add("WARN", "kinsoku",
                    f"{p + 1} ページ {i + 1} 行目が {stripped[0]!r} で始まっています (行頭禁則)",
                    stripped)
            if i < len(lines) - 1 and stripped[-1] in rules.get("forbid_line_end", ""):
                add("WARN", "kinsoku",
                    f"{p + 1} ページ {i + 1} 行目が {stripped[-1]!r} で終わっています (行末禁則)",
                    stripped)

    # --- 表記ルール -------------------------------------------------------
    for entry in rules.get("ban_patterns", []):
        for m in re.finditer(entry["pattern"], text):
            add(entry.get("severity", "WARN"), "notation", entry["message"],
                f"{m.start()} 文字目付近: {text[max(0, m.start() - 6):m.end() + 6]}")
            break  # 同じルールは 1 行につき 1 回だけ報告する

    # --- 用語集 -----------------------------------------------------------
    for entry in glossary:
        for wrong in entry["forbidden"]:
            if wrong in text:
                add("ERROR", "glossary",
                    f"用語集では {entry['term']!r} です ({wrong!r} が使われています)",
                    entry["note"])
        if entry["term"] and entry["term"] in original and entry["term"] not in text:
            if not any(w in text for w in entry["forbidden"]):
                add("WARN", "glossary",
                    f"原文にあった用語 {entry['term']!r} が訳文から消えています",
                    entry["note"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="抽出した TSV を機械的に校正チェックする")
    ap.add_argument("tsv", help="チェックする TSV")
    ap.add_argument("--lang", default="ja", help="rules.json 内の言語キー (既定: ja)")
    ap.add_argument("--rules", default=os.path.join(REPO, "data", "rules.json"))
    ap.add_argument("--glossary", default=os.path.join(REPO, "data", "glossary.tsv"))
    ap.add_argument("--font-chars", default=os.path.join(REPO, "data", "font_chars.txt"))
    ap.add_argument("--names", default=os.path.join(REPO, "data", "names.tsv"))
    ap.add_argument("--no-font-check", action="store_true")
    ap.add_argument("--only-errors", action="store_true", help="ERROR だけ表示する")
    ap.add_argument("--report", help="指摘一覧を TSV で書き出す")
    args = ap.parse_args()

    rules = load_rules(args.rules, args.lang)
    glossary = load_glossary(args.glossary) if os.path.exists(args.glossary) else []
    font_chars = None
    if not args.no_font_check and os.path.exists(args.font_chars):
        font_chars = load_font_chars(args.font_chars)
    names = load_names(args.names) if os.path.exists(args.names) else {}

    rows = scrp.read_tsv(args.tsv)
    findings: list[Finding] = []
    for row in rows:
        findings += check_row(row, rules, glossary, font_chars)
    findings.sort(key=lambda f: f.sort_key())

    counts = Counter(f.severity for f in findings)
    speaker_of = {}
    for row in rows:
        m = re.search(r"<NAME:([0-9A-Fa-f]{2})>", row.get("original", ""))
        if m:
            speaker_of[row["id"]] = names.get(m.group(1).upper(), "")

    shown = [f for f in findings if not (args.only_errors and f.severity != "ERROR")]
    current = None
    for f in shown:
        if f.row_id != current:
            current = f.row_id
            speaker = speaker_of.get(f.row_id, "")
            head = f"[id {f.row_id}]" + (f" {speaker}" if speaker else "")
            row = next((r for r in rows if r["id"] == f.row_id), None)
            print()
            print(head)
            if row is not None:
                print(f"  原文: {row.get('original', '')}")
                text = scrp.final_text(row)
                if text != row.get("original", ""):
                    print(f"  訳文: {text}")
        print(f"  {f.severity:<5} {f.rule:<11} {f.message}")
        if f.detail:
            print(f"        {' ' * 11} └ {f.detail}")

    print()
    print(f"{len(rows)} 行をチェック: "
          f"ERROR {counts['ERROR']} 件 / WARN {counts['WARN']} 件 / "
          f"問題なし {len(rows) - len({f.row_id for f in findings})} 行")
    orig_chars = sum(len(scrp.strip_tags(r.get("original", ""))) for r in rows)
    new_chars = sum(len(scrp.strip_tags(scrp.final_text(r))) for r in rows)
    print(f"表示文字数: 原文 {orig_chars:,} → 訳文 {new_chars:,} ({new_chars - orig_chars:+,})")

    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("id\tseverity\trule\tmessage\tdetail\n")
            for f in findings:
                fh.write(f"{f.row_id}\t{f.severity}\t{f.rule}\t{f.message}\t{f.detail}\n")
        print(f"指摘一覧を書き出しました: {args.report}")

    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    scrp.cli_main(main)
