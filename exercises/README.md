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
python3 tools/hexdump.py work/SCRIPT.BIN --message 0

# 4. id 0 が最後まで読めたら、この課題は達成
python3 tools/hexdump.py work/MSG_ENC.BIN --table work/my_guess.tbl --message 0
```

手順 2 の出力の下に「表に無いバイト」の一覧が出ます。**それが足すべきものの
全部**です。点を目で数える必要はありません。

`--message 0` を付けずに `dump_text.py` で全文を抽出しようとすると、まだ通りません。
この課題が求めているのは id 0 だけで、全文には漢字を全部そろえる必要があるからです
(何件読めていて何が足りないかは、そのとき道具が出します)。全部そろえたければ
そのまま続けてください。

```bash
python3 tools/dump_text.py work/MSG_ENC.BIN --table work/my_guess.tbl -o work/mine.tsv
```

追加で考えること: 漢字が 2 バイトになっているのはなぜか。
1 バイト目に使われている値は何か。

**確認:** `answers/custom.tbl` と `work/my_guess.tbl` を比べる

---

## 課題 4 — フォントのグリフから文字を特定する

`work/FONT.BIN` は 16x16・1bpp のグリフが並んだだけのファイルです。
`data/font_chars.txt` を見ずに、グリフ 0〜20 が何の文字か答える。

```bash
python3 tools/font_view.py work/FONT.BIN --ascii 0-20 --across 4
```

`--across 4` で 4 個ずつ横に並びます。**並びの規則は隣り合わせにして初めて
見えます** (小書き → 大きい字、清音 → 濁音で対になっていて、濁音は右上に
点が 2 つ足されているだけ)。1 つずつ縦に流すと 400 行を超えて画面から消えます。

よく見ると `け` と `げ` だけ、字全体が 1 ドット下にずれています。点が増えた分
だけ字の外形が変わり、**16x16 の枠の中で中央に置き直される**からです。
形を機械で見比べて文字を当てる道具を作るなら、この 1 ドットが効きます
(実物のフォントでも同じことが起きます。docs/11 の 10 節)。

PNG で全体の一覧を出すと、行の折り返しごと見えます (Pillow が要ります。
無ければ上の `--ascii` で同じ中身が見られます)。

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
python3 tools/compare_tsv.py work/qa_fixed.tsv work/verify.tsv \
    --left translation --right original               # 往復して一致するか
```

最後の 1 行が **往復の確認** です。39 行を目で見比べるのは現実的ではないので、
機械に突き合わせさせます。全部一致すれば終了コード 0、1 行でも違えば 1 と、
食い違った id が出ます。

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

---

## 課題 8 — 実物と同じ形で一周する (総合)

市販ソフトと同じ形 (別ファイルの索引、フォルダの入れ子、フォント画像、入れ物、
表が複数の会話) の練習データで、索引から校正用 TSV まで通す。
手順書は [10-僕夏2の手順.md](../docs/10-僕夏2の手順.md)。

```bash
python3 tools/make_boku2_sample.py                 # work/BOKU2SAMPLE/ に一式と答え
python3 tools/boku2.py check work/BOKU2SAMPLE      # まず診断 (問題なし、と出る)
```

やること:

1. 構造探査台に `BOKU2.IDX` `BOKU2.IMG` `MAP/M_A01000.BIN` を読ませ、索引タブで
   切り分ける。`system/system.msg` のようにフォルダ付きの名前が並ぶことを確かめる
2. `system.msg` を「既知の形式」で `.msg` として読む。文字番号 `[123]` の並びを見る
3. `bk_font.tms` を開き「文字の番号を重ねる」。`.msg` 読みの「使われている文字番号」の
   雛形 (`12=`) に、画像の該当する番号の文字を書いて貼る (練習データの画像は模様なので、
   付属の `font.txt` を見て埋めてよい)
4. `M_A01000.BIN` を「マップの入れ物を切り分ける」→ `1.bin` を `.msg` として読む
5. 「校正用の TSV をコピー」→ ファイルに貼り、`python3 tools/proofread.py` にかける
6. 一括処理でも同じ結果になることを確かめる:

```bash
python3 tools/boku2.py unpack work/BOKU2SAMPLE/BOKU2.IDX work/BOKU2SAMPLE/BOKU2.IMG work/OUT
python3 tools/boku2.py maps work/BOKU2SAMPLE/MAP/*.BIN -o work/OUT/maps
python3 tools/boku2.py text work/OUT -f work/BOKU2SAMPLE/font.txt -o work/all.tsv
```

**確認:** `work/all.tsv` の `original` 列が `work/BOKU2SAMPLE/answer.tsv` と全部一致する
(音声の番号の行は TSV に入らないので、答えの `<VOICE:…>` は除いて比べる)。

```bash
python3 tools/compare_tsv.py work/all.tsv work/BOKU2SAMPLE/answer.tsv --ignore "<VOICE:"
```

要点: ここまでの課題 1〜6 の道具 (16 進、文字テーブル、ポインタ表、フォント、校正) が、
実物と同じ形でも **そのまま通る** こと。形式が変わっても考え方は変わらない。

## 課題 9 — わざと壊して、診断の「→」を読む

実物では、練習データのように「問題なし」とは限らない。`check` が `→` の行を出したとき、
それが索引・本体・.msg・フォント・入れ物のどの段で外れたかを読めるようになる練習。
壊し方は 5 通り (`--break` の選択肢)。

```text
python3 tools/make_boku2_sample.py --break idx  --out work/BROKEN   # 索引の先頭 (DFI ではなくなる)
python3 tools/make_boku2_sample.py --break name --out work/BROKEN   # 索引の名前の置き場
python3 tools/make_boku2_sample.py --break msg  --out work/BROKEN   # system.msg の先頭
python3 tools/make_boku2_sample.py --break font --out work/BROKEN   # bk_font.tms の TIM2 の目印
python3 tools/make_boku2_sample.py --break map  --out work/BROKEN   # MAP の入れ物の先頭
python3 tools/boku2.py check work/BROKEN                            # 終了コードは 1 (問題あり)
```

やること (5 通りそれぞれで):

1. `check` の出力から `→` の行を書き出す。「先頭 16 バイト …」のような手がかりが
   付いていれば、それも一緒に
2. [10-僕夏2の手順.md](../docs/10-僕夏2の手順.md) の「困ったとき」の表で、その行に
   対応する症状を探す
3. 構造探査台にも同じ `work/BROKEN` を読ませ、「報告用の要約」に **同じ行** が出る
   ことを確かめる (ブラウザと一括処理は同じ診断を出す)

**確認:** 5 通りとも `→` の行が 1 つ以上出て、`== 結果` が「確認事項 N 件」になる
(「問題なし」にならない)。`idx` だけは索引が読めないので、そこで診断が止まる (それが正しい)。

要点: 実物で最初に貼るのはこの出力 ([10-僕夏2の手順.md](../docs/10-僕夏2の手順.md) の
「報告するとき」)。**どの段で外れたか** を自分で言えれば、次の手はほぼ決まる。
