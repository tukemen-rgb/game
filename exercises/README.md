# 課題

答えは `answers/` にあります。**先に自力でやってから開いてください。**
どれも 15〜30 分程度を想定しています。

準備:

```bash
python3 tools/make_sample.py
```

---

## 課題 1 — ヘッダを手で読む

`work/MSG_ENC.BIN` の先頭 32 バイトを 16 進で表示し、次を紙かメモに書き出す。

```bash
python3 tools/hexdump.py work/MSG_ENC.BIN --length 32
```

1. マジック (先頭 4 バイト) は何か
2. メッセージ数はいくつか。どのバイトを見て、どう読んだか
3. 0 番目のメッセージの本文が始まるオフセットはいくらか
4. ポインタテーブルの直後のアドレスと、3 の答えは一致するか

**確認:** `python3 tools/hexdump.py work/MSG_ENC.BIN --struct`

要点: リトルエンディアンを電卓なしで読めるようになること。

---

## 課題 2 — Shift-JIS のセリフを 16 進から書き出す

`work/SCRIPT.BIN` の id 9 のメッセージを 16 進で表示し、**抽出ツールを使わずに**
本文を書き出す。改行や終端がどのバイトかも指摘する。

```bash
python3 tools/hexdump.py work/SCRIPT.BIN --message 9
```

**確認:** `python3 tools/dump_text.py work/SCRIPT.BIN -o work/SCRIPT.tsv` の
id 9 の行

要点: `82 xx` がひらがな、`F0` が改行、`FF` が終端、という対応を目で覚えること。

---

## 課題 3 — 独自文字コードを解読する

`work/MSG_ENC.BIN` は独自の文字コードです。`answers/custom.tbl` を見ずに、
**id 0 のメッセージを全部読めるようにする**。

手順の目安:

```bash
# 1. 相対検索でかなのテーブルを割る
python3 tools/relative_search.py work/MSG_ENC.BIN --search ここは \
    --derive work/my_guess.tbl

# 2. 割れたテーブルで眺める (読めない部分が漢字・記号)
python3 tools/hexdump.py work/MSG_ENC.BIN --table work/my_guess.tbl --message 0

# 3. 読めないバイトに対応する文字を work/my_guess.tbl に手で足す
#    (SCRIPT.BIN の同じ id と見比べると答えが分かる)

# 4. 全文が読めたら抽出する
python3 tools/dump_text.py work/MSG_ENC.BIN --table work/my_guess.tbl \
    -o work/mine.tsv
```

追加で考えること: 漢字が 2 バイトになっているのはなぜか。
1 バイト目に使われている値は何か。

**確認:** `answers/custom.tbl` と `work/my_guess.tbl` を比べる

---

## 課題 4 — フォントのグリフから文字を特定する

`work/FONT.BIN` は 16x16・1bpp のグリフが並んだだけのファイルです。
`data/font_chars.txt` を見ずに、グリフ 0〜20 が何の文字か答える。

```bash
python3 tools/font_view.py work/FONT.BIN --ascii 0-20
```

PNG で一覧を出すと、並びの規則がすぐ見えます。

```bash
python3 tools/font_view.py work/FONT.BIN --png work/font_sheet.png --cols 24
```

**確認:** `python3 tools/font_view.py work/FONT.BIN --find あ --chars data/font_chars.txt`

要点: フォントが画像で持たれている場合、**文字コードとはグリフの並び順の
番号にすぎない**。だからフォントシートを読むこと自体がテーブルの復元になる。

---

## 課題 5 — 目視で校正する (これが本番)

`exercises/qa_target.tsv` は、翻訳会社から戻ってきた訳文のつもりのファイルです。
`original` 列が原文、`translation` 列が戻ってきた訳文です。

**まず `proofread.py` を使わずに**、表計算ソフトかテキストエディタで開いて
不具合を洗い出す。見つけた行の id と、何が問題かをメモする。

仕様:

* 1 行 18 文字以内 (全角 1 文字 = 1.0、半角 = 0.5)
* 1 ページ 3 行以内 (`<WAIT>` `<CLEAR>` でページが変わる)
* 使える文字は `data/font_chars.txt` にあるものだけ
* 用語は `data/glossary.tsv` に従う
* `<VAR:xx>` `<NAME:xx>` は原文と同じものが同じ数だけ必要

洗い出したら、機械にかけて比べる。

```bash
python3 tools/proofread.py exercises/qa_target.tsv
```

さらに、画面でどう崩れるかを見る。原文と訳文を `T` キーで切り替えると、
仕込んだ不具合が見た目の崩れとして現れます。

```bash
python3 tools/make_viewer.py     # → work/viewer.html をブラウザで開く
```

**自分が見つけられなかった項目が、自分のチェックリストに足すべきもの**です。
逆に、機械が拾えていないのに自分が気づいたものがあれば、それは
`proofread.py` に足すべきルールです。

**確認:** `answers/qa_answers.md`

---

## 課題 6 — 直して入れ直す

課題 5 で見つけた不具合を全部直したファイルを作り、データに入れ直す。
`exercises/qa_target.tsv` をコピーして編集してください。

```bash
cp exercises/qa_target.tsv work/qa_fixed.tsv
# work/qa_fixed.tsv の translation 列を直す

python3 tools/proofread.py work/qa_fixed.tsv          # ERROR 0 件にする
python3 tools/insert_text.py work/qa_fixed.tsv -o work/SCRIPT_fixed.BIN \
    --original work/SCRIPT.BIN                        # 容量に収める
python3 tools/dump_text.py work/SCRIPT_fixed.BIN -o work/verify.tsv
```

```bash
python3 tools/make_viewer.py --tsv work/qa_fixed.tsv -o work/viewer_fixed.html
```

条件:

* `proofread.py` の ERROR が 0 件
* `insert_text.py` が容量オーバーで落ちない
* 入れ直したファイルから再抽出したテキストが、意図したものと一致する
* 検査台の画面で、枠から出た文字と □ が 1 つも無い

最後の再抽出まで含めて確認するのが大事です。「TSV は直したがデータに
反映されていない」が実務でよくある事故で、往復して確認する癖をつけます。

---

## 課題 7 — ツールを直す (応用)

[04-校正とQA.md](../docs/04-校正とQA.md) に書いた `proofread.py` の弱点を直す。

`<VAR:00>` の表示幅を 0 として数えているので、プレイヤー名が入ったときの
実際の行幅が分からない。**変数の最大長 (たとえば 6 文字) を仮定して
チェックできるようにする。**

* `scrp.display_width()` に、タグの想定幅を渡せるようにする
* `data/rules.json` に `var_width` のような設定を足す
* `--var-width 6` で上書きできるようにする
* `tests/run_tests.py` にテストを足す

これができると、`id 2` の `<VAR:00>！　こんなところにいたのね。` が
仕様違反として検出されるようになります (14 文字 + 名前 6 文字 = 20 文字 > 18)。
検査台 (`make_viewer.py`) でプレイヤー名を `ながいなまえ` にすると、
直すべき状態が画面で確認できます。
