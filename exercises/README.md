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

要点: `82 xx` がかな、`F0` が改行、`FF` が終端、という対応を目で覚えること。

ただし **`82` なら必ず仮名、ではありません**。Shift-JIS では

| 範囲 | 中身 | 例 |
| --- | --- | --- |
| `82 9F`〜`82 F1` | ひらがな | `82 A2` = い |
| `82 4F`〜`82 9A` | **全角の英数字** | `82 52` = ３、`82 81` = ａ |

`work/SCRIPT.BIN` では前者が 517 回、後者が 20 回 (id 3 の `３００`、
id 24 の `８５０` など)。「82 なら仮名」と覚えると、**数字のところで読み違えます**。
後半の値が `9F` より小さいかどうかで見分けます。

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

手順 2 の出力の下に「表に無いバイト」の一覧が出ます。**足りないのはそこに出た
値だけ**です。点を目で数える必要はありません。

数えるのは **本文の中だけ**です。先頭の見出しとポインタ表 (`work/MSG_ENC.BIN`
なら 0xAC より前) は文字ではないので、そこに出たバイトは
「見出しとポインタ表にも N 個」と別に報せるだけで、足す値には数えません。
`--message 0` を付けずに眺めると既定の窓 (先頭 256 バイト) はほとんどが
その範囲なので、**そこの値を足すと表が汚れます**。

ただし **1 つの値が 1 行とはかぎりません**。続くバイトが毎回違う値は
「2 バイトで 1 文字」の前半の疑いが濃く、その場合は組の数だけ 4 桁で足します。
道具はそれも数えて「足すのは 1 行ではなく N 行です」と言います。
1 バイトのつもりで 1 行だけ足しても読めるようにならないのは、そのためです。

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
  — **この 2 つは別々には満たせません。** `ERROR` の行だけ直すと 3 バイト
  はみ出します。浮かせる分は `WARN` の側 (三点リーダの詰め、行末の空白) に
  あるので、`WARN` も見ないと入りません。実測は `answers/qa_answers.md` の
  「容量」の節
* 入れ直したファイルから再抽出したテキストが、意図したものと一致する
* 検査台の画面で、枠から出た文字と □ が 1 つも無い

最後の再抽出まで含めて確認するのが大事です。「TSV は直したがデータに
反映されていない」が実務でよくある事故で、往復して確認する癖をつけます。

---

## 課題 7 — ツールを直す (応用)

まず、**すでに入っている仕組みを使ってみる**。`<VAR:00>` には実行時に
プレイヤー名が入るので、名前の長さを数えないと本当の行幅は分かりません。

```bash
python3 tools/proofread.py exercises/qa_target.tsv --var-width 6
```

`id 22` の `<VAR:00>は<COLOR:02>２４<COLOR:00>のダメージを受けた！` が
`line_width` で出ます (13 文字 + 名前 6 文字 = 19 文字 > 18)。
`--var-width` を付けないと 13 文字なので出ません。

**`id 2` では出ません。** そちらは訳文の `<VAR:00>` が `あなた` に
置き換わっている行 (課題 5 の `placeholder` の仕込み) なので、差し込む変数が
そもそも無く、17 文字にしかなりません。**校正が見るのは訳文の側**です。
原文のほうは 20 文字になるので、検査台 (`make_viewer.py`) で **原文に切り替えて**
プレイヤー名を `ながいなまえ` にすると、その 20 文字が画面で確認できます
(docs/06 の「変数の長さを試す」)。

**ここからが課題です。** 同じ理屈が `<NAME:xx>` にも当てはまります。
話者名を枠の外に出すゲームなら幅 0 でよいのですが、**枠の中に
「セリカ『……』」のように出すゲーム**では、話者名の分だけ 1 行目が狭くなります。

* `--name-width` を足して、`<NAME:xx>` の分も数えられるようにする
  (`scrp.display_width()` は既にタグごとの幅を受け取れます)
* ただし数字を手で打つのは筋が悪い。**話者名は `data/names.tsv` に実際に
  書いてある**ので、その最長を既定値として使えるようにする
  (`--name-width auto` のような形)
* `data/rules.json` にも `name_width` を置けるようにする
* `tests/run_tests.py` にテストを足す。最低でも「auto が names.tsv の
  最長と一致する」「幅 0 のときは今までどおり」の 2 つ

要点: 仕様の数字を**手で書き写さない**。同じ数字が 2 か所にあると必ずずれます。
元データから取れるものは元データから取る。

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
5. 「校正用の TSV をコピー」→ ファイルに貼り、校正にかける。このとき
   **その作品の文字表を渡すこと**。既定の文字表は練習用の作品 (リィンフォルト戦記)
   のもので、別の作品に当てると「フォントに無い文字」が総崩れになります

   ```bash
   python3 tools/proofread.py work/from_browser.tsv \
       --font-chars work/BOKU2SAMPLE/font.txt
   ```
6. 一括処理でも同じ結果になることを確かめる:

```bash
python3 tools/boku2.py unpack work/BOKU2SAMPLE/BOKU2.IDX work/BOKU2SAMPLE/BOKU2.IMG work/OUT
python3 tools/boku2.py maps work/BOKU2SAMPLE/MAP/*.BIN -o work/OUT/maps
python3 tools/boku2.py text work/OUT -f work/BOKU2SAMPLE/font.txt -o work/all.tsv
```

**確認:** `work/all.tsv` の `original` 列が `work/BOKU2SAMPLE/answer.tsv` と全部一致する。

```bash
python3 tools/compare_tsv.py work/all.tsv work/BOKU2SAMPLE/answer.tsv
```

音声の番号の行 (`<VOICE:…>`) は既定では `text` が落とすので、どちらにも入りません。
`text --keep-voice` で残したときだけ左に 2 行余るので、そのときは
`--ignore "<VOICE:"` を足します。

なお、**このコマンドが「全部一致しました」と言うのは、1 行以上比べたときだけ**です。
両方が空だったり `--ignore` で全部除いたりすると、
「突き合わせた行が 1 行もありません」と言って止まります (何も確かめていないので)。

要点: ここまでの課題 1〜6 の道具 (16 進、文字テーブル、ポインタ表、フォント、校正) が、
実物と同じ形でも **そのまま通る** こと。形式が変わっても考え方は変わらない。

## 課題 9 — わざと壊して、診断の「→」を読む

実物では、練習データのように「問題なし」とは限らない。`check` が `→` の行を出したとき、
それが索引・本体・.msg・フォント・入れ物のどの段で外れたかを読めるようになる練習。
壊し方は 15 通り (`--break` の選択肢)。

```text
python3 tools/make_boku2_sample.py --break idx  --out work/BROKEN   # 索引の先頭 (DFI ではなくなる)
python3 tools/make_boku2_sample.py --break name --out work/BROKEN   # 索引の名前の置き場
python3 tools/make_boku2_sample.py --break msg  --out work/BROKEN   # system.msg の先頭
python3 tools/make_boku2_sample.py --break font --out work/BROKEN   # bk_font.tms の TIM2 の目印
python3 tools/make_boku2_sample.py --break map  --out work/BROKEN   # MAP の入れ物の先頭
python3 tools/make_boku2_sample.py --break allnames --out work/BROKEN  # 索引の名前を最後まで
python3 tools/make_boku2_sample.py --break empty --out work/BROKEN  # 本体の中身をゼロで埋める
python3 tools/make_boku2_sample.py --break bignum --out work/BROKEN # 文字番号を文字表の字数より大きく
python3 tools/make_boku2_sample.py --break crc  --out work/BROKEN   # BOKU2.CRC の検査値を 1 つ変える
python3 tools/make_boku2_sample.py --break crcname --out work/BROKEN # BOKU2.CRC の名前を 1 つ変える
python3 tools/make_boku2_sample.py --break length --out work/BROKEN # 索引のレコードの長さの欄を小さく
python3 tools/make_boku2_sample.py --break box  --out work/BROKEN   # 文言の入れ物 (diary.bin など) の中身
python3 tools/make_boku2_sample.py --break sparse --out work/BROKEN # BOKU2.IMG の後ろに詰め物を足す
python3 tools/make_boku2_sample.py --break halfrip --out work/BROKEN # BOKU2.IMG の後半をゼロにする
python3 tools/make_boku2_sample.py --break altbreak --out work/BROKEN # 一覧に無い .msg に 0x8002 を入れる
python3 tools/boku2.py check work/BROKEN                            # 終了コードは 1 (問題あり)
```

`length` だけは、ほかと読み方が違います。**索引の読み方そのものが外れている**形なので、
検査値も `.msg` もフォントも全部「読めません」になりますが、**直す所は 1 つだけ**。
`→` は 1 本しか出ず、あとの段は「上の『…』から来ています」と字下げで出ます。
**吸い出し直しでは直りません** (ディスクは無事で、読み方のほうが違う)。

やること (15 通りそれぞれで):

1. `check` の出力から `→` の行を書き出す。「先頭 16 バイト …」のような手がかりが
   付いていれば、それも一緒に
2. [10-僕夏2の手順.md](../docs/10-僕夏2の手順.md) の
   **「診断の `→` の行の読み方」** の表で、その行に対応する段と次の手を探す。
   **`→` で始まる行は、どの道具のものでもこの表に載っています** (`check` だけでなく
   `unpack` / `maps` / `text` / `used` の分も)。その少し上の「困ったとき」は、
   `→` が付かない出方 —— 数がおかしい、何も起きない —— を引くための別の表
3. 構造探査台にも同じ `work/BROKEN` を読ませ、「報告用の要約」に **同じ行** が出る
   ことを確かめる (ブラウザと一括処理は同じ診断を出す)

**確認:** 15 通りとも `→` の行が 1 つ以上出て、`== 結果` が「確認事項 N 件」になる
(「問題なし」にならない)。`idx` だけは索引が読めないので、そこで診断が止まる (それが正しい)。

要点: 実物で最初に貼るのはこの出力 ([10-僕夏2の手順.md](../docs/10-僕夏2の手順.md) の
「報告するとき」)。**どの段で外れたか** を自分で言えれば、次の手はほぼ決まる。
