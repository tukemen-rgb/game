# 答え

先に自力でやってから開いてください。

| ファイル | 対応する課題 | 中身 |
| --- | --- | --- |
| `custom.tbl` | 課題 3 | `work/MSG_ENC.BIN` の文字テーブル |
| `qa_answers.md` | 課題 5 | 仕込んだ不具合の一覧と直し方 |
| `plant_errors.py` | 課題 5 | `exercises/qa_target.tsv` を作るスクリプト |

`custom.tbl` は `tools/make_sample.py` が毎回書き出すので、消しても復元できます。

課題 1・2・4 は答え合わせをツールで行います。

```bash
python3 tools/hexdump.py work/MSG_ENC.BIN --struct            # 課題 1
python3 tools/dump_text.py work/SCRIPT.BIN -o work/SCRIPT.tsv  # 課題 2
python3 tools/font_view.py work/FONT.BIN --find あ --chars data/font_chars.txt  # 課題 4
```
