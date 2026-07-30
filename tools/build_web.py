#!/usr/bin/env python3
"""web/ の構造探査台を 1 枚の HTML にまとめる.

web/index.html はそのままブラウザで開けますが (ES モジュールを使わず
1 本の script にしてあるので file:// でも動きます)、配布や公開のためには
CSS と JS を埋め込んだ 1 ファイルにしたほうが扱いやすいのでまとめます。

    python3 tools/build_web.py
    # → work/explorer.html

--embed-sample を付けると work/RINFOLT.iso を埋め込み、ページ上の
「練習用のイメージを読む」ボタンが使えるようになります。file:// では
fetch が使えないので、サンプルを持たせるにはこの方法しかありません。
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    ap = argparse.ArgumentParser(description="構造探査台を 1 枚の HTML にまとめる")
    ap.add_argument("--webdir", default=os.path.join(REPO, "web"))
    ap.add_argument("-o", "--out", default=os.path.join(REPO, "work", "explorer.html"))
    ap.add_argument("--embed-sample", metavar="ISO",
                    nargs="?", const=os.path.join(REPO, "work", "RINFOLT.iso"),
                    help="練習用イメージを埋め込む (既定: work/RINFOLT.iso)")
    ap.add_argument("--fragment", action="store_true",
                    help="<html> を付けず本文だけを出す (ページに埋め込む場合)")
    args = ap.parse_args()

    html = read(os.path.join(args.webdir, "index.html"))
    css = read(os.path.join(args.webdir, "style.css"))
    js = read(os.path.join(args.webdir, "app.js"))

    sample_js = ""
    if args.embed_sample:
        if not os.path.exists(args.embed_sample):
            raise scrp.ScrpError(
                f"{args.embed_sample} がありません。先に python3 tools/make_iso.py を実行してください。")
        with open(args.embed_sample, "rb") as fh:
            blob = fh.read()
        b64 = base64.b64encode(blob).decode("ascii")
        name = os.path.basename(args.embed_sample)
        sample_js = (f'window.SAMPLE_ISO_NAME = "{name}";\n'
                     f'window.SAMPLE_ISO = "{b64}";\n')

    html = html.replace('<link rel="stylesheet" href="style.css">',
                        "<style>\n" + css + "\n</style>")
    html = html.replace('<script src="app.js"></script>',
                        "<script>\n" + sample_js + js + "\n</script>")

    if args.fragment:
        # <head> の中身と <body> の中身を続けて並べる (公開時は外側が付く)
        head = re.search(r"<title>.*?</title>", html, re.S).group(0)
        style = re.search(r"<style>.*?</style>", html, re.S).group(0)
        body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
        html = head + "\n" + style + "\n" + body.strip() + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    for bad in ("http://", "https://", 'src="app.js"', 'href="style.css"'):
        if bad in html:
            raise scrp.ScrpError(f"出力に外部参照が残っています: {bad}")

    print(f"{args.out}: {len(html):,} バイト")
    if sample_js:
        print(f"  練習用イメージを埋め込みました ({len(blob):,} バイト → base64 {len(b64):,} 文字)")
    print("  外部への通信は一切しません")
    return 0


if __name__ == "__main__":
    scrp.cli_main(main)
