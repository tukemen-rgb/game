# ゲームテキストの抽出とローカライズ校正の練習場

PS2 世代の家庭用ゲームで使われている「テキストの持ち方」を、権利面の心配が
まったくない自作データで一通り練習するためのリポジトリです。抽出から校正
チェック、実データへの入れ直しまでを、市販ツールと同じ手順・同じ考え方で
自分の手で動かせます。

前提として整理しておくと、この題材で学ぶのは次の 2 つのうち後者です。

| やること | 中身 | 学べること |
| --- | --- | --- |
| ディスクを読む | 円盤の中身を PC にコピーする | ドライブの操作。技術的な学びは少ない |
| **文章を取り出す** | **データの中から意味のある文字を特定する** | **文字コード・フォント・ポインタの仕組み** |

校正・ローカライズ QA の実務で毎日効くのは後者です。業務では開発元から
正規のテキストデータを受け取るので吸い出しの技術は使いませんが、
「なぜ 18 文字で切らないといけないのか」「なぜこの漢字は使えないのか」を
判断するにはデータ側の仕組みを知っている必要があります。

## 3 分で一周する

Python 3.9 以降だけあれば動きます (フォント演習だけ Pillow を使います)。

```bash
# 1. 疑似ゲームデータを作る (= ゲーム側のビルド)
python3 tools/make_sample.py

# 2. ヘッダとポインタテーブルを構造として読む
python3 tools/hexdump.py work/SCRIPT.BIN --struct | head -20

# 3. バイナリを 16 進で眺める (右側に日本語が出る)
python3 tools/hexdump.py work/SCRIPT.BIN --message 2

# 4. テキストを抽出して TSV にする
python3 tools/dump_text.py work/SCRIPT.BIN -o work/SCRIPT.tsv

# 5. 独自文字コードのファイルを、答えを見ずに解読する
python3 tools/relative_search.py work/MSG_ENC.BIN --search こんなところ

# 6. 校正チェックにかける (不具合を仕込んだ訳文が題材)
python3 tools/proofread.py exercises/qa_target.tsv

# 7. 画面でどう見えるかを確かめる (メッセージウィンドウの検査台)
python3 tools/make_viewer.py && open work/viewer.html   # Windows は start、Linux は xdg-open

# 8. 構造が分からないファイルを推定する (構造探査台)
python3 tools/make_iso.py       # 練習用のディスクイメージ
python3 tools/make_archive.py   # 練習用の「索引 + 本体」の組
open web/index.html

# 9. 直したテキストをデータに入れ直す (ポインタは自動で振り直される)
python3 tools/insert_text.py work/SCRIPT.tsv -o work/SCRIPT_new.BIN \
    --original work/SCRIPT.BIN

# 10. 実物と同じ形の練習データで、索引 → 会話 → 校正用 TSV まで一括で通す
python3 tools/make_boku2_sample.py
python3 tools/boku2.py check work/BOKU2SAMPLE        # まず診断 (実物でも最初にこれ)
python3 tools/boku2.py unpack work/BOKU2SAMPLE/BOKU2.IDX work/BOKU2SAMPLE/BOKU2.IMG work/OUT
python3 tools/boku2.py maps work/BOKU2SAMPLE/MAP/*.BIN -o work/OUT/maps
python3 tools/boku2.py text work/OUT -f work/BOKU2SAMPLE/font.txt -o work/all.tsv

# 11. ツール自体のテスト
python3 tests/run_tests.py
```

`work/` は生成物なので Git には入りません。消しても手順 1 で作り直せます。

## 市販ツールとの対応

前に挙げた Windows の定番ツールと、このリポジトリのスクリプトは同じことを
します。仕組みを理解したら、あとは使いやすい方を使えば十分です。

| 定番ツール | ここでの相当品 | 役割 |
| --- | --- | --- |
| HxD (バイナリエディタ) | `tools/hexdump.py` | 16 進と文字を並べて見る |
| Monkey-Moore | `tools/relative_search.py` | 相対検索で未知の文字コードを割る |
| Crystal Tile 2 | `tools/font_view.py` | フォント画像からグリフの並びを読む |
| 自作の抽出/挿入スクリプト | `tools/dump_text.py` / `tools/insert_text.py` | 抽出・再挿入とポインタ再計算 |
| Excel の目視チェック | `tools/proofread.py` | 校正チェックの機械化 |
| 実機・開発ビルドでの表示確認 | `tools/make_viewer.py` | 画面での見え方を再現して照合する |
| ImgBurn + 手作業の当たり探し | `web/` (構造探査台) | ディスクイメージを開いて構造を推定する |
| 専用アンパッカーを探す/書く | `web/` の「索引ファイル」タブ | 索引の形を総当たりで当てて中身を取り出す |
| IDA Pro / Ghidra | `web/` の「逆アセンブル」タブ / `tools/elfdump.py` | 本体プログラム (ELF32 / MIPS) を命令まで戻して読む |
| 作品専用の抽出スクリプト | `tools/boku2.py` | 形式が分かった作品 (僕の夏休み 2) の索引・入れ物・会話を一括で取り出す |

## 覚えるのはこの 3 つ + 1

| 概念 | 一言でいうと | 詳しい説明 |
| --- | --- | --- |
| 文字テーブル | どのバイトがどの文字か | [docs/01-文字テーブル.md](docs/01-文字テーブル.md) |
| 相対検索 | 未知の文字コードでも規則性から日本語を探す | [docs/02-相対検索.md](docs/02-相対検索.md) |
| ポインタテーブル | 各セリフの開始位置の一覧 | [docs/03-ポインタテーブル.md](docs/03-ポインタテーブル.md) |
| 校正 QA の勘所 | 文字数・禁則・用語・変数・フォント | [docs/04-校正とQA.md](docs/04-校正とQA.md) |
| 画面での確認 | データ上の文字列と見た目を突き合わせる | [docs/06-画面で確かめる.md](docs/06-画面で確かめる.md) |
| 構造の推定 | 未知のファイルからポインタ表や文字列を見つける | [docs/07-構造探査台.md](docs/07-構造探査台.md) |
| コードを読む | 本体プログラムを逆アセンブルして「どう読んでいるか」を見る | [docs/08-コードを読む.md](docs/08-コードを読む.md) |
| 実物での通し手順 | 索引 → 会話 → 文字表 → 校正用 TSV を順番どおりに | [docs/10-僕夏2の手順.md](docs/10-僕夏2の手順.md) (根拠は [docs/09](docs/09-調査ログと引き継ぎ.md)) |
| 形式を突き止めるまで | どこで間違え、何が決め手だったか | [docs/11-形式を突き止めるまで.md](docs/11-形式を突き止めるまで.md) |

実物のディスクを扱う話 (ISO 化、エミュレータでの照合、日本の著作権法上の
注意点) は [docs/05-実物のディスクを扱う場合.md](docs/05-実物のディスクを扱う場合.md)
にまとめてあります。**この練習自体には実物のソフトは一切必要ありません。**

## 練習コース

順番にやると、抽出から校正までひと通り身につきます。課題文は
[exercises/README.md](exercises/README.md)、答えは `answers/` にあります
(先に自力でやってから開いてください)。

1. **構造を読む** — `hexdump.py --struct` でヘッダを読み、ポインタの値と
   実際の本文位置が一致していることを自分で確かめる
2. **Shift-JIS を抽出する** — `SCRIPT.BIN` を `dump_text.py` にかけ、
   16 進表示と突き合わせる
3. **独自コードを解読する** — `MSG_ENC.BIN` を `relative_search.py` で割り、
   `--derive` で作ったテーブルを手で育てて全文を読めるようにする
4. **フォントを読む** — `font_view.py` で `FONT.BIN` のグリフを見て、
   「文字コード = グリフの並び順」を体感する
5. **校正する** — `exercises/qa_target.tsv` の不具合を洗い出し、直す
6. **画面で確かめる** — `make_viewer.py` で原文と訳文を切り替え、崩れを目で見る
7. **入れ直す** — 直した TSV を `insert_text.py` で元の容量に収める
8. **構造を推定する** — `make_iso.py` で作ったイメージを構造探査台に読ませ、
   形式を何も教えない状態からポインタ表とテキストを見つける
9. **コードを読む** — 同じイメージの `SLPS_900.99` を「逆アセンブル」タブで開き、
   `SYSTEM.CNF` → 本体プログラム → 文字列の参照元、という順路を辿る
   (`make_elf.py` が答えを表示するので突き合わせられる)
10. **実物と同じ形で通す** — `make_boku2_sample.py` のデータを構造探査台と
    `boku2.py` で索引 → 会話 → 文字表 → 校正用 TSV まで通し、`answer.tsv` と
    突き合わせる (docs/10)

## ディレクトリ構成

```
data/       疑似ゲームの原本 (マスターテキスト・用語集・チェック設定)
tools/      抽出・解読・校正・再挿入・画面生成のスクリプト
web/        構造探査台 (ブラウザで動く解析サイト)
docs/       仕組みの解説
exercises/  課題 (不具合を仕込んだ訳文)
answers/    答え (文字テーブル・仕込み一覧)
tests/      ツールの自己テスト
work/       生成物 (Git 管理外)
```

## 題材について

`data/script_source.tsv` の文章とフォーマット (SCRP) は、この練習のために
書いた架空の RPG「リィンフォルト戦記」のものです。市販ソフトのデータは
一切含んでいません。構造は実際のゲームでよくある形に寄せてあります。
