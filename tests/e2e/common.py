"""ブラウザで動かす検査 (tests/e2e/*.py) の共通部分.

    python3 tests/e2e/run_all.py          # 全部まとめて (playwright が無ければ skip)

Playwright と Chromium が要る。入れ方:
    pip install playwright && python3 -m playwright install chromium
Chromium を別の場所に置いているなら、環境変数 E2E_CHROMIUM にその実行ファイルを指定する。
"""

from __future__ import annotations

import os
import re
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(REPO, "work")
os.makedirs(WORK, exist_ok=True)


def doc_shape(quote: str) -> str:
    """docs の引用を、実際の出力に当てはめて探すための正規表現にする (#208).

    文書は数の代わりに `N` / `M` / `K` と書き、途中の省略を `…` と書く。
    **1 文字で立っているときだけ**数に変えること。どこでも変えると
    `ANSI` の `N` まで数になり、**何を書いても当たらない**引用ができあがる。
    それを緑のまま見逃した実例が #207 (tests/run_tests.py 側で見つけて直した)。
    ここに置いてあるのは、同じ間違いを 2 か所で繰り返さないため。
    """
    rx = re.escape(quote)
    rx = re.sub(r"(?<![0-9A-Za-z])[NMK](?![0-9A-Za-z])", r"\\d[\\d,]*", rx)
    #: `…` の前後の空白は**読みやすさのために置いた空白**で、出力には無いことがある
    #: (「/ … 文字表に無い K 種」の実物は「/ 上の …のうち文字表に無い 0 種」)
    return re.sub(r"(?:\\?\s)*…(?:\\?\s)*", ".{0,60}", rx)


def chromium_path() -> str | None:
    """明示指定 → よくある置き場 → Playwright 同梱 (None) の順."""
    env = os.environ.get("E2E_CHROMIUM")
    if env and os.path.exists(env):
        return env
    for cand in ("/opt/pw-browsers/chromium", shutil.which("chromium"), shutil.which("chromium-browser")):
        if cand and os.path.exists(cand):
            return cand
    return None


async def launch(p):
    path = chromium_path()
    if path:
        return await p.chromium.launch(executable_path=path)
    return await p.chromium.launch()


async def select_file(page, query: str, name: str):
    """一覧からファイルを選ぶ。固定の待ち時間ではなく、選ばれたことを待つ.

    固定の 300 ミリ秒待ちにしていたら、検査をまとめて走らせたときだけ落ちた (#73)。
    選び終わる前に次のボタンを押していて、押した先が別のファイルだった。
    """
    await page.fill("#treeq", query)
    row = f"#tree .filerow:has(.nm:text-is('{name}'))"
    await page.wait_for_selector(row, timeout=20000)
    await page.click(row)
    await page.wait_for_selector(row + '[aria-current="true"]', timeout=20000)
