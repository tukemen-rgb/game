"""ブラウザで動かす検査 (tests/e2e/*.py) の共通部分.

    python3 tests/e2e/run_all.py          # 全部まとめて (playwright が無ければ skip)

Playwright と Chromium が要る。入れ方:
    pip install playwright && python3 -m playwright install chromium
Chromium を別の場所に置いているなら、環境変数 E2E_CHROMIUM にその実行ファイルを指定する。
"""

from __future__ import annotations

import os
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(REPO, "work")
os.makedirs(WORK, exist_ok=True)


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
