#!/usr/bin/env python3
"""ツール一式の自己テスト.

    python3 tests/run_tests.py

外部ライブラリは使いません (Pillow が入っていればフォント生成も試します)。
"""

from __future__ import annotations

import codecs
import contextlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.join(REPO, "answers"))
sys.path.insert(0, os.path.join(REPO, "tests", "e2e"))

#: 文書の引用を実際の出力に当てはめる書き方。**e2e と同じものを使う** (#208)。
#: playwright が無くても読める所に置いてあるので、ここからも借りられる
from common import doc_shape  # noqa: E402

import scrp
import make_sample
import proofread
import relative_search
import boku2  # noqa: F401  (TestBoku2Cli で使う。読み込めることも確認)


SOURCE = os.path.join(REPO, "data", "script_source.tsv")


def ensure_practice(marker: str, *tools: str) -> str | None:
    """練習データが無ければ作る。作れなければ理由を返す (#82).

    「無いから飛ばす」で済ませると、取得したままの環境では**一度も走らない検査**に
    なる。作る道具は同じリポジトリにあるので、飛ばす前に作る。
    道具を順に実行するのは、材料に前後関係があるため (make_archive は make_sample の出力を使う)。
    """
    import subprocess

    if os.path.exists(os.path.join(REPO, marker)):
        return None
    for tool in tools:
        res = subprocess.run([sys.executable, os.path.join(REPO, "tools", tool)],
                             cwd=REPO, capture_output=True, text=True)
        if res.returncode != 0:
            return f"python3 tools/{tool} が落ちました: {(res.stdout + res.stderr).strip()[:300]}"
    if not os.path.exists(os.path.join(REPO, marker)):
        return f"{marker} を作れませんでした"
    return None


class Fixture:
    """マスターテキストから疑似ゲームデータを組み立てたもの."""

    def __init__(self, pool_duplicates: bool = False):
        self.rows = make_sample.load_source(SOURCE)
        self.texts = [text for _, text in self.rows]
        self.mapping, self.glyph_order = make_sample.build_table(self.texts)
        self.table_codec = scrp.TableCodec(self.mapping)
        self.sjis_codec = scrp.SjisCodec(set(self.glyph_order))
        self.sjis = make_sample.compile_archive(
            self.texts, scrp.ENC_SJIS, self.sjis_codec, pool_duplicates)
        self.custom = make_sample.compile_archive(
            self.texts, scrp.ENC_CUSTOM, self.table_codec, pool_duplicates)


FIX = Fixture()


class TestContainer(unittest.TestCase):
    def test_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.bin")
            with open(path, "wb") as fh:
                fh.write(FIX.custom)
            archive = scrp.read_archive(path)
        self.assertEqual(archive.count, len(FIX.texts))
        self.assertEqual(archive.encoding_id, scrp.ENC_CUSTOM)
        self.assertEqual(archive.pointers[0], scrp.HEADER_SIZE + archive.count * 4)

    def test_pointers_are_ascending_and_inside_file(self):
        archive = scrp.Archive(scrp.ENC_CUSTOM, [], b"")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.bin")
            with open(path, "wb") as fh:
                fh.write(FIX.sjis)
            archive = scrp.read_archive(path)
        for a, b in zip(archive.pointers, archive.pointers[1:]):
            self.assertLess(a, b)
        self.assertLess(archive.pointers[-1], len(archive.data))

    def test_rejects_foreign_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.bin")
            with open(path, "wb") as fh:
                fh.write(b"NOPE" + b"\x00" * 32)
            with self.assertRaises(scrp.ScrpError):
                scrp.read_archive(path)

    def test_rejects_truncated_pointer_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.bin")
            with open(path, "wb") as fh:
                fh.write(FIX.custom[:20])
            with self.assertRaises(scrp.ScrpError):
                scrp.read_archive(path)


class TestRoundTrip(unittest.TestCase):
    """抽出 → 再挿入でバイト単位に元へ戻ること."""

    def _roundtrip(self, blob: bytes, codec: scrp.Codec, encoding_id: int):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.bin")
            with open(path, "wb") as fh:
                fh.write(blob)
            archive = scrp.read_archive(path)
            rows = archive.decode_all(codec)
            self.assertEqual([text for _, _, text in rows], FIX.texts)
            rebuilt = scrp.build_archive(
                encoding_id, [scrp.encode_message(t, codec) for _, _, t in rows])
        self.assertEqual(rebuilt, blob)

    def test_sjis(self):
        self._roundtrip(FIX.sjis, scrp.SjisCodec(), scrp.ENC_SJIS)

    def test_custom(self):
        self._roundtrip(FIX.custom, FIX.table_codec, scrp.ENC_CUSTOM)

    def test_sizes_match_declared_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in.bin")
            with open(path, "wb") as fh:
                fh.write(FIX.custom)
            archive = scrp.read_archive(path)
            for i, (offset, size, _) in enumerate(archive.decode_all(FIX.table_codec)):
                self.assertEqual(offset, archive.pointers[i])
                self.assertEqual(size, len(archive.raw_block(i)))


class TestPointerRecalc(unittest.TestCase):
    """本文の長さを変えたら、ポインタが正しく振り直されること."""

    def test_longer_message_shifts_following_pointers(self):
        texts = list(FIX.texts)
        texts[0] = texts[0].replace("ようこそ", "ようこそようこそ")
        blobs = [scrp.encode_message(t, FIX.table_codec) for t in texts]
        rebuilt = scrp.build_archive(scrp.ENC_CUSTOM, blobs)

        with tempfile.TemporaryDirectory() as tmp:
            before_path = os.path.join(tmp, "before.bin")
            after_path = os.path.join(tmp, "after.bin")
            with open(before_path, "wb") as fh:
                fh.write(FIX.custom)
            with open(after_path, "wb") as fh:
                fh.write(rebuilt)
            before = scrp.read_archive(before_path)
            after = scrp.read_archive(after_path)

            grew = len(blobs[0]) - len(before.raw_block(0))
            self.assertEqual(grew, 4 * 1)  # 「ようこそ」4 文字 x 1 バイト
            self.assertEqual(before.pointers[0], after.pointers[0])
            for i in range(1, before.count):
                self.assertEqual(after.pointers[i], before.pointers[i] + grew)
            # 振り直したポインタで読み直しても全文が一致する
            self.assertEqual([t for _, _, t in after.decode_all(FIX.table_codec)], texts)


class TestPoolDuplicates(unittest.TestCase):
    """同一本文をまとめると、容量が減ってポインタが共有されること."""

    def test_pooling(self):
        pooled = Fixture(pool_duplicates=True)
        self.assertLess(len(pooled.custom), len(FIX.custom))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.bin")
            with open(path, "wb") as fh:
                fh.write(pooled.custom)
            archive = scrp.read_archive(path)
        self.assertLess(len(set(archive.pointers)), archive.count)
        # 共有していても、読み出せる本文は変わらない
        self.assertEqual([t for _, _, t in archive.decode_all(pooled.table_codec)],
                         FIX.texts)


class TestEncoding(unittest.TestCase):
    def test_font_missing_char_is_rejected(self):
        with self.assertRaises(scrp.ScrpError):
            scrp.encode_message("薔薇", FIX.table_codec)

    def test_raw_newline_is_rejected(self):
        with self.assertRaises(scrp.ScrpError):
            scrp.encode_message("あ\nい", FIX.table_codec)

    def test_unknown_tag_is_rejected(self):
        with self.assertRaises(scrp.ScrpError):
            scrp.encode_message("あ<BEEP>い", FIX.table_codec)

    def test_unclosed_tag_is_rejected(self):
        with self.assertRaises(scrp.ScrpError):
            scrp.encode_message("あ<BR い", FIX.table_codec)

    def test_literal_angle_bracket(self):
        raw = scrp.encode_message("<LT>", scrp.SjisCodec())
        self.assertEqual(scrp.decode_message(raw, 0, scrp.SjisCodec())[0], "<LT>")

    def test_control_codes_survive(self):
        text = "あ<BR>い<WAIT><COLOR:0A>う<VAR:00><CLEAR>え"
        raw = scrp.encode_message(text, FIX.table_codec)
        self.assertEqual(scrp.decode_message(raw, 0, FIX.table_codec)[0], text)

    def test_no_single_byte_collides_with_control_codes(self):
        for raw in FIX.mapping:
            if len(raw) == 1:
                self.assertNotIn(raw[0], scrp.CONTROL_CODES)
                self.assertNotEqual(raw[0], scrp.END)

    def test_display_width(self):
        self.assertEqual(scrp.display_width("あいう"), 3.0)
        self.assertEqual(scrp.display_width("abc"), 1.5)
        self.assertEqual(scrp.display_width("あ<BR>い"), 2.0)


class TestRelativeSearch(unittest.TestCase):
    """相対検索が、答えを見ずに正しい文字コードを導けること."""

    def setUp(self):
        self.order = relative_search.build_order("unicode", None)

    def test_finds_known_phrase_and_derives_table(self):
        hits = relative_search.search(FIX.custom, "こんなところ", self.order, 1, "le", 256)
        self.assertTrue(hits, "既知の語が見つかりませんでした")
        offset, code = hits[0]
        self.assertEqual(FIX.custom[offset:offset + 1], FIX.table_codec.by_char["こ"])
        derived = relative_search.derived_mapping(code, "こんなところ", self.order, 1, "le", 256)
        # 導いたテーブルのかな部分が、答えのテーブルと一致する
        for raw, ch in derived.items():
            if ch in make_sample.HIRAGANA or ch in make_sample.KATAKANA:
                self.assertEqual(FIX.mapping.get(raw), ch, f"{ch} のコードがずれています")

    def test_rejects_query_with_kanji(self):
        with self.assertRaises(scrp.ScrpError):
            relative_search.deltas("影の司祭", self.order)

    def test_sjis_needs_two_byte_mode(self):
        # Shift-JIS のかなは 2 バイト・ビッグエンディアン相当で連番になっている
        hits = relative_search.search(FIX.sjis, "こんなところ", self.order, 2, "be", 65536)
        self.assertTrue(hits)


class TestPracticeDocsAreRunnable(unittest.TestCase):
    """練習用の文書のコマンドが、上から順に打って通る形であること (#80, #81).

    見るのは 3 点。**穴埋めのまま**の引数が無いこと (`FILE` をそのままコピペすると
    落ちる)、**使うファイルの作り方がどこかに書いてある**こと (docs/03 は
    work/SCRIPT.tsv を使うのに、作る dump_text.py に触れていなかった)、そして
    その作り方が**使う行より前**にあること (docs/08 は work/BOOT.ELF を使う
    4 行を並べたあとで make_elf.py に触れていた。上から順に打つと 1 行目で止まる)。
    """

    DOCS = ("01-文字テーブル.md", "02-相対検索.md", "03-ポインタテーブル.md",
            "04-校正とQA.md", "06-画面で確かめる.md", "07-構造探査台.md",
            "08-コードを読む.md", "../README.md")

    #: この引数の次に来る名前は、入力ではなくその行が作るもの
    #: `--outdir` は #143 で足した。docs/03 が題材を別の場所に作るのに使う
    OUT_FLAGS = ("-o", "--out", "--outdir", "--derive", "--report")

    #: 旗ではなく**位置**で出力先が決まるコマンド (末尾の引数が出力先)
    OUT_TAIL = ("boku2.py unpack",)

    #: 読む対象にする行の頭。cp は「題材を作業場に写してから直す」に使う (docs/06)
    VERBS = ("python3 ", "cp ")

    @classmethod
    def commands_at(cls, doc: str) -> list[tuple[str, int]]:
        """```bash ブロックの中の実行行と、その文字位置 (行継続はつなぐ)."""
        out, in_block, buf, start, pos = [], False, "", 0, 0
        for line in doc.split("\n"):
            here, pos = pos, pos + len(line) + 1
            if line.startswith("```"):
                in_block = "bash" in line
                continue
            if not in_block:
                continue
            line = line.split("#", 1)[0].rstrip()
            if buf:
                buf += " " + line.strip()
            elif line.startswith(cls.VERBS):
                buf, start = line.strip(), here
            else:
                continue
            if buf.endswith("\\"):
                buf = buf[:-1].strip()
            else:
                out.append((buf, start))
                buf = ""
        return out

    @classmethod
    def commands(cls, doc: str) -> list[str]:
        return [cmd for cmd, _ in cls.commands_at(doc)]

    @staticmethod
    def shipped(tok: str) -> bool:
        """リポジトリに同梱されているファイルか (work/ の中は練習で作るものなので違う).

        os.path.exists だけで判定すると、一度でも生成器を走らせた環境では work/ が
        埋まっていて、この一群の検査が丸ごと素通しになる (#81 で踏んだ)。
        work/ は git に入れていない作業場なので、常に「無い」として扱う。
        """
        if tok.startswith("work/"):
            return False
        return os.path.exists(os.path.join(REPO, tok))

    def doc_text(self, name: str) -> str:
        with open(os.path.join(REPO, "docs", name), encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    def label(name: str) -> str:
        """報告用の見出し (README は docs/ の外にあるので docs/../ にしない)."""
        return os.path.normpath(os.path.join("docs", name)).replace(os.sep, "/")

    def test_every_command_block_is_marked_bash(self):
        """コマンドの入った囲みには ```bash の印が要る (#81).

        この試験の一群は ```bash だけを読む。印の無い囲みは**素通し**になるので、
        中身が壊れていても全部緑のまま通る。docs/08 が実際そうだった
        (印が無かったので、順序の誤りを誰も見ていなかった)。
        """
        for name in self.DOCS:
            fence, buf = None, []
            for lineno, line in enumerate(self.doc_text(name).split("\n"), 1):
                if not line.startswith("```"):
                    if fence is not None:
                        buf.append(line)
                    continue
                if fence is None:
                    fence, buf = (lineno, line[3:].strip()), []
                    continue
                if any(l.startswith("python3 ") for l in buf):
                    self.assertEqual("bash", fence[1],
                                     f"{self.label(name)}:{fence[0]} コマンドの囲みに ```bash "
                                     "の印が無い (この文書の検査が素通しになる)")
                fence = None

    def test_no_placeholder_is_left_in_a_command(self):
        for name in self.DOCS:
            doc = self.doc_text(name)
            for cmd in self.commands(doc):
                for tok in cmd.split():
                    self.assertNotIn(tok, ("FILE", "ファイル名", "PATH", "<file>"),
                                     f"{self.label(name)}: 穴埋めのまま打てない行がある: {cmd}")

    def inputs_needing_a_maker(self, doc: str):
        """(生成器を要る入力, その名前, その行, 行の文字位置) を順に返す.

        道の途中で作られる物は「その行より前に作る行があるか」で見る。旗 (-o など) の
        ほか、`cp 元 先` の先と、出力先が位置で決まるコマンド (OUT_TAIL) の末尾も
        作る側として数える。フォルダごと作る物があるので、名前は基底名だけでなく
        **道の各段**を見る (`work/BOKU2SAMPLE/MAP/*.BIN` は BOKU2SAMPLE が作る)。
        """
        produced: set[str] = set()
        for cmd, at in self.commands_at(doc):
            toks = cmd.split()
            tail_is_out = toks[0] == "cp" or any(k in cmd for k in self.OUT_TAIL)
            if tail_is_out and len(toks) >= 3:
                produced.add(os.path.basename(toks[-1]))
            for i, tok in enumerate(toks):
                if tok in self.OUT_FLAGS:
                    if i + 1 < len(toks):
                        produced.add(os.path.basename(toks[i + 1]))
                    continue
                if tok.startswith("-") or "/" not in tok or tok.startswith("tools/"):
                    continue
                parts = tok.split("/")
                if produced.intersection(parts):
                    continue
                made = next((p for p in parts if p in scrp.MAKERS), None)
                if made is not None:
                    yield tok, made, cmd, at        # 作り方はある。順序は別の試験が見る
                    continue
                if self.shipped(tok):
                    continue
                yield tok, os.path.basename(tok), cmd, at

    def test_every_file_used_is_made_earlier_in_the_same_doc(self):
        """使うファイルは、実在するか、同じ文書の前の行が作っているか、生成器が作るもの."""
        for name in self.DOCS:
            for _tok, base, cmd, _at in self.inputs_needing_a_maker(self.doc_text(name)):
                self.assertIn(base, scrp.MAKERS,
                              f"{self.label(name)}: {base} の作り方が先に無い ({cmd})")

    def test_the_maker_is_named_before_the_line_that_needs_it(self):
        """生成器の名前は、その生成物を使う行より前に出てくること (#81).

        MAKERS に載っていれば作り方はあるが、それが文書の**どこに**書いてあるかは
        別の話。docs/08 は 4 行打たせたあとに make_elf.py を紹介していた。

        探すのは `python3 tools/xxx.py` という**打てる形**。ただの
        `tools/make_elf.py` は説明の中で名前を出しているだけのことがあり
        (docs/08 は hi_lo() の話で触れていた)、それを作り方と数えると
        順序が逆のままでも通ってしまう。
        """
        for name in self.DOCS:
            doc = self.doc_text(name)
            for _tok, base, cmd, at in self.inputs_needing_a_maker(doc):
                if base not in scrp.MAKERS:
                    continue          # 上の試験が報告する
                for script in re.findall(r"tools/(\w+\.py)", scrp.MAKERS[base]):
                    where = doc.find("python3 tools/" + script)
                    self.assertTrue(0 <= where < at,
                                    f"{self.label(name)}: {base} を使う行より前に "
                                    f"`python3 tools/{script}` が無い ({cmd})")

    def test_the_insert_step_shows_how_to_get_the_tsv(self):
        doc = self.doc_text("03-ポインタテーブル.md")
        self.assertIn("dump_text.py", doc, "docs/03 に TSV の作り方が無い")
        self.assertLess(doc.index("dump_text.py"), doc.index("insert_text.py work/SCRIPT.tsv"),
                        "TSV の作り方が入れ直しより後に書かれている")


class TestNumbersAreJudgedNotJustPrinted(unittest.TestCase):
    """診断が出す数字は、良し悪しまで言うこと (#96).

    `check` は「索引が指す合計 … (82.0%)」と出していたが、**その数字を一度も
    判定に使っていなかった**。実物で 12% でも「問題なし」と言う。読む人は
    82% が良いのか悪いのかも分からない。数字だけ出すのは、出さないより悪い
    (確かめた気になる)。基準はブラウザ側が候補から外す線と同じ 2 割。
    """

    def setUp(self):
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = lambda *a: subprocess.run([sys.executable, *a], capture_output=True,
                                             text=True, cwd=REPO)
        made = self.run(os.path.join(REPO, "tools", "make_boku2_sample.py"),
                        "--out", os.path.join(self.tmp.name, "S"))
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.folder = os.path.join(self.tmp.name, "S")

    def check(self, folder):
        return self.run(os.path.join(REPO, "tools", "boku2.py"), "check", folder)

    def test_the_number_says_what_it_means(self):
        out = self.check(self.folder).stdout
        self.assertIn("索引が本体をどれだけ使い切っているか", out,
                      "割合の意味が書かれていない")
        self.assertIn("5 割を超えます", out, "どれくらいなら普通なのかが無い")

    def test_a_low_coverage_is_reported_as_a_problem(self):
        """本体だけ大きくして割合を下げると、→ の行が出て終了コードが 1 になること."""
        with open(os.path.join(self.folder, "BOKU2.IMG"), "ab") as fh:
            fh.write(b"\0" * 1_000_000)
        res = self.check(self.folder)
        self.assertEqual(res.returncode, 1, res.stdout)
        arrows = [l for l in res.stdout.split("\n") if l.startswith("→")]
        self.assertTrue(any("しか指していません" in l for l in arrows),
                        f"低い割合を指摘していない: {arrows}")
        self.assertIn("確認事項", res.stdout)

    def test_a_length_field_mismatch_is_counted_as_a_problem(self):
        """「この行ごと報告」と言うなら、確認事項に数えること (#97).

        位置表の長さの欄が合わないとき、報告してほしいと書いておきながら
        problems に数えていなかったので、最後の行は「問題なし」のままだった。
        **報告してほしい = 問題**。言葉と判定が食い違っていた。

        **この検査は元々ソースを読んでいた** (「`位置表の長さの欄` の 600 文字以内に
        `problems += 1` があるか」)。数える行が**通らない枝**にあっても通るし、
        最後の行が実際に何と出るかは見ていない。#133・#136 と同じ形なので、
        **実際に壊して走らせる**形に変えた (#137)。
        """
        import shutil
        import struct
        import subprocess

        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "LENBAD")
            shutil.copytree(sample, folder)
            with open(os.path.join(folder, "BOKU2.IDX"), "rb") as fh:
                idx = fh.read()
            size = os.path.getsize(os.path.join(folder, "BOKU2.IMG"))
            entry = next(e for e in boku2.read_dfi(idx, size, "flag")
                         if e["path"].endswith("system.msg"))
            with open(os.path.join(folder, "BOKU2.IMG"), "rb") as fh:
                img = bytearray(fh.read())
            # 8 バイト刻みの 1 件目の**長さの欄だけ**をずらす (位置はそのまま)
            length = struct.unpack_from("<I", img, entry["at"] + 8)[0]
            self.assertGreater(length, 0, "長さの欄が 0 の題材では壊せない (前提が崩れた)")
            struct.pack_into("<I", img, entry["at"] + 8, length + 7)
            with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
                fh.write(bytes(img))
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "boku2.py"), "check", folder],
                capture_output=True, text=True, cwd=REPO)
        out = res.stdout + res.stderr
        self.assertIn("合わない 1 件", out, out[-600:])
        self.assertTrue(any(l.startswith("→") and "位置表の長さの欄" in l
                            for l in out.splitlines()), f"→ の行が出ていない:\n{out[-600:]}")
        # ここが本題。「報告してほしい」と言うなら、最後の行も問題ありでなければならない
        self.assertIn("確認事項", out, f"→ を出しておいて「問題なし」で終わっている:\n{out[-300:]}")
        self.assertNotIn("問題なし", out, out[-300:])
        self.assertEqual(res.returncode, 1, f"終了コードが 0 のまま:\n{out[-300:]}")

    def test_no_conversation_found_is_reported(self):
        """入れ物は読めたのに会話が 0 件なら、→ を出すこと (#97)."""
        import shutil
        import struct

        for name in sorted(os.listdir(os.path.join(self.folder, "MAP"))):
            path = os.path.join(self.folder, "MAP", name)
            with open(path, "rb") as fh:
                b = bytearray(fh.read())
            off, _ln = struct.unpack_from("<II", b, 12)     # 1 番の部品
            b[off:off + 4] = b"\xee\xee\xee\xee"            # 表の数を壊す
            with open(path, "wb") as fh:
                fh.write(bytes(b))
        res = self.check(self.folder)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("1 番が会話だった 0 件", res.stdout)
        self.assertIn("会話として読めたファイルが 0 件", res.stdout,
                      "会話 0 件を指摘していない")
        self.assertIn("確認事項", res.stdout)
        del shutil

    def test_the_pixel_kind_is_said_in_words(self):
        """「画素の種類 5」は TIM2 の書式番号そのまま。素人に意味がないので言い換える (#98)."""
        out = self.check(self.folder).stdout
        self.assertIn("1 画素 1 バイトのパレット番号", out, out)
        self.assertNotIn("画素の種類 5", out, "番号のまま出している")

    def test_a_too_narrow_font_is_reported(self):
        """1 行 23 字 × 22 ドットが載らない幅なら → を出すこと (#98)."""
        import struct

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_tim2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        tim2, _ = make_tim2.font_sheet(rows=72, cols=boku2.FONT_COLS - 1,
                                       cell=boku2.FONT_CELL)
        tms = b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + tim2
        img = os.path.join(self.folder, "BOKU2.IMG")
        with open(os.path.join(self.folder, "BOKU2.IDX"), "rb") as fh:
            idx = fh.read()
        entry = next(e for e in boku2.read_dfi(idx, os.path.getsize(img))
                     if e["path"].endswith("bk_font.tms"))
        with open(img, "r+b") as fh:
            fh.seek(entry["at"])
            fh.write(tms[:entry["len"]])
        res = self.check(self.folder)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("ドットに足りません", res.stdout, res.stdout)
        self.assertIn(str(boku2.FONT_COLS * boku2.FONT_CELL), res.stdout,
                      "必要な幅を数字で言っていない")

    def test_a_too_wide_font_is_reported(self):
        """**広すぎる**幅も言うこと (#166).

        #98 で足した判定は「足りない」側だけを見ていました。ところが番号振りは
        **幅 ÷ 刻み**で列数を決めるので、広すぎる側も同じだけ危ない。この作品には
        1024 ドット幅の画像が普通にあり (公開ソースの書き出しでは `config.tm2` /
        `insect_base.tm2` / `bumper.tm2` がそう)、それを渡すと 1 行 46 字で番号が
        振られ、文字表が丸ごとずれます。**出てくる字は日本語のまま**なので、
        目では気づけません (#158 と同じ型)。
        """
        import struct

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_tim2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        wide_cols = boku2.FONT_COLS * 2
        tim2, _ = make_tim2.font_sheet(rows=4, cols=wide_cols, cell=boku2.FONT_CELL)
        # 前提: この幅なら列数が 23 にならないこと (ならなければ検査にならない)
        self.assertNotEqual(wide_cols * boku2.FONT_CELL // boku2.FONT_CELL, boku2.FONT_COLS)
        tms = b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + tim2
        img = os.path.join(self.folder, "BOKU2.IMG")
        with open(os.path.join(self.folder, "BOKU2.IDX"), "rb") as fh:
            idx = fh.read()
        entry = next(e for e in boku2.read_dfi(idx, os.path.getsize(img))
                     if e["path"].endswith("bk_font.tms"))
        self.assertLessEqual(len(tms), entry["len"],
                             "作った画像が入れ物より大きい (切り詰めると TIM2 として読めない)")
        with open(img, "r+b") as fh:
            fh.seek(entry["at"])
            fh.write(tms)
        res = self.check(self.folder)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue("ドットに足りません" not in res.stdout,
                        "広すぎるのに「足りません」と言っている")
        self.assertIn(f"1 行 {wide_cols} 字になります", res.stdout,
                      "このままだと何字になるかを言っていない")
        self.assertIn(f"この作品は 1 行 {boku2.FONT_COLS} 字", res.stdout,
                      "本当は何字かを言っていない")

    def test_one_font_page_is_not_enough_and_check_says_so(self):
        """1 枚では文字表がまかなえないことを、`check` が先に言うこと (#167).

        手順書は「番号 0 から順に書き出して文字表に貼る」で終わっています。
        ところが **1 枚では終わりません**。公開ソースの `font.txt` は
        72 行 × 23 = 1656 字あるのに、`bk_font.tms` の頁は 512×1024 ドット、
        つまり 23 × 46 = **1058 マス**しかない。残り 598 字は別の画像にあり、
        向こうの `font2.txt` の字数がちょうど 598 で合います。

        これを言わないと、社長は 1058 字を手で書き写したあと、本文に
        `[1234]` が残るのを見て **自分の書き写しを疑います**。原因は別です。

        **問題 (→) ではなく、先に知っておくこと**なので、健全な練習データでも
        終了コードは 0 のままであるべき —— それも一緒に見ます。
        """
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        res = self.check(self.folder)
        self.assertEqual(res.returncode, 0,
                         "先に知らせるだけの行で、健全な練習データが赤くなっている")
        self.assertIn(f"文字表は全部で {boku2.FONT_GLYPHS} 字", res.stdout,
                      "文字表ぜんぶの字数を言っていない")
        # 練習データの画像の大きさを**実際に読んで**、道具と同じ式でマス数を出す。
        # 数字を写すと、練習データの形を変えたときにここだけ古くなる (#169)
        import struct
        img = os.path.join(self.folder, "BOKU2.IMG")
        with open(os.path.join(self.folder, "BOKU2.IDX"), "rb") as fh:
            idx = fh.read()
        entry = next(e for e in boku2.read_dfi(idx, os.path.getsize(img))
                     if e["path"].endswith("bk_font.tms"))
        with open(img, "rb") as fh:
            fh.seek(entry["at"])
            blob = fh.read(entry["len"])
        pages = boku2.tim2_pages(blob)
        self.assertGreaterEqual(len(pages), 2,
                                "練習データのフォントが 1 枚しかない (実物は 2 枚に分かれている)")
        # **このファイルの頁を全部足した数**で言うこと (#170)。1 枚目だけで数えると、
        # 「1 枚では N 字足りない」と言った直後に 2 枚目を挙げることになり、
        # 足し算が合わない
        each = [boku2.font_page_cells(p) for p in pages]
        cells = sum(each)
        self.assertGreater(min(each), 0, "マス数が 0 の頁がある")
        self.assertIn(f"マスは {' + '.join(str(n) for n in each)} = {cells} 個", res.stdout,
                      "頁ごとのマス数と合計を言っていない")
        self.assertIn(f"{boku2.FONT_GLYPHS - cells} 字ぶん足りない", res.stdout,
                      "足りない字数を言っていない")
        # 数えた頁を、**探索がもう一度挙げていない**こと (二重に数えたことになる)
        for pg in pages:
            self.assertNotIn(f"同じファイルの位置 0x{pg['at']:X}", res.stdout,
                             f"数えた頁 0x{pg['at']:X} を候補としても挙げている")
        del struct

    def test_the_hunt_reaches_a_real_sized_second_page(self):
        """**実物の大きさ**でも同じファイルの 2 枚目に届くこと (#171).

        #168〜#170 の探索は、ファイルの先頭 64KB しか読んでいませんでした。
        練習データは頁が小さい (2 枚目が 0xB2B0 = 45KB) ので通っていましたが、
        **実物の頁 1 枚は 512×1024 ドットの 8bit 索引で 51 万バイト**あり、
        2 枚目は 0x7D508 あたりに来ます。64KB しか読まなければ、実物では
        **必ず見落とす** —— 練習データの形に合わせた検査だけでは見えない穴でした。

        ここでは実物と同じ行数 (46 行 + 26 行) で組み立てて確かめます。
        """
        import io as _io
        import struct

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_tim2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        # 実物と同じ: 1 枚目 46 行 (1058 マス)、2 枚目 26 行 (598 マス)
        p1, _ = make_tim2.font_sheet(rows=46, cols=boku2.FONT_COLS, cell=boku2.FONT_CELL)
        p2, _ = make_tim2.font_sheet(rows=26, cols=boku2.FONT_COLS, cell=boku2.FONT_CELL)
        blob = b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + p1 + p2
        second_at = 0x80 + len(p1)
        # 前提: 2 枚目が「先頭だけ」の窓より **ずっと後ろ** にあること。
        # ここが成り立たないと、この検査は穴を見張れていない
        self.assertGreater(second_at, boku2.FONT_HUNT_HEAD * 4,
                           f"2 枚目が 0x{second_at:X}。窓 {boku2.FONT_HUNT_HEAD} より"
                           "十分後ろでないと、この検査に意味が無い")
        self.assertEqual(boku2.font_page_cells(boku2.tim2_pages(blob)[0]), 1058)

        entry = {"path": "system/bk_font.tms", "at": 0, "len": len(blob)}
        lines = boku2.font_page_hunt(_io.BytesIO(blob), [entry], entry,
                                     46 * boku2.FONT_COLS, {0x80})
        joined = "\n".join(lines)
        self.assertIn(f"同じファイルの位置 0x{second_at:X}", joined,
                      f"実物の大きさだと 2 枚目に届いていない:\n{joined}")
        self.assertIn(f"{26 * boku2.FONT_COLS} マス", joined, "2 枚目のマス数が違う")

    def test_a_continuation_buried_deep_in_another_file_is_still_found(self):
        """続きが**ファイルの奥**にあっても見つけること (#219).

        ほかのファイルは先頭 64KB しか読んでいませんでした。ところが英語化パッチの
        公開ソースに残っている書き出しの名前を見ると、実物には
        `title.tms_0x150100` `title.tms_0x164580` `title.tms_0x18b780`、
        `saveload.bin_0x22100` `bumper2.tm2_0x20300` があります。
        **TIM2 を 1.5 MB 先に持つファイルが実在する。** 文字表の続きがその形の
        ファイルに入っていたら、64KB しか読まないこちらは必ず見落とし、
        「この吸い出しの中には続きが見つかりませんでした」と言ってしまう。

        #171 で**同じファイル**の 2 枚目については直しました (4MB まで読む)。
        ほかのファイルだけが 64KB のまま残っていた形です。
        """
        import io as _io
        import struct

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_tim2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        cell = boku2.FONT_CELL
        page1, _ = make_tim2.font_sheet(rows=46, cols=boku2.FONT_COLS, cell=cell)
        page2, _ = make_tim2.font_sheet(rows=26, cols=boku2.FONT_COLS, cell=cell)
        want_w = boku2.tim2_pages(page1)[0]["width"]
        deep_at = 0x150100                      # 公開ソースの title.tms と同じ深さ
        self.assertGreater(deep_at, boku2.FONT_HUNT_HEAD,
                           "材料が弱い: 64KB の窓より浅い所に置いている")

        blob = bytearray()
        entries = []

        def put(path, data):
            entries.append({"path": path, "at": len(blob), "len": len(data)})
            blob.extend(data)

        put("system/bk_font.tms", b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + page1)
        put("system/title.tms", b"TMS\0" + b"\0" * (deep_at - 4) + page2)

        lines = boku2.font_page_hunt(_io.BytesIO(bytes(blob)), entries, entries[0],
                                     46 * boku2.FONT_COLS, {0x80}, wide=want_w)
        joined = "\n".join(lines)
        self.assertIn("title.tms", joined, f"奥に置いた続きが見つからない:\n{joined}")
        self.assertIn(f"0x{deep_at:X}", joined, f"見つけた位置が違う:\n{joined}")

    def test_the_hunt_says_how_deep_it_looked(self):
        """見つからなかったときは、**どこまで見たか**を言うこと (#219).

        「見つかりませんでした」だけだと、社長は 1951 個を手で開く側に回る。
        見た深さが書いてあれば、「奥にあるかもしれない」と分かる。
        """
        import io as _io

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        entry = {"path": "system/bk_font.tms", "at": 0, "len": 32}
        lines = boku2.font_page_hunt(_io.BytesIO(b"\0" * 32), [entry], entry, 100, set())
        joined = "\n".join(lines)
        self.assertIn("見つかりませんでした", joined, joined)
        self.assertIn("KB まで", joined, f"どこまで見たかを言っていない:\n{joined}")
        self.assertIn("MB まで", joined, f"深く見た分を言っていない:\n{joined}")

    def test_the_same_width_candidate_is_named_first(self):
        """続きの候補は、**1 枚目と同じ幅**のものを先に挙げること (#211).

        `looks_like_a_font_page` の決め手は「1 行 23 字の幅で割り切れる」だけ。
        幅 506〜527 ドットの画像は背景や UI にいくらでもあるので、実物では
        **候補が多すぎて本命が埋もれます**。しかも 5 件見つけたら探索を打ち切って
        いたので、**幅の違うものが先に 5 件出ただけで本命が一覧に載らない**。

        文字表はどの頁も同じ升目なので、続きなら幅は 1 枚目と同じはず。
        英語化パッチの公開ソースに残っている 2 枚も 512×1024 と 512×640 で、
        幅は揃っていました (大きさを見ただけで、中身は使っていません)。
        """
        import io as _io
        import struct

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_tim2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        cell = boku2.FONT_CELL
        page1, _ = make_tim2.font_sheet(rows=46, cols=boku2.FONT_COLS, cell=cell)
        want_w = boku2.tim2_pages(page1)[0]["width"]
        # おとり: 同じ「23 字で割り切れる」枠 (506〜527 ドット) には入るが、**幅が違う**。
        # 実物でこれに当たるのは、たまたま幅が近い背景や UI の画像
        decoy, _ = make_tim2.font_sheet(rows=60, cols=47, cell=11)
        decoy_w = boku2.tim2_pages(decoy)[0]["width"]
        self.assertNotEqual(decoy_w, want_w, "おとりの幅が本命と同じでは、検査にならない")
        self.assertTrue(boku2.looks_like_a_font_page(boku2.tim2_pages(decoy)[0]),
                        "おとりが候補として拾われない (材料が弱い)")
        real, _ = make_tim2.font_sheet(rows=26, cols=boku2.FONT_COLS, cell=cell)

        blob = bytearray()
        entries = []

        def put(path, data):
            entries.append({"path": path, "at": len(blob), "len": len(data)})
            blob.extend(data)

        put("system/bk_font.tms", b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + page1)
        # **本命より先に、おとりを 6 件**置く。5 件で打ち切る作りだと本命に届かない
        for i in range(6):
            put(f"bg/back{i}.tm2", decoy)
        put("system/bk_font2.tms", b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + real)

        lines = boku2.font_page_hunt(_io.BytesIO(bytes(blob)), entries, entries[0],
                                     46 * boku2.FONT_COLS, {0x80}, wide=want_w)
        joined = "\n".join(lines)
        self.assertIn("bk_font2.tms", joined, f"本命が一覧に無い:\n{joined}")
        named = [ln for ln in lines if "・" in ln]
        self.assertIn("bk_font2.tms", named[0],
                      f"同じ幅の候補が先頭に来ていない:\n{joined}")
        self.assertIn("1 枚目と同じ幅", named[0], f"同じ幅だと言っていない:\n{joined}")
        self.assertIn(f"同じ幅 {want_w} ドット", joined, f"見出しが幅を言っていない:\n{joined}")
        # おとりも**捨てない** (幅の読みがこちらの思い込みかもしれない)
        self.assertTrue(any("back" in ln for ln in named), f"幅の違う候補が消えた:\n{joined}")

    def test_fonts_are_found_by_shape_when_names_are_gone(self):
        """名前が付かなくても、フォントの診断が飛ばないこと (#172).

        今までフォントは **名前に `font` が入っているか** だけで選んでいました。
        ところが docs/09 の「実物で確かめたこと」に書いてあるとおり、
        **社長の実物では名前が付かず `#0 #1 …` のままでした** (#1・#3)。
        名前の並びの読み方はその後直しましたが、実物で通ったことはまだありません。

        名前が付かなければ、フォントの診断 (幅・マス数・2 枚目の探索) が
        **まるごと飛びます** —— しかも「見つかりません」とも言わずに。
        いちばん要るときに、いちばん静かに落ちる形でした。
        """
        import shutil
        import tempfile

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            shutil.copytree(self.folder, folder)
            idx_path = os.path.join(folder, "BOKU2.IDX")
            with open(idx_path, "rb") as fh:
                idx = bytearray(fh.read())
            # 名前はレコードの直後から並ぶ。**そこから先を全部**潰す
            # (e2e の name の傷は 64 バイトだけなので、名前が残って fallback に届かない)
            names_at = 16 + 27 * 16
            idx[names_at:] = b"\xff" * (len(idx) - names_at)
            with open(idx_path, "wb") as fh:
                fh.write(bytes(idx))

            # 前提: 本当に名前が 1 つも付かなくなったこと
            entries = boku2.read_dfi(bytes(idx), os.path.getsize(os.path.join(folder, "BOKU2.IMG")))
            named = [e for e in entries if "font" in os.path.basename(e["path"]).lower()]
            self.assertEqual(named, [],
                             f"名前がまだ残っている ({named}) ので、この検査は形の探索を試せていない")

            res = self.check(folder)
            self.assertIn("中身の形", res.stdout,
                          f"名前で拾えないのに、形で探したと言っていない:\n{res.stdout[-600:]}")
            # 形で拾ったうえで、**中身の診断まで進んでいる**こと
            self.assertIn("ドット / 1 画素 1 バイトのパレット番号", res.stdout,
                          "形で拾ったのに、フォントの中身を診ていない")
            self.assertIn("マスは", res.stdout, "マス数の知らせまで届いていない")

    def test_every_notice_is_in_the_troubleshooting_table(self):
        """**道具が出す「注意:」が、docs/10 で引けること** (#207).

        `→` の行は #184 で表に揃えました。ところが `注意:` で始まる行は
        別枠で、**どこにも一覧がありません**。docs/10 の「困ったとき」は
        `→` が付かない出方を引くための表なのに、数えたら **6 つのうち 6 つとも
        載っていませんでした** —— しかも 4 つは #188・#200・#202 で
        **自分が足したもの**。矢印のほうだけ表に足して、注意のほうを忘れていた。

        「困ったとき」の表は、素人が**画面に出た言葉をそのまま探す**所です。
        載っていなければ、その注意は読み捨てられます。
        """
        import glob
        import re

        def norm(s):
            return re.sub(r"\s+", " ", s.replace("`", "").replace("*", "")).strip()

        def key_of(text):
            """その注意を文書から探すときの鍵 (最初のまとまった固定部分)."""
            for frag in re.split(r"\{[^}]*\}", text):
                frag = norm(frag).strip(" 　(（)）、。:：")
                if len(frag) >= 8:
                    return frag[:12]
            return ""

        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = norm(fh.read())
        one = re.compile(r'\s*f?"((?:[^"\\]|\\.)*)"')
        notices = []
        # 実物で使う 3 本だけを見る (練習用の道具や、共通の入出力の失敗は別)
        for name in ("boku2.py", "proofread.py", "compare_tsv.py"):
            with open(os.path.join(REPO, "tools", name), encoding="utf-8") as fh:
                src = fh.read()
            # **注釈と説明文は落とす。** そこに例を書いても「出している」ことにはならない
            src = re.sub(r"^\s*#.*$", "", src, flags=re.M)
            src = re.sub(r'"""(?:.|\n)*?"""', "", src)
            # **行頭の改行を書いた注意も拾う** (#224)。`f"\\n注意: …"` の形が
            # 5 本あって、この見張りは**一度も見ていなかった**。#207 で足した
            # ばかりの検査に、足した本人が気づかない穴が空いていた
            for m in re.finditer(r'(?:print|say)\(\s*(?=f?"(?:\\n)*注意: )', src):
                pos, parts = m.end(), []
                while True:
                    got = one.match(src, pos)
                    if not got:
                        break
                    parts.append(got.group(1))
                    pos = got.end()
                text = "".join(parts)
                notices.append((name, text.split("注意: ", 1)[-1]))

        self.assertGreaterEqual(len(notices), 5,
                                f"注意を {len(notices)} 件しか拾えない (0 件なら必ず一致する)")
        missing = []
        for name, text in notices:
            key = key_of(text)
            self.assertTrue(key, f"{name}: 注意から鍵を作れない: {text[:40]}")
            if key not in doc:
                missing.append(f"{name}: {text[:46]} (鍵: {key})")
        self.assertFalse(missing,
                         "docs/10 で引けない「注意:」があります (困ったときの表に足す):\n  "
                         + "\n  ".join(missing))

    def test_the_first_hour_table_really_walks(self):
        """**docs/10 の「最初の 1 時間」を、書いてあるとおりに歩けること** (#206).

        あの表は社長がいちばん大事な 1 時間に見るもので、こう書いてあります:

            表の「見る 1 点」は、**道具が実際にその言葉で出力する**ものだけに
            してあります (練習データで 1 つずつ確かめました)。

        **その確かめに、検査が付いていませんでした。** 実際に歩いたら、
        20 分の行が外れていた —— 「続けて `system/system.msg` のような
        **フォルダ付きの名前**が並ぶか」と書いてあるのに、`unpack` は
        「20 個に切り分けました」の 1 行しか出しません。名前が読めたかは
        **索引の読み方が当たっているかの一番の手がかり**で、実物ではまさに
        そこが外れました (#1・#3)。`ls` を打たないと分からない、では困ります。

        ここでは表の行を読み取って、**書いてある言葉が本当に出るか**を見ます。
        画面でやる行 (文字表を作る) は走らせようが無いので、
        **飛ばした数を数えて**出します (黙って飛ばさない)。
        """
        import re
        import subprocess
        import make_boku2_sample

        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        table = doc.split("## 実物が届いた日の最初の 1 時間", 1)[1].split("\n\n表の", 1)[0]
        rows = [ln for ln in table.splitlines()
                if ln.startswith("| 〜")]
        self.assertGreaterEqual(len(rows), 6, f"表から {len(rows)} 行しか読めない")

        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            out_dir = os.path.join(tmp, "OUT")
            tsv = os.path.join(tmp, "all.tsv")
            font = os.path.join(sample, "font.txt")

            def run(*argv):
                res = subprocess.run([sys.executable, *argv],
                                     capture_output=True, text=True, cwd=REPO)
                return res.stdout + res.stderr

            boku2py = os.path.join(REPO, "tools", "boku2.py")
            checked = run(boku2py, "check", sample)
            unpacked = run(boku2py, "unpack", os.path.join(sample, "BOKU2.IDX"),
                           os.path.join(sample, "BOKU2.IMG"), out_dir)
            mapped = run(boku2py, "maps", os.path.join(sample, "MAP"),
                         "-o", os.path.join(out_dir, "maps"))
            texted = run(boku2py, "text", out_dir, "-f", font, "-o", tsv)
            proofed = run(os.path.join(REPO, "tools", "proofread.py"), tsv,
                          "--font-chars", font)

            #: 表の行の頭 → その行を確かめる材料と、出るはずの言葉
            expect = {
                "〜5 分": (checked, ["問題なし"]),
                # 検査値ファイルの段 (#243)。**数**と**中身**の 2 つが出ること
                "〜8 分": (checked, ["[検査値]", "同じ数", "件すべて合いました"]),
                "〜10 分": (checked, ["[MAP]", "入れ物として読めた", "1 番が会話だった"]),
                "〜20 分": (unpacked, ["個に切り分けました", "/"]),
                "〜25 分": (mapped, ["個の入れ物から", "個の部品"]),
                "〜40 分": (None, []),            # 画面でやる行。走らせようが無い
                "〜55 分": (texted, ["文字表で全部読めました"]),
                "〜60 分": (proofed, ["使った設定", "文字表 "]),
            }
            skipped = []
            for row in rows:
                head = row.split("|")[1].strip()
                self.assertIn(head, expect, f"表に知らない行がある: {head}")
                got, wants = expect[head]
                if got is None:
                    skipped.append(head)
                    continue
                for want in wants:
                    self.assertTrue(want in got,
                                    f"{head}: 「{want}」が出ていない\n{got[-400:]}")
            # **飛ばした行を黙って通さない** (0 件で緑にしないのと同じ理由)
            self.assertEqual(skipped, ["〜40 分"],
                             f"走らせられない行が変わっています: {skipped}")

            # 20 分の行が言う「フォルダ付きの名前」が、本当に名前として出ていること
            names = [ln for ln in unpacked.splitlines() if "最初の名前" in ln]
            self.assertTrue(names, f"名前の行が出ていない:\n{unpacked}")
            self.assertTrue("/" in names[0],
                            f"フォルダ付きの名前が並んでいない: {names[0]}")

            # 60 分の行は「文字表が**実物のもの**か」を見る行。道が読めること
            said = next(ln for ln in proofed.splitlines() if ln.startswith("使った設定"))
            self.assertFalse("../.." in said,
                             f"渡した道が読めない形で出ている: {said}")

    def test_the_two_sides_classify_the_same_bytes_the_same_way(self):
        """**同じバイトを見て、画面と CLI が同じ性質を言うこと** (#205).

        画面には「性質を地図にする」(`blockStats` / `classifyStats`) があり、
        圧縮らしい / 波形らしい / ゼロ埋め まで言えます。CLI には**同じものが
        無かった**ので、読めない `.msg` については 16 進を見せるだけでした。
        16 バイトでは何も言えませんが、数 KB あればここまで言えます。

        写したからには**ずれていないこと**を見ます。言葉を比べるのではなく、
        **同じ材料を両方に通して、同じ答えになるか**を見ます (言葉だけそろえて
        判定がずれる、を防ぐ)。
        """
        import json
        import math
        import random
        import shutil
        import subprocess
        import boku2

        if not shutil.which("node"):
            self.skipTest("node がありません")

        random.seed(9)
        fixtures = {
            "乱数": bytes(random.getrandbits(8) for _ in range(4096)),
            "波形": bytes(int(127 + 100 * math.sin(i / 20)) & 0xFF for i in range(4096)),
            "ゼロ": bytes(4096),
            "英文": (b"The quick brown fox jumps over the lazy dog. " * 100)[:4096],
            "本文の番号": b"".join(int.to_bytes(random.randint(1, 900), 2, "little")
                                   for _ in range(2048)),
            "詰め物混じり": (b"\x41" * 80) + bytes(4016),
            # **小さい標本の乱数**。しきい値を固定値にすると、ここを取りこぼす
            # (256 個しか見ていないと、完全な乱数でもエントロピーは 7.3 程度)
            "小さい乱数": bytes(random.getrandbits(8) for _ in range(256)),
        }
        mine = {k: boku2.classify_block(boku2.block_stats(v)) for k, v in fixtures.items()}
        # **標本数から期待値を出していること**の裏付け。固定のしきい値だと
        # 小さい乱数を「不明」に落とす (docs/07 の「しきい値を固定値にしてはいけない」)
        self.assertEqual(mine["小さい乱数"], "high",
                         "小さい標本の乱数を取りこぼしている (しきい値が固定値になっていないか)")
        # **1 種類しか出ないなら、比べても意味が無い** (#197 と同じ)
        self.assertGreaterEqual(len(set(mine.values())), 3,
                                f"材料が偏っていて答えが {set(mine.values())} しか出ない")

        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "in.json")
            with open(data, "w", encoding="utf-8") as fh:
                json.dump({k: list(v) for k, v in fixtures.items()}, fh)
            script = os.path.join(tmp, "run.mjs")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(f'''
import fs from "node:fs";
const src = fs.readFileSync({json.dumps(os.path.join(REPO, "web", "app.js"))}, "utf8");
/* blockStats / classifyStats は sniff の切り出し印の外にあるので、
   その 2 つと、使っている SJIS_LEAD / SJIS_TRAIL だけを名前で切り出す */
const take = (head, stop) => {{
  const i = src.indexOf(head);
  if (i < 0) throw new Error("見つからない: " + head);
  const j = src.indexOf(stop, i + head.length);
  return src.slice(i, j < 0 ? src.length : j);
}};
const code = take("const SJIS_LEAD =", "\\nfunction ")
  + take("function blockStats(", "\\n/**")
  + take("function classifyStats(", "\\n/**");
const m = new Function(code + "\\nreturn {{ blockStats, classifyStats }};")();
const input = JSON.parse(fs.readFileSync({json.dumps(data)}, "utf8"));
const out = {{}};
for (const [k, v] of Object.entries(input)) {{
  out[k] = m.classifyStats(m.blockStats(Uint8Array.from(v)));
}}
console.log(JSON.stringify(out));
''')
            res = subprocess.run(["node", script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0,
                             f"画面側を動かせない:\n{res.stderr[-400:]}")
            theirs = json.loads(res.stdout)

        self.assertEqual(mine, theirs,
                         "同じバイトを見て、画面と CLI が違う性質を言っています\n"
                         f"  CLI : {mine}\n  画面: {theirs}")

    def test_the_unreadable_bytes_are_named_when_they_can_be(self):
        """**読めなかった 16 バイトから、分かることは言うこと** (#204).

        `check` は読めない `.msg` の先頭 16 バイトを見せて終わっていました。
        画面のほうは同じバイトを見て「音声 (VAG)」まで言うのに、
        **社長が最初に打つのは `check`** です。16 進を渡されても、素人には
        「読めなかった」以上のことが分かりません。

        言えるのは 3 通り: 別の形式の目印がある (名前と中身が違う) /
        ゼロ埋めや同じバイトの繰り返し (詰め物か壊れている) / 制御コードばかり。
        **分からないときは黙ります** —— 当てずっぽうを足すと、16 進だけのほうが
        まだましになるので。
        """
        import re
        import boku2

        cases = [
            (b"VAGp" + bytes(12), "音声 (VAG)", True),
            (b"TIM2" + bytes(12), "画像 (TIM2)", True),
            (b"\x7FELF" + bytes(12), "本体プログラム (ELF)", True),
            (bytes(16), "ゼロ埋め", False),
            (b"\xEE" * 16, "同じバイト (0xEE)", False),
        ]
        for head, want, known in cases:
            got = boku2.guess_kind(head)
            self.assertTrue(want in got, f"{head[:4]!r}: 「{want}」と言わない ({got!r})")
            note = boku2.guess_kind_note(head)
            # **別の形式の目印が出たときだけ**「名前と中身が違う」と言う
            self.assertEqual("中身は別のもの" in note, known,
                             f"{head[:4]!r}: 名前と中身の話を出す/出さないが逆: {note}")

        # **分からないものには何も言わない**
        self.assertEqual(boku2.guess_kind(bytes(range(16))), "",
                         "分からないのに当てずっぽうを言っている")
        self.assertEqual(boku2.guess_kind_note(bytes(range(16))), "")

        # **先頭で分からなくても、中身の性質でなら言える** (#236)。
        # ここが `guess_kind_note` の最後の枝で、実物の「知らない形」で必ず通る道。
        # 練習データの `.msg` は 56〜80 バイトしかなく、この分類は `tile`
        # (言うことなし) にしかならないので、通しでは一度も通らない
        unknown = bytes(range(16))
        bodies = {
            "ゼロ埋め": b"\x00" * 512,
            "日本語テキストらしい": bytes(v for i in range(256) for v in (0x82, 0xA0 + (i % 40))),
            "ASCII テキストらしい": b"HELLO WORLD THIS IS PLAIN TEXT. " * 16,
        }
        for want, body in bodies.items():
            note = boku2.guess_kind_note(unknown, body)
            self.assertTrue(want in note,
                            f"中身の性質で「{want}」と言えるはずが {note!r}")
            self.assertFalse("中身は別のもの" in note,
                             f"目印が無いのに名前と中身の話をしている: {note!r}")
        # **性質も分からなければ黙る** (当てずっぽうを足さない)
        self.assertEqual(boku2.guess_kind_note(unknown, bytes(range(16)) * 4), "",
                         "分からない中身に当てずっぽうを言っている")

        # 診断にその言葉が出ること (関数にあっても出さなければ意味が無い)
        import io
        import make_boku2_sample
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            make_boku2_sample.damage(folder, "msg")
            out = io.StringIO()
            boku2.check(folder, out=out)
            line = next((ln for ln in out.getvalue().splitlines()
                         if "読めない .msg の例" in ln), "")
            self.assertTrue(line, "読めない .msg の行が出ていない")
            self.assertTrue("同じバイト" in line,
                            f"16 進を見せるだけで、分かることを言っていない: {line}")

        # **先頭 4 バイトで分からないときは、広く見た性質を言うこと** (#205)
        import random as _random
        _random.seed(3)
        blob = bytes(_random.getrandbits(8) for _ in range(4096))
        with tempfile.TemporaryDirectory() as tmp:
            idx = bytearray(b"DFI\0" + struct.pack("<III", 1, 0, 0))
            idx += struct.pack("<HHIII", 0, 0, 0, 0, len(blob))
            idx += b"a.msg\0"
            with open(os.path.join(tmp, "BOKU2.IDX"), "wb") as fh:
                fh.write(bytes(idx))
            with open(os.path.join(tmp, "BOKU2.IMG"), "wb") as fh:
                fh.write(blob)
            out = io.StringIO()
            boku2.check(tmp, out=out)
            line = next((ln for ln in out.getvalue().splitlines()
                         if "読めない .msg の例" in ln), "")
            self.assertTrue("圧縮らしい" in line,
                            f"広く見た性質を言っていない: {line}")

        # **画面と同じ一覧を持っていること** (#189 と同じ、片側だけ増えるのを防ぐ)
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        start = app.index("const MAGICS = [")
        block = app[start:app.index("\n];", start)]
        ui = set(re.findall(r'label: "([^"]+)"', block))
        cli = {label for _magic, label in boku2.MAGICS}
        self.assertGreaterEqual(len(ui), 8, f"画面の一覧を {len(ui)} 件しか拾えない")
        self.assertEqual(cli, ui, "先頭 4 バイトで分かる形式の一覧が、画面と CLI で違います")

    def test_where_the_dump_runs_out_is_said_not_just_how_many(self):
        """**取り出せない項目の「どこ」まで言うこと** (#203).

        今までは数だけでした ——「索引は 20 個を名乗っていますが、取り出せるのは
        12 個です」。社長は**何かが足りない**ことは分かっても、**どこから
        足りないのか**が分かりません。道具は知っています: 索引が指す一番後ろの
        位置と、本体の大きさ。引けば**あと何バイト足りないか**が出ます。

        さらに、外を指す項目が**索引の後ろのほうに固まっている**なら
        吸い出しが途中で切れた形、**ばらけている**なら位置の単位 (バイト /
        セクタ) の読み違い。原因が 2 つあると言うだけだった所を、
        材料から**どちらかに寄せられます**。
        """
        import boku2

        def build(lbas, size):
            idx = bytearray(b"DFI\0" + struct.pack("<III", len(lbas), 0, 0))
            for lba in lbas:
                idx += struct.pack("<HHIII", 0, 0, 0, lba, 2048)
            for i in range(len(lbas)):
                idx += f"f{i:02d}.bin".encode() + b"\0"
            return boku2.dfi_dropped(bytes(idx), size)

        # 1. **途中で切れた形**: 後ろの 8 件が本体の外
        cut = build(list(range(20)), 12 * 2048)
        self.assertEqual(cut["outside"], 8, f"外を指す数が違う: {cut}")
        self.assertTrue(cut["clean_cut"], f"切れた形だと見ていない: {cut}")
        self.assertEqual(cut["needs"] - cut["have"], 8 * 2048,
                         f"足りない量が違う: {cut}")
        note = boku2.dropped_note(cut)
        self.assertTrue("バイト足りない" in note, f"足りない量を言っていない: {note}")
        self.assertTrue(f"0x{cut['first_outside']:X}" in note,
                        f"どこから外なのかを言っていない: {note}")
        self.assertTrue("切れた形" in note, f"どちらの形かを言っていない: {note}")

        # 2. **単位の読み違いの形**: 1 つおきに遠くを指す
        mixed = build([i if i % 2 == 0 else i + 10_000 for i in range(20)], 20 * 2048)
        self.assertFalse(mixed["clean_cut"], f"ばらけているのに切れた形と見ている: {mixed}")
        note = boku2.dropped_note(mixed)
        self.assertTrue("ばらけて" in note and "単位" in note,
                        f"単位の読み違いだと言っていない: {note}")
        self.assertFalse("切れた形" in note, f"両方の言い方をしている: {note}")

        # 3. **並べ替えで判定しない。** 位置の順に並べると遠いものは必ず後ろに
        #    来るので、「固まっている」が**いつも真**になる (#197 と同じ型の罠)
        self.assertNotEqual(cut["clean_cut"], mixed["clean_cut"],
                            "2 つの形を見分けられていない (いつも同じ答えになっている)")

        # 4. 全部が本体の中なら、何も言わない
        fine = build(list(range(20)), 20 * 2048)
        self.assertEqual(fine["outside"], 0, f"無事なのに外があると言う: {fine}")
        self.assertEqual(boku2.dropped_note(fine), "", "無事なのに知らせている")

    def test_entries_that_cannot_be_taken_out_are_counted(self):
        """索引が名乗る数と、**取り出せる数**の差を言うこと (#178).

        `read_dfi` は「本体の外を指す項目」を黙って落とします。落とすこと自体は
        正しい (読めば本体の外を読む) のですが、**落とした数を捨てていた**ので、
        吸い出しが途中で切れていても

            12 個に切り分けました → OUT          ← 終了コード 0

        で終わっていました。20 個のうち 8 個が出ていないことが分かりません。

        `check` のほうも、使用率の行から「索引の読み方が外れている疑い」とだけ
        言っていました。**吸い出しが途中で切れている**ときも同じ見え方になるので、
        数を出さないと見分けがつきません。
        """
        import shutil
        import subprocess
        import tempfile

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        with open(os.path.join(self.folder, "BOKU2.IDX"), "rb") as fh:
            idx = fh.read()
        whole = os.path.getsize(os.path.join(self.folder, "BOKU2.IMG"))

        # 1. そろっていれば、名乗る数と取り出せる数が同じで、何も言わない
        full = boku2.dfi_dropped(idx, whole)
        self.assertEqual(full["records"], full["taken"],
                         f"健全なのに数が合わない: {full}")
        self.assertGreater(full["records"], 10, f"前提が崩れた: {full}")
        self.assertEqual(boku2.dropped_note(full), "", "健全なのに文句を言っている")

        # 2. 本体を半分に切ると、差が出て、その数を言う
        half = boku2.dfi_dropped(idx, whole // 2)
        self.assertLess(half["taken"], full["taken"], "半分にしたのに取り出せる数が同じ")
        self.assertEqual(half["records"], full["records"],
                         "索引が名乗る数は本体の長さで変わらないはず")
        self.assertEqual(half["outside"], full["taken"] - half["taken"],
                         f"外を指す数と減った数が合わない: {half}")
        note = boku2.dropped_note(half)
        for must in (str(half["records"]), str(half["taken"]), str(half["outside"])):
            self.assertIn(must, note, f"{must} を言っていない: {note}")
        self.assertIn("途中で切れた形", note, f"いちばんありそうな原因を言っていない: {note}")

        # 3. 通しで: unpack が赤くなり、check もその行を出すこと
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "trunc")
            os.makedirs(d)
            shutil.copy(os.path.join(self.folder, "BOKU2.IDX"), d)
            with open(os.path.join(self.folder, "BOKU2.IMG"), "rb") as fh:
                img = fh.read()
            with open(os.path.join(d, "BOKU2.IMG"), "wb") as fh:
                fh.write(img[:len(img) // 2])
            shutil.copytree(os.path.join(self.folder, "MAP"), os.path.join(d, "MAP"))
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "boku2.py"), "unpack",
                 os.path.join(d, "BOKU2.IDX"), os.path.join(d, "BOKU2.IMG"),
                 os.path.join(tmp, "out")], capture_output=True, text=True)
            self.assertEqual(res.returncode, 1,
                             f"8 個取り出せていないのに {res.returncode}:\n{res.stdout}{res.stderr}")
            self.assertIn("取り出せるのは", res.stderr, f"数を言っていない:\n{res.stderr}")
            self.assertIn("取り出せるのは", self.check(d).stdout, "check が言っていない")
            # 画面側にも同じ判定があること (片側にだけ足して忘れない)
            with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
                ui = fh.read()
            self.assertTrue("本体の外を指す ${outside} 個" in ui,
                            "web/app.js に同じ判定が無い")

    def test_an_arrow_always_means_a_nonzero_exit(self):
        """**→ を出したら終了コードは 1** —— どの命令でも (#177).

        `check` は前からそうでした。ところが `unpack` / `maps` / `text` は
        → を出しながら 0 を返していました。docs/10 の手順 6 は 3 つの命令を
        続けて打つので、途中で

            → 読めるはずの形なのに 1 行も出なかったファイル 4 個 (…)

        と出ていても、終了コードが 0 なら**次の命令に進んでしまう**。しかも
        `text` のこの場合は **31 行のうち 19 行**しか出ていません。#159 で
        61% 落としたときと同じ壊れ方が、今度は終了コードの側に残っていた。

        **どの → でも赤にする、ではありません。** 「入れ物ではありません
        (飛ばしました)」はごみを飛ばしただけで仕事は済んでいるし (#162)、
        「名前が付かなかった」もファイルは全部出ています。
        **出るはずのものが出ていない**ときだけ赤にします。

        ここでは 2 つ見ます: **健全なら 0**、**本文が落ちたら 1**。
        片方だけだと「いつも 1 を返す」でも通ってしまいます。
        """
        import glob
        import subprocess
        import tempfile

        def call(*a):
            return subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), *a],
                                  capture_output=True, text=True)

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            # 1. 健全な通しは、どの命令も 0 で終わること (→ を出していない)
            steps = [
                ("unpack", [os.path.join(self.folder, "BOKU2.IDX"),
                            os.path.join(self.folder, "BOKU2.IMG"), out]),
                ("maps", [os.path.join(self.folder, "MAP"), "-o", os.path.join(out, "maps")]),
                ("text", [out, "-f", os.path.join(self.folder, "font.txt"),
                          "-o", os.path.join(tmp, "all.tsv")]),
            ]
            for cmd, rest in steps:
                res = call(cmd, *rest)
                self.assertEqual(res.returncode, 0,
                                 f"健全なのに {cmd} が {res.returncode}:\n{res.stdout}{res.stderr}")
                # **行頭の → だけ**が知らせの印。「20 個に切り分けました → OUT」の
                # ような進み具合の行にも同じ字を使っているので、含むかで見ない
                arrows = [l for l in (res.stdout + res.stderr).splitlines()
                          if l.startswith("→")]
                self.assertEqual(arrows, [], f"健全なのに {cmd} が知らせを出している: {arrows}")
            full = sum(1 for _ in open(os.path.join(tmp, "all.tsv"), encoding="utf-8")) - 1
            self.assertGreater(full, 20, f"通しで {full} 行しか出ていない (前提が崩れた)")

            # 2. 本文の一部を読めなくすると、→ が出て 1 で終わること
            for path in glob.glob(os.path.join(out, "**", "*.msg"), recursive=True):
                with open(path, "r+b") as fh:
                    fh.write(b"\xee" * 16)
            res = call("text", out, "-f", os.path.join(self.folder, "font.txt"),
                       "-o", os.path.join(tmp, "less.tsv"))
            less = sum(1 for _ in open(os.path.join(tmp, "less.tsv"), encoding="utf-8")) - 1
            # **本当に減っていること**を先に確かめる。減っていなければ検査にならない
            self.assertLess(less, full, f"壊したのに行数が減っていない ({full} → {less})")
            arrows = [l for l in res.stderr.splitlines()
                      if l.startswith("→") and "1 行も出なかった" in l]
            self.assertTrue(arrows, f"減ったのに知らせを出していない:\n{res.stderr}")
            self.assertEqual(res.returncode, 1,
                             f"→ を出したのに {res.returncode} で終わっている "
                             f"({full} 行が {less} 行に減った):\n{res.stderr[:300]}")

    def test_no_section_can_see_nothing_and_still_pass(self):
        """**段まるごと 0 件で「問題なし」にならない**ことを、まとめて見張る (#176).

        #174 (MAP が無い)、#175 (診ていない段を数える) と 1 件ずつ直してきましたが、
        1 件ずつでは漏れます。実際 #175 の直後に確かめたら、**`MAP/` はあるが空**の
        とき (吸い出しの失敗、フォルダ違い) がまだ通り抜けていました:

            [MAP] 0 件 / 入れ物として読めた 0 件 / … / 会話 0 行
            == 結果: 問題なし。docs/10 の手順へ

        そこで、**段を 1 つずつ欠けさせた吸い出しを作って回す**形にします。
        どの段が欠けても、`問題なし` で終わってはいけない (→ が出るか、
        「診ていない段」に数えるか、どちらかは必ず起きる)。
        新しい段を足したら、ここに 1 行足せば同じ見張りが効きます。
        """
        import shutil
        import tempfile

        def strip_map_files(folder):
            for f in os.listdir(os.path.join(folder, "MAP")):
                os.remove(os.path.join(folder, "MAP", f))

        def drop_map_dir(folder):
            shutil.rmtree(os.path.join(folder, "MAP"))

        def blank_img(folder):
            with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
                fh.write(b"\0" * 2048)

        #: (欠けさせ方, その段の名前)。**欠けさせたら診断が黙ってはいけない**もの
        CASES = [
            (strip_map_files, "MAP はあるが空 (会話を 1 つも診ない)"),
            (drop_map_dir, "MAP が無い (会話を 1 つも診ない)"),
            (blank_img, "本体が空 (本文もフォントも診ない)"),
        ]
        bad = []
        with tempfile.TemporaryDirectory() as tmp:
            for i, (break_it, what) in enumerate(CASES):
                folder = os.path.join(tmp, f"c{i}")
                shutil.copytree(self.folder, folder)
                break_it(folder)
                res = self.check(folder)
                out = res.stdout
                said = ("問題なし" not in out) or ("診ていない段" in out)
                if not said or res.returncode == 0 and "問題なし" in out:
                    bad.append(f"{what}: 「{out.strip().splitlines()[-1]}」で終わっている")
        self.assertEqual(len(CASES), 3, "欠けさせ方を足したら数も直すこと")
        self.assertEqual(bad, [],
                         "段がまるごと 0 件なのに、そのまま通している:\n  " + "\n  ".join(bad))

    def test_saving_the_table_as_ansi_loses_real_glyphs_and_is_said(self):
        """**ANSI で保存すると `♡` などが `?` になる。それを言うこと** (#199).

        docs/10 は「メモ帳の ANSI でも構いません。どの形式でも道具の側が
        見分けて読みます」と書いていました。**読むのは確かにできます。**
        問題は保存のほうで、cp932 に無い字はその場で `?` に変わります。

        公開ソースの `font.txt` (1656 字) を数えると、cp932 で書けない字は
        **4 つ** —— `¥` `—` `♡` `︙`。`♡` は台詞に普通に出るので、
        本文がその場で書き換わったまま先へ進むことになります。しかも `?` は
        半角なので、校正では「半角文字が混ざっています」と出て**訳文のせいに
        見えます** (#188 と同じ、原因が埋もれる形)。

        半角の `?` は本物のフォントに **1 つだけ**あるので、**2 つ以上あれば
        潰れた疑い**として言います。
        """
        import boku2

        # 1. **前提を実物で確かめる** (ここが崩れたら話ごと変わる)
        table = os.path.join(PUBLIC_SRC, "font.txt")
        if not os.path.isfile(table):
            self.skipTest(f"公開ソースに font.txt が無い ({table})")
        with open(table, encoding="utf-8", errors="replace") as fh:
            chars = [c for c in fh.read() if c not in "\r\n"]
        self.assertGreater(len(chars), 1000, f"文字表が {len(chars)} 字しかない")
        lost = sorted({c for c in chars if not self._fits_cp932(c)})
        self.assertTrue(lost, "cp932 で書けない字が 1 つも無い (前提が崩れた)")
        self.assertEqual(chars.count("?"), 1,
                         "本物の ? が 1 つでないなら、2 つ以上で疑う読みが崩れる")

        # 2. **実際に ANSI で保存して読み直すと、その字が ? になる**
        with tempfile.TemporaryDirectory() as tmp:
            ansi = os.path.join(tmp, "font.txt")
            with open(ansi, "wb") as fh:
                fh.write("".join(chars).encode("cp932", "replace"))
            got = boku2.load_font(ansi)
            self.assertTrue(got, "ANSI の文字表を読めない")
            marks = [i for i, g in enumerate(got) if g == "?"]
            self.assertGreaterEqual(len(marks), 1 + len(lost),
                                    f"? が {len(marks)} 個 (潰れた {len(lost)} 字 + 本物 1)")

            # 3. **潰れていると言うこと**
            bad = boku2.ansi_damage(got)
            self.assertTrue(bad, "潰れているのに気づいていない")
            note = boku2.ansi_damage_note(bad)
            self.assertTrue("ANSI" in note and "UTF-8" in note,
                            f"原因と直し方を言っていない: {note}")
            # **消える字を並べて言うこと。** ♡ だけで見ると、下の
            # 「本文の ♡ などが」で通ってしまう (全文から語句を探さない、6 度目)
            for ch in ("¥", "—", "♡", "︙"):
                self.assertTrue(ch in note, f"消える字 {ch} を挙げていない: {note}")

            # 4. **無事な文字表には何も言わない** (毎回出たら誰も読まなくなる)
            ok = boku2.load_font(table)
            self.assertFalse(boku2.ansi_damage(ok),
                             "UTF-8 のままの文字表に文句を言っている")

    @staticmethod
    def _fits_cp932(ch: str) -> bool:
        try:
            ch.encode("cp932")
        except UnicodeEncodeError:
            return False
        return True

    # ---- 使われている文字番号の最大を、意味まで付けて言う (#230) ----------

    def test_the_biggest_glyph_number_is_judged_not_just_printed(self):
        """**この 1 つの数で 3 つ決まる**ので、数字だけ出さない (#230/#96).

        実物が届いた日にいちばん早く出る数です。1656 に収まらなければ
        「2 バイトを 1 字の番号として読む」という読み方ごと外れている合図で、
        収まるなら**文字表の 2 枚目を探す必要があるか**がその場で決まります。
        (2 枚目がどのファイルにあるかは、まだ分かっていない —— docs/09)
        """
        import boku2

        one = boku2.FONT_PAGE1_GLYPHS
        self.assertEqual(one + 598, boku2.FONT_GLYPHS,
                         "1 枚目 + 2 枚目が文字表ぜんぶにならない (前提が崩れた)")
        # 1. 1 枚目に収まる → 2 枚目は要らない、と言い切る
        low = boku2.glyph_range_note(one - 1)
        self.assertTrue("2 枚目の画像は要りません" in low, low)
        self.assertFalse(low.startswith("→"), f"問題でないのに → を付けている: {low}")
        # 2. 1 枚目を超える → **何字ぶん**要るかまで言う
        mid = boku2.glyph_range_note(one)
        self.assertTrue("2 枚目の画像が要ります" in mid, mid)
        self.assertTrue("その先頭から 1 字ぶん" in mid, f"何字ぶんかを言っていない: {mid}")
        top = boku2.glyph_range_note(boku2.FONT_GLYPHS - 1)
        self.assertTrue(f"その先頭から {598} 字ぶん" in top,
                        f"2 枚目ぴったりの字数になっていない: {top}")
        # 3. 文字表に収まらない → **読み方ごと違う**と言い、報告を促す
        over = boku2.glyph_range_note(boku2.FONT_GLYPHS)
        self.assertTrue(over.startswith("→"), f"問題として出していない: {over}")
        self.assertTrue("読み方そのものが違います" in over, over)
        self.assertTrue("報告してください" in over, over)

    def test_a_partly_read_dump_does_not_get_a_page_verdict(self):
        """**診ていない所があるなら、頁の判定を出さない** (#234).

        `check` は「いちばん大きい番号は 165 なので 2 枚目の画像は要りません」と
        言い切っていました。ところが MAP フォルダが渡されていなければ、
        **物語の会話を 1 行も読んでいない**。この作品の本文の大半がそこにあるので、
        本当の最大はもっと大きいかもしれず、頁の判定はひっくり返り得ます。
        #232 で `used` に足したのと同じ断りを、`check` にも入れます。

        ただし **1656 以上**という判定だけは一部しか見ていなくても動きません。
        1 つでも収まらない番号が出た時点で、読み方が違うと決まるからです。
        """
        import io
        import shutil
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)

            out = io.StringIO()
            boku2.check(folder, out=out)
            whole = out.getvalue()
            self.assertTrue("2 枚目の画像は要りません" in whole,
                            f"ぜんぶ診たのに判定を出していない:\n{whole[-600:]}")

            shutil.rmtree(os.path.join(folder, "MAP"))
            out = io.StringIO()
            boku2.check(folder, out=out)
            part = out.getvalue()
            self.assertTrue("MAP の会話を診ていない" in part,
                            f"何を診ていないか言っていない:\n{part[-600:]}")
            self.assertFalse("2 枚目の画像は要りません" in part,
                             f"本文の一部だけで頁の判定を言い切っている:\n{part[-600:]}")
            self.assertTrue("ここでは決まりません" in part,
                            f"決まらないと言っていない:\n{part[-600:]}")

        # 1656 以上は、一部しか見ていなくても言い切ること (読み方そのものの合図)
        over = boku2.glyph_range_note(boku2.FONT_GLYPHS + 1, "MAP の会話")
        self.assertTrue(over.startswith("→"), f"一部でも言い切るべき所を弱めた: {over}")
        self.assertTrue("読み方そのものが違います" in over, over)

    def test_the_two_sides_judge_the_biggest_number_the_same_way(self):
        """画面と一括処理が、同じ番号に同じことを言う (#230)."""
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node が無い")
        import boku2

        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        i = app.find("function glyphRangeNote(")
        self.assertGreater(i, 0, "app.js に glyphRangeNote が無い")
        j = app.index("\n}\n", i) + 3
        consts = re.findall(r"^const (?:FONT_COLS|FONT_GLYPHS|FONT_PAGE1_GLYPHS) = \d+;",
                            app, re.M)
        self.assertEqual(len(consts), 3, f"app.js から定数を {len(consts)} 件しか拾えない")
        tops = [0, 1, boku2.FONT_PAGE1_GLYPHS - 1, boku2.FONT_PAGE1_GLYPHS,
                boku2.FONT_GLYPHS - 1, boku2.FONT_GLYPHS, boku2.FONT_GLYPHS + 100, 40000]
        #: 診ていない所の有無も突き合わせる (#234)。片側だけ断りを忘れたら落ちる
        unseens = ["", "MAP の会話", "MAP の会話と本文 601 件"]
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "range.mjs")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("\n".join(consts) + "\n" + app[i:j]
                         + "\nconsole.log(JSON.stringify("
                         "JSON.parse(process.argv[2]).map(([t, u]) => glyphRangeNote(t, u))));\n")
            cases = [[t, u] for t in tops for u in unseens]
            res = subprocess.run([node, script, json.dumps(cases)],
                                 capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        got = json.loads(res.stdout)
        for k, (n, u) in enumerate(cases):
            want = boku2.glyph_range_note(n, u)
            self.assertEqual(want, got[k],
                             f"番号 {n} / 診ていない {u!r} で言うことが違う:"
                             f"\n  CLI: {want}\n  画面: {got[k]}")
        # 4 通りとも材料に入っていること (同じ文ばかり比べても一致してしまう)
        for want in ("2 枚目の画像は要りません", "2 枚目の画像が要ります", "収まりません",
                     "ここでは決まりません"):
            self.assertTrue(any(want in g for g in got),
                            f"「{want}」を出す材料が入っていない")

    # ---- 文字表の書き写しで 1 行ぶんずれる事故 (#229) ----------------------

    def _public_table_lines(self, name: str) -> list[str]:
        """公開ソースの文字表を行のまま返す (無ければ skip)."""
        path = os.path.join(PUBLIC_SRC, name)
        if not os.path.isfile(path):
            self.skipTest(f"公開ソースに {name} が無い ({path})")
        with open(path, encoding="utf-8") as fh:
            return [ln for ln in fh.read().replace("\r", "").split("\n") if ln]

    def test_the_real_font_tables_are_all_23_wide(self):
        """**前提を実物で確かめる**: 文字表はどの行もぴったり 23 字 (#229).

        「1 行の幅で数え間違いを見つける」という話は、実物の文字表が
        行ごとに揃っているときしか成り立ちません。公開ソースの 3 つの表で
        確かめます (`font.txt` 72 行 / `font1.txt` 46 行 / `font2.txt` 26 行)。
        """
        import boku2

        want = {"font.txt": (72, boku2.FONT_GLYPHS), "font1.txt": (46, 1058), "font2.txt": (26, 598)}
        for name, (rows, chars) in want.items():
            lines = self._public_table_lines(name)
            self.assertEqual(len(lines), rows, f"{name} が {len(lines)} 行 (前提が崩れた)")
            odd = [(i + 1, len(ln)) for i, ln in enumerate(lines) if len(ln) != boku2.FONT_COLS]
            self.assertEqual(odd, [], f"{name} に {boku2.FONT_COLS} 字でない行がある: {odd}")
            self.assertEqual(sum(len(ln) for ln in lines), chars, f"{name} の字数が合わない")
            # **無事な表には何も言わないこと** (毎回出たら誰も読まなくなる)
            self.assertEqual(boku2.glyph_table_trouble("\n".join(lines) + "\n"), [],
                             f"そのままの {name} に文句を言っている")

    def test_a_miscounted_row_says_which_row_and_from_which_number(self):
        """**どの行でいくつずれて、何番から先が狂うか**まで言うこと (#229).

        文字表を手で書き写して 1 行だけ 22 字にすると、そこから下の番号が
        全部ずれます。**ずれても全部の番号に字は当たる**ので、`text` は
        「文字表で全部読めました」と言い、TSV は日本語のまま出てきます。
        目で見つけられない事故なので、行の幅で指すしかありません。
        """
        import boku2

        lines = self._public_table_lines("font.txt")
        for short, mark in ((True, "-1"), (False, "+1")):
            bad = list(lines)
            bad[10] = bad[10][:-1] if short else bad[10] + bad[10][0]
            notes = boku2.glyph_table_trouble("\n".join(bad) + "\n")
            self.assertTrue(notes, "1 行だけ幅が違うのに黙っている")
            joined = "\n".join(notes)
            self.assertIn("11 行目", joined, f"何行目かを言っていない: {joined}")
            self.assertIn(mark, joined, f"いくつずれたかを言っていない: {joined}")
            # その行の先頭の文字番号 = 23 × 10。ここから下が全部ずれる
            self.assertIn(str(boku2.FONT_COLS * 10), joined,
                          f"何番から下がずれるかを言っていない: {joined}")
            self.assertTrue(any(n.lstrip().startswith("直し方:") for n in notes),
                            f"直し方を言っていない: {joined}")
            # **まだ書き終えていない途中**と取り違えないこと。11 行目は途中ではない
            self.assertNotIn("72 行目", joined, f"関係の無い行を指している: {joined}")

    def test_a_whole_skipped_row_is_caught_by_the_count(self):
        """行を 1 つ**丸ごと飛ばす**と、幅は全部 23 のままになる (#229).

        この形は幅では絶対に見つかりません。字数が 23 の倍数ぶん足りないことで
        気づきます。逆に同じ行を二度書いた形も、23 の倍数ぶん多くなります。
        """
        import boku2

        lines = self._public_table_lines("font.txt")
        skipped = [ln for i, ln in enumerate(lines) if i != 30]
        notes = "\n".join(boku2.glyph_table_trouble("\n".join(skipped) + "\n"))
        self.assertIn(str(boku2.FONT_GLYPHS - boku2.FONT_COLS), notes,
                      f"字数を言っていない: {notes}")
        self.assertIn("飛ばした", notes, f"行を飛ばした疑いだと言っていない: {notes}")

        twice = lines[:30] + [lines[30]] + lines[30:]
        notes = "\n".join(boku2.glyph_table_trouble("\n".join(twice) + "\n"))
        self.assertIn("二度書いた", notes, f"二度書いた疑いだと言っていない: {notes}")
        self.assertIn("多いです", notes, f"多い側だと言っていない: {notes}")

    def test_a_table_still_being_written_is_left_alone(self):
        """**途中経過には文句を言わない** (#229).

        手順 3 は上から書き写していく作業なので、最後の行が短いのも、
        まだ 1 枚目の途中なのも普通です。ここで毎回鳴らすと読まれなくなります。
        """
        import boku2

        lines = self._public_table_lines("font.txt")
        # 1. 最後の行だけ短い (書きかけ)
        half = lines[:40] + [lines[40][:7]]
        self.assertEqual(boku2.glyph_table_trouble("\n".join(half) + "\n"), [],
                         "書きかけの表に文句を言っている")
        # 2. 1 枚目だけ書き終えた (1058 字)。まだ 2 枚目が残っているのは正常
        page1 = self._public_table_lines("font1.txt")
        self.assertEqual(boku2.glyph_table_trouble("\n".join(page1) + "\n"), [],
                         "1 枚目だけの表に文句を言っている")
        # 3. 改行せず 1 行に流し込んだ書き方では、幅で見られない (黙る)
        self.assertEqual(boku2.glyph_table_trouble("".join(lines)), [],
                         "1 行に流し込んだ表に、幅の話をしている")
        # 4. 「12=あ」の対応表は行の幅に意味が無い
        pairs = "\n".join(f"{i}={c}" for i, c in enumerate("あいうえおかきくけこ"))
        self.assertEqual(boku2.glyph_table_trouble(pairs), [],
                         "対応表の書き方に、幅の話をしている")

    def test_check_really_says_both_font_table_warnings(self):
        """**呼び出し側まで見る** (#235).

        文字表の警告は 2 つ (ANSI で潰れた / 書き写しが 1 行ずれた)。どちらも
        判定の関数は画面と一括処理で 1 字まで突き合わせているのに、
        **その関数に何を渡しているか**を見ている検査がありませんでした。
        #229 で足した検査も `boku2.py` の字面に呼び出しがあるかを見るだけで、
        渡す引数を取り違えても落ちません。ここは実際に `check` を走らせて、
        壊した文字表で 2 つとも出ることと、締めが「問題なし」にならないことを見ます。
        """
        import io
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            table = os.path.join(folder, "font.txt")
            with open(table, encoding="utf-8") as fh:
                rows = [ln for ln in fh.read().replace("\r", "").split("\n") if ln]
            self.assertGreater(len(rows), 3, "材料が弱い: 練習データの文字表が短すぎる")

            # まず無事な文字表では何も言わないこと (毎回出たら誰も読まない)
            out = io.StringIO()
            rc = boku2.check(folder, out=out)
            clean = out.getvalue()
            self.assertEqual(rc, 0, f"無事な練習データが問題ありになった:\n{clean[-400:]}")
            for word in ("ANSI", "行目だけ"):
                self.assertFalse(word in clean,
                                 f"無事な文字表に「{word}」と言っている:\n{clean[-400:]}")

            # 2 通りまとめて壊す: 2 行目を 1 字減らす + 半角 ? を 2 つ混ぜる
            broken = list(rows)
            broken[1] = broken[1][:-1]
            broken[-1] = broken[-1] + "??"
            with open(table, "w", encoding="utf-8") as fh:
                fh.write("\n".join(broken) + "\n")

            out = io.StringIO()
            rc = boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertTrue("ANSI" in got, f"ANSI で潰れた疑いを言っていない:\n{got[-600:]}")
            self.assertTrue("2 行目だけ" in got,
                            f"書き写しのずれを言っていない:\n{got[-600:]}")
            self.assertEqual(rc, 1, f"2 通り壊れているのに問題なしで終わった:\n{got[-600:]}")
            self.assertFalse("== 結果: 問題なし" in got,
                             f"締めが「問題なし」になっている:\n{got[-600:]}")

    def test_the_two_sides_judge_the_table_the_same_way(self):
        """**画面と一括処理が、同じ表に同じことを言う** (#229).

        文字表は画面 (「.msg として読む」の欄) でも一括処理でも使います。
        片側にだけ判定を足すと、画面で通ったものが CLI で鳴る (逆も) ので、
        `app.js` の `glyphTableTrouble` を実際に node で走らせて、
        `boku2.py` の `glyph_table_trouble` と 1 字まで突き合わせます。
        """
        import json
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node が無い")
        import boku2

        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        i = app.find("function glyphTableTrouble(")
        self.assertGreater(i, 0, "app.js に glyphTableTrouble が無い")
        j = app.index("\n}\n", i) + 3
        consts = re.findall(r"^const (?:FONT_COLS|FONT_GLYPHS) = \d+;", app, re.M)
        self.assertEqual(len(consts), 2, f"app.js から定数を {len(consts)} 件しか拾えない")

        lines = self._public_table_lines("font.txt")
        def joined(ls):
            return "\n".join(ls) + "\n"
        blank = list(lines)
        blank.insert(5, "")
        blank[11] = blank[11][:-1]                  # 空行をまたいで数える形
        cases = [
            joined(lines),                                                  # 無事
            joined(lines[:10] + [lines[10][:-1]] + lines[11:]),             # 1 字少ない行
            joined(lines[:10] + [lines[10] + "X"] + lines[11:]),            # 1 字多い行
            joined([ln for k, ln in enumerate(lines) if k != 30]),          # 行を飛ばした
            joined(lines[:30] + [lines[30]] + lines[30:]),                  # 行を二度書いた
            joined(blank),
            joined(lines[:40] + [lines[40][:7]]),                           # 書きかけ
            "".join(lines),                                                 # 1 行に流し込んだ
            "\n".join(f"{k}={c}" for k, c in enumerate("あいうえお")),      # 対応表
        ]
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "parity.mjs")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("\n".join(consts) + "\n" + app[i:j]
                         + "\nconsole.log(JSON.stringify("
                         "JSON.parse(process.argv[2]).map(glyphTableTrouble)));\n")
            res = subprocess.run([node, script, json.dumps(cases)],
                                 capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        got = json.loads(res.stdout)
        want = [boku2.glyph_table_trouble(c) for c in cases]
        # 拾えていること自体を先に確かめる (空どうしは必ず一致する)
        self.assertGreaterEqual(sum(1 for w in want if w), 5,
                                "鳴るはずの材料で 1 つも鳴っていない")
        for k, (a, b) in enumerate(zip(want, got)):
            self.assertEqual(a, b, f"{k} 番目の文字表で言うことが違う:\n  CLI: {a}\n  画面: {b}")

    def test_the_slip_shows_up_where_the_table_is_actually_used(self):
        """**取り出す前**に言うこと。`text` / `fontlist` / `check` の 3 か所 (#229).

        見つけられても、出る場所が違えば誰も読みません。文字表を渡す命令は
        `text` (取り出し) と `fontlist` (校正の一覧作り) の 2 つ、それに
        吸い出しフォルダを診る `check` があります。
        """
        import boku2

        lines = self._public_table_lines("font.txt")
        bad = list(lines)
        bad[10] = bad[10][:-1]
        with tempfile.TemporaryDirectory() as tmp:
            table = os.path.join(tmp, "font.txt")
            with open(table, "w", encoding="utf-8") as fh:
                fh.write("\n".join(bad) + "\n")
            for cmd in (["text", tmp, "-f", table, "-o", os.path.join(tmp, "o.tsv")],
                        ["fontlist", table, "-o", os.path.join(tmp, "o.txt")]):
                res = self.run(os.path.join(REPO, "tools", "boku2.py"), *cmd)
                out = res.stdout + res.stderr
                self.assertIn("11 行目", out, f"{cmd[0]} が書き写しのずれを言っていない:\n{out}")
        # `check` からも出ること。**書いてあるか**ではなく、**出るか**で見る (#235)

    def test_where_the_names_stopped_is_pointed_at(self):
        """**名前の読み取りが止まった所を指すこと** (#202).

        索引の名前は 0 終わりで並んでいます。読み手は「使えない字が出たら、
        そこから先は名前の置き場ではない」と見て止まります —— ごみを名前として
        並べないための用心で、これ自体は正しい。問題は**止まった所を言わない**
        ことでした。名前に空白が 1 つ混じっただけで、**そこから先の名前が全部**
        番号 (`#6` `#7` …) になるのに、案内は

            名前の置き場 (上の 0x…) 付近の 64 バイトを報告してください

        で、指しているのは**名前の置き場の先頭**。原因は途中の 1 バイトなので、
        社長は関係の無い所を見ることになります。

        **社長の実物はまさに名前が付かない吸い出しでした** (docs/09 の #1・#3)。
        この道は実際に通っています。
        """
        import io
        import boku2

        n = 30
        spoiled = 5                        # 6 個目の名前に空白を 1 つ
        names = [f"f{i:03d}.msg" if i != spoiled else "f005 x.msg" for i in range(n)]
        codes = [1, 2, 3, 0x8000]
        good = (struct.pack("<I", 1) + struct.pack("<I", 12) + b"\0" * 4
                + struct.pack(f"<{len(codes)}H", *codes))
        data, recs = bytearray(), []
        for _i in range(n):
            recs.append((len(data) // 2048, len(good)))
            data += good + bytes(2048 - len(good))
        idx = bytearray(b"DFI\0" + struct.pack("<III", n, 0, 0))
        for sector, ln in recs:
            idx += struct.pack("<HHIII", 0, 0, 0, sector, ln)
        for name in names:
            idx += name.encode() + b"\0"

        # 1. **前提**: 空白 1 つで、そこから先の名前が全部落ちる
        entries = boku2.read_dfi(bytes(idx), len(data))
        named = [e for e in entries if not os.path.basename(e["path"]).startswith("#")]
        self.assertEqual(len(named), spoiled,
                         f"名前が付いたのが {len(named)} 件 ({spoiled} 件のはず)")

        # 2. **止まった所を言う**
        stop = boku2.dfi_name_stop(bytes(idx))
        self.assertTrue(stop, "止まったことに気づいていない")
        self.assertEqual(stop["nth"], spoiled + 1, f"何個目かが違う: {stop}")
        self.assertEqual(stop["byte"], 0x20, f"止めた 1 バイトが違う: {stop}")
        note = boku2.dfi_name_stop_note(stop)
        self.assertTrue(f"{spoiled + 1} 個目" in note, f"何個目かを言っていない: {note}")
        self.assertTrue(f"0x{stop['at']:X}" in note, f"位置を言っていない: {note}")
        self.assertTrue("0x20" in note, f"どの字で止まったかを言っていない: {note}")

        # 3. **診断にその行が出る** (関数にあっても出さなければ意味が無い)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "BOKU2.IDX"), "wb") as fh:
                fh.write(bytes(idx))
            with open(os.path.join(tmp, "BOKU2.IMG"), "wb") as fh:
                fh.write(bytes(data))
            out = io.StringIO()
            boku2.check(tmp, out=out)
            said = out.getvalue()
            line = next((ln for ln in said.splitlines() if "個目で止まって" in ln), "")
            self.assertTrue(line, f"診断が止まった所を言っていない:\n{said[:700]}")
            self.assertTrue("0x20" in line, f"その行に 1 バイトが無い: {line}")

            # 4. **名前が全部読めているときは、余計なことを言わない**
            import make_boku2_sample
            ok = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(ok)
            with open(os.path.join(ok, "BOKU2.IDX"), "rb") as fh:
                healthy = fh.read()
            # 判定そのものを直に見る (診断の側は「名前が付かない」ときしか
            # この行に来ないので、**通らない道で「言わない」を確かめない**)
            self.assertIsNone(boku2.dfi_name_stop(healthy),
                              "全部読めている索引を、止まったと言っている")
            self.assertEqual(boku2.dfi_name_stop_note(None), "",
                             "止まっていないのに案内を出している")
            out = io.StringIO()
            boku2.check(ok, out=out)
            self.assertFalse("個目で止まって" in out.getvalue(),
                             "全部読めているのに止まったと言っている")

    def test_renaming_a_file_is_said_out_loud(self):
        """**ファイル名を変えたなら、変えたと言うこと** (#202).

        索引の名前に `:` `?` `*` や空白が入っていると、そのままでは Windows で
        ファイルを作れないので `_` に変えています。変えること自体は正しい。
        **黙って変えていた**のが問題で、索引に出ている名前と手元のファイル名が
        食い違います。20 個のうち 3 個だけ違っていても、社長は気づけません。
        """
        import contextlib
        import io
        import boku2

        names = ["a:b.msg", "c?d.bin", "e*f.tm2", "ok.msg"]
        data, recs = bytearray(), []
        for i, _n in enumerate(names):
            body = bytes([i + 1]) * 64
            recs.append((len(data) // 2048, len(body)))
            data += body + bytes(2048 - len(body))
        idx = bytearray(b"DFI\0" + struct.pack("<III", len(names), 0, 0))
        for sector, ln in recs:
            idx += struct.pack("<HHIII", 0, 0, 0, sector, ln)
        for name in names:
            idx += name.encode() + b"\0"

        with tempfile.TemporaryDirectory() as tmp:
            ip = os.path.join(tmp, "BOKU2.IDX")
            gp = os.path.join(tmp, "BOKU2.IMG")
            with open(ip, "wb") as fh:
                fh.write(bytes(idx))
            with open(gp, "wb") as fh:
                fh.write(bytes(data))
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                boku2.unpack(ip, gp, os.path.join(tmp, "OUT"))
            said = buf.getvalue()
            self.assertTrue("3 個" in said, f"変えた数を言っていない: {said!r}")
            # **変える前と後を両方言う** (後だけだと、索引の名前と突き合わせられない)
            self.assertTrue("a:b.msg" in said and "a_b.msg" in said,
                            f"変える前と後を言っていない: {said!r}")

            # 変える必要が無ければ黙る
            import make_boku2_sample
            ok = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(ok)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                boku2.unpack(os.path.join(ok, "BOKU2.IDX"),
                             os.path.join(ok, "BOKU2.IMG"), os.path.join(tmp, "O2"))
            self.assertFalse("`_` にしました" in buf.getvalue(),
                             f"変えていないのに言っている: {buf.getvalue()!r}")

    def test_a_tab_in_the_game_text_does_not_break_the_tsv(self):
        """**本文のタブや改行で、TSV の列がずれないこと** (#201).

        TSV は列をタブで、行を改行で分けます。本文にどちらかが 1 つ入るだけで
        列がずれ、`proofread` は「**列数が 7 で、見出しの 5 と違います**」で
        止まります。取り出す側は「31 行 → all.tsv」と言って終わっているので、
        社長は**校正の段になって初めて**、しかも意味の分からない言葉で知ることになる。

        絵空事ではありません。保存画面の文言は **Shift-JIS の生バイト**を読むので、
        0x09 や 0x0A がそのまま文になり得ます (ここで実際に作って確かめます)。

        前のやり方は「タブを空白に」でした。**空白にすると元に戻せません。**
        改行のほうは何もしていませんでした。
        """
        import io
        import boku2

        # 1. **保存画面の道で、本当にタブが出てくる**
        body = ("ぼく\tなつやすみ".encode("cp932") + b"\0"
                + "かわ\nあそび".encode("cp932") + b"\0"
                + "むしとり".encode("cp932") + b"\0")
        got = boku2.parse_sjis_list(body)
        self.assertTrue(got, "Shift-JIS の並びとして読めない (前提が崩れた)")
        texts = [it.get("text", "") for it in got]
        self.assertTrue(any("\t" in t for t in texts), f"タブが出てこない: {texts}")
        self.assertTrue(any("\n" in t for t in texts), f"改行が出てこない: {texts}")

        # 2. **その本文を TSV に書いて、読み直せること**
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "all.tsv")
            rows = [(f"s:{i}", i * 16, len(t) * 2, t) for i, t in enumerate(texts)]
            with open(path, "w", encoding="utf-8-sig", newline="\n") as fh:
                changed = boku2.write_tsv(rows, fh)
            self.assertEqual(changed, 2, f"書き換えた行が {changed} 行 (2 行のはず)")
            back = scrp.read_tsv(path)
            self.assertEqual(len(back), len(texts),
                             f"読み直したら {len(back)} 行 (書いたのは {len(texts)} 行)")
            # **元に戻ること。** 空白に潰すと戻らない
            self.assertEqual([r["original"] for r in back], texts,
                             "読み直した本文が元と違う (元に戻せない形で書いている)")

            # 3. **黙って変えない。** 変えたことを言う
            out = io.StringIO()
            boku2.write_tsv(rows, out)
            self.assertTrue("<TAB>" in out.getvalue() and "<LF>" in out.getvalue(),
                            "記号に置き換えていない")

        # 4. 画面側も同じ形にしていること (`<BR>` と混ぜない)
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        for tag in ("<TAB>", "<CR>", "<LF>"):
            self.assertTrue(tag in app, f"画面側の TSV が {tag} を使っていない")
        self.assertFalse('replace(/\\t/g, " ")' in app,
                         "画面側がまだタブを空白に潰している")

    def test_an_all_uppercase_dump_reads_the_same(self):
        """**名前が全部大文字の吸い出しでも、同じように読めること** (#198).

        ディスクから吸い出す道具によっては、名前が `SYSTEM/SYSTEM.MSG` のように
        全部大文字になります (ISO9660 の作法)。社長の環境がどちらかは分かりません。
        名前で拾っている所は 5 つあり (`.msg` / 入れ物の一覧 / `font` / `1.bin` /
        `0x8002` の読み方が変わる 7 ファイル)、**どれか 1 つでも大文字小文字を
        見分けていると、そこだけ黙って飛びます**。飛んだ先は「形で探す」道に
        落ちるので、**動いてはいるが診断が別物になる**のがたちが悪い。

        練習データの索引の名前だけを大文字にして、端から端まで通します。
        """
        import io
        import shutil
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            low = os.path.join(tmp, "low")
            make_boku2_sample.build_sample(low)
            up = os.path.join(tmp, "up")
            shutil.copytree(low, up)

            # 名前の置き場 (レコードの直後) から先だけを大文字にする
            path = os.path.join(up, "BOKU2.IDX")
            with open(path, "rb") as fh:
                idx = bytearray(fh.read())
            end = 16
            while end + 16 <= len(idx) and (idx[end] | (idx[end + 1] << 8)) in (0, 1):
                end += 16
            self.assertGreater(end, 16, "レコードの終わりを見つけられない")
            before = bytes(idx[end:])
            idx[end:] = before.upper()
            self.assertNotEqual(bytes(idx[end:]), before, "大文字にできていない")
            with open(path, "wb") as fh:
                fh.write(bytes(idx))

            entries = boku2.read_dfi(bytes(idx),
                                     os.path.getsize(os.path.join(up, "BOKU2.IMG")))
            self.assertTrue(any(e["path"].isupper() for e in entries),
                            "索引の名前が大文字になっていない")

            out = io.StringIO()
            rc = boku2.check(up, out=out)
            said = out.getvalue()
            self.assertEqual(rc, 0, f"大文字の吸い出しで確認事項が出た:\n{said[-600:]}")
            # **形で探す道に落ちていないこと。** 落ちても動くので、ここを見ないと分からない
            self.assertFalse("中身の形" in said,
                             f"名前で拾えず、形で探す道に落ちている:\n{said[-600:]}")
            msg_line = next((ln for ln in said.splitlines() if ln.startswith(".msg:")), "")
            self.assertTrue(".MSG" in msg_line, f"大文字の .msg を名前で拾えていない: {msg_line}")
            font_line = next((ln for ln in said.splitlines() if "[フォント]" in ln), "")
            self.assertTrue("BK_FONT.TMS" in font_line,
                            f"大文字のフォントを名前で拾えていない: {font_line}")
            box_line = next((ln for ln in said.splitlines() if "文言の入れ物" in ln), "")
            self.assertTrue("あり" in box_line and "なし" not in box_line.split("/")[0],
                            f"大文字の入れ物を名前で拾えていない: {box_line}")

            # 端まで通して、本文が小文字のときと 1 字も違わないこと
            outdir = os.path.join(tmp, "OUT")
            boku2.unpack(os.path.join(up, "BOKU2.IDX"),
                         os.path.join(up, "BOKU2.IMG"), outdir)
            # MAP の会話は入れ物を切り分けてから (docs/09 の手順 6 と同じ順)
            mapdir = os.path.join(up, "MAP")
            for name in sorted(os.listdir(mapdir)):
                boku2.split_map(os.path.join(mapdir, name),
                                os.path.join(outdir, "maps", os.path.splitext(name)[0]))
            got = {}
            for root, _dirs, files in os.walk(outdir):
                for name in files:
                    for rid, _off, _size, text in boku2.text_rows(
                            os.path.join(root, name), boku2.load_font(
                                os.path.join(up, "font.txt"))):
                        got[rid.lower()] = text
            with open(os.path.join(low, "answer.tsv"), encoding="utf-8-sig") as fh:
                import csv
                want = {r["id"].lower(): r["original"]
                        for r in csv.DictReader(fh, delimiter="\t")}
            self.assertGreaterEqual(len(want), 20, f"答えが {len(want)} 行しかない")
            missing = [k for k in want if k not in got]
            self.assertFalse(missing, f"大文字だと取り出せない行: {missing[:5]}")
            wrong = [k for k in want if got[k] != want[k]]
            self.assertFalse(wrong, f"大文字だと本文が変わる行: {wrong[:5]}")

    def test_a_number_that_can_only_be_one_value_is_not_reported_as_evidence(self):
        """**形で拾ったときの「読めた形 N 件」は、必ず全部になる** (#197).

        名前が読めない吸い出しでは、`.msg` を**中身の形**で選びます。選ぶ条件が
        「読めるか」なので、そのあとで「読めた形は何件か」を数えると、
        答えは**必ず全部**になります。ところがそれが

            先頭 50 件のうち読めた形: 50 件

        と出ていました。**確かめた結果のように読めますが、確かめていません。**
        しかも `.msg: 50 件` の 50 は**打ち切った数**で、実際はもっとあります
        (実物なら 651 件)。社長は「本文は 50 件」と読みます。

        「0 件で緑」の裏返しで、**取りうる値が 1 つしかない数**。
        """
        import io
        import boku2

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            os.makedirs(folder)
            # **打ち切りを必ず越える**数の本文を、名前が読めない索引に入れる
            total = boku2.SHAPE_PICK_LIMIT * 2 + 20
            codes = [1, 2, 3, 0x8000]
            good = (struct.pack("<I", 1) + struct.pack("<I", 12) + b"\0" * 4
                    + struct.pack(f"<{len(codes)}H", *codes))
            data, recs = bytearray(), []
            for i in range(total):
                b = good if i % 2 == 0 else bytes(2048)
                while len(data) % 2048:
                    data += b"\0"
                recs.append((len(data) // 2048, len(b)))
                data += b
            idx = bytearray(b"DFI\0" + struct.pack("<III", total, 0, 0))
            for sector, ln in recs:
                idx += struct.pack("<HHIII", 0, 0, 0, sector, ln)
            for i in range(total):
                idx += f"\xff\xff{i:04d}".encode("latin-1") + b"\0"
            with open(os.path.join(folder, "BOKU2.IDX"), "wb") as fh:
                fh.write(bytes(idx))
            with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
                fh.write(bytes(data))

            out = io.StringIO()
            boku2.check(folder, out=out)
            said = out.getvalue()

            # 1. **打ち切ったなら「以上」と言う。** 数だけ出さない
            head = next((ln for ln in said.splitlines() if ln.startswith(".msg:")), "")
            self.assertTrue(head, f".msg の行が無い:\n{said[:400]}")
            self.assertTrue("件以上" in head and "打ち切り" in head,
                            f"打ち切った数をあった数のように出している: {head}")

            # 2. **必ず全部になる数を、確かめた結果として出さない**
            tautology = [ln for ln in said.splitlines() if "のうち読めた形" in ln]
            self.assertFalse(tautology,
                             f"形で拾ったのに「読めた形 N 件」を出している: {tautology}")
            self.assertTrue("読めたから選んだ" in said,
                            f"選び方そのものが答えだと言っていない:\n{said[:600]}")

            # 3. 残りは「診ていない段」に数える
            line = next((ln for ln in said.splitlines() if ln.startswith("診ていない段:")), "")
            self.assertTrue("本文の残り" in line, f"打ち切った残りを数えていない: {line}")

            # 4. **名前が読めるときは、今までどおり数で出す** (数えた意味がある)
            import make_boku2_sample
            normal = os.path.join(tmp, "N")
            make_boku2_sample.build_sample(normal)
            out = io.StringIO()
            boku2.check(normal, out=out)
            self.assertTrue(any("のうち読めた形" in ln for ln in out.getvalue().splitlines()),
                            "名前が読めるのに件数を出していない")

    def test_the_shape_search_reaches_past_the_first_few_hundred(self):
        """**名前が読めないとき、401 件目から先も探すこと** (#196).

        名前で拾えなかったときの「中身の形で探す」道 (#172・#173) は、
        索引の**先頭 400 件**しか見ていませんでした。実物は 1951 件あるので、
        本文やフォントが 401 件目から先にあれば**永久に見つかりません**。
        名前が読めない吸い出しは、まさにこの道しか無いのに。

        数えたら、全件の先頭を読んでも 32 MB / 0.03 秒でした。
        #193〜#195 と同じ「小さいときだけ正しい」型で、今回は**上限の届く範囲**。
        """
        import io
        import boku2

        self.assertGreaterEqual(boku2.SHAPE_HUNT_FILES, 1951,
                                "実物の 1951 件に届かない上限です")

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            os.makedirs(folder)
            # **本文を 500 件目に 1 つだけ置く** (昔の上限 400 より後ろ)
            hidden_at = 500
            total = 700
            # 「件数 + 位置表 (8 バイト刻み)」の最小形。1 件だけ入れる
            codes = [1, 2, 3, 0x8000]
            body = (struct.pack("<I", 1) + struct.pack("<I", 12) + b"\0" * 4
                    + struct.pack(f"<{len(codes)}H", *codes))
            data, recs = bytearray(), []
            for i in range(total):
                b = body if i == hidden_at else bytes(2048)
                while len(data) % 2048:
                    data += b"\0"
                recs.append((len(data) // 2048, len(b)))
                data += b
            idx = bytearray(b"DFI\0" + struct.pack("<III", total, 0, 0))
            for sector, ln in recs:
                idx += struct.pack("<HHIII", 0, 0, 0, sector, ln)
            for i in range(total):
                idx += f"\xff\xff{i:04d}".encode("latin-1") + b"\0"   # 名前は読めない形
            with open(os.path.join(folder, "BOKU2.IDX"), "wb") as fh:
                fh.write(bytes(idx))
            with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
                fh.write(bytes(data))

            with open(os.path.join(folder, "BOKU2.IMG"), "rb") as img:
                entries = boku2.read_dfi(bytes(idx), len(data))
                self.assertEqual(len(entries), total, "索引を読めていない")
                got = boku2.pick_by_shape(img, entries,
                                          lambda b: bool(boku2.pick_msg(b, {})))
            self.assertTrue(got, "500 件目に置いた本文を、形で探しても見つけられない")

            # **探した件数を、探した分だけ言うこと。** 本文が 1 つも無い吸い出しを
            # 別に作る (上の材料では見つかるので、その行が出ない。**0 件で緑**を防ぐ)
            empty = os.path.join(tmp, "E")
            os.makedirs(empty)
            data = bytes(2048 * total)
            idx = bytearray(b"DFI\0" + struct.pack("<III", total, 0, 0))
            for i in range(total):
                idx += struct.pack("<HHIII", 0, 0, 0, i, 2048)
            for i in range(total):
                idx += f"\xff\xff{i:04d}".encode("latin-1") + b"\0"
            with open(os.path.join(empty, "BOKU2.IDX"), "wb") as fh:
                fh.write(bytes(idx))
            with open(os.path.join(empty, "BOKU2.IMG"), "wb") as fh:
                fh.write(data)
            out = io.StringIO()
            boku2.check(empty, out=out)
            line = next((ln for ln in out.getvalue().splitlines()
                         if "個まで探しました" in ln), "")
            self.assertTrue(line, "本文が 1 つも無いのに、探した件数を言っていない")
            self.assertTrue(f"{min(total, boku2.SHAPE_HUNT_FILES)} 個まで探しました" in line,
                            f"探した件数が実際と違う: {line}")

    def test_only_a_handful_of_msg_files_is_not_a_clean_bill(self):
        """**開いていない `.msg` を、診ていない段に数えること** (#195).

        `check` が中身まで開く `.msg` は、長らく **先頭 50 件**でした。練習データの
        `.msg` は 4 件なので全部入り、何も困りません。ところが実物は **651 件**あり、
        **601 件は触れてもいないのに**、出るのは「先頭 50 件のうち読めた形: 50 件」の
        1 行と、締めの「問題なし」だけでした。#193・#194 と同じ
        「**小さいときだけ正しい**」型です。

        651 件を全部開いても 0.04 秒しか変わらなかったので上限を上げました。
        それでも上限は残す (壊れた吸い出しで何万件になっても止まらないように)。
        **上限に当たったら数える**ので、黙って減ることはありません。
        """
        import io
        import boku2

        self.assertGreaterEqual(boku2.MSG_CHECK_FILES, 651,
                                "実物の .msg (651 件) を診ない上限になっています")

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            os.makedirs(folder)
            n = boku2.MSG_CHECK_FILES + 30       # **上限を必ず越える**件数にする
            data, recs = bytearray(), []
            for i in range(n):
                recs.append(len(data) // 2048)
                data += bytes(2048)
            idx = bytearray(b"DFI\0" + struct.pack("<III", n, 0, 0))
            for sector in recs:
                idx += struct.pack("<HHIII", 0, 0, 0, sector, 2048)
            for i in range(n):
                idx += f"f{i:05d}.msg".encode() + b"\0"
            with open(os.path.join(folder, "BOKU2.IDX"), "wb") as fh:
                fh.write(bytes(idx))
            with open(os.path.join(folder, "BOKU2.IMG"), "wb") as fh:
                fh.write(bytes(data))

            out = io.StringIO()
            boku2.check(folder, out=out)
            got = out.getvalue()
            line = next((ln for ln in got.splitlines() if ln.startswith("診ていない段:")), "")
            self.assertTrue(line, f"診ていない段の行が無い:\n{got[-400:]}")
            # **その行の中に**本文の件数があること (全文から探すと別の行で通る)
            self.assertTrue("本文" in line and str(n - boku2.MSG_CHECK_FILES) in line,
                            f"開いていない .msg を数えていない: {line}")
            # 上限に当たっていないときは、余計なことを言わない
            out = io.StringIO()
            import make_boku2_sample
            small = os.path.join(tmp, "small")
            make_boku2_sample.build_sample(small)
            boku2.check(small, out=out)
            line = next((ln for ln in out.getvalue().splitlines()
                         if ln.startswith("診ていない段:")), "")
            self.assertFalse("本文" in line, f"全部診たのに数えている: {line}")

    def test_what_was_not_checked_is_counted(self):
        """**診ていない段**を数えて、報告に必ず出すこと (#175).

        #174 で MAP について 1 件だけ直しましたが、同じ形はほかにもあります。
        `→` が 1 本も出なければ「問題なし」と言う —— それは「全部を診た結果」では
        なく「**診た分には**問題が無かった」でしかありません。社長は前者の意味で
        読みます。何を診ていないかは、毎回数えて出す形にしました。

        練習データは全部そろっているので、**わざと段を欠けさせて**数が動くことを
        見ます。数えているだけで出していない、という素通しを防ぐため、
        **欠けさせる前は 0 件であること**も一緒に確かめます。
        """
        import re
        import shutil
        import struct
        import tempfile

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        def skipped_of(folder: str) -> list[str]:
            out = self.check(folder).stdout
            line = next((l for l in out.splitlines() if l.startswith("診ていない段:")), None)
            if line is None:
                return []
            got = re.search(r"(\d+) 件 \((.+)\)$", line)
            self.assertTrue(got, f"数と中身を言っていない: {line}")
            names = got.group(2).split(", ")
            self.assertEqual(int(got.group(1)), len(names),
                             f"数と並びが合っていない: {line}")
            return names

        # 1. そろっているときは 0 件 (要らない心配をさせない)
        self.assertEqual(skipped_of(self.folder), [],
                         "全部そろっているのに「診ていない段」が出ている")

        with tempfile.TemporaryDirectory() as tmp:
            # 2. font.txt が無ければ、文字表の段が数に入る
            nofont = os.path.join(tmp, "nofont")
            shutil.copytree(self.folder, nofont)
            os.remove(os.path.join(nofont, "font.txt"))
            names = skipped_of(nofont)
            self.assertTrue(any("文字表" in n for n in names),
                            f"font.txt が無いのに文字表を数えていない: {names}")

            # 3. 入れ子の無い索引なら、フォルダの閉じ方も数に入る。
            #    **2 つ同時に欠けさせて、数が 2 になること**まで見る
            #    (1 件ずつしか試さないと、足し方が壊れていても気づけない)
            flat = os.path.join(tmp, "flat")
            shutil.copytree(self.folder, flat)
            os.remove(os.path.join(flat, "font.txt"))
            idx_path = os.path.join(flat, "BOKU2.IDX")
            with open(idx_path, "rb") as fh:
                idx = bytearray(fh.read())
            # フォルダの印 (先頭 u16 の is_dir) を全部落として、入れ子を無くす
            at = 16
            while at + 16 <= len(idx) and (idx[at] | (idx[at + 1] << 8)) in (0, 1):
                struct.pack_into("<H", idx, at, 0)
                at += 16
            with open(idx_path, "wb") as fh:
                fh.write(bytes(idx))
            names = skipped_of(flat)
            self.assertGreaterEqual(len(names), 2,
                                    f"2 つ欠けさせたのに {len(names)} 件しか数えていない: {names}")
            self.assertTrue(any("フォルダ" in n for n in names),
                            f"フォルダの閉じ方を数えていない: {names}")
            # 画面側にも同じ言葉があること (片側にだけ足して忘れない)。
            # **`skipped.push(…)` の中だけ**を見る。ファイル全体を探すと、
            # 同じ言い回しを書いた注釈で通ってしまう (#63 の「説明文を拾う」と同じ)
            with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
                ui = fh.read()
            pushed = re.findall(r'skipped\.push\("([^"]+)"\)', ui)
            self.assertGreaterEqual(len(pushed), 2,
                                    f"画面側が {len(pushed)} 件しか数えていない: {pushed}")
            for n in names:
                head = n.split(" (")[0]
                self.assertTrue(any(p.startswith(head) for p in pushed),
                                f"画面側が「{head}」を数えていない: {pushed}")
        del boku2

    def test_a_missing_map_is_not_called_a_clean_bill(self):
        """MAP が無いのに「問題なし」で終わらせないこと (#174).

        `check` は `MAP/` を **指定フォルダの直下だけ** で探していました。
        吸い出しに包みのフォルダが 1 つ増えるだけ (`吸い出し/DATA/MAP`) で
        「MAP/: 無い」となり、**物語の会話をまるごと診ないまま**

            == 結果: 問題なし。docs/10 の手順へ

        と言って終了コード 0 を返していました。社長はこれを「診た結果、大丈夫」と
        読みます。この学習の目的そのもの (物語の本文) を、いちばん静かに落とす形でした。

        見るのは 2 つ: **少し下まで探すこと**と、**それでも無ければ言うこと**。
        """
        import re
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # 1. 1 段深い所にある MAP を見つけること
            nested = os.path.join(tmp, "nested")
            os.makedirs(nested)
            for n in ("BOKU2.IDX", "BOKU2.IMG"):
                shutil.copy(os.path.join(self.folder, n), nested)
            shutil.copytree(os.path.join(self.folder, "MAP"),
                            os.path.join(nested, "DATA", "MAP"))
            res = self.check(nested)
            self.assertIn("MAP/: あり", res.stdout, f"1 段下の MAP を見つけていない:\n{res.stdout}")
            self.assertIn("DATA/MAP", res.stdout, "どこで見つけたかを言っていない")
            # 会話まで診ていること (「あり」と言うだけで中を見ていないのが最悪)
            talk = next(l for l in res.stdout.splitlines() if l.startswith("[MAP]"))
            got = re.search(r"会話 ([\d,]+) 行", talk)
            self.assertTrue(got, f"会話の行数を言っていない: {talk}")
            self.assertGreater(int(got.group(1).replace(",", "")), 0,
                               f"MAP は見つけたのに会話を 1 行も読めていない: {talk}")
            self.assertEqual(res.returncode, 0, f"見つかったのに赤くなっている:\n{res.stdout}")

            # 2. 本当に無ければ、「問題なし」で終わらせないこと
            bare = os.path.join(tmp, "bare")
            os.makedirs(bare)
            for n in ("BOKU2.IDX", "BOKU2.IMG"):
                shutil.copy(os.path.join(self.folder, n), bare)
            res = self.check(bare)
            self.assertNotIn("問題なし", res.stdout,
                             f"会話を診ていないのに問題なしと言っている:\n{res.stdout[-400:]}")
            self.assertEqual(res.returncode, 1, "終了コードが 0 のまま")
            self.assertIn("物語の会話は 1 つも診ていません", res.stdout,
                          "何を診ていないのかを言っていない")

    def test_text_is_found_by_shape_when_names_are_gone(self):
        """名前が無くても、**本文と入れ物**の診断が飛ばないこと (#173).

        #172 でフォントを形で拾えるようにしました。ところが同じ吸い出しで
        `.msg` と文言の入れ物は名前だけで選んだままで、報告はこうなっていました:

            .msg: 0 件 (例: )
            [入れ物] 文言の入れ物: あり なし / 見つからない diary.bin, …
            [文字表] … 文字番号を使っている行が無いので、文字表は試せていない

        **0 件と言うだけで、→ も出ない。** 社長には「この作品に本文が無い」のか
        「名前が読めていないだけ」なのか区別できません。文字表の出来具合も
        まるごと試せなくなります。

        `TestDamageDrill` の `allnames` は「中身の形」で照合しますが、それは
        **フォントの行でも通ってしまう** (#172 の言葉)。ここは 3 つの道を
        1 つずつ見ます。
        """
        import re
        import shutil
        import tempfile

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_boku2_sample
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            shutil.copytree(self.folder, folder)
            make_boku2_sample.damage(folder, "allnames")
            with open(os.path.join(folder, "BOKU2.IDX"), "rb") as fh:
                idx = fh.read()
            entries = boku2.read_dfi(idx, os.path.getsize(os.path.join(folder, "BOKU2.IMG")))
            # 前提: 名前が 1 つも残っていないこと
            self.assertEqual([e for e in entries if not e["path"].startswith("#")], [],
                             "名前が残っている (この検査は形の探索を試せていない)")

            out = self.check(folder).stdout
            lines = {l.split(":")[0].strip(): l for l in out.splitlines()}
            # 1. .msg —— 0 件のままにしない
            msg_line = next(l for l in out.splitlines() if l.startswith(".msg:"))
            self.assertNotIn(".msg: 0 件", msg_line, f"本文を 1 つも拾えていない: {msg_line}")
            self.assertIn("中身の形", msg_line, f"形で拾ったと言っていない: {msg_line}")
            # 2. 入れ物 —— 「あり なし」で終わらせない
            box_line = next(l for l in out.splitlines() if l.startswith("[入れ物]"))
            self.assertNotIn("あり なし", box_line, f"日本語として壊れている: {box_line}")
            self.assertIn("中身の形", box_line, f"形で探したと言っていない: {box_line}")
            # **「0 件」でも言葉は出る**ので、数も見る (これが無いと探索を外しても緑)
            got = re.search(r"で探すと (\d+) 件", box_line)
            self.assertTrue(got, f"探した件数を言っていない: {box_line}")
            self.assertGreater(int(got.group(1)), 0, f"入れ物を 1 つも拾えていない: {box_line}")
            # 3. 文字表 —— 本文が拾えたので、出来具合まで進むこと
            table_line = next(l for l in out.splitlines() if l.startswith("[文字表]"))
            self.assertNotIn("試せていない", table_line,
                             f"本文を拾えたのに文字表を試していない: {table_line}")
            del lines

    def test_nothing_font_like_is_said_not_skipped(self):
        """フォントらしい画像が 1 つも無ければ、**黙らずに言う** (#172)."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        import io as _io

        # 名前に font が無く、形も合わない吸い出し。**中身が空では検査にならない** ——
        # TIM2 が 1 つも無ければ、幅の見極めをどう緩めても結果が変わらない (#160 と同じ)。
        # 本物の TIM2 を、**1 行 23 字にならない幅**で置く (この作品の 1024 幅の画像と同じ形)
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import make_tim2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        wide, _ = make_tim2.font_sheet(rows=4, cols=boku2.FONT_COLS * 2, cell=boku2.FONT_CELL)
        self.assertNotEqual(
            (boku2.FONT_COLS * 2 * boku2.FONT_CELL) // boku2.FONT_CELL, boku2.FONT_COLS,
            "この幅でも 1 行 23 字になってしまう (検査にならない)")
        entries = [{"path": "#0", "at": 0, "len": len(wide)}]
        blob = wide
        # 前提: 中身は**ちゃんと TIM2 として読める**こと。読めなければ形の判定に届かない
        self.assertEqual(len(boku2.tim2_pages(blob)), 1, "置いた TIM2 が読めていない")
        fonts, by_shape = boku2.pick_fonts(_io.BytesIO(blob), entries)
        self.assertEqual(fonts, [],
                         "1 行 23 字にならない画像をフォントとして拾っている")
        self.assertTrue(by_shape, "名前で拾えなかったのに、形で探していない")
        # 見つからなかったことを言う言葉が、両側にあること
        for path in (os.path.join(REPO, "tools", "boku2.py"),
                     os.path.join(REPO, "web", "app.js")):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertTrue("フォント画像が見つかりません" in src,
                            f"{os.path.basename(path)} に、見つからないときの言葉が無い")

    def test_enough_cells_is_said_plainly(self):
        """マスが足りているときは、**足りていると言う** (#170).

        「足りない」側だけを見ていると、足りたときに何も出ない —— 社長は
        「言われないということは大丈夫なのか、見ていないのか」が分からない。
        練習データでは足りないので、**道具の判定そのもの**を直接呼んで確かめる。
        """
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        # 実物の頁 2 枚ぶん (512×1024 と 512×640) で、文字表はまかなえるか
        real = [(512, 1024), (512, 640)]
        cells = sum((w // boku2.FONT_CELL) * (h // boku2.FONT_CELL) for w, h in real)
        self.assertGreaterEqual(cells, boku2.FONT_GLYPHS,
                                f"実物の 2 枚で {cells} マス。文字表 {boku2.FONT_GLYPHS} 字に"
                                "届かない (頁の読みが間違っている)")
        # 画面側も同じ言葉を持っていること (片側にだけ足して忘れない)
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            ui = fh.read()
        with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
            cli = fh.read()
        for src, name in ((ui, "web/app.js"), (cli, "tools/boku2.py")):
            self.assertTrue("これで文字表はまかなえます" in src,
                            f"{name} に、足りているときの言葉が無い")

    def test_the_real_font_page_holds_only_part_of_the_table(self):
        """実物の頁の大きさで、足りない字数が公開ソースと合うこと (#167).

        512×1024 ドットの頁は 1058 マス。1656 - 1058 = 598 で、公開ソースの
        `font2.txt` の字数とちょうど一致します。**この一致が、文字表が 2 枚に
        分かれているという読みの裏付け**なので、数字が動いたら気づけるようにする。
        """
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        page1 = (512 // boku2.FONT_CELL) * (1024 // boku2.FONT_CELL)
        self.assertEqual(page1, 1058, "実物の頁のマス数が変わった")
        self.assertEqual(boku2.FONT_GLYPHS - page1, 598,
                         "足りない字数が、公開ソースの font2.txt の 598 字と合わない")
        # 公開ソースが手元にあるなら、その 598 を**数え直して**確かめる
        if os.path.isdir(PUBLIC_SRC):
            for name, want in (("font.txt", boku2.FONT_GLYPHS), ("font2.txt", 598)):
                path = os.path.join(PUBLIC_SRC, name)
                if not os.path.isfile(path):
                    continue
                with open(path, encoding="utf-8") as fh:
                    got = len(fh.read().replace("\n", ""))
                self.assertEqual(got, want, f"公開ソースの {name} が {got} 字 (前提は {want})")

    def test_check_hunts_for_the_missing_font_page(self):
        """「別の画像にある」で終わらせず、**探して挙げる**こと (#168).

        #167 で「1 枚では足りない」とは言えるようになりましたが、**どこを見れば
        いいかは言えていませんでした**。社長は 1951 個のどれかは分かりません。

        ここでは 3 つの結末を全部見ます。**「見つからない」だけを見て緑にすると、
        探す処理が壊れていても気づけません** (#134 と同じ形)。
        """
        import io as _io
        import struct

        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_tim2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))

        def page(rows: int) -> bytes:
            """1 行 23 字の頁を 1 枚。rows 行ぶん."""
            tim2, _ = make_tim2.font_sheet(rows=rows, cols=boku2.FONT_COLS, cell=boku2.FONT_CELL)
            return tim2

        first = b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * (0x80 - 8) + page(2)
        cells = 2 * boku2.FONT_COLS
        want = boku2.FONT_GLYPHS - cells
        rows_needed = -(-want // boku2.FONT_COLS)         # 残りが入る行数 (切り上げ)

        # --- 1. 同じファイルの後ろに 2 枚目がある ---
        blob = first + page(3)
        img = _io.BytesIO(blob)
        entry = {"path": "system/bk_font.tms", "at": 0, "len": len(blob)}
        lines = boku2.font_page_hunt(img, [entry], entry, cells, {0x80})
        joined = "\n".join(lines)
        self.assertIn("同じファイルの位置", joined, f"同じファイルの 2 枚目を挙げていない:\n{joined}")
        self.assertIn(f"0x{len(first):X}", joined, "2 枚目の位置を言っていない")

        # --- 2. 別のファイルに、残りが入る大きさの頁がある ---
        other = page(rows_needed)
        img = _io.BytesIO(first + b"\0" * 16 + other)
        font_e = {"path": "system/bk_font.tms", "at": 0, "len": len(first)}
        other_e = {"path": "system/bk_font2.tms", "at": len(first) + 16, "len": len(other)}
        lines = boku2.font_page_hunt(img, [font_e, other_e], font_e, cells, {0x80})
        joined = "\n".join(lines)
        self.assertIn("bk_font2.tms", joined, f"別ファイルの頁を挙げていない:\n{joined}")
        self.assertIn(f"{rows_needed * boku2.FONT_COLS} マス", joined, "マス数を言っていない")

        # --- 3. 何も無ければ「見つからなかった」と言う (黙らない) ---
        img = _io.BytesIO(first)
        lines = boku2.font_page_hunt(img, [font_e], font_e, cells, {0x80})
        joined = "\n".join(lines)
        self.assertIn("見つかりませんでした", joined, f"黙っている:\n{joined}")
        self.assertIn(f"{want} 字ぶん", joined, "何字ぶん探したのかを言っていない")

        # --- 4. 幅が 1 行 23 字にならない画像は、続きとして挙げない ---
        wide, _ = make_tim2.font_sheet(rows=rows_needed, cols=boku2.FONT_COLS * 2,
                                       cell=boku2.FONT_CELL)
        img = _io.BytesIO(first + b"\0" * 16 + wide)
        wide_e = {"path": "system/config.tm2", "at": len(first) + 16, "len": len(wide)}
        lines = boku2.font_page_hunt(img, [font_e, wide_e], font_e, cells, {0x80})
        joined = "\n".join(lines)
        self.assertNotIn("config.tm2", joined,
                         f"1 行 {boku2.FONT_COLS} 字にならない画像を続きとして挙げている:\n{joined}")

    def test_the_font_grid_numbers_match_the_sample_maker(self):
        """1 行の字数と刻みが、練習データを作る側と同じ数字であること."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
            import make_boku2_sample
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        self.assertEqual(make_boku2_sample.COLS, boku2.FONT_COLS)
        self.assertEqual(make_boku2_sample.CELL, boku2.FONT_CELL)

    def test_a_healthy_sample_is_still_clean(self):
        res = self.check(self.folder)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("問題なし", res.stdout)

    def test_the_threshold_matches_the_browser(self):
        """CLI の基準とブラウザ側の基準が同じ数字であること (2 か所にずれた数字を置かない)."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        self.assertTrue(f"const COVERAGE_MIN = {boku2.COVERAGE_MIN}" in app,
                        f"ブラウザ側の線が {boku2.COVERAGE_MIN} と違う")


class TestBothSidesDiagnoseTheSame(unittest.TestCase):
    """画面の「報告用の要約」と `boku2.py check` が、同じ判定をすること (#99).

    画面はその場で「一括処理なら boku2.py check が同じものを出します」と言い、
    docs/10 も同じ約束をしている。ところが #96〜#98 で **CLI にだけ**判定を 5 つ
    足していたので、**画面の側は数字を出すだけ**に戻っていた。
    どちらか片方に足すと、もう片方が黙って古くなる。両方に要ることを見張る。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
            cls.cli = fh.read()
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            cls.ui = fh.read()

    #: 両方に無いといけない判定の言葉 (#96〜#98 で足したもの)
    SHARED = (
        "しか指していません",
        "位置表の長さの欄が",
        "会話として読めたファイルが 0 件",
        "ドットに足りません",
        "ドットで足ります",           # 広すぎる側の判定 (#166)
        "残りは別の画像にあります",   # 1 枚では文字表がまかなえない (#167)
        "続きが見つかりませんでした",   # 続きを探して、無ければ無いと言う (#168)
        "1 画素 1 バイトのパレット番号",
        "索引が本体をどれだけ使い切っているか",
        "1 枚目と同じ幅",             # 続きの候補の並べ方 (#211)
    )

    #: `check` にだけあって画面に無くてよい → と、その理由。
    #: **理由の書けるものだけ**をここに置く。書けないなら片側の抜けなので直すこと
    ONLY_CLI = {
        "索引と本体が揃っていません":
            "画面は 2 つのファイルを選ばせるので、揃っていない状態そのものが作れない",
        "同じ場所に":
            "`text` / `used` にファイルを並べて渡したときの知らせ (#160/#232)。"
            "画面はファイルを 1 つずつ選ぶ作りなので、並べて渡す状態そのものが作れない。"
            "画面側の同じ穴 (1 ファイル分の数を全部の数に見せる) は、"
            "「いま開いているファイルの分だけ」と書いて塞いだ",
    }

    def test_every_judgement_exists_on_both_sides(self):
        for word in self.SHARED:
            self.assertTrue(word in self.cli, f"tools/boku2.py に無い: {word}")
            self.assertTrue(word in self.ui, f"web/app.js に無い: {word}")

    def test_the_two_sides_have_the_same_arrows(self):
        """**言葉の一覧を手で並べるのをやめる** (#179).

        上の `SHARED` は手で書いた一覧です。**足し忘れれば、片側にしかない判定が
        あっても誰も気づきません。** #178 で見張りを広げたとき、`text` と `used` の
        → が一度も docs/10 に載っていなかったのが出てきたのと同じ穴が、
        ここにも空いていました。

        そこで `check` が出し得る → を**両側から機械で集めて**突き合わせます。
        `check` 以外の命令 (`unpack` / `maps` / `text` / `used`) の → は、
        画面に相当する機能が無いので数えません。
        """
        import re

        def head(h):
            return re.split(r"[。、(:]", h)[0].strip()[:14]

        i = self.cli.index("def check(folder: str, out=sys.stdout) -> int:")
        j = self.cli.index("\ndef ", i + 10)
        body = self.cli[i:j]
        cli = {head(h) for h in re.findall(r'(?:say\(|^\s+)f?"→ ([^"{]+)', body, re.M)}
        # `check` から呼ぶ助けの関数が組み立てる → も、check の → として数える
        cli |= {head(h) for h in re.findall(r'return \(f?"→ ([^"{]+)', self.cli)}
        # 一覧にして返す助けの関数 (`glyph_table_trouble`) の → も同じ (#229)。
        # 一覧にした瞬間、上の 2 つの形では拾えなくなる —— #179 と同じ穴
        cli |= {head(h) for h in re.findall(r'notes\.append\(\s*f?"→ ([^"{]+)', self.cli)}
        # 行を**その場で並べて返す**助けの関数 (`crc_report`) の → も同じ (#239)。
        # 形が 1 つ増えるたびに見張りから漏れる —— 出る言葉のほうを見る
        cli |= {head(h) for h in re.findall(r'lines\.append\(\s*f?"→ ([^"{]+)', self.cli)}
        ui = {head(h) for h in re.findall(r'lines\.push\([`"]→ ([^`"$]+)', self.ui)}
        # 知らせの文を組み立てる助けの関数 (`ansiDamageNote`) に移すと `lines.push` の
        # 形では見つからない。**出る言葉のほうを見る** (CLI 側で #178 に直したのと同じ)
        ui |= {head(h) for h in re.findall(r'return "→ ([^"]+)"', self.ui)}
        ui |= {head(h) for h in re.findall(r'return `→ ([^`$]+)', self.ui)}
        ui |= {head(h) for h in re.findall(r'notes\.push\(\s*`→ ([^`$]+)', self.ui)}

        # 拾えていること自体を先に確かめる (0 件どうしは必ず一致する)
        self.assertGreaterEqual(len(cli), 12, f"CLI の → を {len(cli)} 件しか拾えない")
        self.assertGreaterEqual(len(ui), 12, f"画面の → を {len(ui)} 件しか拾えない")

        only_cli = cli - ui - set(self.ONLY_CLI)
        only_ui = ui - cli
        self.assertEqual(sorted(only_cli), [],
                         "check にあって画面に無い判定 (片側に足して忘れた):\n  "
                         + "\n  ".join(sorted(only_cli)))
        self.assertEqual(sorted(only_ui), [],
                         "画面にあって check に無い判定:\n  " + "\n  ".join(sorted(only_ui)))
        # 逃がした分が**本当にまだ CLI にある**こと。消えたのに残しておくと、
        # そこだけ見張りの外になる
        for word in self.ONLY_CLI:
            self.assertTrue(word in cli, f"ONLY_CLI に残っているが CLI にもう無い: {word}")

    def test_the_thresholds_are_the_same_number(self):
        """基準の数字が 2 か所でずれていないこと."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        for name, value in (("COVERAGE_MIN", boku2.COVERAGE_MIN),
                            ("FONT_COLS", boku2.FONT_COLS),
                            ("FONT_CELL", boku2.FONT_CELL),
                            ("FONT_GLYPHS", boku2.FONT_GLYPHS)):
            self.assertTrue(f"const {name} = {value}" in self.ui,
                            f"web/app.js の {name} が {value} と違う")

    def test_the_pixel_kind_table_agrees(self):
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        for code, word in boku2.TIM2_PIXEL_KIND.items():
            self.assertTrue(f'{code}: "{word}"' in self.ui,
                            f"web/app.js の画素の種類 {code} が「{word}」と違う")


class TestTheLegalNoteComesFirst(unittest.TestCase):
    """docs/05 は、吸い出しの手順より先に立ち位置と法律を書くこと (#95).

    この一式で**間違えると実害が出る唯一の文書**。それなのに、ImgBurn で
    イメージ化して展開して…という手順が 180 行続いたあと、いちばん最後に
    「私的目的でも違法になり得ます」が来る形だった。**方法を教えてから警告する**
    順番になっていた。順番そのものを機械で見張る。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "docs", "05-実物のディスクを扱う場合.md"),
                  encoding="utf-8") as fh:
            cls.doc = fh.read()

    def at(self, needle: str) -> int:
        where = self.doc.find(needle)
        self.assertNotEqual(where, -1, f"docs/05 に「{needle}」が無い")
        return where

    def test_the_scope_and_law_come_before_the_ripping_steps(self):
        note = self.at("先に読む")
        law = self.at("技術的保護手段")
        rip = self.at("イメージ化")
        self.assertLess(note, rip, "立ち位置の説明が吸い出しの手順より後にある")
        self.assertLess(law, rip, "法律の話が吸い出しの手順より後にある")

    def test_it_says_the_practice_needs_no_real_disc(self):
        need = self.at("実物のソフトは要りません")
        self.assertLess(need, self.at("イメージ化"), "先に言っていない")

    def test_it_does_not_decide_the_law_for_the_reader(self):
        """断定しないこと。当てはめは事案によるので、専門家に送る."""
        self.assertIn("専門家に確認", self.doc)
        self.assertIn("法律の助言ではありません", self.doc)
        for overclaim in ("違法です", "問題ありません。", "合法です"):
            self.assertNotIn(overclaim, self.doc, f"言い切っている: {overclaim}")

    def test_it_states_the_read_only_stance(self):
        self.assertIn("読むだけ", self.doc)
        self.assertIn("10-僕夏2の手順.md", self.doc, "書き戻さない根拠への導線が無い")

    def test_the_private_copy_exception_is_stated_precisely(self):
        """「私的目的でも違法」で終わらせず、例外から外れる話として書くこと.

        私的目的の回避そのものに刑事罰は無い。そこを曖昧にすると、
        読む人は「刑務所に入る」と読むか「お咎めなし」と読むかのどちらかに振れる。
        """
        self.assertIn("30 条 1 項 2 号", self.doc, "根拠の条文が無い")
        self.assertIn("例外から外れ", self.doc)
        self.assertIn("刑事罰", self.doc, "刑事と民事の別が書かれていない")


class TestWindowsCanFollowTheDocs(unittest.TestCase):
    """文書のコマンドのうち Windows で素直に打てないものが、全部説明してあること (#94).

    社長の環境は Windows。README の「3 分で一周する」の 1 行目が `python3 …` で、
    読み替えの案内は docs/10 の中、しかも README からの導線は 100 行以上下にあった。
    **最初の 1 行で詰まる**のに、詰まったときの案内が遠い。

    加えて `| head -20`、行末の `\\` での継続、`cp`、`cmp … && echo` は
    コマンドプロンプトに無い / 別物なので、そのまま打つと止まる。
    新しくそういう書き方を足したときに気づけるよう、**使っている構文の集合**が
    README の読み替え表に載っている集合を超えないことを見る。
    """

    #: (名前, 見つける正規表現). README の読み替え表がこの名前で説明していること
    CONSTRUCTS = (
        ("head", re.compile(r"\|\s*head\b")),
        ("grep", re.compile(r"\|\s*grep\b")),
        ("行継続", re.compile(r"\\\s*$")),
        ("cp", re.compile(r"^cp\s")),
        ("cmp", re.compile(r"^cmp\s")),
    )

    #: README の読み替え表に、その構文の説明があると認める手がかり
    EXPLAINED = {
        "head": "head -20",
        "grep": "grep 同じ",
        "行継続": "1 行につなげて",
        "cp": "`cp A B`",
        "cmp": "`cmp A B && echo 一致`",
    }

    DOCS = ("README.md", "docs/01-文字テーブル.md", "docs/02-相対検索.md",
            "docs/03-ポインタテーブル.md", "docs/04-校正とQA.md",
            "docs/06-画面で確かめる.md", "docs/07-構造探査台.md",
            "docs/08-コードを読む.md", "docs/10-僕夏2の手順.md",
            "exercises/README.md")

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            cls.readme = fh.read()
        # **読み替え表の中だけ**を見る。README 全体を見ると、目印の文字列が
        # コマンドの側 (`| head -20`) にも出るので、表の行を消しても通ってしまう。
        # 実際この検査を入れた回に、表の行を消しても緑のままだった (#94)
        cls.table = "\n".join(line for line in cls.readme.split("\n")
                              if line.startswith("> |"))

    def bash_lines(self, path: str) -> list[str]:
        with open(os.path.join(REPO, path), encoding="utf-8") as fh:
            doc = fh.read()
        out, in_block = [], False
        for line in doc.split("\n"):
            if line.startswith("```"):
                in_block = "bash" in line
                continue
            if in_block and line.strip():
                out.append(line.rstrip())
        return out

    def test_every_unportable_construct_is_explained(self):
        used = {}
        for path in self.DOCS:
            for line in self.bash_lines(path):
                for name, rx in self.CONSTRUCTS:
                    if rx.search(line):
                        used.setdefault(name, []).append(f"{path}: {line.strip()[:60]}")
        for name, where in sorted(used.items()):
            self.assertTrue(self.EXPLAINED[name] in self.table,
                            f"{name} を使っているのに README の読み替え表に無い "
                            f"(例: {where[0]})")

    def test_the_note_comes_before_the_first_command(self):
        """読み替えの案内が、最初のコマンドより前にあること."""
        note = self.readme.find("Windows の方は先にここだけ")
        first = self.readme.find("python3 tools/make_sample.py")
        self.assertNotEqual(note, -1, "README に Windows の案内が無い")
        self.assertNotEqual(first, -1)
        self.assertLess(note, first, "案内が最初のコマンドより後にある")

    def test_the_python_launcher_is_named(self):
        self.assertTrue("py -3" in self.table, "python3 の読み替え先が表に無い")

    def test_the_table_is_really_there(self):
        """表そのものを取り出せていること (取り出せないと上が全部素通しになる)."""
        self.assertGreaterEqual(len(self.table.split("\n")), 6, self.table)
        self.assertIn("Windows では", self.table)

    def test_the_scan_actually_finds_something(self):
        """この検査が 0 件を見て通っていないこと (#81 と同じ形の素通し防止)."""
        found = [name for name, rx in self.CONSTRUCTS
                 for path in self.DOCS for line in self.bash_lines(path)
                 if rx.search(line)]
        self.assertGreaterEqual(len(found), 5, "走査が当たっていない")


class TestTheDocMatchesTheButtons(unittest.TestCase):
    """docs/10 が「押す」と書いているボタンが、画面に実在すること (#92).

    手順書は画面のボタン名を名指しで引用している。ボタンの文言を変えても文書は
    黙って古くなり、**書いてあるボタンが見つからない**状態になる。実物を前にした
    人がそこで止まるので、両方向で見張る (画面から消えたら落ちる / 文書から
    引用が消えても落ちる)。

    引用の確認を**ソースの grep だけ**で済ませてはいけないことも、この回に踏んだ。
    「候補 1 件 (DFI 形式として読みました)」は `${top.known} 形式として…` と
    組み立てているので、リテラルを探すと見つからず、誤って「文書が間違っている」と
    判断しかけた。組み立て式の文言は e2e で実際の出力を見る (sample が見ている)。
    """

    #: docs/10 の「1. 画面で確かめる」が名指しするボタン。画面と文書の両方に要る
    BUTTONS = (
        "フォルダごと読む",
        "索引ファイル",
        "解析する",
        "報告用の要約を作る",
        "既知の形式",
        "このファイルを .msg として読む",
        "文字の番号を重ねる",
        "文字表の下書きを作る",
        "マップの入れ物を切り分ける",
        "校正用の TSV をコピー",
        "文字表をファイルに保存",
        "TSV をファイルに保存",
    )

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            cls.app = fh.read()
        with open(os.path.join(REPO, "web", "index.html"), encoding="utf-8") as fh:
            cls.html = fh.read()
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        cls.section = doc.split("## 1. 画面で確かめる")[1].split("## 2. ")[0]
        # **押せるものが定義されている場所**だけを集める。2 つのファイルを繋いだ
        # 文字列に部分一致で当てると、片方を書き換えても**もう片方の説明文が
        # 残っているせいで通ってしまう** (実際この検査を入れた回に踏んだ)
        cls.defined = set(re.findall(r'\.textContent\s*=\s*"([^"]{2,60})"', cls.app))
        cls.defined |= set(re.findall(r"<button[^>]*>([^<]{2,60})</button>", cls.html))
        cls.defined |= set(re.findall(r'data-tab="[^"]*"[^>]*>([^<]{2,60})<', cls.html))

    def test_every_named_button_exists_in_the_page(self):
        for label in self.BUTTONS:
            self.assertTrue(label in self.defined,
                            f"画面に「{label}」というボタンが無い (docs/10 が押せと書いている)")

    def test_help_text_in_the_page_does_not_name_a_missing_button(self):
        """画面の説明文が名指しするボタンも実在すること (#92).

        index.html の説明文は「『文字表の下書きを作る』で使う」のようにボタン名を
        引用している。app.js 側の文言を変えると、**説明文だけが古い名前を指したまま**
        になる。押すものが見つからない案内は、無い案内より悪い。
        """
        for label in self.BUTTONS:
            for quoted in re.findall(r"「([^」]{4,60})」", self.html):
                if quoted == label:
                    self.assertTrue(label in self.defined,
                                    f"index.html の説明文が「{label}」を指しているが、"
                                    "そのボタンが無い")

    def test_every_named_button_is_still_quoted_in_the_doc(self):
        for label in self.BUTTONS:
            self.assertIn(label, self.section,
                          f"docs/10 の「画面で確かめる」から「{label}」の案内が消えている")

    def test_every_button_that_writes_a_file_is_in_the_list(self):
        """**ファイルを書き出す釦は、必ずこの一覧を通すこと** (#209).

        上の 3 つの検査は「一覧に載っている釦」しか見ないので、釦を足しただけでは
        何も落ちない —— 実際 #209 で「ファイルに保存」を 2 つ足したとき、
        検査は全部緑のままだった。**文書に一行も書かなくても通ってしまう。**

        全部の釦を文書必須にすると練習用のタブまで巻き込むので、ここでは
        **ファイルを書き出すもの**に絞る。押した結果が社長の手元に残る釦で、
        どんな形で落ちるか (UTF-8 か、BOM が付くか) を知らずに使うと、
        この一式がいちばん長く付き合ってきた事故 (#199/#200) がそのまま起きる。
        """
        labels = set(re.findall(r"<button[^>]*>([^<]{2,60})</button>", self.html))
        labels |= set(re.findall(r'\.textContent\s*=\s*"([^"]{2,60})"', self.app))
        writers = sorted(l for l in labels if "ファイルに保存" in l)
        self.assertTrue(writers, "「ファイルに保存」の釦が 1 つも見つからない (拾い方が壊れた)")
        for label in writers:
            self.assertIn(label, self.BUTTONS,
                          f"「{label}」は書き出す釦なのに一覧に無い "
                          "(一覧に入れると docs/10 に書いたかどうかも見ます)")

    def test_the_list_is_not_empty_or_trivially_passing(self):
        """一覧が空だったり、短すぎる文言で素通しになっていないこと (#81 と同じ形)."""
        self.assertGreaterEqual(len(self.BUTTONS), 8)
        for label in self.BUTTONS:
            self.assertGreaterEqual(len(label), 4, f"{label!r} は短すぎて偶然一致する")


class TestTheColumnsCanBeSwapped(unittest.TestCase):
    """`original` と `translation` を入れ替えて保存した TSV を見つけること (#226).

    表計算で列を並べ替えると、原文の欄に訳文、訳文の欄にゲームの原文が入ります。
    **このままでは何も鳴りません** —— 1 行ずつ見るとどちらも日本語で、
    タグの数も合うからです。実際、練習データで作って通したら
    「ERROR 0 件 / WARN 0 件 / 問題なし 31 行」で**全部通りました**。
    そのまま入れ直せば、訳文が消えて原文が戻ります。

    決め手は `size` の欄 (取り出したときの原文の大きさ)。原文の側だけが
    合うはずなので、訳文の側ばかり合うなら入れ替わっている。
    """

    @staticmethod
    def sized(lines: list) -> list:
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import boku2
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        return [str(boku2.msg_bytes(one)) for one in lines]

    def run_on(self, rows: list) -> str:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.tsv")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("id\tsize\toriginal\ttranslation\n")
                for r in rows:
                    fh.write("\t".join(r) + "\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"), path,
                 "--no-font-check"], capture_output=True, text=True, cwd=REPO)
            return res.stdout + res.stderr

    def material(self) -> tuple[list, list, list]:
        originals = [f"げんぶん{i}ばんめのせりふ。" for i in range(8)]
        translations = [f"やく{i}" for i in range(8)]
        return originals, translations, self.sized(originals)

    def test_a_swapped_file_is_reported(self):
        originals, translations, sizes = self.material()
        rows = [[f"r{i}", sizes[i], translations[i], originals[i]] for i in range(8)]
        out = self.run_on(rows)
        self.assertIn("入れ替わっている疑い", out, out[-700:])
        self.assertIn("訳文が消えて原文が戻ります", out, f"何が起きるかを言っていない:\n{out[-700:]}")

    def test_a_normal_file_is_not_accused(self):
        """**普通に訳した TSV で鳴らないこと。** ここが鳴ると誰も読まなくなる.

        材料を弱くしない: 実物の校正では、**原文と同じ長さの訳文**がいくらでもある
        (字数を合わせて訳すので)。その行は「訳文の側も size に合う」ので、
        1 行でも当たれば言う作りにすると**健全なファイルで鳴ります**。
        ここでは半分を同じ長さにして、それでも黙ることを見ます。
        """
        originals, translations, sizes = self.material()
        rows = []
        for i in range(8):
            same_len = "や" * len(originals[i])          # 字数を合わせた訳文
            rows.append([f"r{i}", sizes[i], originals[i],
                         same_len if i % 2 else translations[i]])
        out = self.run_on(rows)
        self.assertNotIn("入れ替わっている", out, f"普通の TSV で鳴っている:\n{out[-500:]}")

    def test_an_untranslated_file_is_not_accused(self):
        """取り出したまま (原文の写し) では、どちらの向きでも同じなので黙ること."""
        originals, _t, sizes = self.material()
        rows = [[f"r{i}", sizes[i], originals[i], originals[i]] for i in range(8)]
        self.assertNotIn("入れ替わっている", self.run_on(rows), "写しのままで鳴っている")

    def test_it_needs_the_size_column(self):
        """`size` の欄が無ければ**言わない** (決め手が無いので)."""
        import subprocess

        originals, translations, _s = self.material()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.tsv")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("id\toriginal\ttranslation\n")
                for i in range(8):
                    fh.write(f"r{i}\t{translations[i]}\t{originals[i]}\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"), path,
                 "--no-font-check"], capture_output=True, text=True, cwd=REPO)
        self.assertNotIn("入れ替わっている", res.stdout + res.stderr,
                         "決め手が無いのに言っている")


class TestTheIdColumnTellsOnItself(unittest.TestCase):
    """`id` の欄の事故を、**最初にかける道具**が言うこと (#225).

    取り出したままの TSV では `id` は全部違います (ファイル名 + 番号で作るため)。
    同じ id が 2 つあるのは、表計算で行を複製したときだけ。そのまま入れ直すと
    **どちらか片方が黙って消えます**。`compare_tsv.py` は前から断っていましたが
    (id で突き合わせるので気づける)、**`proofread.py` は黙っていました** ——
    社長が最初にかけるのはそちらなので、気づくのが一段遅れます。
    """

    def run_on(self, body: str) -> str:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.tsv")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("id\toriginal\ttranslation\n" + body)
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"), path,
                 "--no-font-check"], capture_output=True, text=True, cwd=REPO)
            return res.stdout + res.stderr

    def test_a_duplicated_id_is_named(self):
        out = self.run_on("r0\tあ\tア\nr1\tい\tイ\nr1\tう\tウ\n")
        self.assertIn("同じ id の行", out, out[-500:])
        self.assertIn("r1", out, f"どの id が重なったかを言っていない:\n{out[-500:]}")
        self.assertIn("片方が黙って消えます", out, f"何が起きるかを言っていない:\n{out[-500:]}")

    def test_a_blank_id_is_named(self):
        out = self.run_on("r0\tあ\tア\n\tい\tイ\n")
        self.assertIn("id の欄が空の行", out, out[-500:])

    def test_a_clean_file_is_not_accused(self):
        """**普通の TSV で鳴らないこと。** ここが鳴ると誰も読まなくなる."""
        out = self.run_on("r0\tあ\tア\nr1\tい\tイ\nr2\tう\tウ\n")
        self.assertNotIn("同じ id の行", out, "普通の TSV で鳴っている")
        self.assertNotIn("id の欄が空", out, "普通の TSV で鳴っている")

    def test_the_other_tool_still_refuses_outright(self):
        """`compare_tsv.py` は今までどおり**断る** (突き合わせが成り立たないので).

        同じ家族の事故でも、道具によって扱いが違ってよい。**理由が違う**からで、
        あちらは id で突き合わせる道具なので、重なっていたら仕事にならない。
        こちらは 1 行ずつ見る道具なので、言って先へ進める。
        """
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a.tsv")
            with open(a, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("id\toriginal\ttranslation\nr0\tあ\tア\nr0\tい\tイ\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "compare_tsv.py"), a, a],
                capture_output=True, text=True, cwd=REPO)
            out = res.stdout + res.stderr
        self.assertNotEqual(res.returncode, 0, f"重なった id を受け取ってしまう:\n{out}")
        self.assertIn("2 回出てきます", out, out[-300:])

    def test_a_gap_in_the_numbers_is_not_called_an_error(self):
        """**番号が飛んでいても事故ではない** (#225 で測って分かったこと).

        `.msg` には中身の無い項目 (null) があり、`text` はそこを飛ばして書き出します。
        つまり `holes:0 / holes:2 / holes:4` は**正しい取り出し**。
        飛びを「行が消えた」と言うと、健全なファイルで鳴ります。
        """
        out = self.run_on("h:0\tあ\tア\nh:2\tい\tイ\nh:4\tう\tウ\n")
        for word in ("欠番", "飛んで", "抜けて"):
            self.assertNotIn(word, out, f"番号の飛びを事故と呼んでいる ({word})")


class TestTheRowsCanSlipByOne(unittest.TestCase):
    """訳文が**1 行ずれている**のを見つけること (#224).

    取り出した TSV は `translation` が `original` の写しなので、表計算で行を
    1 つ挿入・削除したまま訳し始めると、以降の訳文が全部**隣の行のもの**になります。

    **1 行ずつ見るかぎり、どの行も普通に見えます。** 原文と訳文が違うのは
    訳したのだから当たり前で、`placeholder` も `number` も鳴りません。
    気づくのは実機に入れてから —— しかも直すには全行を突き合わせ直すことになる、
    いちばん高くつく事故です。
    """

    def run_on(self, rows: list, header=("id", "original", "translation")) -> str:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.tsv")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\t".join(header) + "\n")
                for r in rows:
                    fh.write("\t".join(r) + "\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"), path,
                 "--no-font-check"], capture_output=True, text=True, cwd=REPO)
            return res.stdout + res.stderr

    @staticmethod
    def lines(n: int) -> list:
        return [f"せりふ{i}ばんめです。" for i in range(n)]

    def test_a_one_row_slip_is_reported(self):
        text = self.lines(12)
        rows = [[f"r{i}", text[i], text[i - 1] if i else text[0]] for i in range(len(text))]
        out = self.run_on(rows)
        self.assertIn("1 行ずれている疑い", out, out[-700:])
        self.assertIn("すぐ上の行の原文", out, out[-700:])
        self.assertIn("r1", out, f"最初の例を言っていない:\n{out[-700:]}")

    def test_the_slip_says_where_it_starts(self):
        """**どこから始まったか**を言うこと (#228).

        実物は 1 万行を超える見込みです。「ずれています」だけでは、社長が
        目で探すことになります。表計算で直すのに要るのは
        「この id から下を 1 つ上げる」という一言なので、そこまで出す。
        途中から始まるずれ (表の真ん中でセルを挿入した形) で確かめます。
        """
        text = self.lines(20)
        rows = []
        for i in range(len(text)):
            tr = f"やくぶん{i}" if i < 8 else text[i - 1]   # r8 から下だけずれている
            rows.append([f"r{i}", text[i], tr])
        out = self.run_on(rows)
        self.assertIn("1 行ずれている疑い", out, out[-700:])
        self.assertIn("r8 から", out, f"始まりを言い当てていない:\n{out[-700:]}")
        self.assertIn("最後の行まで", out, f"どこまで続くかを言っていない:\n{out[-700:]}")
        self.assertIn("訳文の列だけを 1 つ上げる", out, f"直し方を言っていない:\n{out[-700:]}")
        # **始まりが先頭だと言わない** (途中から始まったのに r0 と言えば探す所が違う)
        self.assertNotIn("r0 から", out, f"始まりを取り違えている:\n{out[-700:]}")

    def test_a_slip_that_stops_partway_is_not_called_endless(self):
        """途中で終わるずれを「最後の行まで」と言わないこと."""
        text = self.lines(20)
        rows = []
        for i in range(len(text)):
            tr = text[i - 1] if 3 <= i < 12 else f"やくぶん{i}"
            rows.append([f"r{i}", text[i], tr])
        out = self.run_on(rows)
        self.assertIn("1 行ずれている疑い", out, out[-700:])
        self.assertNotIn("最後の行まで", out, f"途中で終わるのに最後までと言っている:\n{out[-700:]}")

    def test_a_slip_the_other_way_is_named_as_such(self):
        text = self.lines(12)
        rows = [[f"r{i}", text[i], text[i + 1] if i + 1 < len(text) else text[i]]
                for i in range(len(text))]
        out = self.run_on(rows)
        self.assertIn("1 行ずれている疑い", out, out[-700:])
        self.assertIn("すぐ下の行の原文", out, f"ずれの向きが違う:\n{out[-700:]}")

    def test_a_healthy_file_is_not_accused(self):
        """**普通に訳した TSV で鳴らないこと。** ここが鳴ると誰も読まなくなる."""
        text = self.lines(12)
        rows = [[f"r{i}", text[i], f"やくぶん{i}"] for i in range(len(text))]
        self.assertNotIn("1 行ずれている", self.run_on(rows), "普通の訳文で鳴っている")
        copy = [[f"r{i}", text[i], text[i]] for i in range(len(text))]
        self.assertNotIn("1 行ずれている", self.run_on(copy), "写しのままで鳴っている")

    def test_a_few_repeated_lines_do_not_trigger_it(self):
        """「はい」「いいえ」の繰り返しで**偶然当たる**のを、事故と呼ばないこと.

        短い定型文は同じ原文があちこちに出てくるので、隣と同じになることがある。
        少数なら黙る (数と割合の両方で見る)。
        """
        rows = [["r0", "はい", "はい"], ["r1", "いいえ", "はい"], ["r2", "はい", "いいえ"]]
        rows += [[f"r{i}", f"ふつうのせりふ{i}", f"やくぶん{i}"] for i in range(3, 30)]
        self.assertNotIn("1 行ずれている", self.run_on(rows),
                         "繰り返しの定型文を 1 行ずれと呼んでいる")


class TestTheTranslationHasToFitTheHole(unittest.TestCase):
    """訳文が**原文の入れ物に入るか**を見る検査 (#210).

    取り出した TSV の `size` は、その文がディスク上で占めていたバイト数。
    #209 まで、この欄は書き出すだけで**どの道具も読んでいなかった**。
    幅や行数は読みやすさの話だが、入るかどうかは物理の話で、外すと実機に
    入れる段まで分からない。先行事例が文字列を詰める道具を別に書いている
    くらい、この作品では効いてくる制約。

    数え方は 2 通りある (本文の 2 バイト符号 / 保存画面の Shift-JIS)。
    **原文と `size` を突き合わせれば、どちらかに決まる**というのが土台なので、
    そこを実際の取り出しで確かめる。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        import boku2

        cls.tmp = tempfile.TemporaryDirectory()
        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        out = os.path.join(cls.tmp.name, "OUT")
        boku2.main(["unpack", os.path.join(sample, "BOKU2.IDX"),
                    os.path.join(sample, "BOKU2.IMG"), out])
        boku2.main(["maps", os.path.join(sample, "MAP"), "-o", os.path.join(out, "maps")])
        cls.tsv = os.path.join(cls.tmp.name, "all.tsv")
        boku2.main(["text", out, "-f", os.path.join(sample, "font.txt"), "-o", cls.tsv])
        cls.rows = scrp.read_tsv(cls.tsv)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_extracted_row_can_be_measured(self):
        """取り出した行は、全部どちらかの数え方で `size` と合うこと.

        ここが崩れたら、入るかどうかは**判らない**のが正しい。だから
        「数えられなかった行」は黙って素通りではなく、下の検査で数えて出す。
        """
        self.assertGreaterEqual(len(self.rows), 20, "材料が少なすぎる")
        bad = [r["id"] for r in self.rows if proofread.room_of(r) is None]
        self.assertEqual(bad, [], f"size と数え方が合わない行: {bad[:5]}")

    def test_the_two_ways_of_counting_never_both_fit(self):
        """**両方に当てはまる大きさが無いこと。** ここが土台 (#210).

        両方当てはまる行があると、どちらで測るかを勝手に決めることになり、
        Shift-JIS の枠を 2 バイト符号で測って**入らないものを入ると言う**。
        """
        both = []
        for r in self.rows:
            size = int(r["size"], 0)
            glyph = 0 <= size - boku2.msg_bytes(r["original"]) < 4
            try:
                sjis = size == len(r["original"].encode("cp932"))
            except UnicodeEncodeError:
                sjis = False
            if glyph and sjis:
                both.append(r["id"])
        self.assertEqual(both, [], f"2 通りとも当てはまる行がある: {both[:5]}")
        # **0 件で緑にしない。** 両方の数え方が実際に使われていること
        kinds = {proofread.room_of(r)[0] for r in self.rows if proofread.room_of(r)}
        self.assertEqual(kinds, {"glyph", "sjis"},
                         f"材料に片方の数え方しか入っていない: {kinds}")

    def run_proofread(self, rows, header=None):
        import subprocess

        path = os.path.join(self.tmp.name, "case.tsv")
        head = header or ["id", "size", "original", "translation"]
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\t".join(head) + "\n")
            for r in rows:
                fh.write("\t".join(str(r.get(k, "")) for k in head) + "\n")
        res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                              path, "--no-font-check"], capture_output=True, text=True)
        return res.stdout + res.stderr

    def test_a_voice_row_is_measured_too(self):
        """音声番号の行も測れること (`--keep-voice` で取り出したとき).

        あの行は符号 4 個がそのまま 1 件で、**終わりの印が付かない**。
        本文と同じ数え方をすると 2 バイトずれて「数えられない」に落ちる。
        落ちても嘘は言わないが、`--keep-voice` を使うと全部の音声行が
        そこに積み上がって、注意の数だけが膨らむ。
        """
        import boku2

        out = os.path.join(self.tmp.name, "OUT")
        tsv = os.path.join(self.tmp.name, "voice.tsv")
        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        boku2.main(["text", out, "-f", os.path.join(sample, "font.txt"),
                    "--keep-voice", "-o", tsv])
        rows = scrp.read_tsv(tsv)
        voices = [r for r in rows if r["original"].startswith("<VOICE:")]
        self.assertTrue(voices, "材料に音声番号の行が無い")
        bad = [r["id"] for r in voices if proofread.room_of(r) is None]
        self.assertEqual(bad, [], f"音声番号の行が測れない: {bad}")

    def test_a_translation_that_does_not_fit_is_reported(self):
        out = self.run_proofread([
            {"id": "a", "size": 12, "original": "はじめから", "translation": "ゲームをさいしょからはじめる"},
        ])
        self.assertIn("room", out, out[-600:])
        self.assertIn("12 バイト", out, out[-600:])

    def test_a_translation_that_fits_is_not_reported(self):
        """**入る訳文では鳴らないこと。** 鳴りっぱなしの検査は読まれなくなる."""
        out = self.run_proofread([
            {"id": "a", "size": 12, "original": "はじめから", "translation": "はじめる"},
            {"id": "b", "size": 12, "original": "はじめから", "translation": "つづきから"},
        ])
        self.assertNotIn("room", out, out[-600:])

    def test_a_tsv_without_the_size_column_says_the_check_is_off(self):
        """`size` の欄が無い TSV で「入るかどうかも見た」と読ませないこと (#186 と同じ形)."""
        out = self.run_proofread(
            [{"id": "a", "original": "はじめから", "translation": "ゲームをさいしょからはじめる"}],
            header=["id", "original", "translation"])
        self.assertIn("入るかどうかの検査は動いていません", out, out[-600:])
        self.assertNotIn("/ room", out, "止まっている検査を効いている側に混ぜている")

    def test_rows_that_cannot_be_measured_are_counted(self):
        """数え方が合わない行は、**何行あるか**を言うこと (黙って飛ばさない)."""
        out = self.run_proofread([
            {"id": "a", "size": 12, "original": "はじめから", "translation": "はじめから"},
            {"id": "b", "size": 7, "original": "はじめから", "translation": "はじめから"},
        ])
        self.assertIn("1 行は入れ物の大きさが数えられませんでした", out, out[-600:])

    def test_the_shift_jis_frame_refuses_a_character_it_cannot_hold(self):
        """Shift-JIS の枠に cp932 で書けない字を入れたら ERROR (入れようがない)."""
        out = self.run_proofread([
            {"id": "a", "size": 4, "original": "はい", "translation": "\u2661い"},
        ])
        self.assertIn("cp932 で書けない字", out, out[-600:])


class TestTheFrameKnobsAreRealAndInOnePlace(unittest.TestCase):
    """実物の枠の仕様が分かったときに**直す所**が、本当にそこにあること (#216).

    `line_width` / `line_count` の上限は、いまは練習用の作品の数字
    (1 行 18 文字 / 1 ページ 3 行) です。実物の枠は分かっていないので、
    docs/10 に「分かったらここを直す」を 1 か所だけ書きました。

    **書いただけの直し方は、書いた瞬間から古くなる。** 節が挙げている名前が
    本当に `data/rules.json` の鍵で、本当に旗としてあることを見ます。
    道具の側で名前を変えたら、この節ごと落ちます。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        head = "### 実物の枠の仕様が分かったら"
        if head not in doc:
            raise unittest.SkipTest("docs/10 にその節が無い")
        cls.section = doc.split(head, 1)[1].split("\n## ", 1)[0]
        with open(os.path.join(REPO, "data", "rules.json"), encoding="utf-8") as fh:
            cls.rules = json.load(fh)

    def test_every_setting_the_section_names_is_a_real_key(self):
        import re

        names = set(re.findall(r"`(line_max_width|lines_max|var_width)`", self.section))
        self.assertGreaterEqual(len(names), 3, f"節から設定の名前を {names} しか拾えない")
        for name in sorted(names):
            self.assertIn(name, self.rules["ja"],
                          f"data/rules.json の ja に {name} が無い (節の直し方が古い)")

    def test_every_flag_the_section_names_really_exists(self):
        """節が挙げている旗が、本当に打てること (打てない旗を書かない)."""
        import re
        import subprocess

        flags = set(re.findall(r"`?(--line-width|--lines-max|--var-width)`?", self.section))
        self.assertGreaterEqual(len(flags), 2, f"節から旗を {flags} しか拾えない")
        res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"), "--help"],
                             capture_output=True, text=True, cwd=REPO)
        for flag in sorted(flags):
            self.assertIn(flag, res.stdout, f"proofread.py に {flag} が無い")

    def test_the_two_tools_read_the_same_file(self):
        """**直す所が 1 か所であること。** 画面と一括処理が別の設定を見ていたら嘘になる."""
        for tool in ("proofread.py", "make_viewer.py"):
            with open(os.path.join(REPO, "tools", tool), encoding="utf-8") as fh:
                src = fh.read()
            # **ファイル丸ごとを haystack にしない** (落ちたときに全文が出る)。
            # 見つかったかどうかだけを渡す
            self.assertTrue("rules.json" in src, f"{tool} が rules.json を見ていない")
        with open(os.path.join(REPO, "tools", "make_viewer.py"), encoding="utf-8") as fh:
            viewer = fh.read()
        for key in ("line_max_width", "lines_max"):
            self.assertTrue(key in viewer,
                            f"検査台が {key} を見ていない (rules.json を直しても画面が動かない)")


class TestPracticeSettingsAreAlwaysDeclared(unittest.TestCase):
    """**練習用の作品の物差しを黙って別の作品に当てる道具が無いこと** (#215).

    この一式の既定の用語集・文字表・仕様は、全部**練習用の作品**
    (リィンフォルト戦記) のものです。別の作品の文章にそのまま当てると、
    出てくる判定は嘘になります。`proofread.py` は #89 から、
    `make_viewer.py` は #214 からそう断っています。

    #214 の最後に「ほかの道具はたぶん埋まっている」と書きました。
    **「たぶん」を機械で潰すのがこの検査です。** 物差しを既定で持っている道具を
    ソースから数え上げ、その全部が「練習用の作品」と口に出すことを見ます。
    新しく物差しを既定で持つ道具を足したら、断りを書くまで落ちます。
    """

    #: 「判定の物差し」の既定。これを持っている = 別の作品に当てると嘘になる道具
    YARDSTICKS = ("font_chars", "glossary.tsv", "rules.json")

    #: 数え上げた道具の走らせ方。**一覧に無い道具が見つかったら落とす** ので、
    #: 道具を足した人はここに書き足すことになる (書き方が分からないまま緑にしない)
    HOW = {
        "proofread.py": lambda tsv, out: [tsv],
        "make_viewer.py": lambda tsv, out: ["--tsv", tsv, "-o", out],
    }

    #: 物差しの名前は出てくるが、**判定はしない**道具。
    #: **理由の書けるものだけ**をここに置く (書けないなら断りの抜けなので直すこと)
    NOT_A_JUDGE = {
        "make_sample.py": "練習用の物差しそのものを**作る**道具。判定はしない",
        "font_view.py": "渡されたフォントを覗くだけ。物差しを既定で持たない "
                        "(`--chars` に既定は無い)",
    }

    def judges(self) -> list:
        """既定で物差しを持っている道具を、ソースから数え上げる.

        **広めに拾う。** 物差しの名前が出てくれば候補にして、判定しない道具は
        `NOT_A_JUDGE` に理由を書いて外す。狭く拾うと、断りの要る道具が
        **黙って漏れる** —— 漏れる側に倒すと、この検査そのものが意味を失う。
        """
        import glob

        found = []
        for path in sorted(glob.glob(os.path.join(REPO, "tools", "*.py"))):
            with open(path, encoding="utf-8") as fh:
                src = TestDocs.code_only(fh.read())
            if any(f'"{mark}' in src or f"/{mark}" in src or mark in src
                   for mark in self.YARDSTICKS) and "add_argument" in src:
                found.append(os.path.basename(path))
        return found

    def test_the_excuses_are_still_true(self):
        """**除いた道具が、本当に物差しを既定で持っていないこと** (#215).

        理由を書いて除くのは、書いた時点では正しくても**古くなる**。
        `--font-chars` のような旗に既定を足した瞬間に、その道具は判定する側に
        変わるので、除外の理由ごと落とす。
        """
        import re

        for tool in self.NOT_A_JUDGE:
            with open(os.path.join(REPO, "tools", tool), encoding="utf-8") as fh:
                src = fh.read()
            for line in re.findall(r"add_argument\([^\n]*", src):
                if "default=" in line:
                    self.assertFalse(any(y in line for y in self.YARDSTICKS),
                                     f"{tool} は物差しを既定で持つようになった "
                                     f"(除外の理由が古い): {line.strip()[:90]}")

    def test_the_list_is_not_empty(self):
        """前提: 数え上げが効いていること (0 件なら下が全部素通しになる)."""
        got = self.judges()
        self.assertIn("proofread.py", got, f"数え上げが壊れている: {got}")
        self.assertIn("make_viewer.py", got, f"数え上げが壊れている: {got}")

    def test_every_tool_with_practice_settings_says_so(self):
        import subprocess

        missing_recipe = [t for t in self.judges()
                          if t not in self.HOW and t not in self.NOT_A_JUDGE]
        self.assertEqual(missing_recipe, [],
                         "練習用の物差しを既定で持っているのに、この検査が走らせ方を"
                         f"知らない道具: {missing_recipe} (HOW に足すこと)")
        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "other.tsv")
            with open(tsv, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                fh.write("r0\t夏休みの虫取り\t夏休みの虫取り\n")
            for tool in self.judges():
                if tool in self.NOT_A_JUDGE:
                    continue
                with self.subTest(tool):
                    argv = self.HOW[tool](tsv, os.path.join(tmp, tool + ".html"))
                    res = subprocess.run(
                        [sys.executable, os.path.join(REPO, "tools", tool), *argv],
                        capture_output=True, text=True, cwd=REPO)
                    out = res.stdout + res.stderr
                    self.assertIn("練習用の作品", out,
                                  f"{tool} が練習用の物差しで測ったことを言っていない:\n{out[-700:]}")


class TestTheRuleSetMatchesTheGame(unittest.TestCase):
    """校正の設定が、見ている作品のものであること (#89).

    既定の用語集・文字表はこの練習用の作品 (リィンフォルト戦記) のもの。実物の
    作品の TSV にそのまま当てると、**別の作品の物差しで測った結果**が出る。
    とくに文字表が違うと「フォントに無い文字」が総崩れになる。
    """

    def test_a_grid_shaped_glyph_table_loads_completely(self):
        """1 行 23 文字の文字表 (実物側の形) を、全部読むこと.

        以前は行の先頭 1 文字しか見ておらず、187 文字が 9 文字として読まれていた。
        落ちも警告もしないので、出力を信じてしまう。
        """
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        path = os.path.join(REPO, "work", "BOKU2SAMPLE", "font.txt")
        with open(path, encoding="utf-8") as fh:
            expected = {ch for ch in fh.read() if ch not in "\n\r"}
        self.assertGreater(len(expected), 100, "練習データの文字表が小さすぎる")
        self.assertEqual(expected, proofread.load_font_chars(path))

    def test_the_one_char_per_line_table_still_loads(self):
        """今までの形 (1 行 1 文字 + # のコメント) が変わっていないこと."""
        chars = proofread.load_font_chars(os.path.join(REPO, "data", "font_chars.txt"))
        self.assertEqual(set(FIX.glyph_order), chars)
        self.assertNotIn("#", chars, "コメント行を文字として読んでいる")

    def test_the_wrong_glyph_table_would_flag_everything(self):
        """壊れた読み方だと全行が誤検出になることを、数で残しておく."""
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        path = os.path.join(REPO, "work", "BOKU2SAMPLE", "font.txt")
        with open(path, encoding="utf-8") as fh:
            broken = {line[0] for line in fh.read().split("\n") if line}   # 直す前の読み方
        good = proofread.load_font_chars(path)
        self.assertLess(len(broken), len(good) // 10, "この比較に意味が無い")
        rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")
        sample = "はじめから"
        row = {"id": "0", "original": sample, "translation": sample}
        with_broken = {f.rule for f in proofread.check_row(row, rules, [], broken)}
        with_good = {f.rule for f in proofread.check_row(row, rules, [], good)}
        self.assertIn("font", with_broken, "壊れた表でも指摘が出ないなら例が悪い")
        self.assertNotIn("font", with_good, "正しい表なのに指摘が出ている")

    def test_fontlist_and_the_raw_table_agree(self):
        """docs/10 の fontlist 経由と、font.txt 直渡しが同じ集合になること (#89).

        fontlist は全角の空白 (U+3000) を落としていた。フォントには入っている
        (僕の夏休み 2 では 0 番) ので、本文の空白が「フォントに無い文字」として
        誤って指摘される。docs/10 が案内している方の道でだけ起きていた。
        """
        import subprocess

        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        table = os.path.join(REPO, "work", "BOKU2SAMPLE", "font.txt")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "font_chars.txt")
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                                  "fontlist", table, "-o", out],
                                 capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            via_fontlist = proofread.load_font_chars(out)
        direct = proofread.load_font_chars(table)
        self.assertEqual(direct, via_fontlist, "2 つの道で文字表が食い違う")
        self.assertIn("　", via_fontlist, "全角の空白が落ちている")

    def test_a_full_width_space_is_not_reported_as_missing(self):
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        chars = proofread.load_font_chars(
            os.path.join(REPO, "work", "BOKU2SAMPLE", "font.txt"))
        rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")
        row = {"id": "0", "original": "ぼく　なつやすみ", "translation": "ぼく　なつやすみ"}
        hit = {f.rule for f in proofread.check_row(row, rules, [], chars)}
        self.assertNotIn("font", hit, "フォントにある全角空白を無いと言っている")

    def test_the_output_says_which_settings_were_used(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.tsv")
            scrp.write_tsv(path, [{"id": "0", "original": "はい", "translation": "はい"}])
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                                  path], capture_output=True, text=True, cwd=REPO)
        out = res.stdout + res.stderr
        self.assertIn("使った設定", out, out)
        self.assertIn("glossary.tsv", out)
        self.assertIn("font_chars.txt", out)
        self.assertIn("練習用の作品のものです", out, "既定だと分かる断りが無い")

    def test_passing_the_games_own_table_drops_the_warning_for_it(self):
        import subprocess

        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.tsv")
            scrp.write_tsv(path, [{"id": "0", "original": "はい", "translation": "はい"}])
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"), path,
                 "--font-chars", os.path.join(REPO, "work", "BOKU2SAMPLE", "font.txt")],
                capture_output=True, text=True, cwd=REPO)
        out = res.stdout + res.stderr
        self.assertIn("font.txt", out)
        self.assertIn("この用語集は練習用の作品のものです", out,
                      "文字表だけ差し替えたのに、用語集の断りが消えている")


class TestInertChecksAreAnnounced(unittest.TestCase):
    """訳文が原文のままなら、比べる検査が動いていないと言うこと (#88).

    取り出したばかりのテキストは訳文の欄が原文のまま。原文と見比べる 5 つの検査は
    **構造的に何も見ていない**のに、「問題なし 23 行」とだけ出る。13 個の検査を
    全部通ったように読めてしまう。実物の text を最初にかけるのがまさにこの状態。
    """

    def run_proofread(self, rows: list[dict], *args):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.tsv")
            scrp.write_tsv(path, rows)
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                                  path, *args], capture_output=True, text=True, cwd=REPO)
        return res.stdout + res.stderr

    def test_all_rows_untranslated_says_which_checks_did_nothing(self):
        rows = [{"id": "0", "original": "こんにちは。", "translation": "こんにちは。"},
                {"id": "1", "original": "さようなら。", "translation": "さようなら。"}]
        out = self.run_proofread(rows)
        self.assertIn("原文と見比べる検査は動いていません", out, out)
        for rule in proofread.COMPARING_RULES:
            self.assertIn(rule, out, f"{rule} が一覧に無い")
        self.assertIn("line_width", out, "効いている検査の一覧が出ていない")

    def test_a_translated_file_does_not_get_the_notice(self):
        rows = [{"id": "0", "original": "こんにちは。", "translation": "こんばんは。"}]
        out = self.run_proofread(rows)
        self.assertNotIn("原文と見比べる検査は動いていません", out, out)

    def test_a_partly_translated_file_says_how_many_are_untouched(self):
        rows = [{"id": "0", "original": "こんにちは。", "translation": "こんばんは。"},
                {"id": "1", "original": "さようなら。", "translation": "さようなら。"}]
        out = self.run_proofread(rows)
        self.assertIn("1 行は訳文の欄が原文のままです", out, out)

    def test_the_two_rule_lists_cover_every_rule_the_tool_emits(self):
        """一覧に載せ忘れた rule があれば気づけること (#81 と同じ形の素通し防止)."""
        with open(os.path.join(REPO, "tools", "proofread.py"), encoding="utf-8") as fh:
            src = fh.read()
        emitted = set(re.findall(r'"(?:ERROR|WARN|INFO)",\s*"([a-z_]+)"', src))
        emitted |= set(re.findall(r'\("(?:ERROR|WARN)",\s*"([a-z_]+)"\)', src))
        emitted |= set(re.findall(r'entry\.get\([^)]*\),\s*"([a-z_]+)"', src))
        listed = set(proofread.COMPARING_RULES) | set(proofread.ABSOLUTE_RULES)
        self.assertEqual(set(), emitted - listed,
                         "どちらの一覧にも入っていない rule があります")


class TestRoundTripIsChecked(unittest.TestCase):
    """課題 6 の「往復して確認する」が、道具で通せること (#87).

    課題 6 は「入れ直したファイルから再抽出したテキストが、意図したものと一致する」
    を条件にしているのに、**2 つの TSV を突き合わせる道具が無かった**。39 行を目で
    見比べろ、ということになっていた (課題 8 の答え合わせも同じ)。
    """

    def setUp(self):
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = lambda *a: subprocess.run([sys.executable, *a], capture_output=True,
                                             text=True, cwd=REPO)
        problem = ensure_practice("work/SCRIPT.BIN", "make_sample.py")
        if problem:
            self.skipTest(problem)

    def tool(self, name: str) -> str:
        return os.path.join(REPO, "tools", name)

    def write(self, name: str, rows: list[dict]) -> str:
        path = os.path.join(self.tmp.name, name)
        scrp.write_tsv(path, rows)
        return path

    def test_matching_files_report_zero(self):
        rows = [{"id": "0", "original": "あ", "translation": "あ"},
                {"id": "1", "original": "い", "translation": "い"}]
        left, right = self.write("a.tsv", rows), self.write("b.tsv", rows)
        res = self.run(self.tool("compare_tsv.py"), left, right)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("全部一致しました", res.stdout)

    def test_a_difference_names_the_row_and_fails(self):
        left = self.write("a.tsv", [{"id": "0", "original": "あ", "translation": "あ"},
                                    {"id": "1", "original": "い", "translation": "い"}])
        right = self.write("b.tsv", [{"id": "0", "original": "あ", "translation": "あ"},
                                     {"id": "1", "original": "う", "translation": "う"}])
        res = self.run(self.tool("compare_tsv.py"), left, right)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("id 1", res.stdout)
        self.assertIn("食い違い 1 行", res.stdout)

    def test_ignore_drops_rows_from_the_comparison(self):
        """課題 8 は答えの <VOICE:…> の行を除いて比べる."""
        left = self.write("a.tsv", [{"id": "0", "original": "あ", "translation": ""}])
        right = self.write("b.tsv", [{"id": "0", "original": "あ", "translation": ""},
                                     {"id": "9", "original": "<VOICE:00010001>",
                                      "translation": ""}])
        res = self.run(self.tool("compare_tsv.py"), left, right, "--ignore", "<VOICE:")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        without = self.run(self.tool("compare_tsv.py"), left, right)
        self.assertEqual(without.returncode, 1, "除かなければ食い違うはずが通っている")

    def test_the_whole_exercise_6_round_trip_passes(self):
        """課題 6 を答え (原文に戻した状態) で通し、往復が一致すること."""
        rows = scrp.read_tsv(os.path.join(REPO, "exercises", "qa_target.tsv"))
        for row in rows:                       # 答え = 全行を原文に戻した状態
            row["translation"] = row["original"]
        fixed = self.write("qa_fixed.tsv", rows)
        out = os.path.join(self.tmp.name, "SCRIPT_fixed.BIN")
        ins = self.run(self.tool("insert_text.py"), fixed, "-o", out,
                       "--original", os.path.join(REPO, "work", "SCRIPT.BIN"))
        self.assertEqual(ins.returncode, 0, ins.stdout + ins.stderr)
        self.assertIn("ぴったり", ins.stdout, "元の容量に収まっていない")
        verify = os.path.join(self.tmp.name, "verify.tsv")
        dump = self.run(self.tool("dump_text.py"), out, "-o", verify)
        self.assertEqual(dump.returncode, 0, dump.stdout + dump.stderr)
        cmp_res = self.run(self.tool("compare_tsv.py"), fixed, verify,
                           "--left", "translation", "--right", "original")
        self.assertEqual(cmp_res.returncode, 0, cmp_res.stdout + cmp_res.stderr)

    def test_insert_text_refuses_to_write_data_it_cannot_read_back(self):
        """組み立てた結果が読み直せないなら、書かずに止まること (#87).

        道具の不具合で本文が化けたまま書き出すと、次の工程まで気づけない。
        往復の確認を道具の中でやってしまう。
        """
        rows = scrp.read_tsv(os.path.join(REPO, "exercises", "qa_target.tsv"))
        for row in rows:                       # 容量の検査で先に止まらないよう、直した状態にする
            row["translation"] = row["original"]
        fixed = self.write("for_break.tsv", rows)
        argv = ["insert_text.py", fixed, "-o", os.path.join(self.tmp.name, "nope.BIN"),
                "--original", os.path.join(REPO, "work", "SCRIPT.BIN")]
        script = (
            "import runpy, sys\n"
            f"sys.path.insert(0, {os.path.join(REPO, 'tools')!r})\n"
            "import scrp\n"
            "real = scrp.build_archive\n"
            "def broken(enc, blobs, pool_duplicates=False):\n"
            "    data = bytearray(real(enc, blobs, pool_duplicates))\n"
            "    data[scrp.HEADER_SIZE + len(blobs) * 4 + 4] ^= 0x01\n"
            "    return bytes(data)\n"
            "scrp.build_archive = broken\n"
            f"sys.argv = {argv!r}\n"
            f"runpy.run_path({self.tool('insert_text.py')!r}, run_name='__main__')\n"
        )
        import subprocess
        res = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, cwd=REPO)
        out = res.stdout + res.stderr
        self.assertEqual(res.returncode, 1, out)
        self.assertIn("取り出し直すと", out, out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "nope.BIN")),
                         "読み直せないデータを書き出している")


class TestEveryToolUsesTheSharedEntry(unittest.TestCase):
    """道具はみな scrp.cli_main を通ること (#85).

    #79・#83 で入れた親切な伝言 (無いファイル名と作り方) は cli_main にある。
    ところが font_view.py / make_elf.py / boku2.py は `sys.exit(main())` で
    自前に出口を作っていて、**その 3 つだけ素の traceback が出ていた**。
    落ちるわけではないので、緑のまま何回も見過ごしていた (#81 と同じ形)。
    """

    #: 入口を持つ道具 (ライブラリとして読むだけのものは除く)
    @staticmethod
    def cli_tools() -> list[str]:
        out = []
        for name in sorted(os.listdir(os.path.join(REPO, "tools"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO, "tools", name), encoding="utf-8") as fh:
                body = fh.read()
            if '__name__ == "__main__"' in body:
                out.append(name)
        return out

    def test_no_tool_makes_its_own_exit(self):
        self.assertTrue(self.cli_tools(), "道具が 1 つも見つからない")
        for name in self.cli_tools():
            with open(os.path.join(REPO, "tools", name), encoding="utf-8") as fh:
                body = fh.read()
            self.assertTrue("cli_main(main)" in body,
                            f"tools/{name} が scrp.cli_main を通っていない "
                            "(無いファイルの案内が出ない)")
            self.assertFalse("sys.exit(main())" in body,
                             f"tools/{name} が自前の出口を持っている")

    def test_a_missing_file_is_explained_by_every_tool(self):
        """入口を持つ道具はどれも、無いファイルで traceback を出さないこと."""
        import subprocess

        checked = 0
        for name in self.cli_tools():
            if name.startswith("make_"):
                continue                      # 入力ファイルを取らない生成器は対象外
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", name),
                                  os.path.join("work", "NO_SUCH_FILE.bin")],
                                 capture_output=True, text=True, cwd=REPO)
            out = res.stdout + res.stderr
            if "usage:" in out and "No such file" not in out:
                continue                      # 引数の形が違う道具 (使い方が出る) は対象外
            self.assertNotIn("Traceback", out, f"tools/{name}: 素の例外が出ている\n{out}")
            checked += 1
        self.assertGreater(checked, 3, "確かめた道具が少なすぎる (選び方が外れている)")


class TestOptionalModuleIsExplained(unittest.TestCase):
    """追加で入れる部品が無いときに、入れ方と代わりの手を言うこと (#85).

    課題 4 は「PNG で一覧を出すと並びの規則がすぐ見えます」と勧めているのに、
    Pillow が無いと素の ImportError が出ていた。README は「Python だけで動く
    (フォント演習だけ Pillow)」と書いてあるので、素人は必ずここを踏む。
    """

    def run_without_pillow(self, *args):
        import subprocess

        script = (
            "import builtins, runpy, sys\n"
            "real = builtins.__import__\n"
            "def fake(name, *a, **k):\n"
            "    if name == 'PIL' or name.startswith('PIL.'):\n"
            "        raise ImportError(\"No module named 'PIL'\")\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = fake\n"
            f"sys.argv = {list(args)!r}\n"
            f"runpy.run_path({os.path.join(REPO, 'tools', args[0])!r}, run_name='__main__')\n"
        )
        return subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, cwd=REPO)

    def test_font_view_png_says_how_to_install_and_what_to_do_instead(self):
        problem = ensure_practice("work/FONT.BIN", "make_sample.py")
        if problem:
            self.skipTest(problem)
        res = self.run_without_pillow("font_view.py", "work/FONT.BIN", "--png",
                                      os.path.join(REPO, "work", "unused_sheet.png"))
        out = res.stdout + res.stderr
        self.assertEqual(res.returncode, 1, out)
        self.assertNotIn("Traceback", out, out)
        self.assertIn("Pillow", out)
        self.assertIn("pip install", out, "入れ方が出ていない")
        self.assertIn("--ascii", out, "代わりの手つきが出ていない")

    def test_a_typo_in_our_own_import_is_not_hidden(self):
        """こちらの書き間違い (知らない部品名) は隠さず投げること."""
        with self.assertRaises(ModuleNotFoundError):
            def boom() -> int:
                import scrp_no_such_module  # noqa: F401
                return 0

            try:
                scrp.cli_main(boom)
            except SystemExit as exc:          # 握りつぶされていたら失敗として扱う
                self.fail(f"知らない部品名を握りつぶした (終了コード {exc.code})")


class TestReadingTheFontSheet(unittest.TestCase):
    """課題 4 (グリフから文字を特定する) が、実際にやって成立すること (#85).

    要点は「並びの規則が見える」こと。1 つずつ縦に流すと 21 文字で 400 行を超え、
    規則 (小書き→大, 清音→濁音) は見えない。横に並べて初めて見える。
    """

    def setUp(self):
        problem = ensure_practice("work/FONT.BIN", "make_sample.py")
        if problem:
            self.skipTest(problem)
        import subprocess

        self.run = lambda *a: subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "font_view.py"), *a],
            capture_output=True, text=True, cwd=REPO)

    def test_across_puts_the_glyphs_side_by_side(self):
        res = self.run(os.path.join(REPO, "work", "FONT.BIN"), "--ascii", "10-13",
                       "--across", "4", "--chars", os.path.join(REPO, "data", "font_chars.txt"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        head = next(l for l in res.stdout.split("\n") if "グリフ 10" in l)
        for want in ("グリフ 11", "グリフ 12", "グリフ 13"):
            self.assertIn(want, head, "同じ行に並んでいない")

    def test_one_per_line_still_works(self):
        res = self.run(os.path.join(REPO, "work", "FONT.BIN"), "--ascii", "0-2")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(3, res.stdout.count("オフセット"))

    def test_the_pairs_really_differ_only_by_the_dakuten(self):
        """規則が本当にあること: 清音と濁音は右上以外がほぼ同じ.

        文書で「濁音は右上に点が 2 つ足されているだけ」と言い切っているので、
        練習データが本当にそうなっているかを機械で確かめる。
        """
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import font_view
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        glyphs = font_view.load_glyphs(os.path.join(REPO, "work", "FONT.BIN"))
        chars = font_view.load_chars(os.path.join(REPO, "data", "font_chars.txt"))
        pairs = [(chars.index(a), chars.index(b)) for a, b in
                 (("か", "が"), ("き", "ぎ"), ("く", "ぐ"), ("け", "げ"), ("こ", "ご"))]
        size = font_view.GLYPH_SIZE
        for plain, voiced in pairs:
            rows_a = font_view.glyph_rows(glyphs[plain])
            rows_b = font_view.glyph_rows(glyphs[voiced])
            # 点が増えると外形が変わり、枠の中で中央に置き直されて 1 ドットずれることが
            # ある (け/げ が実際そう)。縦 1 ドットまでのずれは許して比べる
            best = min(
                sum(rows_a[r][c] != (rows_b[r + dy][c] if 0 <= r + dy < size else 0)
                    for r in range(size) for c in range(size)
                    if not (r < size // 3 and c > size * 2 // 3))
                for dy in (-1, 0, 1))
            self.assertLessEqual(best, 3,
                                 f"{chars[plain]} と {chars[voiced]} が右上以外でも違う "
                                 f"(異なるマス {best})")

    def test_the_dakuten_shift_is_at_most_one_dot(self):
        """ずれても 1 ドットまでであること (文書でそう言い切っているので測る)."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        try:
            import font_view
        finally:
            sys.path.remove(os.path.join(REPO, "tools"))
        glyphs = font_view.load_glyphs(os.path.join(REPO, "work", "FONT.BIN"))
        chars = font_view.load_chars(os.path.join(REPO, "data", "font_chars.txt"))
        size = font_view.GLYPH_SIZE
        worst = 0
        for a, b in (("か", "が"), ("き", "ぎ"), ("く", "ぐ"), ("け", "げ"), ("こ", "ご")):
            rows_a = font_view.glyph_rows(glyphs[chars.index(a)])
            rows_b = font_view.glyph_rows(glyphs[chars.index(b)])
            shifts = {dy: sum(rows_a[r][c] != (rows_b[r + dy][c] if 0 <= r + dy < size else 0)
                              for r in range(size) for c in range(size)
                              if not (r < size // 3 and c > size * 2 // 3))
                      for dy in (-2, -1, 0, 1, 2)}
            worst = max(worst, abs(min(shifts, key=lambda d: shifts[d])))
        self.assertLessEqual(worst, 1, f"1 ドットより大きくずれている ({worst} ドット)")


class TestGrowingATableByHand(unittest.TestCase):
    """課題 3 (表を手で育てる) の途中で、道具が残りの量を言うこと (#84).

    実際に課題 3 を解いてみて分かったこと。表に無いバイトは文字欄で「.」に
    なるだけなので**どの値が足りないのか読み取れず**、全文抽出は最初の 1 バイトで
    止まって「0xD5: 文字テーブルにないコード 0xE0」としか言わなかった。
    1 バイト直しては打ち直す、を 100 回以上繰り返すことになる。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import subprocess

        self.run = lambda *a: subprocess.run([sys.executable, *a], capture_output=True,
                                             text=True, cwd=REPO)
        res = self.run(os.path.join(REPO, "tools", "make_sample.py"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.enc = os.path.join(REPO, "work", "MSG_ENC.BIN")
        self.partial = os.path.join(self.tmp.name, "my_guess.tbl")
        derived = self.run(os.path.join(REPO, "tools", "relative_search.py"), self.enc,
                           "--search", "ここは", "--derive", self.partial)
        self.assertEqual(derived.returncode, 0, derived.stdout + derived.stderr)

    def test_hexdump_lists_the_bytes_that_are_missing(self):
        res = self.run(os.path.join(REPO, "tools", "hexdump.py"), self.enc,
                       "--table", self.partial, "--message", "0")
        out = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, out)
        self.assertIn("表に無いバイト", out)
        self.assertIn("0xE0", out, "足りないバイト値が並んでいない")
        self.assertIn("すぐ次の文字は当てにできません", out,
                      "2 バイト文字の後半が別の字に読まれる件の注意が無い")

    def test_hexdump_does_not_give_away_the_answer(self):
        """答え (実在の対応) を例に出さないこと. 課題 3 の答えを漏らさない."""
        res = self.run(os.path.join(REPO, "tools", "hexdump.py"), self.enc,
                       "--table", self.partial, "--message", "0")
        out = res.stdout + res.stderr
        answers = scrp.load_table(os.path.join(REPO, "answers", "custom.tbl"))
        for raw, ch in answers.by_bytes.items():
            if len(raw) == 2:
                self.assertNotIn(f"{raw.hex().upper()}={ch}", out, "答えを例に出している")

    def test_a_full_dump_says_how_much_is_left(self):
        res = self.run(os.path.join(REPO, "tools", "dump_text.py"), self.enc,
                       "--table", self.partial, "-o",
                       os.path.join(self.tmp.name, "mine.tsv"))
        out = res.stdout + res.stderr
        self.assertEqual(res.returncode, 1, out)
        self.assertNotIn("Traceback", out, out)
        self.assertIn("読めるのが", out, "読める件数が出ていない")
        self.assertIn("表に無いバイト値は", out, "残りの量が出ていない")
        # 案内する id は、その表で本当に読める id であること (適当な 0 番ではなく)
        archive = scrp.read_archive(self.enc)
        ok, _bad, _missing = archive.survey_unreadable(scrp.load_table(self.partial))
        self.assertTrue(ok, "この表で読める id が 1 つも無い")
        self.assertIn(f"--message {ok[0]}", out, "1 件だけ読む道が示されていない")
        scrp.decode_message(archive.data, archive.pointers[ok[0]],
                            scrp.load_table(self.partial))     # 本当に読めることを確かめる

    def test_the_complete_table_still_dumps_cleanly(self):
        """答えの表を渡せば、今までどおり全文が通ること."""
        res = self.run(os.path.join(REPO, "tools", "dump_text.py"), self.enc,
                       "--table", os.path.join(REPO, "answers", "custom.tbl"),
                       "-o", os.path.join(self.tmp.name, "all.tsv"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("表に無いバイト", res.stdout + res.stderr)

    def test_the_survey_agrees_with_the_real_decode(self):
        """調査の「読める id」は、実際に読める id と一致すること."""
        archive = scrp.read_archive(self.enc)
        codec = scrp.load_table(self.partial)
        ok, bad, missing = archive.survey_unreadable(codec)
        self.assertEqual(sorted(ok + bad), list(range(archive.count)))
        self.assertTrue(missing, "足りないバイトがあるはずなのに空")
        for index in ok:
            scrp.decode_message(archive.data, archive.pointers[index], codec)
        for index in bad:
            with self.assertRaises(scrp.ScrpError):
                scrp.decode_message(archive.data, archive.pointers[index], codec)


class TestMissingPracticeData(unittest.TestCase):
    """練習データが無いときに、素の例外ではなく作り方を出すこと (#79).

    docs/01〜03 の**最初のコマンド**は work/ の練習データを使う。作る手順が
    書かれておらず、無いまま打つと Python の traceback が出ていた。
    素人はそれを「道具が壊れた」と読む。
    """

    def run_tool(self, *args):
        import subprocess

        return subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"), *args],
                              capture_output=True, text=True, cwd=REPO)

    def test_a_missing_practice_file_says_how_to_make_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_tool(os.path.join(tmp, "work", "SCRIPT.BIN"))
            out = res.stdout + res.stderr
            self.assertNotEqual(res.returncode, 0, out)
            self.assertNotIn("Traceback", out, "素の例外が出ている")
            self.assertIn("ファイルがありません", out)
            self.assertIn("make_sample.py", out, out)

    def test_a_work_file_no_tool_makes_does_not_send_you_to_make_sample(self):
        """work/ でも、この一式が作らない名前には作り方を言わないこと (#83).

        docs/06 の `work/qa_fixed.tsv` は読む人が自分で用意するファイル。そこに
        「make_sample.py を実行してください」と出すと、言われたとおりにしても
        何も変わらない。**指示どおりにして直らない**のがいちばん困る。
        """
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_tool(os.path.join(tmp, "work", "qa_fixed.tsv"))
            out = res.stdout + res.stderr
            self.assertNotIn("Traceback", out, out)
            self.assertNotIn("make_sample.py を実行", out,
                             "作れない名前に作り方を言っている")
            self.assertIn("この一式にはありません", out, out)
            self.assertIn("SCRIPT.BIN", out, "作れる名前の一覧が出ていない")

    def test_an_unrelated_missing_file_is_plain(self):
        """関係ないファイルには練習データの話をしないこと."""
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_tool(os.path.join(tmp, "nope.bin"))
            out = res.stdout + res.stderr
            self.assertNotIn("Traceback", out, out)
            self.assertIn("ファイルがありません", out)
            self.assertNotIn("make_sample.py", out, out)

    def test_a_directory_is_named_as_such(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_tool(tmp)
            out = res.stdout + res.stderr
            self.assertNotIn("Traceback", out, out)
            self.assertIn("フォルダが指定されています", out)

    def test_every_maker_actually_exists(self):
        """作り方として案内するコマンドの道具が、本当にあること."""
        for name, cmd in scrp.MAKERS.items():
            for token in cmd.split():
                if token.startswith("tools/") and token.endswith(".py"):
                    self.assertTrue(os.path.exists(os.path.join(REPO, token)),
                                    f"{name} の案内が指す {token} がありません")

    def test_suggested_commands_are_documented(self):
        """**道具が案内するコマンドは、手順書にある形と同じであること** (#183).

        #182 で「この作品の文字表を渡してください」と案内を足したとき、
        `--font-chars font.txt` と書きました。ところが docs/10 の「3. 校正にかける」は

            python3 tools/boku2.py fontlist font.txt -o font_chars.txt
            python3 tools/proofread.py all.tsv --font-chars font_chars.txt

        の 2 行です。`font.txt` は**番号順の対応表**、`font_chars.txt` は
        **使える字の一覧**で、別のファイル。どちらを渡しても動いてしまうので
        気づかず、**道具と手順書が違う道を案内する**ところでした。社長はどちらが
        正しいか分かりません。

        そこで、道具が印字するコマンドを集めて、docs/10 にその形があるかを見ます。
        """
        import re

        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        found, missing = [], []
        for tool in ("boku2.py", "proofread.py"):
            with open(os.path.join(REPO, "tools", tool), encoding="utf-8") as fh:
                src = fh.read()
            # `print(...)` / `say(...)` が出す文字列だけを見る。説明文 (docstring) の
            # 中の例まで拾うと、手順書に無くて当たり前のものが混ざる
            for m in re.finditer(r'(?:print|say)\(\s*f?"((?:[^"\\]|\\.)*)"', src):
                for cmd in re.findall(r"python3 tools/[^\s{]+(?: [^\s{]+)*", m.group(1)):
                    cmd = cmd.strip().rstrip("。")
                    # 日本語が混ざっていたら、コマンドではなく文章の一部
                    if re.search(r"[ぁ-んァ-ヶ一-龥]", cmd):
                        continue
                    found.append((tool, cmd))
                    if cmd not in doc:
                        missing.append(f"{tool}: {cmd}")
        # 拾えていること自体を確かめる (0 件なら必ず一致する)
        self.assertGreaterEqual(len(found), 3,
                                f"案内するコマンドを {len(found)} 件しか拾えない: {found}")
        self.assertEqual(missing, [],
                         "道具が案内するコマンドが docs/10 に無い "
                         "(道具と手順書で違う道を教えている):\n  " + "\n  ".join(missing))

    def test_the_control_codes_in_lesson_one_are_what_the_tool_decodes(self):
        """docs/01 が載せた実物の制御コードの表が、道具の読み方と合っていること (#213).

        docs/01 は「制御コードは、その文字コードが使っていないバイト範囲に置く」
        という原則を、**実物 (僕の夏休み 2) の 0x8000 番台**で説明している。
        表の値と意味が道具の読み方とずれたら、教材が嘘をつく。**表を読んで、
        その値を実際に道具に食わせて**確かめる (書き写した値を並べても意味がない)。
        """
        import re

        import boku2

        with open(os.path.join(REPO, "docs", "01-文字テーブル.md"), encoding="utf-8") as fh:
            doc = fh.read()
        rows = re.findall(r"^\| `0x([0-9A-F]{4})`([^|]*)\| `?([^|`]+)`? \|", doc, re.M)
        self.assertGreaterEqual(len(rows), 3, f"表を {len(rows)} 行しか拾えていない")
        for code, arg, tag in rows:
            value = int(code, 16)
            tag = tag.strip()
            with self.subTest(code):
                if value == 0xCDCD:                  # 詰め物は「出てこない」のが正しい
                    self.assertEqual(boku2.decode([0x10, value, 0x8000], ["あ"] * 20), "あ")
                    continue
                codes = [value, 0x05] if arg.strip() else [value]
                got = boku2.decode(codes + [0x8000], ["あ"] * 20, tags=("<" in tag))
                self.assertTrue(got.startswith(tag.split("xx")[0]),
                                f"0x{code} を道具は {got!r} と読む (docs/01 は {tag!r})")

    def test_the_practice_docs_say_to_make_the_data_first(self):
        """docs/01〜03 の頭に、練習データの作り方への導線があること."""
        for name in ("01-文字テーブル.md", "02-相対検索.md", "03-ポインタテーブル.md"):
            doc = open(os.path.join(REPO, "docs", name), encoding="utf-8").read()
            head = doc[:1200]
            self.assertIn("make_sample.py", head,
                          f"docs/{name} の頭に練習データの作り方が無い")


class TestTextCommandDeadEnds(unittest.TestCase):
    """正しい道具に正しく渡したのに何も取れないとき、黙って終わらないこと (#77).

    以前は 0 行の TSV を作って rc=0 で終わっていた。素人は Excel で開くまで
    何も起きていないことに気づけない。踏むのは「unpack / maps を先に回していない」
    か「場所違い」なので、そこまで言う。
    """

    def run_text(self, *args):
        import io as _io
        import contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = boku2.main(["text", *args])
        return rc, buf.getvalue()

    def test_nothing_found_says_which_step_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            rc, out = self.run_text(empty, "-o", os.path.join(tmp, "a.tsv"))
            self.assertEqual(rc, 1, out)
            self.assertIn("1 行も見つかりませんでした", out)
            self.assertIn("unpack", out, out)
            self.assertIn("maps", out, out)
            # 見出しだけの TSV は残し、そのことも言う
            self.assertTrue(os.path.exists(os.path.join(tmp, "a.tsv")))
            self.assertIn("見出しだけの", out)

    def test_unreadable_files_point_at_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "junk.msg")
            with open(junk, "wb") as fh:
                fh.write(b"not a msg at all, just plain text here")
            rc, out = self.run_text(junk, "-o", os.path.join(tmp, "j.tsv"))
            self.assertEqual(rc, 1, out)
            self.assertIn("読めませんでした", out)
            self.assertIn("boku2.py check", out, out)
            self.assertNotIn("unpack", out, "ファイルはあるのに unpack を勧めている")

    def test_an_empty_glyph_table_is_not_reported_as_missing(self):
        """-f を渡したのに「文字表なし」と言うと、渡していないように読める."""
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            out = os.path.join(tmp, "OUT")
            boku2.main(["unpack", os.path.join(sample, "BOKU2.IDX"),
                        os.path.join(sample, "BOKU2.IMG"), out])
            msg = os.path.join(out, "system", "system.msg")
            empty = os.path.join(tmp, "empty.txt")
            open(empty, "w", encoding="utf-8").close()

            rc, said = self.run_text(msg, "-f", empty, "-o", os.path.join(tmp, "c.tsv"))
            self.assertEqual(rc, 0, said)
            self.assertIn("読めた字が 0", said, said)
            self.assertIn(empty, said, said)
            self.assertNotIn("文字表なし", said, said)

            rc, said = self.run_text(msg, "-o", os.path.join(tmp, "b.tsv"))
            self.assertEqual(rc, 0, said)
            self.assertIn("文字表なし", said, said)
            self.assertIn("-f font.txt", said, said)


class TestFormatHint(unittest.TestCase):
    """SCRP でないファイルを渡されたとき、次に使う道具まで言うこと (#76).

    docs/01〜03 の練習は SCRP 形式で進むので、素人はその流れのまま実物のファイルを
    渡す。「SCRP ではありません」だけでは次の手が分からない。
    自信のあるときだけ言い、無関係なデータには何も言わないこと。
    """

    def test_it_names_the_format_and_the_tool(self):
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            idx = open(os.path.join(folder, "BOKU2.IDX"), "rb").read()
            self.assertIn("boku2.py unpack", scrp.guess_other_format(idx))
            self.assertIn("索引", scrp.guess_other_format(idx))
            mapfile = open(os.path.join(folder, "MAP", "M_A01000.BIN"), "rb").read()
            self.assertIn("boku2.py maps", scrp.guess_other_format(mapfile))

    def test_it_recognises_a_msg_and_a_font(self):
        # .msg: u32 件数 + 位置表 (8 バイト刻み) + 本文
        entries = [[1, 2, 0x8000], [3, 0x8000]]
        tab = 4 + len(entries) * 8
        head, body, p = struct.pack("<I", len(entries)), b"", tab
        for e in entries:
            head += struct.pack("<II", p, len(e) * 2)
            body += struct.pack(f"<{len(e)}H", *e)
            p += len(e) * 2
        hint = scrp.guess_other_format(head + body)
        self.assertIn("boku2.py text", hint, hint)
        # フォント (TMS の前置き + TIM2)
        tms = b"TMS\0" + struct.pack("<I", 0x80) + b"\0" * 0x78 + b"TIM2" + b"\0" * 64
        self.assertIn("TIM2", scrp.guess_other_format(tms))

    def test_it_stays_quiet_on_unrelated_data(self):
        """無関係なデータに当てずっぽうを言わないこと (言われると余計に迷う)."""
        import random

        random.seed(7)
        for name, blob in [
            ("乱数", bytes(random.randrange(256) for _ in range(4096))),
            ("ゼロ埋め", bytes(4096)),
            ("ASCII", b"BOOT2 = cdrom0:\\SCPS_150.26;1\nVER = 1.00\n" * 40),
            ("短すぎる", b"\x01\x02\x03"),
        ]:
            self.assertEqual(scrp.guess_other_format(blob), "", name)

    def test_the_error_carries_the_hint(self):
        import subprocess
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "dump_text.py"),
                 os.path.join(folder, "BOKU2.IDX"), "-o", os.path.join(tmp, "x.tsv")],
                capture_output=True, text=True)
            out = res.stdout + res.stderr
            self.assertNotEqual(res.returncode, 0, out)
            self.assertIn("SCRP ファイルではありません", out)
            self.assertIn("boku2.py unpack", out, out)


class TestWrongPipelineFile(unittest.TestCase):
    """列が同じ 2 つの TSV を取り違えたときに、症状ではなく状況を言うこと (#75).

    練習用 SCRP の TSV と、僕の夏休み 2 の取り出し (boku2.py text) の TSV は
    列がまったく同じ。以前は「id は 0 から連番で」としか言わなかったので、
    素人は手で連番に振り直そうとしてしまう (振り直しても入れ直し先が違う)。
    """

    def test_boku2_tsv_into_insert_text_explains_the_situation(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "all.tsv")
            with open(tsv, "w", encoding="utf-8-sig", newline="\n") as fh:
                fh.write("id\toffset\tsize\toriginal\ttranslation\n")
                fh.write("diary#0:0\t0x20\t12\tあ\tあ\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "insert_text.py"),
                 tsv, "-o", os.path.join(tmp, "out.bin"), "--encoding", "sjis"],
                capture_output=True, text=True)
            out = res.stdout + res.stderr
            self.assertNotEqual(res.returncode, 0, out)
            self.assertIn("boku2.py text", out, out)
            self.assertIn("読み取り専用", out, out)
            self.assertIn("proofread.py", out, out)
            # 文言が途中で欠けていないこと (置換で壊した実績がある)
            self.assertIn("列は同じですが", out, out)
            self.assertIn("練習用の SCRP 形式に入れ直すためのもの", out, out)
            self.assertIn("diary#0:0", out, out)
            # 症状だけを言う古い文言に戻っていないこと
            self.assertNotIn("0 から連番で", out, out)
            self.assertNotIn("**", out, "端末の文言に markdown の印が混ざっている")

    def test_a_plain_id_mistake_still_says_what_to_fix(self):
        """本来の相手 (連番の id) が崩れているだけなら、今までどおり直し方を言う."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "s.tsv")
            with open(tsv, "w", encoding="utf-8-sig", newline="\n") as fh:
                fh.write("id\toffset\tsize\toriginal\ttranslation\n")
                fh.write("5\t0x20\t12\tあ\tあ\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "insert_text.py"),
                 tsv, "-o", os.path.join(tmp, "out.bin"), "--encoding", "sjis"],
                capture_output=True, text=True)
            out = res.stdout + res.stderr
            self.assertNotEqual(res.returncode, 0, out)
            self.assertIn("0 から連番で", out, out)
            self.assertNotIn("boku2.py text", out, out)


class TestProofread(unittest.TestCase):
    def setUp(self):
        self.rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")
        self.glossary = proofread.load_glossary(os.path.join(REPO, "data", "glossary.tsv"))
        self.font_chars = set(FIX.glyph_order)

    def check(self, original: str, translation: str):
        row = {"id": "0", "original": original, "translation": translation}
        return proofread.check_row(row, self.rules, self.glossary, self.font_chars)

    def rules_hit(self, original: str, translation: str) -> set[str]:
        return {f.rule for f in self.check(original, translation)}

    def test_the_width_limit_is_measured_against_the_real_text(self):
        """幅の上限が、**原文が一度も使っていない広さ**なら、そう言うこと (#180).

        `data/rules.json` の `line_max_width` は、その注記どおり
        「開発元から渡される仕様」の欄です。**僕の夏休み 2 については、その仕様が
        ありません。** 既定の 18 は練習用の作品 (リィンフォルト戦記) の数字が
        そのまま入っているだけでした。

        公開ソースに残る実物の日本語を測ると、いちばん長い行でも **13.5 文字分**
        しかありません (`diaries.txt` 825 行 / `sumo_script.txt` 182 行 /
        `bug_info.txt` 23 行)。**原文が一度も使っていない幅を上限にしても、この検査は
        何も捕まえません。** 16 文字の訳文を書いても素通りし、実機で枠から出ます。

        数字を当てずっぽうで変えるのは筋が悪い (枠の幅は実物でしか決まらない) ので、
        **測って見せる**ことと、`--line-width` で試せることの 2 つにしました。
        """
        import subprocess
        import tempfile

        def run(rows, *extra):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "a.tsv")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("id\toriginal\ttranslation\n")
                    for i, (o, t) in enumerate(rows):
                        fh.write(f"{i}\t{o}\t{t}\n")
                res = subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                     path, "--no-font-check", *extra], capture_output=True, text=True)
                return res.returncode, res.stdout + res.stderr

        limit = float(self.rules.get("line_max_width", 0))
        self.assertGreater(limit, 0, "上限が設定されていない")

        # 1. 原文が上限より狭ければ、測った数と「一度も使っていない」ことを言う
        rc, out = run([("みじかい", "みじかい")])
        self.assertIn("原文の 1 行の幅", out, f"測って見せていない:\n{out}")
        self.assertIn(f"上限は {limit:g}", out, "上限を出していない")
        self.assertIn("一度も使っていない幅です", out, "上限が広すぎることを言っていない")

        # 2. 原文が上限いっぱいなら、余計なことを言わない
        wide = "あ" * int(limit)
        rc, out = run([(wide, wide)])
        self.assertIn("原文の 1 行の幅", out, "測って見せていない")
        self.assertNotIn("一度も使っていない幅です", out,
                         f"原文が上限まで使っているのに文句を言っている:\n{out}")

        # 3. --line-width で上限を下げると、その場で捕まえること
        rc, out = run([("みじかい", "あ" * 14)], "--line-width", "13")
        self.assertEqual(rc, 1, f"上限 13 で 14 文字が通っている:\n{out}")
        self.assertIn("上限 13", out, f"打ち込んだ上限を使っていない:\n{out}")

        # 4. 実物の裏付け。**日本語の原文**と、**向こうが枠に収めた英訳**を分けて測る。
        #    #180 で最初に測ったとき、`diaries.txt` などを「実物の日本語」と書いたが、
        #    あれは **Hilltop の英訳**だった (中を見ずに数だけ取った)。結論
        #    (18 は広すぎる) は変わらなかったが、**根拠の中身が違っていた**ので、
        #    ここで言語ごとに分けて、取り違えたら落ちるようにする
        if os.path.isdir(PUBLIC_SRC):
            import unicodedata

            def widths(name):
                path = os.path.join(PUBLIC_SRC, name)
                if not os.path.isfile(path):
                    return []
                out = []
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = re.sub(r"\{[^}]*\}|<[^>]*>|^///.*|^&\d+", "", line).strip()
                        if line:
                            out.append((line, sum(
                                1.0 if unicodedata.east_asian_width(c) in "WFA" else 0.5
                                for c in line)))
                return out

            def is_jp(s):
                return any("\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" for c in s)

            # 日本語の原文が残っているのは test.txt だけ (会話の抜粋)
            jp = widths("test.txt")
            self.assertGreater(len(jp), 50, f"日本語の行を {len(jp)} 行しか拾えない")
            self.assertTrue(all(is_jp(t) for t, _ in jp),
                            "test.txt に日本語でない行が混ざっている (別のファイルになった)")
            jp_top = max(w for _, w in jp)
            self.assertLess(jp_top, limit,
                            f"日本語の原文が {jp_top} 文字分まで使っている。"
                            f"上限 {limit:g} は広すぎるという読みが崩れた")

            # 向こうが同じ枠に収めた英訳。**日本語ではない**ことを確かめてから使う
            en = [x for name in ("diaries.txt", "sumo_script.txt", "bug_info.txt")
                  for x in widths(name)]
            self.assertGreater(len(en), 500, f"英訳の行を {len(en)} 行しか拾えない")
            self.assertLess(sum(1 for t, _ in en if is_jp(t)), len(en) * 0.05,
                            "英訳のつもりのファイルが日本語だった (中を見ずに数えている)")
            en_top = max(w for _, w in en)
            self.assertLess(en_top, limit,
                            f"枠に収めた英訳が {en_top} 文字分まで使っている。"
                            f"上限 {limit:g} は広すぎるという読みが崩れた")

    def test_the_page_height_is_not_one_number_for_the_whole_game(self):
        """**枠は 1 種類ではない** —— 行数で総崩れしたら、そう言うこと (#181).

        `lines_max: 3` も `line_max_width` と同じで、練習用の作品の数字です。
        公開ソースで測ると、Hilltop が実機に収めた**日記**は 111 ページで
        **5〜9 行** —— `lines_max: 3` を当てると全ページが ERROR になります。
        日記の頁と会話の枠は別物なので、これは訳文の誤りではありません。

        社長が日記の文を混ぜて `proofread.py` にかけると、**本物の誤りが
        大量の偽 ERROR に埋もれます**。数字を勝手に変えるのではなく、
        (1) 大半が行数で落ちたら「枠は 1 種類ではない」と言い、
        (2) `--lines-max` で枠ごとに見られるようにしました。
        """
        import subprocess
        import tempfile

        def run(body, *extra):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "a.tsv")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(f"id\toriginal\ttranslation\n1\t{body}\t{body}\n")
                res = subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                     path, "--no-font-check", *extra], capture_output=True, text=True)
                return res.returncode, res.stdout + res.stderr

        lines_max = int(self.rules.get("lines_max", 0))
        self.assertGreater(lines_max, 0, "行数の上限が設定されていない")
        tall = "<BR>".join("あいうえおかきく"[:lines_max + 2])

        # 1. 既定では落ちて、**枠が 1 種類でないこと**を言う
        rc, out = run(tall)
        self.assertEqual(rc, 1, f"上限を超えているのに通っている:\n{out}")
        self.assertIn("line_count", out, "行数で引っかかっていない")
        self.assertIn("枠は 1 種類ではありません", out, f"枠の違いを言っていない:\n{out}")

        # 2. --lines-max を上げれば通り、**余計な注意も出ない**
        rc, out = run(tall, "--lines-max", str(lines_max + 5))
        self.assertEqual(rc, 0, f"上限を上げたのに落ちている:\n{out}")
        self.assertNotIn("枠は 1 種類ではありません", out,
                         f"落ちていないのに注意を出している:\n{out}")

        # 3. 実物の裏付け: 公開ソースの日記は 1 ページに 3 行では収まらない
        if os.path.isdir(PUBLIC_SRC):
            path = os.path.join(PUBLIC_SRC, "diaries.txt")
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
                counts = []
                for page in re.split(r"^///$", raw, flags=re.M):
                    got = [ln for ln in page.split("\n")
                           if ln.strip() and not re.match(r"^&\d+$", ln.strip())]
                    if got:
                        counts.append(len(got))
                self.assertGreater(len(counts), 50, f"{len(counts)} ページしか測れていない")
                self.assertGreater(max(counts), lines_max,
                                   f"日記が最大 {max(counts)} 行。上限 {lines_max} で"
                                   "収まってしまうなら、枠が違うという読みが崩れた")

    def test_the_practice_font_table_does_not_condemn_real_text(self):
        """**練習用の文字表のまま実物にかけると、正しい原文が大量に ERROR になる** (#182).

        #180 の幅、#181 の行数と同じで、`data/font_chars.txt` も練習用の作品の
        ものです。ところがこれは前の 2 つより悪く、**偽の ERROR を出します**。

        練習用は 347 字、僕の夏休み 2 は 1656 字。公開ソースの会話 (`test.txt`) を
        20 行かけると「フォントに無い文字」が **14 件** —— 全部まちがいです。
        社長はこれを見て正しい原文を直しにかかるか、道具を信じなくなります。

        下のほうに「この文字表は練習用のものです」とは書いてありましたが、
        **22 件の ERROR に埋もれていました**。数えて、原因を名指しします。
        """
        import subprocess
        import tempfile

        if not os.path.isdir(PUBLIC_SRC):
            self.skipTest(f"公開ソースが無い ({PUBLIC_SRC})")
        src = os.path.join(PUBLIC_SRC, "test.txt")
        table = os.path.join(PUBLIC_SRC, "font.txt")
        for path in (src, table):
            if not os.path.isfile(path):
                self.skipTest(f"公開ソースに {os.path.basename(path)} が無い")

        with open(src, encoding="utf-8", errors="replace") as fh:
            lines = [re.sub(r"\{[^}]*\}|^///.*|^&\d+", "", ln).strip() for ln in fh]
        lines = [ln for ln in lines if ln][:20]
        self.assertGreaterEqual(len(lines), 20, f"{len(lines)} 行しか取れない")

        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "a.tsv")
            with open(tsv, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                for i, ln in enumerate(lines):
                    fh.write(f"{i}\t{ln}\t{ln}\n")

            def run(*extra):
                res = subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv, *extra],
                    capture_output=True, text=True)
                out = res.stdout + res.stderr
                return out, out.count("フォントに無い文字です")

            # 1. 既定 (練習用) では大量に落ち、**原因を名指しする**
            out, n_default = run()
            self.assertGreater(n_default, 5,
                               f"練習用の文字表で {n_default} 件しか落ちない (前提が崩れた)")
            self.assertTrue("文字表のせい" in out,
                            f"原因が文字表だと言っていない:\n{out[-600:]}")
            # **下にもとからある案内にも `--font-chars` は出る**ので、
            # 「含むか」では見ない。docs/10 で作る名前まで言っていること
            self.assertTrue("--font-chars font_chars.txt" in out,
                            f"この作品の文字表の渡し方を具体的に言っていない:\n{out[-500:]}")

            # 2. **この作品の文字表を渡せば、ぐっと減る** (減らなければ読みが崩れている)
            out, n_real = run("--font-chars", table)
            self.assertLess(n_real, n_default / 2,
                            f"実物の文字表でも {n_real} 件 (練習用は {n_default} 件)。"
                            "文字表のせいだという読みが崩れた")
            self.assertFalse("文字表のせい" in out,
                             f"この作品の文字表を渡したのに文字表のせいにしている:\n{out[-400:]}")

    def test_ansi_saving_of_the_translation_is_named_as_the_cause(self):
        """**訳文の側も、ANSI 保存で字が消える。それを名指しすること** (#200).

        #199 で文字表について直しました。同じことが**訳文**にも起きます ——
        `all.tsv` を Excel やメモ帳の「ANSI」で保存すると、cp932 に無い字
        (`¥` `—` `♡` `︙`) がその場で `?` に変わります。`?` は半角なので
        校正では「半角文字が混ざっています」と出て、**訳文の書き方の問題に
        見えます**。実際には保存の仕方の問題で、訳文を直しても直りません。

        見分けは 2 通り。ファイルが cp932 で保存されているか、原文には
        cp932 で書けない字があるのに訳文はその場が `?` になっているか。
        """
        import subprocess
        import tempfile

        def run(path):
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                 path, "--no-font-check"], capture_output=True, text=True)
            return res.stdout + res.stderr

        with tempfile.TemporaryDirectory() as tmp:
            # 1. **原文は無事、訳文だけ潰れている** (別の所から貼ったとき)
            one = os.path.join(tmp, "one.tsv")
            with open(one, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                fh.write("r0\tぼくの♡なつやすみ\tぼくの?なつやすみ\n")
            out = run(one)
            note = next((ln for ln in out.splitlines()
                         if ln.startswith("注意:") and "`?`" in ln), "")
            self.assertTrue(note, f"訳文が潰れていると言っていない:\n{out[-600:]}")
            self.assertTrue("訳文の書き方の問題ではありません" in out,
                            f"訳文のせいではないと言っていない:\n{out[-600:]}")
            self.assertTrue("UTF-8" in out, f"直し方を言っていない:\n{out[-600:]}")

            # 2. **ファイルごと ANSI** (このときは原文も潰れているので比べられない)
            two = os.path.join(tmp, "two.tsv")
            with open(two, "wb") as fh:
                fh.write("id\toriginal\ttranslation\n"
                         "r0\tかわで?をとる\tかわで?をとる\n".encode("cp932"))
            out = run(two)
            self.assertTrue("ANSI (cp932) で保存" in out,
                            f"保存形式そのものを言っていない:\n{out[-600:]}")

            # 3. **無事な TSV では黙る** (毎回出たら誰も読まなくなる)
            ok = os.path.join(tmp, "ok.tsv")
            with open(ok, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                fh.write("r0\tぼくの♡なつやすみ\tぼくの♡なつやすみ\n")
            out = run(ok)
            # **2 通りのどちらでも出る言葉**で見る。"ANSI" だけだと、
            # 訳文の比較で出る側 (「原文にある字が訳文では ?」) を見落とす
            self.assertFalse("保存した瞬間に" in out,
                             f"無事な TSV に文句を言っている:\n{out[-600:]}")

            # 4. 半角の `?` があっても、**原文に消える字が無ければ**言わない
            #    (`?` を普通に使っている訳文を、保存のせいにしない)
            plain = os.path.join(tmp, "plain.tsv")
            with open(plain, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                fh.write("r0\tなにをしているの\tなにしてる?\n")
            out = run(plain)
            self.assertFalse("保存した瞬間に" in out,
                             f"ただの ? を保存のせいにしている:\n{out[-600:]}")

    def test_a_wall_of_findings_does_not_bury_the_summary(self):
        """**実物並みの行数で、締めの行と注意が指摘に埋もれないこと** (#194).

        #182 で「原因を名指しする注意が 22 件の ERROR に埋もれていた」を直しました。
        ところが実物並み (12000 行) を練習用の文字表のままかけると、指摘は 9509 件、
        画面に出る行は **47,565 行 (2 MB)**。名指しの注意はその**いちばん下**に出ます。
        端末の巻き戻しから落ちれば、社長が見るのは ERROR の壁だけ。
        #193 と同じ「実物の大きさで通っていない」型です。

        最初の何行かを見れば調子は分かるので、残りは**数と内訳**で言い、
        全部見たい人は `--report` の TSV に回します。
        """
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "big.tsv")
            rows = 600
            with open(tsv, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                for i in range(rows):
                    fh.write(f"r{i}\t夏休みの虫取り\t夏休みの虫取り\n")   # 練習用の表に無い漢字
            report = os.path.join(tmp, "rep.tsv")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv,
                 "--report", report], capture_output=True, text=True)
            out = res.stdout + res.stderr
            lines = out.splitlines()

            # 1. **画面が壁にならない。** 行数が指摘の数に比例して増えない
            self.assertLess(len(lines), 150,
                            f"{rows} 行で画面が {len(lines)} 行になった (最初の数行だけ出すこと)")
            # 2. **切ったことを言う** (黙って隠すと、見たものが全部だと思われる)
            self.assertTrue("ほか" in out and "画面に出したのは最初の" in out,
                            f"残りがあることを言っていない:\n{out[-500:]}")
            self.assertTrue("--report" in out, f"全部の見方を言っていない:\n{out[-500:]}")
            # 3. **内訳を言う** (どの検査で落ちているかが分かれば、次の手が決まる)
            self.assertTrue("指摘の内訳" in out and "font" in out,
                            f"内訳が出ていない:\n{out[-500:]}")
            # 4. **締めと注意は残る。** ここが今回の本題
            self.assertTrue(f"{rows} 行をチェック" in out, f"締めの行が無い:\n{out[-500:]}")
            self.assertTrue("文字表のせい" in out, f"原因の名指しが消えた:\n{out[-500:]}")
            # 5. **捨てていない。** --report には全部入っている
            with open(report, encoding="utf-8-sig") as fh:
                got = [ln for ln in fh.read().splitlines() if ln.strip()]
            self.assertGreater(len(got) - 1, 150,
                               f"--report が {len(got) - 1} 件しか書いていない")

            # 少ない行数のときは、今までどおり全部出す (切るのは多いときだけ)
            small = os.path.join(tmp, "small.tsv")
            with open(small, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                for i in range(3):
                    fh.write(f"r{i}\t夏休みの虫取り\t夏休みの虫取り\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"), small],
                capture_output=True, text=True)
            self.assertFalse("画面に出したのは最初の" in res.stdout + res.stderr,
                             "3 行しかないのに切ったと言っている")

    def test_text_that_is_still_glyph_numbers_is_named_as_such(self):
        """**文字表を付けずに取り出した TSV を、そうと名指しすること** (#188).

        `boku2.py text` に `-f font.txt` を付け忘れると、原文が
        `[21][79][14]` のような番号のまま出ます。取り出す側は
        「文字表なし: 番号のまま」と 1 行言いますが、**それはすぐ流れます**。
        校正にかけると、`[` と数字が半角なので **全行に halfwidth の ERROR**
        が出て、本当の原因 (文字表を渡していない) は埋もれていました。

        一部だけ番号なら原因は別で、**文字表がその番号まで届いていない**
        (この作品の文字表は 1656 字あり、画像 1 枚では 1058 字分しか入らない。#167)。
        言うことが変わるので、2 つを見分けます。
        """
        import subprocess
        import tempfile

        def run(rows):
            with tempfile.TemporaryDirectory() as tmp:
                tsv = os.path.join(tmp, "a.tsv")
                with open(tsv, "w", encoding="utf-8") as fh:
                    fh.write("id\toriginal\ttranslation\n")
                    for i, text in enumerate(rows):
                        fh.write(f"{i}\t{text}\t{text}\n")
                res = subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                     tsv, "--no-font-check"], capture_output=True, text=True)
                return res.stdout + res.stderr

        # 1. ほとんどが番号 → **取り出し直し**を言う
        out = run([f"[{n}][{n + 1}][{n + 2}]" for n in range(10)])
        self.assertTrue("文字表なしで取り出したもの" in out,
                        f"番号だらけの TSV をそうと言っていない:\n{out[-600:]}")
        self.assertTrue("-f font.txt" in out,
                        f"取り出し直し方を言っていない:\n{out[-600:]}")

        # 2. 一部だけ番号 → **文字表が届いていない**と言う (取り出し直しではない)
        out = run(["ボクの夏休み"] * 9 + ["ボクの[900]休み"])
        notice = next((ln for ln in out.splitlines() if "届いていない番号" in ln), "")
        self.assertTrue(notice, f"残った番号を指摘していない:\n{out[-600:]}")
        # **その行の中に**番号があること。出力全体で探すと、指摘の
        # 「原文: ボクの[900]休み」で通ってしまう (「全文から語句を探さない」5 度目)
        self.assertTrue("[900]" in notice, f"どの番号かを言っていない: {notice}")
        self.assertFalse("文字表なしで取り出したもの" in out,
                         f"一部なのに取り出し直しを勧めている:\n{out[-600:]}")

        # 3. ちゃんと読めている TSV では、どちらも言わない (黙る)
        out = run(["ボクの夏休み", "きょうはいい天気だ"])
        self.assertFalse("文字番号のまま" in out, f"読めている TSV に注意を出した:\n{out[-600:]}")
        self.assertFalse("届いていない番号" in out, f"読めている TSV に注意を出した:\n{out[-600:]}")

    def test_every_file_option_refuses_a_path_that_is_not_there(self):
        """**ファイルを指す指定は、どれも「渡したのに無い」を断ること** (#187).

        #186 で `--font-chars` だけ直しましたが、同じ書き方
        (`os.path.exists` で黙って飛ばす) が `--glossary` と `--names` にも
        残っていました。用語集は実害があります —— 打ち間違えると
        **用語の検査が 1 件も出ないまま「ERROR 0 件」**になり、
        「いま効いている検査」には `glossary` が載ったままでした。
        (`--names` は話し手の名前を出すだけなので、失うのは表示。
        `--rules` は前から断っていました。)

        1 つずつ書くと、次に指定が増えたときまた漏れます。道具側の
        `FILE_OPTIONS` の並びをそのまま回るので、**足したら自動で見張られます**。
        """
        import importlib.util
        import subprocess
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "proofread_opts", os.path.join(REPO, "tools", "proofread.py"))
        pr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pr)
        self.assertGreaterEqual(len(pr.FILE_OPTIONS), 4,
                                f"ファイルを指す指定が {len(pr.FILE_OPTIONS)} 個しかない")

        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "a.tsv")
            with open(tsv, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n0\tボク\tボク\n")
            gone = os.path.join(tmp, "nope.dat")

            for attr, flag, label, default, _how in pr.FILE_OPTIONS:
                res = subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                     tsv, flag, gone],
                    capture_output=True, text=True)
                out = res.stdout + res.stderr
                self.assertEqual(res.returncode, 1,
                                 f"{flag} に無い道を渡したのに通した:\n{out[-300:]}")
                self.assertTrue(f"→ {label} " in out,
                                f"{flag} の断りが呼び名 ({label}) を言っていない:\n{out[-300:]}")
                self.assertTrue(gone in out,
                                f"{flag} の断りが渡された道を言っていない:\n{out[-300:]}")
                # 既定の置き場が本当にその名前であること (並びが古くなっていないか)
                self.assertTrue(os.path.basename(default),
                                f"{flag} の既定が空です")

            # 用語集が空なら、**効いている検査の一覧から glossary を外す**
            empty = os.path.join(tmp, "empty.tsv")
            with open(empty, "w", encoding="utf-8") as fh:
                fh.write("term\tforbidden\tnote\n")
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "proofread.py"),
                 tsv, "--glossary", empty], capture_output=True, text=True)
            out = res.stdout + res.stderr
            self.assertTrue("用語集なし" in out,
                            f"「使った設定」が用語集の欄を黙って省いている:\n{out[-400:]}")
            working = re.search(r"いま効いているのは、訳文だけを見て分かる検査 \(([^)]*)\)", out)
            self.assertTrue(working, f"効いている検査の一覧が出ていない:\n{out[-400:]}")
            self.assertFalse("glossary" in working.group(1).split(" / "),
                             f"止まっている glossary を効いている側に入れている: {working.group(1)}")

    def test_a_font_table_that_is_not_there_is_refused(self):
        """**渡した文字表が見つからないときは断ること** (#186).

        docs/10 の手順は `--font-chars font_chars.txt` と打たせますが、その前に
        `fontlist` で作る必要があります。作り忘れたまま打つと、道具は
        `os.path.exists` で**黙って飛ばして**いました。出るのは「ERROR 0 件」と、
        `font` を含む「いま効いている検査」の一覧。**実機で □ になる字の検査だけが
        止まっているのに、動いていると読める。** #123 で 0 字は断るようにしたのに、
        「無い」はその手前をすり抜けていました。

        既定 (`data/font_chars.txt`) が無いときまで断ると別の作品で使えなくなるので、
        **自分で渡したときだけ**断ります。
        """
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "a.tsv")
            with open(tsv, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n0\tボク\tボク\n")

            def run(*extra):
                res = subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv, *extra],
                    capture_output=True, text=True)
                return res.returncode, res.stdout + res.stderr

            code, out = run("--font-chars", os.path.join(tmp, "nope.txt"))
            self.assertEqual(code, 1, f"無い文字表を渡したのに通した:\n{out[-400:]}")
            self.assertTrue("がありません" in out, f"理由を言っていない:\n{out[-400:]}")
            self.assertTrue("fontlist" in out, f"作り方を言っていない:\n{out[-400:]}")

            # 既定のままなら、今までどおり通る (別の作品で使えなくならないこと)
            code, out = run()
            self.assertEqual(code, 0, f"既定のままで断っている:\n{out[-400:]}")

            # 検査が止まっているときは、**止まっていると言う**。黙って省かない
            code, out = run("--no-font-check")
            self.assertEqual(code, 0, out[-200:])
            self.assertTrue("文字表なし" in out,
                            f"「使った設定」が文字表の欄を黙って省いている:\n{out[-400:]}")
            working = re.search(r"いま効いているのは、訳文だけを見て分かる検査 \(([^)]*)\)", out)
            self.assertTrue(working, f"効いている検査の一覧が出ていない:\n{out[-400:]}")
            self.assertFalse("font" in working.group(1).split(" / "),
                             f"止まっている font を効いている側に入れている: {working.group(1)}")
            # 動いているときは、ちゃんと名前が載ること (0 件で緑にしない)
            code, out = run()
            working = re.search(r"いま効いているのは、訳文だけを見て分かる検査 \(([^)]*)\)", out)
            self.assertTrue(working and "font" in working.group(1).split(" / "),
                            f"動いている font が一覧に無い: {working and working.group(1)}")

    def test_the_two_handbooks_recommend_the_same_proofreading(self):
        """**docs/09 の手順 5 と docs/10 の「3. 校正にかける」が、同じ形を勧めること** (#186).

        docs/09 の「次の一手」手順 5 は長いあいだ
        `proofread.py そのファイル --no-font-check` でした。#96 の回に
        「画面は `--font-chars`、docs/09 は `--no-font-check`。場面が違うだけ」と
        判断して残したのですが、その判断は **#182 より前**のものです。#182 で
        道具が「練習用の文字表のせいだ」と名指しするようになった今、
        手順 3 で文字表を作った直後の手順 5 が検査を切る理由はありません。

        ここでは 2 つ見ます:

        * **切ると何が止まるのか** —— 実物の文字表に無い字を訳文に入れて、
          `--font-chars` は捕まえ、`--no-font-check` は黙ることを実際に走らせて確かめる
        * **2 冊が同じ形を勧めているか** —— 同じ作業を違うコマンドで案内していないこと
        """
        import subprocess
        import tempfile

        if not os.path.isdir(PUBLIC_SRC):
            self.skipTest(f"公開ソースが無い ({PUBLIC_SRC})")
        table = os.path.join(PUBLIC_SRC, "font.txt")
        if not os.path.isfile(table):
            self.skipTest("公開ソースに font.txt が無い")

        with open(table, encoding="utf-8", errors="replace") as fh:
            in_font = set(fh.read()) - set(" \t\r\n")
        self.assertGreater(len(in_font), 1000, f"文字表が {len(in_font)} 字しか無い")
        # この作品のフォントに**無い**字を 1 つ選ぶ (決め打ちにすると表が変わったとき黙る)
        missing = next((c for c in "彁鰆躑靉纐咏鴫聢" if c not in in_font), None)
        self.assertTrue(missing, "文字表に無い字が候補の中に見つからない")
        base = "".join(c for c in "ボクの夏休み" if c in in_font)
        self.assertGreaterEqual(len(base), 4, "原文に使える字が足りない")

        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "a.tsv")
            with open(tsv, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                fh.write(f"0\t{base}\t{base}{missing}\n")

            def run(*extra):
                res = subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv, *extra],
                    capture_output=True, text=True)
                return res.stdout + res.stderr

            caught = run("--font-chars", table)
            self.assertTrue("フォントに無い文字" in caught,
                            f"実物の文字表を渡しても「{missing}」を捕まえない:\n{caught[-400:]}")
            blind = run("--no-font-check")
            self.assertFalse("フォントに無い文字" in blind,
                             f"--no-font-check なのにフォント検査が動いている:\n{blind[-400:]}")
            # **黙るだけ**なのが怖い所。ほかは同じように通ってしまう
            self.assertTrue("問題なし" in blind or "ERROR 0 件" in blind,
                            f"--no-font-check で他の検査まで止まっている:\n{blind[-400:]}")

        # 2 冊が勧める形。節に絞って読む (全文だと説明や記録欄の引用に当たる)
        def options(doc_path, head, end="\n## "):
            with open(os.path.join(REPO, "docs", doc_path), encoding="utf-8") as fh:
                doc = fh.read()
            self.assertTrue(head in doc, f"{doc_path} に「{head}」がありません")
            body = doc.split(head, 1)[1].split(end, 1)[0]
            cmds = re.findall(r"python3 tools/proofread\.py[^\n`]*", body)
            self.assertTrue(cmds, f"{doc_path} の「{head}」に proofread のコマンドがありません")
            return [sorted(t for t in c.split() if t.startswith("--")) for c in cmds]

        nine = options("09-調査ログと引き継ぎ.md", "## 次の一手 (優先順)")
        ten = options("10-僕夏2の手順.md", "## 3. 校正にかける")
        self.assertEqual(nine[0], ten[0],
                         f"2 冊が違う形を勧めています\n  docs/09: {nine[0]}\n  docs/10: {ten[0]}")
        self.assertTrue("--font-chars" in nine[0],
                        f"docs/09 の手順が文字表を渡していません: {nine[0]}")
        # 逃げ道として `--no-font-check` に触れるのは構わないが、**何が止まるか**を書くこと
        with open(os.path.join(REPO, "docs", "09-調査ログと引き継ぎ.md"), encoding="utf-8") as fh:
            steps = fh.read().split("## 次の一手 (優先順)", 1)[1].split("\n## ", 1)[0]
        if "--no-font-check" in steps:
            self.assertTrue("止まります" in steps or "止まる" in steps,
                            "--no-font-check の逃げ道に、何が止まるのかが書かれていません")

    def test_a_changed_number_is_caught(self):
        """数字の取り違えは、字数も用語も禁則も通ってしまう (#86)."""
        hit = self.rules_hit("報酬は<COLOR:02>８５０<COLOR:00>ギルだ。",
                             "報酬は<COLOR:02>８５<COLOR:00>ギルだ。")
        self.assertIn("number", hit)
        # 数字以外では引っかかっていないこと (この訳文は他の検査を全部通る)
        self.assertEqual({"number"}, hit, hit)

    def test_the_same_number_written_differently_is_not_a_number_error(self):
        """全角と半角は同じ数として見る (表記の揺れは halfwidth の担当)."""
        hit = self.rules_hit("８５０ギル", "850ギル")
        self.assertNotIn("number", hit)
        self.assertIn("halfwidth", hit, "半角の指摘まで消えている")

    def test_numbers_inside_tags_are_not_counted(self):
        """<COLOR:02> や <VAR:00> の数字は書式であって本文ではない."""
        self.assertNotIn("number", self.rules_hit("<COLOR:02>赤<COLOR:00>い",
                                                  "<COLOR:03>赤<COLOR:00>い"))

    def test_a_row_with_no_numbers_is_quiet(self):
        self.assertNotIn("number", self.rules_hit("こんにちは。", "こんばんは。"))

    def test_the_same_original_translated_differently_is_caught(self):
        """訳ぶれは 1 行ずつ見ても絶対に分からない (#86)."""
        rows = [{"id": "5", "original": "いいえ", "translation": "いいえ", "_lineno": 2},
                {"id": "38", "original": "いいえ", "translation": "いえ", "_lineno": 3},
                {"id": "4", "original": "はい", "translation": "はい", "_lineno": 4}]
        found = proofread.check_consistency(rows, lambda rid: None)
        self.assertEqual(["38"], [f.row_id for f in found], [f.message for f in found])
        self.assertEqual("consistency", found[0].rule)
        self.assertEqual("WARN", found[0].severity, "訳し分けが正しい場合もあるので WARN")
        # 1 行ずつの検査では出ないことも確かめる (出るなら consistency は要らない)
        for row in rows:
            self.assertNotIn("consistency",
                             {f.rule for f in proofread.check_row(
                                 row, self.rules, self.glossary, self.font_chars)})

    def test_consistent_rows_are_quiet(self):
        rows = [{"id": "5", "original": "いいえ", "translation": "いいえ", "_lineno": 2},
                {"id": "38", "original": "いいえ", "translation": "いいえ", "_lineno": 3}]
        self.assertEqual([], proofread.check_consistency(rows, lambda rid: None))

    def test_a_substituted_name_widens_the_line(self):
        """<VAR:00> に入る名前の長さを数えられること (課題 7, #88)."""
        line = "<NAME:01><VAR:00>！　こんなところにいたのね。"
        self.assertEqual(14.0, scrp.display_width(line))
        self.assertEqual(20.0, scrp.display_width(line, {"VAR": 6}),
                         "14 文字 + 名前 6 文字 = 20 にならない")
        # <NAME:xx> は枠の外に出るので既定では数えない (make_viewer がそう描いている)
        self.assertEqual(20.0, scrp.display_width(line, {"VAR": 6, "NAME": 0}))
        self.assertEqual(23.0, scrp.display_width(line, {"VAR": 6, "NAME": 3}),
                         "枠の中に名前を出す作りなら数えられること")

    def test_var_width_turns_a_passing_line_into_a_violation(self):
        row = {"id": "2", "original": "<VAR:00>！　こんなところにいたのね。",
               "translation": "<VAR:00>！　こんなところにいたのね。"}
        quiet = proofread.check_row(row, self.rules, self.glossary, self.font_chars)
        self.assertNotIn("line_width", {f.rule for f in quiet}, "幅 0 のとき指摘が出ている")
        loud = proofread.check_row(row, self.rules, self.glossary, self.font_chars,
                                   {"VAR": 6})
        hit = [f for f in loud if f.rule == "line_width"]
        self.assertEqual(1, len(hit), [f.message for f in loud])
        self.assertIn("20 文字分", hit[0].message)
        self.assertIn("差し込み 6", hit[0].detail, "文字と差し込みの内訳が出ていない")

    def test_var_width_comes_from_the_rules_or_the_flag(self):
        rules = dict(self.rules)
        self.assertEqual({}, proofread.tag_widths_of(rules), "既定は数えないこと")
        self.assertEqual({"VAR": 6.0}, proofread.tag_widths_of(rules, 6.0))
        rules["var_width"] = 4
        self.assertEqual({"VAR": 4.0}, proofread.tag_widths_of(rules))
        self.assertEqual({"VAR": 6.0}, proofread.tag_widths_of(rules, 6.0),
                         "旗が設定より優先されること")

    def test_the_master_text_itself_overflows_once_names_are_counted(self):
        """原文そのものが、名前の長さを考えずに書かれていること (#88).

        docs/04 で「原文の側にも 2 行の違反が出る」と言い切っているので測る。
        仕込んだ不具合ではなく、実際の案件でも起きる素の状態。
        """
        rows = scrp.read_tsv(os.path.join(REPO, "exercises", "qa_target.tsv"))
        over = set()
        for row in rows:
            base = dict(row)
            base["translation"] = row["original"]        # 原文だけを見る
            for finding in proofread.check_row(base, self.rules, self.glossary,
                                               self.font_chars, {"VAR": 6}):
                if finding.rule == "line_width":
                    over.add(row["id"])
        self.assertEqual({"2", "22"}, over, "docs/04 の「id 2 と id 22」と食い違う")

    def test_findings_follow_the_order_of_the_file(self):
        """指摘は TSV に出てくる順に並ぶこと (訳す人は表を上から順に直す).

        以前は id を整数として読もうとして必ず失敗し、severity と rule 名の順に
        並んでいた。最初の行の指摘が一番下に出るのに、落ちも警告もしなかった (#74)。
        """
        rows = [
            # 3 行目に ERROR、2 行目に WARN。並びは 2 行目が先になること
            {"id": "a:0", "original": "あ", "translation": "あ", "_lineno": 2},
            {"id": "a:1", "original": "い", "translation": "", "_lineno": 3},
            {"id": "a:2", "original": "う", "translation": "ﾊﾝｶｸ", "_lineno": 4},
            {"id": "a:3", "original": "え", "translation": "え", "_lineno": 5},
        ]
        findings = []
        for row in rows:
            findings += proofread.check_row(row, self.rules, self.glossary, self.font_chars)
        findings.sort(key=lambda f: f.sort_key())
        self.assertTrue(findings, "指摘が 1 件も出ていない (この検査が意味を持たない)")
        ids = [f.row_id for f in findings]
        self.assertEqual(ids, sorted(ids, key=lambda i: [r["id"] for r in rows].index(i)),
                         f"ファイルの順に並んでいない: {ids}")
        # id が整数でも壊れないこと (古い形の TSV)
        old = proofread.Finding("12", "WARN", "x", "m", "", 7)
        new = proofread.Finding("a:0", "ERROR", "x", "m", "", 3)
        self.assertLess(new.sort_key(), old.sort_key(), "行番号の順になっていない")
        # 行番号が無くても落ちないこと
        proofread.Finding("a:0", "WARN", "x", "m").sort_key()

    def test_original_script_is_clean(self):
        """原文そのものは 1 件も指摘が出ないこと (基準線)."""
        for i, text in enumerate(FIX.texts):
            findings = self.check(text, text)
            self.assertEqual(findings, [], f"id {i} で指摘が出ました: "
                             + "; ".join(f.message for f in findings))

    def test_placeholder_loss(self):
        self.assertIn("placeholder", self.rules_hit("<VAR:00>さん", "あなたさん"))

    def test_control_loss(self):
        self.assertIn("control", self.rules_hit("はい<WAIT>", "はい"))

    def test_line_rewrap_is_allowed(self):
        self.assertEqual(self.rules_hit("あい<BR>うえ", "あいうえ"), set())

    def test_line_width(self):
        long_line = "あ" * 19
        self.assertIn("line_width", self.rules_hit(long_line, long_line))

    def test_line_count(self):
        text = "あ<BR>い<BR>う<BR>え"
        self.assertIn("line_count", self.rules_hit(text, text))

    def test_kinsoku(self):
        self.assertIn("kinsoku", self.rules_hit("あい。<BR>うえ", "あい<BR>。うえ"))

    def test_halfwidth_and_font(self):
        hit = self.rules_hit("ありがとう", "ありがとう!")
        self.assertIn("halfwidth", hit)
        self.assertIn("font", self.rules_hit("宝箱", "薔薇"))

    def test_glossary_forbidden_and_dropped(self):
        self.assertIn("glossary", self.rules_hit("薬草を使う", "クスリ草を使う"))
        self.assertIn("glossary", self.rules_hit("薬草を使う", "それを使う"))

    def test_notation(self):
        self.assertIn("notation", self.rules_hit("……ね", "...ね"))
        self.assertIn("notation", self.rules_hit("また来てね", "また来てね〜"))

    def test_empty_and_untranslated(self):
        self.assertIn("empty", self.rules_hit("こんにちは<WAIT>", "<WAIT>"))
        self.assertIn("untranslated", self.rules_hit("こんにちは", ""))

    def test_the_exercise_file_matches_its_generator(self):
        """exercises/qa_target.tsv が plant_errors.py の出力と一致すること (#86).

        答えの一覧 (PLANTED) を直しても、練習データを作り直し忘れると両者がずれる。
        ずれても落ちないので気づけない。実際、リポジトリの版は BOM 無し、生成器の
        出力は BOM 付き (Excel 用) で、作り直すたびに差分が出る状態だった。
        """
        import plant_errors

        src = os.path.join(REPO, "work", "SCRIPT.tsv")
        problem = ensure_practice("work/SCRIPT.BIN", "make_sample.py")
        if problem:
            self.skipTest(problem)
        if not os.path.exists(src):
            import subprocess
            subprocess.run([sys.executable, os.path.join(REPO, "tools", "dump_text.py"),
                            os.path.join(REPO, "work", "SCRIPT.BIN"), "-o", src],
                           capture_output=True, cwd=REPO, check=True)
        rows = scrp.read_tsv(src)
        by_id = {int(row["id"]): row for row in rows}
        for rid, before, after, _why in plant_errors.PLANTED:
            row = by_id[rid]
            self.assertIn(before, row["translation"], f"id {rid}: 置換前が原文に無い")
            row["translation"] = row["translation"].replace(before, after, 1)
        with tempfile.TemporaryDirectory() as tmp:
            made = os.path.join(tmp, "qa_target.tsv")
            scrp.write_tsv(made, rows)
            with open(made, "rb") as fh:
                fresh = fh.read()
        with open(os.path.join(REPO, "exercises", "qa_target.tsv"), "rb") as fh:
            committed = fh.read()
        self.assertEqual(fresh, committed,
                         "exercises/qa_target.tsv が古いです "
                         "(python3 answers/plant_errors.py で作り直してください)")

    def test_planted_exercise_is_all_caught(self):
        """exercises/qa_target.tsv に仕込んだ行が、すべて検出されること."""
        import plant_errors

        target = os.path.join(REPO, "exercises", "qa_target.tsv")
        if not os.path.exists(target):
            self.skipTest("exercises/qa_target.tsv がありません")
        rows = scrp.read_tsv(target)
        flagged = set()
        for row in rows:
            if proofread.check_row(row, self.rules, self.glossary, self.font_chars):
                flagged.add(int(row["id"]))
        # 行をまたぐ検査 (訳ぶれ) はここでしか動かない。1 行ずつの検査だけで
        # 数えていると、仕込んだ訳ぶれを「見逃している」と誤って報告する (#86)
        for finding in proofread.check_consistency(rows, lambda rid: None):
            flagged.add(int(finding.row_id))
        planted = {rid for rid, *_ in plant_errors.PLANTED}
        self.assertEqual(planted - flagged, set(), "見逃している行があります")
        self.assertEqual(flagged - planted, set(), "仕込んでいない行を誤検出しています")


class TestViewer(unittest.TestCase):
    """メッセージウィンドウのシミュレータが正しいデータを埋め込むこと."""

    @classmethod
    def setUpClass(cls):
        import argparse
        import make_viewer

        cls.mv = make_viewer
        font = os.path.join(REPO, "work", "FONT.BIN")
        if not os.path.exists(font):
            raise unittest.SkipTest("work/FONT.BIN がありません (make_sample.py --font を実行)")
        cls.args = argparse.Namespace(
            tsv=os.path.join(REPO, "exercises", "qa_target.tsv"),
            binary=os.path.join(REPO, "work", "SCRIPT.BIN"),
            font=font,
            font_chars=os.path.join(REPO, "data", "font_chars.txt"),
            names=os.path.join(REPO, "data", "names.tsv"),
            rules=os.path.join(REPO, "data", "rules.json"),
            glossary=os.path.join(REPO, "data", "glossary.tsv"),
            lang="ja",
        )
        cls.data = make_viewer.build_data(cls.args)

    def run_viewer(self, *args):
        import subprocess

        res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "make_viewer.py"), *args],
                             capture_output=True, text=True, cwd=REPO)
        return res.stdout + res.stderr

    def test_another_games_text_is_not_measured_with_the_practice_font(self):
        """別の作品の TSV を渡したら、**練習用のフォントで測っている**と言うこと (#214).

        この画面の売りは「出ている字はゲームが持っているグリフそのもの」。
        ところがそのフォントは練習用の作品 (リィンフォルト戦記) の 333 字なので、
        僕の夏休み 2 の文章を入れると**漢字がほぼ全部 □ になり、`font` の ERROR が
        大量に出る**。どちらもその作品の話ではないのに、画面は同じ顔で出る。
        proofread.py が #89 で断っているのと同じことを、こちらは言っていなかった。
        """
        with tempfile.TemporaryDirectory() as tmp:
            other = os.path.join(tmp, "other.tsv")
            with open(other, "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
                fh.write("r0\t夏休みの虫取り\t夏休みの虫取り\n")     # 練習用の表に無い漢字
            out = self.run_viewer("--tsv", other, "-o", os.path.join(tmp, "v.html"))
            # **材料が弱くないこと**: この題材が実際に ERROR を生んでいること
            self.assertIn("訳文の ERROR 1 件", out, f"材料が弱い (ERROR が出ていない):\n{out}")
            self.assertIn("練習用の作品のフォント", out, f"断っていない:\n{out}")
            self.assertIn("その作品の話ではありません", out, f"何が嘘になるか言っていない:\n{out}")
            self.assertIn("proofread.py", out, f"代わりの見方を言っていない:\n{out}")

    def test_the_practice_material_does_not_get_the_warning(self):
        """**題材が練習用のときは言わない。** 毎回出る注意は読まれなくなる."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_viewer("-o", os.path.join(tmp, "v.html"))
            self.assertIn("メッセージ", out, f"そもそも動いていない:\n{out}")
            self.assertNotIn("練習用の作品のフォント", out,
                             f"練習用の題材なのに断っている:\n{out}")

    def test_glyph_data_matches_char_list(self):
        import base64

        g = self.data["glyphs"]
        raw = base64.b64decode(g["bytes"])
        self.assertEqual(len(raw), len(g["chars"]) * g["w"] * g["h"] // 8)
        self.assertEqual(g["chars"], "".join(FIX.glyph_order))

    def test_every_row_is_present(self):
        rows = scrp.read_tsv(self.args.tsv)
        self.assertEqual(len(self.data["messages"]), len(rows))
        self.assertEqual([m["id"] for m in self.data["messages"]], [r["id"] for r in rows])

    def test_original_is_clean_and_planted_rows_are_flagged(self):
        import plant_errors

        planted = {rid for rid, *_ in plant_errors.PLANTED}
        flagged = set()
        for m in self.data["messages"]:
            self.assertEqual(m["findings"]["original"], [], f"id {m['id']} の原文に指摘が出ました")
            if m["findings"]["translation"]:
                flagged.add(int(m["id"]))
        self.assertEqual(flagged, planted)

    def test_hex_matches_the_binary(self):
        archive = scrp.read_archive(self.args.binary)
        for m in self.data["messages"]:
            expected = archive.raw_block(int(m["id"])).hex(" ").upper()
            self.assertEqual(m["hex"], expected)

    def test_html_is_self_contained(self):
        import json as _json
        import re

        payload = _json.dumps(self.data, ensure_ascii=False,
                              separators=(",", ":")).replace("</", "<\\/")
        html = (self.mv.DOC_OPEN + self.mv.HTML_HEAD + self.mv.DOC_MID
                + self.mv.HTML_BODY.replace("/*__DATA__*/", payload) + self.mv.DOC_CLOSE)
        self.assertNotIn("/*__DATA__*/", html)
        # 外部リソースを読み込まないこと
        self.assertIsNone(re.search(r'(src|href)\s*=\s*"(?!#)[^"]', html))
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        # 埋め込んだ JSON が取り出せること
        m = re.search(r'<script id="viewer-data" type="application/json">(.*?)</script>',
                      html, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(len(_json.loads(m.group(1).replace("<\\/", "</"))["messages"]),
                         len(self.data["messages"]))


class TestIso(unittest.TestCase):
    """練習用ディスクイメージが ISO9660 として正しく読めること."""

    @classmethod
    def setUpClass(cls):
        import make_iso

        cls.make_iso = make_iso
        cls.path = os.path.join(REPO, "work", "RINFOLT.iso")
        if not os.path.exists(cls.path):
            raise unittest.SkipTest("work/RINFOLT.iso がありません (make_iso.py を実行)")
        with open(cls.path, "rb") as fh:
            cls.data = fh.read()

    def test_volume_descriptor(self):
        import struct

        pvd = self.data[16 * 2048:17 * 2048]
        self.assertEqual(pvd[0], 1)
        self.assertEqual(pvd[1:6], b"CD001")
        self.assertEqual(struct.unpack("<H", pvd[128:130])[0], 2048)
        self.assertEqual(struct.unpack("<I", pvd[80:84])[0], len(self.data) // 2048)
        self.assertEqual(pvd[40:72].decode("ascii").strip(), "RINFOLT_SENKI")

    def _walk(self):
        """PVD からディレクトリを辿って {パス: (オフセット, サイズ)} を返す."""
        import struct

        pvd = self.data[16 * 2048:17 * 2048]
        root = pvd[156:190]
        found = {}

        def walk(lba, length, prefix):
            sec = self.data[lba * 2048:lba * 2048 + length]
            i = 0
            while i < len(sec):
                rec_len = sec[i]
                if rec_len == 0:
                    break
                rec = sec[i:i + rec_len]
                extent = struct.unpack("<I", rec[2:6])[0]
                size = struct.unpack("<I", rec[10:14])[0]
                flags = rec[25]
                name = rec[33:33 + rec[32]]
                i += rec_len
                if name in (b"\x00", b"\x01"):
                    continue
                label = name.decode("ascii").split(";")[0]
                if flags & 0x02:
                    walk(extent, size, prefix + label + "/")
                else:
                    found[prefix + label] = (extent * 2048, size)

        walk(struct.unpack("<I", root[2:6])[0], struct.unpack("<I", root[10:14])[0], "/")
        return found

    def test_file_tree(self):
        found = self._walk()
        self.assertIn("/SYSTEM.CNF", found)
        for name in ["SCRIPT.BIN", "MSG_ENC.BIN", "FONT.BIN", "MOVIE.PSS", "BGM.ADP", "PAD.DAT"]:
            self.assertIn("/DATA/" + name, found, f"{name} が見つかりません")

    def test_embedded_files_match_the_originals(self):
        found = self._walk()
        for name in ["SCRIPT.BIN", "MSG_ENC.BIN", "FONT.BIN"]:
            offset, size = found["/DATA/" + name]
            with open(os.path.join(REPO, "work", name), "rb") as fh:
                original = fh.read()
            self.assertEqual(size, len(original))
            self.assertEqual(self.data[offset:offset + size], original)

    def test_synthetic_data_has_the_intended_character(self):
        """解析の練習になるよう、性質の違うデータが入っていること."""
        import math

        def entropy(b):
            hist = [0] * 256
            for v in b:
                hist[v] += 1
            return -sum((c / len(b)) * math.log2(c / len(b)) for c in hist if c)

        found = self._walk()
        movie_off, _ = found["/DATA/MOVIE.PSS"]
        bgm_off, _ = found["/DATA/BGM.ADP"]
        pad_off, pad_size = found["/DATA/PAD.DAT"]
        movie = self.data[movie_off:movie_off + 4096]
        bgm = self.data[bgm_off:bgm_off + 4096]
        self.assertGreater(entropy(movie), 7.8)                  # 圧縮相当
        mean_diff = sum(abs(bgm[i] - bgm[i - 1]) for i in range(1, len(bgm))) / (len(bgm) - 1)
        self.assertLess(mean_diff, 24)                           # 波形相当
        self.assertEqual(self.data[pad_off:pad_off + pad_size], b"\x00" * pad_size)


class TestWebBuild(unittest.TestCase):
    """構造探査台の 1 枚 HTML が自己完結していること."""

    @classmethod
    def setUpClass(cls):
        cls.webdir = os.path.join(REPO, "web")
        for name in ("index.html", "style.css", "app.js"):
            if not os.path.exists(os.path.join(cls.webdir, name)):
                raise unittest.SkipTest(f"web/{name} がありません")

    def _build(self, embed=None, fragment=False):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "explorer.html")
            cmd = [sys.executable, os.path.join(REPO, "tools", "build_web.py"), "-o", out]
            if embed:
                cmd += ["--embed-sample", embed]
            if fragment:
                cmd.append("--fragment")
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            with open(out, encoding="utf-8") as fh:
                return fh.read()

    def test_no_external_references(self):
        html = self._build()
        for bad in ("http://", "https://", 'src="app.js"', 'href="style.css"'):
            self.assertNotIn(bad, html)
        self.assertIn("<style>", html)
        self.assertIn("構造探査台", html)

    def test_fragment_has_no_document_tags(self):
        html = self._build(fragment=True)
        for bad in ("<!doctype", "<html", "<head>", "<body>"):
            self.assertNotIn(bad, html.lower())
        self.assertTrue(html.lstrip().startswith("<title>"))

    def test_sample_is_embedded_and_decodes(self):
        import base64
        import re

        iso = os.path.join(REPO, "work", "RINFOLT.iso")
        if not os.path.exists(iso):
            self.skipTest("work/RINFOLT.iso がありません")
        html = self._build(embed=iso)
        m = re.search(r'window\.SAMPLE_ISO = "([A-Za-z0-9+/=]+)"', html)
        self.assertIsNotNone(m)
        with open(iso, "rb") as fh:
            self.assertEqual(base64.b64decode(m.group(1)), fh.read())

    def test_javascript_has_no_unescaped_control_characters(self):
        """正規表現リテラルに生の制御文字が混ざると読み込み時に落ちる."""
        with open(os.path.join(self.webdir, "app.js"), encoding="utf-8") as fh:
            src = fh.read()
        for lineno, line in enumerate(src.split("\n"), 1):
            bad = [c for c in line if ord(c) < 0x20 and c != "\t"]
            self.assertEqual(bad, [], f"app.js:{lineno} に生の制御文字があります")

    def test_no_replacement_character_in_sources_or_build(self):
        """置換文字 (U+FFFD) と BOM (U+FEFF) が生で入っていないこと.

        置換文字は公開先に弾かれる (#25 で踏んだ)。**BOM のほうは #209 で踏んだ** ——
        「TSV の先頭に BOM を付ける」を書くつもりで、ソースに**生の BOM**が入った。
        生の BOM は目に見えず、ファイルの頭に来れば読み込みが壊れ、文字列の中なら
        幅ゼロの字が 1 つ増える。どちらも**見て気づけない**ので、書くときは必ず
        エスケープ (`\\ufeff`) にする。中身として BOM を持つのは
        `exercises/qa_target.tsv` だけで、あれは「BOM 付きの練習材料」そのもの
        (ここが見ているのはソースなので、はじめから入っていない)。
        """
        import glob
        # **道具と検査も見る** (#199)。ここは web/ と docs/ しか見ていなかったので、
        # `tools/boku2.py` に生の置換文字を書いても誰も気づかなかった
        # (見つけたのは手で数えたときだった)
        files = (glob.glob(os.path.join(self.webdir, "*"))
                 + glob.glob(os.path.join(REPO, "docs", "*.md"))
                 + glob.glob(os.path.join(REPO, "tools", "*.py"))
                 + glob.glob(os.path.join(REPO, "tests", "*.py"))
                 + glob.glob(os.path.join(REPO, "tests", "e2e", "*.py"))
                 + glob.glob(os.path.join(REPO, "tests", "*.mjs"))
                 + [os.path.join(REPO, "README.md")]
                 + glob.glob(os.path.join(REPO, "exercises", "*.md")))
        self.assertGreaterEqual(len(files), 40, f"見ているのが {len(files)} 個しかない")
        for path in files:
            if os.path.isdir(path):
                continue
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertTrue("\ufffd" not in text,
                            f"{os.path.relpath(path, REPO)} に生の置換文字があります")
            self.assertTrue("\ufeff" not in text,
                            f"{os.path.relpath(path, REPO)} に生の BOM があります "
                            "(見えないので、\\ufeff と書くこと)")
        built = self._build()
        self.assertNotIn("\ufffd", built, "組み立てた HTML に置換文字があります")
        self.assertNotIn("\ufeff", built, "組み立てた HTML に生の BOM があります")


class TestArchiveFixture(unittest.TestCase):
    """索引ファイル + データ本体の練習用ペアが正しく作られること."""

    @classmethod
    def setUpClass(cls):
        cls.idx = os.path.join(REPO, "work", "PACK.IDX")
        cls.img = os.path.join(REPO, "work", "PACK.IMG")
        problem = ensure_practice("work/PACK.IDX", "make_sample.py", "make_archive.py")
        if problem or not os.path.exists(cls.img):
            raise unittest.SkipTest(problem or "work/PACK.IMG がありません")

    def test_index_matches_the_body(self):
        import struct

        with open(self.idx, "rb") as fh:
            idx = fh.read()
        size = os.path.getsize(self.img)
        count, _ = struct.unpack_from("<II", idx, 0)
        self.assertEqual(len(idx), 8 + count * 16)
        with open(self.img, "rb") as fh:
            body = fh.read()
        prev_end = 0
        for i in range(count):
            lba, length, _kind, _hash = struct.unpack_from("<IIII", idx, 8 + i * 16)
            at = lba * 2048
            self.assertGreaterEqual(at, prev_end, f"#{i} の位置が前と重なっています")
            self.assertLessEqual(at + length, size, f"#{i} が本体をはみ出しています")
            prev_end = at + length
        # 1 件目は SCRP のはず
        first_lba, first_len = struct.unpack_from("<II", idx, 8)
        self.assertEqual(body[first_lba * 2048:first_lba * 2048 + 4], b"SCRP")
        self.assertEqual(first_len, os.path.getsize(os.path.join(REPO, "work", "SCRIPT.BIN")))


class TestIndexAnalyzer(unittest.TestCase):
    """ブラウザ側の索引推定を Node で動かして回帰を止める."""

    def test_analyzer_finds_the_true_layout(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        script = os.path.join(REPO, "tests", "test_index.mjs")
        problem = ensure_practice("work/PACK.IDX", "make_sample.py", "make_archive.py")
        if problem:
            self.skipTest(problem)
        res = subprocess.run([node, script], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


class TestDisassembler(unittest.TestCase):
    """MIPS の逆アセンブラを capstone の答えと突き合わせる.

    自分で書いた逆アセンブラの正しさは、自分では確かめられません。
    答えは tests/mips_cases.json に固めてあります (作り直すときは
    tests/gen_mips_cases.py)。
    """

    # capstone は MIPS32 として読むので、R5900 独自のオペコードは食い違って正しい
    R5900_ONLY = {0x1E, 0x1F, 0x36, 0x3E}

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import elfdump

        cls.elfdump = elfdump
        path = os.path.join(REPO, "tests", "mips_cases.json")
        if not os.path.exists(path):
            raise unittest.SkipTest("tests/mips_cases.json がありません")
        with open(path, encoding="utf-8") as fh:
            cls.golden = json.load(fh)

    def test_mnemonics_match_capstone(self):
        wrong = []
        matched = 0
        for word, want in self.golden["cases"]:
            mn, _ops = self.elfdump.decode(word, self.golden["addr"])
            if mn == want:
                matched += 1
            elif (word >> 26) in self.R5900_ONLY:
                pass                      # R5900 独自。食い違って正しい
            elif mn == ".word":
                pass                      # 知らない命令。嘘をつくよりましな態度
            else:
                wrong.append(f"0x{word:08X} capstone={want} こちら={mn}")
        self.assertFalse(wrong, "capstone と食い違う命令:\n  " + "\n  ".join(wrong[:20]))
        self.assertGreater(matched, len(self.golden["cases"]) * 0.8,
                           f"一致が {matched} 件しかありません")

    def test_reads_the_practice_boot_elf(self):
        path = os.path.join(REPO, "work", "BOOT.ELF")
        problem = ensure_practice("work/BOOT.ELF", "make_elf.py")
        if problem:
            self.skipTest(problem)
        with open(path, "rb") as fh:
            elf = self.elfdump.Elf(fh.read())
        self.assertEqual(elf.machine, 8)
        self.assertEqual(elf.entry, 0x00100000)
        self.assertEqual(elf.to_offset(0x00100000), 0x1000)
        self.assertEqual(elf.to_offset(0), -1)

        # lui + addiu の組から、参照されている 4 本の文字列が復元できること
        hits = self.elfdump.xrefs(elf)
        texts = sorted(t for _f, _t, t in hits)
        self.assertEqual(texts, sorted([
            "cdrom0:\\BOKU2.IMG;1",
            "cdrom0:\\BOKU2.IDX;1",
            "index open failed\n",
            "read error at sector %d\n",
        ]), "参照されている文字列の顔ぶれが違います")

        # 参照されていない文字列を参照済みと言わないこと
        self.assertNotIn("MAP/NATSU00.PAK", texts)

    def test_disassembles_the_entry_point(self):
        path = os.path.join(REPO, "work", "BOOT.ELF")
        problem = ensure_practice("work/BOOT.ELF", "make_elf.py")
        if problem:
            self.skipTest(problem)
        with open(path, "rb") as fh:
            elf = self.elfdump.Elf(fh.read())
        lines = self.elfdump.disasm(elf, elf.entry, 16)
        self.assertEqual(len(lines), 16)
        self.assertEqual(lines[0][2], "addiu")
        notes = [n for *_rest, n in lines if n]
        self.assertTrue(any("BOKU2.IDX" in n for n in notes),
                        f"文字列の注記が出ていません: {notes}")


class TestLzss(unittest.TestCase):
    """LZSS (奥村版) の伸張・圧縮・探索."""

    @classmethod
    def setUpClass(cls):
        import lzss
        cls.lzss = lzss

    def test_round_trip(self):
        cases = [
            b"",
            b"a",
            "ぼくのなつやすみ。むしとりにいこう。".encode("cp932"),
            ("こんにちは、" * 60).encode("cp932"),
            b"AAAAAAAAAAAA" + bytes(range(256)) + b"BCBCBCBCBC",
            bytes(i % 11 for i in range(4000)),
        ]
        for s in cases:
            packed = self.lzss.compress(s)
            self.assertEqual(self.lzss.decompress(packed), s,
                             f"round-trip 失敗 (len={len(s)})")

    def test_compression_actually_shrinks_repeats(self):
        rep = ("こんにちは、" * 60).encode("cp932")
        self.assertLess(len(self.lzss.compress(rep)), len(rep) * 0.5)

    def test_scan_finds_text_block(self):
        text = "きょうはいいてんきです。むしとりにいこう。".encode("cp932") * 10
        blob = bytes(0x800) + self.lzss.compress(text)
        hits = self.lzss.scan(blob, step=0x400)
        offs = [h[0] for h in hits]
        self.assertIn(0x800, offs, "圧縮テキストの位置を見つけられていない")

    def test_zero_fill_is_not_text(self):
        # ゼロ埋めを伸張すると空白の羅列になる。これをテキストと誤判定しないこと
        self.assertEqual(self.lzss.looks_like_text(b"\x20" * 4096), 0.0)
        self.assertEqual(self.lzss.looks_like_text(b"\x00" * 4096), 0.0)

    def test_broken_input_does_not_crash(self):
        import os
        self.lzss.decompress(os.urandom(2000))     # 例外を出さずに返る


class TestLzssInBrowser(unittest.TestCase):
    """ブラウザ側 LZSS が Python の圧縮を伸張できることを突き合わせる."""

    def test_browser_matches_python(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        subprocess.run([sys.executable, os.path.join(REPO, "tests", "gen_lzss_cases.py")],
                       capture_output=True, cwd=REPO)
        res = subprocess.run([node, os.path.join(REPO, "tests", "test_lzss.mjs")],
                             capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


class TestBoku2Cli(unittest.TestCase):
    """一括抽出 (tools/boku2.py) を、実物と同じ形の合成データで通す."""

    @staticmethod
    def build_dfi(tree):
        """tree: [(is_dir, more, name, data or None)] → (idx, img, 期待する path→data)."""
        import struct
        recs, img, want = [], b"", {}
        stack = []
        for is_dir, more, name, data in tree:
            if is_dir:
                recs.append((1, more, 0, 0))
                stack.append(("" if name == "/" else name, more))
                continue
            lba = len(img) // 2048
            recs.append((0, more, lba, len(data)))
            padded = data + b"\0" * ((2048 - len(data) % 2048) % 2048)
            img += padded
            want["/".join([d for d, _ in stack if d] + [name])] = data
            if more == 0:
                d = stack.pop()
                while d and d[1] == 0 and len(stack) > 1:
                    d = stack.pop()
        idx = b"DFI\0" + struct.pack("<I", 0x100) + b"\0" * 8
        noise = 0x8130
        for kind, more, lba, size in recs:
            idx += struct.pack("<HHIII", kind, more, noise, lba, size)
            noise -= 7
        idx += b"".join(name.encode() + b"\0" for _, _, name, _ in tree)
        return idx, img, want

    @staticmethod
    def build_msg(entries, stride):
        import struct
        tab = 4 + len(entries) * stride
        head = struct.pack("<I", len(entries))
        body, p = b"", tab
        for e in entries:
            head += struct.pack("<I", p if e else 0) + (b"\0" * (stride - 4))
            body += struct.pack(f"<{len(e)}H", *e)
            p += len(e) * 2
        return head + body

    @classmethod
    def build_tables(cls, tables):
        import struct
        head = 4 + len(tables) * 12
        bodies = [cls.build_msg(t, 4) for t in tables]
        out, p, data = struct.pack("<I", len(tables)), head, b""
        for i, b in enumerate(bodies):
            out += struct.pack("<IHHI", 0xDEAD, len(b), 100 + i, p)     # 位置は u32 (MSG_notes.txt)
            data += b
            p += len(b)
        return out + data

    @staticmethod
    def build_map(parts):
        import struct
        n = len(parts)
        head_len = ((4 + n * 8 + 15) // 16) * 16
        out, data, off = struct.pack("<I", n), b"", head_len
        for part in parts:
            if part is None:
                out += struct.pack("<II", 0, 0)
                continue
            padded = part + b"\0" * ((16 - len(part) % 16) % 16)
            out += struct.pack("<II", off, len(part))
            data += padded
            off += len(padded)
        return out + b"\0" * (head_len - len(out)) + data

    def test_windows_bom_and_crlf_do_not_shift_the_table(self):
        """メモ帳 / Excel が付ける BOM (U+FEFF) と CRLF で、文字表・TSV・フォント一覧がずれないこと.

        BOM を文字として数えると 0 番がずれ、全文が別の字に化ける (エラーは出ない)。"""
        import boku2
        import proofread
        import relative_search
        import font_view

        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "font.txt")
            bom = os.path.join(tmp, "font_bom.txt")
            with open(plain, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("あいう\nえお\n")
            with open(bom, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("あいう\nえお\n")
            with open(bom, "rb") as fh:
                self.assertTrue(fh.read().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(boku2.load_font(bom), boku2.load_font(plain))
            self.assertEqual(boku2.load_font(bom)[0], "あ")
            pairs = os.path.join(tmp, "pairs_bom.txt")
            with open(pairs, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("0=あ\n1=い\n")
            self.assertEqual(boku2.load_font(pairs), ["あ", "い"])
            self.assertEqual(boku2.parse_glyph_table("\ufeffあい"), ["あ", "い"])

            # 校正ツールのフォント一覧 (1 行 1 字): 先頭の字が落ちない
            chars = os.path.join(tmp, "chars_bom.txt")
            with open(chars, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("# 見出し\nあ\nい\n")
            self.assertEqual(proofread.load_font_chars(chars), {"あ", "い"})
            self.assertEqual(font_view.load_chars(chars), ["あ", "い"])
            self.assertEqual(relative_search.build_order("x", chars), ["あ", "い"])

            # Excel の「CSV UTF-8」で保存した TSV: 見出しの id が "\ufeffid" にならない
            tsv = os.path.join(tmp, "bom.tsv")
            with open(tsv, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("id\toffset\tsize\toriginal\ttranslation\n0\t0\t2\tあい\tうえ\n")
            rows = scrp.read_tsv(tsv)
            self.assertEqual([r["id"] for r in rows], ["0"])
            self.assertEqual(rows[0]["translation"], "うえ")

            # docs/01 の .tbl (16進=文字) も同じ
            tbl = os.path.join(tmp, "bom.tbl")
            with open(tbl, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("00=あ\n01=い\n")
            self.assertEqual(scrp.load_table(tbl).by_bytes[b"\x00"], "あ")

    def test_two_folder_rules_are_compared(self):
        """フォルダの閉じ方は、こちらの stack 規則と公開ソース UNPACK.py の flag 規則がある.

        練習データや普通の入れ子では一致し、深い入れ子や特殊な並びでは違い得る。
        check はその食い違いを数えて報告する (実物でどちらが正しいかを確かめる材料)."""
        import boku2
        import make_boku2_sample
        # 練習データ (4 段の入れ子を含む) では一致
        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            with open(os.path.join(sample, "BOKU2.IDX"), "rb") as fh:
                idx = fh.read()
            size = os.path.getsize(os.path.join(sample, "BOKU2.IMG"))
            self.assertEqual(boku2.dfi_rule_mismatch(idx, size), [])
            import io
            out = io.StringIO()
            boku2.check(sample, out=out)
            self.assertIn("フォルダの規則: 2 通り (stack / flag) で一致", out.getvalue())
        # 食い違う並び: A (続く=0) の中に B (続く=1) と C。B の最後のファイルで
        # flag 規則は A まで閉じてしまい、C が根に出る
        tree = [(True, 1, "/", None), (True, 0, "A", None), (True, 1, "B", None),
                (False, 0, "b0.bin", b"\x01" * 8), (True, 0, "C", None), (False, 0, "c0.bin", b"\x02" * 8)] + \
               [(False, 0 if i == 7 else 1, f"r{i}.bin", b"\x03" * 8) for i in range(8)]
        idx, img, _ = self.build_dfi(tree)
        mism = boku2.dfi_rule_mismatch(idx, len(img))
        self.assertTrue(mism, "食い違いが検出されない")
        self.assertEqual(mism[0][0], "A/C/c0.bin")
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "BOKU2.IDX"), "wb") as fh:
                fh.write(idx)
            with open(os.path.join(tmp, "BOKU2.IMG"), "wb") as fh:
                fh.write(img)
            out = io.StringIO()
            boku2.check(tmp, out=out)
            self.assertIn("フォルダの規則が 2 通りで食い違うファイル", out.getvalue())

    def test_nested_containers_are_descended(self):
        """入れ物の部品がさらに入れ物 (fish_on_mem.bin の 11〜16 番) でも、その中の文言を拾うこと.

        文言として読めた部品には降りない (誤認を避ける)。深さは 2 段まで。"""
        import boku2
        glyphs = list("あいうえおかきくけこ")
        msg = self.build_msg([[5, 6, 0x8000], [0, 1, 0x8000]], 4)
        inner = self.build_map([b"\x22" * 32, b"\x33" * 48, msg])
        outer = self.build_map([b"\x11" * 40, inner, None])
        rows = boku2.text_rows_bytes(outer, "fish", glyphs)
        self.assertEqual([(r[0], r[3]) for r in rows], [("fish#1#2:0", "かき"), ("fish#1#2:1", "あい")])
        # 位置は外側のファイルの先頭からの値
        outer_off = rows[0][1]
        self.assertEqual(outer[outer_off:outer_off + 6], bytes([5, 0, 6, 0, 0, 0x80]))
        # 3 段目には降りない (2 段まで)
        third = self.build_map([b"\x44" * 16, outer])
        self.assertEqual([r[0] for r in boku2.text_rows_bytes(third, "t", glyphs)], ["t#1#1#2:0", "t#1#1#2:1"])
        fourth = self.build_map([b"\x55" * 16, third])
        self.assertEqual(boku2.text_rows_bytes(fourth, "f", glyphs), [])

    def test_alt_break_files_and_u32_table_offset(self):
        """公開ソースで確認した 2 点: (1) turi_info.msg など 7 つのメニューでは 0x8002 が
        引数の無いページ送り (待ち時間として読むと次の字を飛ばす)、(2) 表の一覧の位置は u32."""
        import boku2
        glyphs = list("あいうえおかきくけこ")
        codes = [5, 0x8002, 6, 7, 0x8000]
        self.assertEqual(boku2.decode(codes, glyphs, tags=False), "か{WAIT 6}く{END}")
        self.assertEqual(boku2.decode(codes, glyphs, tags=False, alt=True), "か{BREAK}\nきく{END}")
        self.assertEqual(boku2.decode(codes, glyphs, alt=True), "か<BREAK>きく")
        self.assertTrue(boku2.is_alt_break("OUT/system/Item_Info.msg"))
        self.assertFalse(boku2.is_alt_break("OUT/system/system.msg"))
        with tempfile.TemporaryDirectory() as tmp:
            for name, want in [("item_info.msg", "か<BREAK>きく"), ("system.msg", "か<WAIT:06>く")]:
                p = os.path.join(tmp, name)
                with open(p, "wb") as fh:
                    fh.write(self.build_msg([codes], 8))
                rows = boku2.text_rows(p, glyphs)
                self.assertEqual([r[3] for r in rows], [want], name)
            # 使われている番号: ページ送りの次の値は文字なので数える
            self.assertEqual(boku2.used_codes([os.path.join(tmp, "item_info.msg")]), [5, 6, 7])
            self.assertEqual(boku2.used_codes([os.path.join(tmp, "system.msg")]), [5, 7])
        # 位置の上位 2 バイト (+10) に何か入っていても読める (公開ソースの読み取りは u16)
        two = bytearray(self.build_tables([[[5, 0x8000]], [[2, 3, 0x8000]]]))
        two[4 + 12 + 10:4 + 12 + 12] = b"\xAB\xCD"       # 2 つ目の項目の +10 を汚す
        dirty = boku2.parse_tables(bytes(two))
        self.assertIsNotNone(dirty)
        self.assertEqual(boku2.decode(dirty[1]["msg"][0]["codes"], glyphs, tags=False), "うえ{END}")
        # 64 KiB を超える位置の表 (表の長さは u16 なので、1 つは 64 KiB 未満。3 つ並べて位置を越えさせる)
        big = self.build_tables([[[0] * 30000]] * 3 + [[[2, 3, 0x8000]]])
        tables = boku2.parse_tables(big)
        self.assertIsNotNone(tables)
        self.assertGreater(tables[3]["off"], 0x10000)
        self.assertEqual(boku2.decode(tables[3]["msg"][0]["codes"], glyphs, tags=False), "うえ{END}")

    def test_helpful_messages_for_common_mistakes(self):
        """初めての人がよく踏む 3 つに、次の一手が分かる文言を出すこと.

        1) 文字表が短くて本文に [番号] が残る → どの番号が無いかを数えて知らせる
        2) check に ISO のままのフォルダを渡す → 展開してから、と言う
        3) check に一段上のフォルダを渡す → 中のフォルダ名を言う"""
        import contextlib
        import io
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            out = os.path.join(tmp, "OUT")
            boku2.main(["unpack", os.path.join(sample, "BOKU2.IDX"), os.path.join(sample, "BOKU2.IMG"), out])
            # 1) 文字表を先頭 3 字だけにする
            with open(os.path.join(sample, "font.txt"), encoding="utf-8") as fh:
                full = fh.read()
            short = os.path.join(tmp, "short.txt")
            with open(short, "w", encoding="utf-8") as fh:
                fh.write("".join(boku2.parse_glyph_table(full)[:3]))
            err, outbuf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(outbuf), contextlib.redirect_stderr(err):
                rc = boku2.main(["text", out, "-f", short, "-o", os.path.join(tmp, "a.tsv")])
            self.assertEqual(rc, 0)
            self.assertIn("文字表に無い番号", err.getvalue())
            self.assertRegex(err.getvalue(), r"文字表に無い番号: \d+ 種 \(例: \d+")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                boku2.main(["text", out, "-f", os.path.join(sample, "font.txt"), "-o", os.path.join(tmp, "b.tsv")])
            self.assertIn("文字表で全部読めました", err.getvalue())
            # 2) ISO のまま
            iso_dir = os.path.join(tmp, "disc")
            os.makedirs(iso_dir)
            with open(os.path.join(iso_dir, "BOKU2.iso"), "wb") as fh:
                fh.write(b"\0" * 4096)
            buf = io.StringIO()
            self.assertEqual(boku2.check(iso_dir, out=buf), 1)
            self.assertIn("ディスクイメージのまま", buf.getvalue())
            self.assertIn("docs/05", buf.getvalue())
            # 3) 一段上
            buf = io.StringIO()
            self.assertEqual(boku2.check(tmp, out=buf), 1)
            self.assertIn("一段下の S/", buf.getvalue())

    def test_excel_round_trip(self):
        """Excel で開いて直して保存し直す往復が、どの保存形式でも壊れないこと.

        出力: BOM 付き UTF-8 (BOM 無しだと Excel は cp932 と誤認して日本語が化ける)。
        入力: 「Unicode テキスト」(UTF-16)、「CSV UTF-8」(BOM 付き)、ANSI (cp932) のどれでも。"""
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            out = os.path.join(tmp, "OUT")
            boku2.main(["unpack", os.path.join(sample, "BOKU2.IDX"), os.path.join(sample, "BOKU2.IMG"), out])
            tsv = os.path.join(tmp, "all.tsv")
            self.assertEqual(boku2.main(["text", out, "-f", os.path.join(sample, "font.txt"), "-o", tsv]), 0)
            with open(tsv, "rb") as fh:
                raw = fh.read()
            self.assertTrue(raw.startswith(codecs.BOM_UTF8 + b"id\t"), raw[:12])
            self.assertTrue(b"\r\n" not in raw, "TSV に CRLF が混ざっている")
            base = scrp.read_tsv(tsv)
            self.assertIn("config:0", [r["id"] for r in base])

            # Excel が保存し直した体で、同じ内容を 4 通りの形式に書き、全部同じに読めること
            text = raw.decode("utf-8-sig").replace("\n", "\r\n")
            variants = {
                "unicode_text.txt": codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
                "csv_utf8.tsv": codecs.BOM_UTF8 + text.encode("utf-8"),
                "ansi.tsv": text.encode("cp932"),
                "plain.tsv": text.encode("utf-8"),
            }
            for name, data in variants.items():
                p = os.path.join(tmp, name)
                with open(p, "wb") as fh:
                    fh.write(data)
                got = scrp.read_tsv(p)
                self.assertEqual([(r["id"], r["original"]) for r in got],
                                 [(r["id"], r["original"]) for r in base], name)

            # 文字表と、校正ツールのフォント一覧も同じ (メモ帳の ANSI / Excel の Unicode テキスト)
            font_text = "あ\nい\nう\nえ\nお\n"          # 1 行 1 字 (fontlist の形。文字表としても読める)
            for name, data in {"ansi.txt": font_text.encode("cp932"),
                               "u16.txt": codecs.BOM_UTF16_LE + font_text.encode("utf-16-le")}.items():
                p = os.path.join(tmp, name)
                with open(p, "wb") as fh:
                    fh.write(data)
                self.assertEqual(boku2.load_font(p), list("あいうえお"), name)
                self.assertEqual(proofread.load_font_chars(p), set("あいうえお"), name)
            # dump_text 系 (scrp.write_tsv) の出力も BOM 付き
            p = os.path.join(tmp, "w.tsv")
            scrp.write_tsv(p, [{"id": "0", "offset": 0, "size": 2, "original": "あ", "translation": "あ"}])
            with open(p, "rb") as fh:
                self.assertTrue(fh.read().startswith(codecs.BOM_UTF8))
            self.assertEqual(scrp.read_tsv(p)[0]["original"], "あ")

    def test_windows_shell_leaves_patterns_unexpanded(self):
        """Windows の cmd / PowerShell は `MAP/*.*` を展開せずそのまま渡す。道具側で展開すること."""
        import boku2
        import subprocess
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            out = os.path.join(tmp, "OUT")
            self.assertEqual(boku2.main(["unpack", os.path.join(sample, "BOKU2.IDX"),
                                         os.path.join(sample, "BOKU2.IMG"), out]), 0)
            # 1) 展開されなかった `MAP/*.*` をそのまま受け取っても動く
            maps1 = os.path.join(tmp, "maps1")
            self.assertEqual(boku2.main(["maps", os.path.join(sample, "MAP", "*.*"), "-o", maps1]), 0)
            self.assertTrue(os.path.exists(os.path.join(maps1, "M_A01000", "1.bin")))
            self.assertTrue(os.path.exists(os.path.join(maps1, "M_A02000", "1.bin")))
            # 2) フォルダをそのまま渡しても同じ
            maps2 = os.path.join(tmp, "maps2")
            self.assertEqual(boku2.main(["maps", os.path.join(sample, "MAP"), "-o", maps2]), 0)
            self.assertEqual(sorted(os.listdir(maps1)), sorted(os.listdir(maps2)))
            # 3) text / used も同じ (`OUT/system/*.msg` の形)
            tsv = os.path.join(tmp, "a.tsv")
            self.assertEqual(boku2.main(["text", os.path.join(out, "system", "*.msg"),
                                         os.path.join(maps1, "*", "1.bin"),
                                         "-f", os.path.join(sample, "font.txt"), "-o", tsv]), 0)
            with open(tsv, encoding="utf-8") as fh:
                ids = [line.split("\t")[0] for line in fh.read().splitlines()[1:]]
            self.assertTrue(any(i.startswith("system:") for i in ids), ids[:5])
            self.assertTrue(any(i.startswith("M_A01000:") for i in ids), ids[:5])
            # 4) 一致しない指定は、追跡表示ではなく 1 行の日本語で止まる
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                                  "maps", os.path.join(sample, "MAP", "*.xyz"), "-o", maps2],
                                 capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(res.returncode, 1)
            self.assertIn("一致するファイルがありません", res.stderr)
            self.assertNotIn("Traceback", res.stderr)
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                                  "maps", os.path.join(sample, "font.txt"), "-o", maps2],
                                 capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(res.returncode, 1)
            self.assertIn("入れ物ではありません", res.stderr)
            self.assertNotIn("Traceback", res.stderr)

    def test_unpack_maps_text_end_to_end(self):
        import boku2
        import subprocess

        glyphs = list("あいうえおかきくけこ")
        menu = self.build_msg([[5, 6, 0x8001, 7, 0x8000], [], [0x8002, 0x12, 9, 0x8000, 0xCDCD]], 8)
        talk = self.build_tables([[[0, 1, 0x8000]], [[0x3130, 0x3332, 0x3534, 0x3736], [2, 3, 0x8000]]])
        map_file = self.build_map([b"\x11" * 40, talk, None])
        tree = [
            (True, 1, "/", None),
            (True, 1, "system", None),
            (False, 1, "system.msg", menu),
            (True, 0, "sub", None),
            (False, 0, "deep.bin", b"\x22" * 10),
            (True, 1, "photo", None),
        ] + [(False, 0 if i == 7 else 1, f"p{i}.tm2", bytes([0x40 + i]) * (100 + i)) for i in range(8)] + [
            (False, 0, "tail.bin", b"\x33" * 3000),
        ]
        idx, img, want = self.build_dfi(tree)
        self.assertEqual(len(want), 11)
        for key in ["system/sub/deep.bin", "system/system.msg", "photo/p0.tm2", "photo/p7.tm2", "tail.bin"]:
            self.assertIn(key, want)

        with tempfile.TemporaryDirectory() as tmp:
            idx_path, img_path = os.path.join(tmp, "BOKU2.IDX"), os.path.join(tmp, "BOKU2.IMG")
            with open(idx_path, "wb") as fh:
                fh.write(idx)
            with open(img_path, "wb") as fh:
                fh.write(img)
            out = os.path.join(tmp, "out")
            # #161 から (切り分けた数, 名前が付かなかった数)、#178 で
            # (取り出せなかった分の内訳) が加わった
            n, unnamed, dropped, _paths = boku2.unpack(idx_path, img_path, out)
            self.assertEqual(n, 11)
            self.assertEqual(unnamed, 0, "この題材では全部に名前が付くはず")
            self.assertEqual(dropped["outside"], 0, f"取りこぼしがある: {dropped}")
            for path, data in want.items():
                with open(os.path.join(out, *path.split("/")), "rb") as fh:
                    self.assertEqual(fh.read(), data, path)

            # 索引の名前を信用しない: 出力先の外に書かない
            self.assertEqual(boku2.safe_parts("../../evil.bin"), ["evil.bin"])
            self.assertEqual(boku2.safe_parts("/abs/x.msg"), ["abs", "x.msg"])
            self.assertEqual(boku2.safe_parts("a\\b\\c.tm2"), ["a", "b", "c.tm2"])
            self.assertEqual(boku2.safe_parts("dir/na:me*.bin"), ["dir", "na_me_.bin"])
            self.assertEqual(boku2.safe_parts("..//"), ["_"])
            hostile = [(True, 1, "/", None)] + \
                [(False, 0 if i == 8 else 1, "../h%d.bin" % i, b"\x55" * 8) for i in range(9)]
            h_idx, h_img, _ = self.build_dfi(hostile)
            h_dir = os.path.join(tmp, "H")
            with open(os.path.join(tmp, "H.IDX"), "wb") as fh:
                fh.write(h_idx)
            with open(os.path.join(tmp, "H.IMG"), "wb") as fh:
                fh.write(h_img)
            boku2.unpack(os.path.join(tmp, "H.IDX"), os.path.join(tmp, "H.IMG"), h_dir)
            self.assertTrue(os.path.exists(os.path.join(h_dir, "h0.bin")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "h0.bin")))

            # 同じ道筋が二度出ても上書きしない (~2 を付ける)。ブラウザ側と同じ規則
            dup_tree = [(True, 1, "/", None), (True, 1, "d", None)] + \
                [(False, 0 if i == 8 else 1, "same.bin", bytes([i]) * 10) for i in range(9)]
            d_idx, d_img, _ = self.build_dfi(dup_tree)
            d_paths = [e["path"] for e in boku2.read_dfi(d_idx, len(d_img))]
            self.assertEqual(d_paths[:3], ["d/same.bin", "d/same.bin~2", "d/same.bin~3"])
            self.assertEqual(len(set(d_paths)), 9)

            # マップの入れ物 → 部品 → 会話 (表が複数、音声つき)
            map_path = os.path.join(tmp, "M_A11000.BIN")
            with open(map_path, "wb") as fh:
                fh.write(map_file)
            parts = boku2.split_map(map_path, os.path.join(tmp, "maps", "M_A11000"))
            self.assertEqual(parts, 2)
            rows = boku2.text_rows(os.path.join(tmp, "maps", "M_A11000", "1.bin"), glyphs, keep_voice=True)
            self.assertEqual([r[0] for r in rows], ["M_A11000:0-0", "M_A11000:1-0", "M_A11000:1-1"])
            self.assertEqual([r[3] for r in rows], ["あい", "<VOICE:01234567>", "うえ"])
            # 音声の番号は既定では省く (校正の対象ではない)
            self.assertEqual([r[3] for r in boku2.text_rows(map_path, glyphs)], ["あい", "うえ"])
            # 入れ物のまま渡しても 1 番を読む (位置は入れ物の先頭から)
            rows2 = boku2.text_rows(map_path, glyphs, keep_voice=True)
            self.assertEqual([r[3] for r in rows2], ["あい", "<VOICE:01234567>", "うえ"])
            self.assertGreater(rows2[0][1], rows[0][1])
            # 単体の .msg
            rows3 = boku2.text_rows(os.path.join(out, "system", "system.msg"), glyphs)
            self.assertEqual([r[3] for r in rows3], ["かき<BR>く", "<WAIT:12>こ"])
            # 見出しの無い並び (日記の雛形・保存画面の文言): 0x8000 で区切るだけ
            import struct
            raw_path = os.path.join(tmp, "diary0.bin")
            with open(raw_path, "wb") as fh:
                fh.write(struct.pack("<9H", 5, 6, 0x8001, 7, 0x8000, 0, 1, 0x8000, 0xCDCD))
            rows4 = boku2.text_rows(raw_path, glyphs)
            self.assertEqual([r[3] for r in rows4], ["かき<BR>く", "あい"])
            self.assertEqual(rows4[1][1], 10)
            self.assertIsNone(boku2.parse_raw(b"\x05\x00\x06\x00"))          # 終わりが無い
            # Shift-JIS の並び (保存画面の入れ物の 2 番): 文字表なしでそのまま読める
            sj = "セーブしますか？\0はい\0いいえ\0".encode("cp932")
            self.assertEqual([x["text"] for x in boku2.parse_sjis_list(sj)], ["セーブしますか？", "はい", "いいえ"])
            self.assertIsNone(boku2.parse_sjis_list(b"\x05\x00\x06\x00\x00\x80"))
            self.assertIsNone(boku2.parse_sjis_list(bytes(range(0x80, 0xa0)) + b"\0"))
            sj_rows = boku2.text_rows_bytes(sj, "saveload", None)
            self.assertEqual([r[3] for r in sj_rows], ["セーブしますか？", "はい", "いいえ"])
            self.assertEqual(sj_rows[1][1], 17)
            # 刻み 8 の入れ物の 0 番 (命令列) は見出しの無い並びとして読まない。1 番以降と刻み 12 は読む
            rawpart = struct.pack("<5H", 5, 6, 0x8001, 7, 0x8000)
            m8 = self.build_map([rawpart, rawpart])
            self.assertEqual([r[0] for r in boku2.text_rows_bytes(m8, "m8", glyphs)], ["m8#1:0"])
            import make_boku2_sample
            m12 = make_boku2_sample.build_map([rawpart, None, rawpart], rec=12)
            self.assertEqual([r[0] for r in boku2.text_rows_bytes(m12, "m12", glyphs)], ["m12#0:0", "m12#2:0"])
            self.assertIsNone(boku2.parse_raw("普通の文章です。".encode("utf-8")[:16]))

            # 文字表は「番号=文字」の対応表でもよい (使われている番号だけ書ける)
            sparse = boku2.parse_glyph_table("5=か\n6 き\n7: く\n9＝こ\n")
            self.assertEqual(sparse[5:8], ["か", "き", "く"])
            self.assertIsNone(sparse[8])
            self.assertEqual(boku2.decode([5, 6, 0x8001, 7, 0x8000], sparse), "かき<BR>く")
            self.assertEqual(boku2.decode([0, 5, 0x8000], sparse), "[0]か")
            self.assertEqual(boku2.parse_glyph_table("あい\nう"), ["あ", "い", "う"])

            # 文字表 → docs/01 の .tbl。練習用の hexdump.py がそのまま .msg を日本語で表示できる
            font_path = os.path.join(tmp, "font.txt")
            with open(font_path, "w", encoding="utf-8") as fh:
                fh.write("あいうえお\nかきくけこ\n")
            tbl = os.path.join(tmp, "boku2.tbl")
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), "table",
                                  font_path, "-o", tbl], capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            codec = scrp.load_table(tbl)
            self.assertEqual(codec.decode_char(b"\x05\x00\x06\x00", 0), ("か", 2))
            self.assertEqual(codec.decode_char(b"\x00\x80", 0), ("{END}", 2))
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"),
                                  os.path.join(out, "system", "system.msg"), "--table", tbl],
                                 capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("かき", res.stdout)

            # 使われている文字番号だけを並べる (音声・制御コード・待ち時間の値は除く)
            self.assertEqual(boku2.used_codes([map_path, os.path.join(out, "system", "system.msg")]),
                             [0, 1, 2, 3, 5, 6, 7, 9])

            # CLI で TSV にして、校正ツールが読めること
            tsv = os.path.join(tmp, "all.tsv")
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), "text",
                                  os.path.join(out, "system", "system.msg"), map_path,
                                  "-f", font_path, "-o", tsv], capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            got = scrp.read_tsv(tsv)
            self.assertEqual(len(got), 4)                       # 音声 1 行は省かれる
            self.assertEqual(got[0]["id"], "system:0")
            fl = os.path.join(tmp, "font_chars.txt")
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), "fontlist",
                                  font_path, "-o", fl], capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertEqual(proofread.load_font_chars(fl), set("あいうえおかきくけこ"))
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv,
                                  "--font-chars", fl], capture_output=True, text=True, cwd=REPO)
            self.assertIn(res.returncode, (0, 1), res.stdout + res.stderr)
            self.assertNotIn("Traceback", res.stderr)

            # ブラウザ側の索引読みと同じ答えになること (同じ合成データを node で読む)
            import shutil
            node = shutil.which("node")
            if node:
                script = ("const fs=require('fs');const src=fs.readFileSync('web/app.js','utf8');"
                          "const s=src.indexOf('/* @extract-start named-index */'),e=src.indexOf('/* @extract-end named-index */');"
                          "const u32le=(b,p)=>(b[p]|(b[p+1]<<8)|(b[p+2]<<16)|(b[p+3]<<24))>>>0;"
                          "const ascii=(b)=>{let t='';for(const c of b)t+=String.fromCharCode(c);return t;};"
                          "const m=new Function('u32le','ascii',src.slice(s,e)+'\\nreturn {readDfi,namedEntries};')(u32le,ascii);"
                          f"const idx=fs.readFileSync({idx_path!r});const size={len(img)};"
                          "const c=m.readDfi(idx,size);const items=m.namedEntries(idx,c,size,4096);"
                          "console.log(JSON.stringify(items.map(i=>[i.name,i.at,i.len])));")
                res = subprocess.run([node, "-e", script], capture_output=True, text=True, cwd=REPO)
                self.assertEqual(res.returncode, 0, res.stderr)
                js = json.loads(res.stdout)
                py = [[e["path"], e["at"], e["len"]] for e in boku2.read_dfi(idx, len(img))]
                self.assertEqual(js, py)


class TestUnpackLandsEveryFile(unittest.TestCase):
    """**切り分けた数と、手元にできたファイルの数が合うこと** (#246).

    `read_dfi` と `names_from_crc` は「同じ道筋なら `~2`」をやっているが、
    見ているのは**索引の名前のまま**の道筋。実際に書くのは `safe_parts` を
    通した後なので、そこで初めてぶつかる形を 2 つ取りこぼしていた。
    どちらも「切り分けました」と言いながらファイルが減る / 途中で落ちる。
    """

    @staticmethod
    def build(tmp: str, tree: list) -> tuple[str, str]:
        import make_boku2_sample
        idx, img, _ = make_boku2_sample.build_dfi(tree)
        idx_path, img_path = os.path.join(tmp, "T.IDX"), os.path.join(tmp, "T.IMG")
        with open(idx_path, "wb") as fh:
            fh.write(idx)
        with open(img_path, "wb") as fh:
            fh.write(img)
        return idx_path, img_path

    @staticmethod
    def landed(out: str) -> list[str]:
        return sorted(os.path.relpath(os.path.join(r, f), out)
                      for r, _d, fs in os.walk(out) for f in fs)

    def test_names_that_become_the_same_after_the_underscore_are_both_kept(self):
        """`sys/a:b.bin` と `sys/a_b.bin` —— 使えない字を `_` にしたら同じになる."""
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            idx_path, img_path = self.build(tmp, [
                (True, 1, "/", None),
                (True, 1, "sys", None),
                (False, 1, "a:b.bin", b"A" * 32),
                (False, 0, "a_b.bin", b"B" * 32),
                (False, 0, "tail.bin", b"C" * 32),
            ])
            out = os.path.join(tmp, "OUT")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                n, _unnamed, _dropped, paths = boku2.unpack(idx_path, img_path, out)
            got = self.landed(out)
            # **材料が弱くないこと**: 索引では 2 件が違う名前になっている
            self.assertEqual(sorted(paths)[:2], ["sys/a:b.bin", "sys/a_b.bin"], paths)
            self.assertEqual(len(got), n,
                             f"{n} 個に切り分けたのに {len(got)} 個しか落ちていない: {got}")
            # 中身も入れ替わっていない
            with open(os.path.join(out, "sys", "a_b.bin"), "rb") as fh:
                self.assertEqual(fh.read(), b"A" * 32)
            with open(os.path.join(out, "sys", "a_b.bin~2"), "rb") as fh:
                self.assertEqual(fh.read(), b"B" * 32)
            # **黙って名前を変えない。** 使えない字とは原因が違うので別に言う
            self.assertIn("別のファイルと同じ名前になったので `~2`", err.getvalue())
            self.assertIn("sys/a_b.bin → sys/a_b.bin~2", err.getvalue())

    def test_a_name_used_for_both_a_file_and_a_folder_does_not_stop_the_unpack(self):
        """`sys` というファイルと `sys` というフォルダ —— 前は FileExistsError で落ちた."""
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            idx_path, img_path = self.build(tmp, [
                (True, 1, "/", None),
                (False, 1, "sys", b"A" * 32),
                (True, 1, "sys", None),
                (False, 0, "x.bin", b"B" * 32),
                (False, 0, "tail.bin", b"C" * 32),
            ])
            out = os.path.join(tmp, "OUT")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                n, _unnamed, _dropped, paths = boku2.unpack(idx_path, img_path, out)
            got = self.landed(out)
            # **材料が弱くないこと**: 同じ名前がファイルにもフォルダにも使われている
            self.assertIn("sys", paths)
            self.assertIn("sys/x.bin", paths)
            self.assertEqual(len(got), n,
                             f"{n} 個に切り分けたのに {len(got)} 個しか落ちていない: {got}")
            self.assertIn(os.path.join("sys", "x.bin"), got)
            self.assertIn("sys~2", got)
            self.assertIn("フォルダと同じ名前だったので `~2`", err.getvalue())

    def test_the_folder_wins_whichever_order_it_comes_in(self):
        """フォルダが先でもファイルが先でも、結果は同じ。逆順だと `open` が
        IsADirectoryError で落ちる形だった."""
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            idx_path, img_path = self.build(tmp, [
                (True, 1, "/", None),
                (True, 1, "sys", None),
                (False, 0, "x.bin", b"B" * 32),       # ここで sys を閉じて根に戻る
                (False, 1, "sys", b"A" * 32),
                (False, 0, "tail.bin", b"C" * 32),
            ])
            out = os.path.join(tmp, "OUT")
            with contextlib.redirect_stderr(io.StringIO()):
                n, _unnamed, _dropped, _paths = boku2.unpack(idx_path, img_path, out)
            got = self.landed(out)
            self.assertEqual(len(got), n, f"落ちたのは {got}")
            self.assertIn(os.path.join("sys", "x.bin"), got)
            self.assertIn("sys~2", got)

    def test_the_notice_only_counts_names_that_really_had_a_bad_letter(self):
        """`~2` を付けただけの件を「使えない字があった」に混ぜない (#96 の「数だけ出すな」)."""
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            idx_path, img_path = self.build(tmp, [
                (True, 1, "/", None),
                (False, 1, "sys", b"A" * 32),
                (True, 1, "sys", None),
                (False, 0, "x.bin", b"B" * 32),
                (False, 0, "tail.bin", b"C" * 32),
            ])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                boku2.unpack(idx_path, img_path, os.path.join(tmp, "OUT"))
            self.assertNotIn("使えない字", err.getvalue(),
                             f"使えない字は 1 つも無いのに言っている: {err.getvalue()}")

    def test_the_place_to_write_is_decided_for_the_whole_list_at_once(self):
        """`out_paths` そのもの: 同じ場所を 2 度返さない."""
        import boku2
        dests, why = boku2.out_paths(
            ["sys/a:b.bin", "sys/a_b.bin", "sys/a?b.bin", "sys", "sys/x.bin", "tail.bin"])
        self.assertEqual(len(set(dests)), len(dests), dests)
        self.assertEqual(why, ["", "collide", "collide", "folder", "", ""], dests)
        # フォルダとして使われている場所を、ファイルが横取りしない
        self.assertNotIn("sys", dests)

    def test_the_practice_data_lands_every_file(self):
        """練習用データでも数が合うこと (壊し方を全部かけても)."""
        import boku2
        import make_boku2_sample
        for kind in [None] + sorted(make_boku2_sample.DAMAGE):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                folder = os.path.join(tmp, "S")
                make_boku2_sample.build_sample(folder)
                if kind:
                    make_boku2_sample.damage(folder, kind)
                out = os.path.join(tmp, "OUT")
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        n, _u, _d, _p = boku2.unpack(
                            os.path.join(folder, "BOKU2.IDX"),
                            os.path.join(folder, "BOKU2.IMG"), out,
                            os.path.join(folder, "BOKU2.CRC"))
                except ValueError:
                    continue                      # 索引そのものが読めない壊し方 (idx)
                got = self.landed(out)
                self.assertEqual(len(got), n,
                                 f"{kind}: {n} 個に切り分けたのに {len(got)} 個")


class TestTheSuffixWeAddDoesNotHideTheFile(unittest.TestCase):
    """**道具が付けた `~2` で、その先の段が取りこぼさないこと** (#247).

    `unpack` は名前がぶつかると `~2` を付ける (#245・#246)。ところが
    「名前で決まる規則」—— `.msg` かどうか / 0x8002 の読み方 / 文言の入れ物か ——
    は付いたままの名前で引いていたので、**`system.msg~2` が丸ごと落ちていた**。
    しかも `text` は「文字表で全部読めました」と言うので、気づけない。
    """

    @staticmethod
    def sample_with_a_duplicate_name(tmp: str) -> tuple[str, str]:
        """練習用データを作り、検査値ファイルの 15 番の名前を 14 番と同じにする.

        実物ではフォルダが 116 あってファイル名だけが重なるので、`--names-from-crc`
        を使えば**必ず**この形になる。@returns (一式のフォルダ, 検査値ファイル)
        """
        import struct
        import boku2
        import make_boku2_sample
        folder = os.path.join(tmp, "S")
        make_boku2_sample.build_sample(folder)
        with open(os.path.join(folder, "BOKU2.CRC"), "rb") as fh:
            raw = bytearray(fh.read())
        dir_start = struct.unpack_from("<5I", raw, 0)[1]
        at = dir_start + 15 * boku2.CRC_ENTRY + 8          # namemsg.msg → system.msg
        raw[at:at + boku2.CRC_ENTRY - 8] = b"\0" * (boku2.CRC_ENTRY - 8)
        raw[at:at + len(b"system.msg")] = b"system.msg"
        crc_path = os.path.join(tmp, "dup.crc")
        with open(crc_path, "wb") as fh:
            fh.write(raw)
        make_boku2_sample.damage(folder, "allnames")        # 索引の名前が読めない吸い出し
        return folder, crc_path

    def test_a_msg_with_a_suffix_is_still_read(self):
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            folder, crc_path = self.sample_with_a_duplicate_name(tmp)
            out = os.path.join(tmp, "OUT")
            with contextlib.redirect_stderr(io.StringIO()):
                boku2.unpack(os.path.join(folder, "BOKU2.IDX"),
                             os.path.join(folder, "BOKU2.IMG"), out, crc_path)
            # **材料が弱くないこと**: `~2` の付いた `.msg` が本当にできている
            self.assertTrue(os.path.exists(os.path.join(out, "system.msg~2")),
                            f"材料が弱い: {sorted(os.listdir(out))}")
            picked = [os.path.basename(p) for p in boku2.expand_inputs([out])]
            self.assertIn("system.msg~2", picked,
                          f"`~2` の付いた .msg を拾っていない: {sorted(picked)}")
            rows = boku2.text_rows(os.path.join(out, "system.msg~2"), None)
            self.assertTrue(rows, "拾っても中身が読めていない")

    def test_the_id_keeps_the_suffix_so_two_files_do_not_share_one(self):
        """`system.msg` と `system.msg~2` の id がぶつかると、校正で直した行が
        どちらのファイルのものか分からなくなる."""
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            folder, crc_path = self.sample_with_a_duplicate_name(tmp)
            out = os.path.join(tmp, "OUT")
            with contextlib.redirect_stderr(io.StringIO()):
                boku2.unpack(os.path.join(folder, "BOKU2.IDX"),
                             os.path.join(folder, "BOKU2.IMG"), out, crc_path)
            ids = [r[0] for p in boku2.expand_inputs([out])
                   for r in boku2.text_rows(p, None)]
            self.assertEqual(len(set(ids)), len(ids),
                             "id がぶつかっている: "
                             + str(sorted(i for i in ids if ids.count(i) > 1)))
            self.assertTrue(any(i.startswith("system~2:") for i in ids), sorted(set(ids))[:8])

    def test_the_page_break_rule_still_applies_to_a_suffixed_name(self):
        """`item_info.msg~2` で 0x8002 を待ち時間として読むと、**本文が 1 字ずれる**."""
        import boku2
        self.assertTrue(boku2.is_alt_break("item_info.msg~2"))
        self.assertTrue(boku2.is_alt_break("OUT/system/ITEM_INFO.MSG~13"))
        codes = [0x0005, 0x8002, 0x0006, 0x8000]
        glyphs = [chr(0x3042 + i) for i in range(16)]
        self.assertEqual(boku2.decode(codes, glyphs, alt=True),
                         boku2.decode(codes, glyphs, alt=boku2.is_alt_break("item_info.msg~2")))
        self.assertNotEqual(boku2.decode(codes, glyphs, alt=True),
                            boku2.decode(codes, glyphs, alt=False),
                            "材料が弱い: この符号では 2 つの読み方が同じ答えになる")

    def test_a_container_with_a_suffix_is_still_a_container(self):
        """`diary.bin~2` を入れ物として拾わないと、`check` が「入れ物はありません」と言う."""
        import boku2
        self.assertEqual(boku2.plain_name("OUT/x/DIARY.BIN~2"), "diary.bin")
        self.assertIn(boku2.plain_name("diary.bin~2"), boku2.TEXT_CONTAINERS)

    def test_only_a_trailing_suffix_is_stripped(self):
        """実物には `~saveload` のように**先頭に `~` が付く**フォルダがある
        (公開ソースの SJIS_FILES = `system\\~saveload\\2.bin`)。そこを削ってはいけない."""
        import boku2
        self.assertEqual(boku2.plain_name("system/~saveload/2.bin"), "2.bin")
        self.assertEqual(boku2.plain_name("~saveload"), "~saveload")
        self.assertEqual(boku2.plain_name("a~2b.msg"), "a~2b.msg")
        self.assertEqual(boku2.plain_name("x.msg~"), "x.msg~")
        self.assertEqual(boku2.plain_name("x.msg~12"), "x.msg")

    def test_a_name_that_matches_the_index_is_not_treated_as_a_clash(self):
        """**いちばん普通の形**: 索引の名前も読めて、検査値ファイルの名前と一致する。
        自分の名前を「ぶつかった」と数えると**全ファイルに `~2` が付き**、
        その全部が `.msg` として拾われなくなる (#247)."""
        import boku2
        import make_boku2_sample
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            out = os.path.join(tmp, "OUT")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                n, _u, _d, paths = boku2.unpack(
                    os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG"),
                    out, os.path.join(folder, "BOKU2.CRC"))
            # **材料が弱くないこと**: 検査値の名前を本当に全部当てている
            self.assertIn(f"検査値ファイルの名前を {n} 件当てました", err.getvalue())
            bumped = [p for p in paths if re.search(r"~\d+$", p)]
            self.assertFalse(bumped, f"自分と同じ名前を当てただけで `~2` が付いた: {bumped}")
            self.assertIn("system/system.msg", paths,
                          f"フォルダ付きの名前が消えている: {paths[:6]}")
            self.assertNotIn("`~2` を付けました", err.getvalue(), err.getvalue())

    def test_it_does_not_say_it_matched_and_matched_nothing_at_once(self):
        """「N 件当てました」と「1 件も当たりませんでした」を同時に言わない (#247)."""
        import boku2
        import make_boku2_sample
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                boku2.unpack(os.path.join(folder, "BOKU2.IDX"),
                             os.path.join(folder, "BOKU2.IMG"),
                             os.path.join(tmp, "OUT"), os.path.join(folder, "BOKU2.CRC"))
            said = err.getvalue()
            self.assertIn("件当てました", said)
            self.assertNotIn("1 件も当たりませんでした", said,
                             f"言っていることが食い違っている:\n{said}")

    def test_the_two_sides_strip_the_same_way(self):
        """画面と一括処理で同じ名前を同じに畳むこと (片側だけ直すと答えが割れる)."""
        import json
        import shutil
        import subprocess
        import boku2

        node = shutil.which("node")
        if not node:
            self.skipTest("node が無い")
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        i = app.find("function plainName(")
        self.assertGreater(i, 0, "app.js に plainName が無い")
        src = app[i:app.index("\n}\n", i) + 3]
        names = ["system.msg", "system.msg~2", "OUT/a/DIARY.BIN~13", "~saveload",
                 "system/~saveload/2.bin", "a~2b.msg", "x.msg~", "1.bin", "#12", "",
                 "a\\b\\item_info.MSG~2"]
        res = subprocess.run(
            [node, "-e", src + f"console.log(JSON.stringify({json.dumps(names)}.map(plainName)));"],
            capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), [boku2.plain_name(n) for n in names])


class TestWhenNothingIsPickedTheAdviceFitsTheCause(unittest.TestCase):
    """**「1 行も見つかりません」の次の 1 行が、原因に合っていること** (#248).

    今までは「先に `unpack` / `maps` を回してください」の一点張りだった。ところが
    **社長の実物はまさにこの形**で (#1・#3)、`unpack` は正しく切り分けたのに
    名前が `#0 #1 …` になる。そこへ「先に `unpack` を回せ」と言うのは、
    **いまやったことをもう一度やらせる**ということ。
    """

    @staticmethod
    def unpack_with_no_names(tmp: str) -> str:
        import boku2
        import make_boku2_sample
        folder = os.path.join(tmp, "S")
        make_boku2_sample.build_sample(folder)
        make_boku2_sample.damage(folder, "allnames")
        out = os.path.join(tmp, "OUT")
        with contextlib.redirect_stderr(io.StringIO()):
            boku2.unpack(os.path.join(folder, "BOKU2.IDX"),
                         os.path.join(folder, "BOKU2.IMG"), out)
        return out

    @staticmethod
    def run_tool(*args: str) -> str:
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), *args],
                           capture_output=True, text=True, cwd=REPO)
        return r.stdout + r.stderr

    def test_numbered_names_are_not_answered_with_run_unpack_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpack_with_no_names(tmp)
            # **材料が弱くないこと**: 番号だけの名前が本当に 20 個できている
            landed = sorted(os.listdir(out))
            self.assertEqual(len(landed), 20, landed)
            self.assertTrue(all(re.fullmatch(r"#\d+", f) for f in landed), landed)

            said = self.run_tool("text", out, "-o", os.path.join(tmp, "a.tsv"))
            self.assertIn("名前が番号だけ", said, said)
            self.assertIn("--names-from-crc", said, said)
            self.assertNotIn("先に unpack", said,
                             f"いまやったことをもう一度やらせている:\n{said}")

    def test_an_empty_place_is_still_answered_with_run_unpack_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "EMPTY")
            os.makedirs(empty)
            said = self.run_tool("text", empty, "-o", os.path.join(tmp, "a.tsv"))
            self.assertIn("先に unpack", said, said)
            self.assertNotIn("名前が番号だけ", said, said)

    def test_the_wrong_place_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = os.path.join(tmp, "TM2")
            os.makedirs(other)
            for n in ("a.tm2", "b.tm2"):
                with open(os.path.join(other, n), "wb") as fh:
                    fh.write(b"\0" * 64)
            said = self.run_tool("text", other, "-o", os.path.join(tmp, "a.tsv"))
            self.assertIn("場所が違うかもしれません", said, said)
            self.assertIn("a.tm2", said, said)
            self.assertNotIn("先に unpack", said, said)
            self.assertNotIn("名前が番号だけ", said, said)

    def test_text_and_used_say_the_same_thing(self):
        """#232 と同じ約束: 同じ困りごとを 2 つの道具が別の言葉で言わない."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpack_with_no_names(tmp)
            a = self.run_tool("text", out, "-o", os.path.join(tmp, "a.tsv"))
            b = self.run_tool("used", out)
            import boku2
            for line in boku2.nothing_picked_note([out]):
                self.assertIn(line.strip(), a, f"text が言っていない: {line}")
                self.assertIn(line.strip(), b, f"used が言っていない: {line}")

    def test_the_example_names_do_not_pretend_there_are_more(self):
        """3 個しか無いのに `…` を付けない (#96 の「数だけ出すな」と同じ筋)."""
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("a.tm2", "b.tm2"):
                with open(os.path.join(tmp, n), "wb") as fh:
                    fh.write(b"\0")
            said = "\n".join(boku2.nothing_picked_note([tmp]))
            self.assertIn("a.tm2, b.tm2)", said, said)
            self.assertNotIn("…", said, said)

    def test_a_suffixed_number_name_still_counts_as_numbered(self):
        """`#12~2` も番号だけの名前 (#245〜#247 で付く形)."""
        import boku2
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("#0", "#12~2"):
                with open(os.path.join(tmp, n), "wb") as fh:
                    fh.write(b"\0")
            total, numbered, _shown = boku2.files_present([tmp])
            self.assertEqual((total, numbered), (2, 2))


class TestBoku2Sample(unittest.TestCase):
    """docs/10 の手順を、練習用データ (tools/make_boku2_sample.py) で最後まで通す."""

    def test_recipe_round_trip(self):
        import glob
        import shutil
        import subprocess
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "BOKU2SAMPLE")
            answer = make_boku2_sample.build_sample(sample)
            for name in ["BOKU2.IDX", "BOKU2.IMG", "font.txt", "answer.tsv", "MAP/M_A01000.BIN"]:
                self.assertTrue(os.path.exists(os.path.join(sample, name)), name)

            tool = os.path.join(REPO, "tools", "boku2.py")
            run = lambda *a: subprocess.run([sys.executable, tool, *a], capture_output=True, text=True, cwd=REPO)
            out = os.path.join(tmp, "OUT")
            res = run("unpack", os.path.join(sample, "BOKU2.IDX"), os.path.join(sample, "BOKU2.IMG"), out)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(os.path.exists(os.path.join(out, "system", "system.msg")))
            self.assertTrue(os.path.exists(os.path.join(out, "system", "namemsg", "namemsg.msg")))
            self.assertTrue(os.path.exists(os.path.join(out, "system", "submenu", "msg", "config", "config.msg")))
            self.assertTrue(os.path.exists(os.path.join(out, "readme.bin")))      # 入れ子が閉じて根に戻る
            self.assertTrue(os.path.exists(os.path.join(out, "00diary", "nik002.tm2")))
            self.assertTrue(os.path.exists(os.path.join(out, "system", "bk_font.tms")))
            res = run("maps", *glob.glob(os.path.join(sample, "MAP", "*.BIN")), "-o", os.path.join(out, "maps"))
            self.assertEqual(res.returncode, 0, res.stderr)
            # フォルダを渡せば、深い所の *.msg とマップの 1.bin を全部拾う (docs/10 のコマンドそのまま)
            tsv = os.path.join(tmp, "all.tsv")
            res = run("text", out, "-f", os.path.join(sample, "font.txt"), "-o", tsv)
            self.assertEqual(res.returncode, 0, res.stderr)
            ids = [r["id"] for r in scrp.read_tsv(tsv)]
            self.assertIn("config:0", ids)
            self.assertIn("namemsg:0", ids)
            self.assertIn("diary#0:2", ids)                 # 日記の入れ物 (12 バイト刻み) の 0 番
            self.assertIn("M_A01000:0-1", ids)             # マップの会話はマップ名が id
            self.assertIn("M_A02000:0-0", ids)
            self.assertFalse(any(i.startswith("1:") for i in ids))

            # 答えと突き合わせる: id と本文が全部一致すること (音声の番号の行は TSV に入らない)
            got = {r["id"]: r["original"] for r in scrp.read_tsv(tsv)}
            want = {rid: text for rows in answer.values() for rid, text in rows
                    if not text.startswith("<VOICE:")}
            self.assertEqual(got, want)

            # 診断: 練習データは問題なし。壊したものは → で場所を示す
            res = run("check", sample)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("DFI: 期待どおり", res.stdout)
            self.assertIn("問題なし", res.stdout)
            self.assertIn("[フォント] system/bk_font.tms: TIM2 (位置 0x80)", res.stdout)
            self.assertIn("[入れ物] 文言の入れ物: あり diary.bin, saveload.bin,"
                          " on_mem_event.bin, fish_on_mem.bin", res.stdout)
            # 並びは TEXT_CONTAINERS のとおり (画面の CONTAINERS と同じ順。#104)
            # 4 つの入れ物が全部そろったので「見つからない」は出ない (#117)
            self.assertNotIn("見つからない", res.stdout)
            # フォルダ付きの名前が出ること。docs/10 が 20 分の所で見ろと言っている
            # のはこれで、以前は生の名前を並べていてフォルダが付かなかった (#104)
            self.assertIn("最初の名前: diary.bin / 00diary/nik000.tm2"
                          " / 00diary/nik001.tm2",
                          res.stdout)
            self.assertIn("1 番が会話だった 2 件", res.stdout)
            self.assertNotIn("はじめから", res.stdout)          # 本文は出さない
            broken = os.path.join(tmp, "BROKEN")
            shutil.copytree(sample, broken)
            with open(os.path.join(broken, "MAP", "M_A01000.BIN"), "r+b") as fh:
                fh.write(b"\xff" * 16)
            # 本体側の .msg を壊す: 読めない例として名前と先頭 16 バイトが出る
            b_idx = open(os.path.join(broken, "BOKU2.IDX"), "rb").read()
            b_size = os.path.getsize(os.path.join(broken, "BOKU2.IMG"))
            sysmsg = next(e for e in boku2.read_dfi(b_idx, b_size) if e["path"] == "system/system.msg")
            with open(os.path.join(broken, "BOKU2.IMG"), "r+b") as fh:
                fh.seek(sysmsg["at"])
                fh.write(b"\xee" * 16)
            res = run("check", broken)
            self.assertEqual(res.returncode, 1)
            self.assertIn("M_A01000.BIN: FF FF", res.stdout)
            self.assertIn("→ 読めない .msg の例: system/system.msg 先頭 16 バイト EE EE", res.stdout)
            # **中身を書き換えたので、ゲーム自身の検査値とも食い違う** (#239)。
            # 3 件目はそれ。読めない形だと言う前に「中身が変わっている」と分かる
            self.assertIn("検査値が 1 件合いません", res.stdout)
            self.assertIn("system/system.msg はこちら 0x", res.stdout)
            self.assertIn("確認事項 3 件", res.stdout)
            res = run("check", tmp)                            # 索引が無いフォルダ
            self.assertEqual(res.returncode, 1)
            self.assertIn("揃っていません", res.stdout)

            # フォント一覧 → 校正 (フォントに無い文字の検査つき) が通る
            fl = os.path.join(tmp, "font_chars.txt")
            res = run("fontlist", os.path.join(sample, "font.txt"), "-o", fl)
            self.assertEqual(res.returncode, 0, res.stderr)
            res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv,
                                  "--font-chars", fl], capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)   # 練習データは指摘ゼロのはず
            self.assertNotIn("Traceback", res.stderr)

            # フォント画像は TIM2 として読める (ブラウザ側の tim2 ブロック)
            node = shutil.which("node")
            if node:
                tms = os.path.join(out, "system", "bk_font.tms")
                script = ("const fs=require('fs');const src=fs.readFileSync('web/app.js','utf8');"
                          "const s=src.indexOf('/* @extract-start tim2 */'),e=src.indexOf('/* @extract-end tim2 */');"
                          "const u32le=(b,p)=>(b[p]|(b[p+1]<<8)|(b[p+2]<<16)|(b[p+3]<<24))>>>0;"
                          "const u16le=(b,p)=>b[p]|(b[p+1]<<8);"
                          "const m=new Function('u32le','u16le',src.slice(s,e)+'\\nreturn {findTim2,parseTim2};')(u32le,u16le);"
                          f"const b=fs.readFileSync({tms!r});const at=m.findTim2(b);const t=m.parseTim2(b,at);"
                          "console.log(JSON.stringify([at,t.pictures[0].width,t.pictures[0].height]));")
                res = subprocess.run([node, "-e", script], capture_output=True, text=True, cwd=REPO)
                self.assertEqual(res.returncode, 0, res.stderr)
                at, w, h = json.loads(res.stdout)
                self.assertEqual(at, 0x80)
                self.assertEqual(w, make_boku2_sample.COLS * make_boku2_sample.CELL)   # 23 列 × 刻み 22 (reprint.py)
                self.assertEqual(h % make_boku2_sample.CELL, 0)


class TestDocs(unittest.TestCase):
    """文書が壊れていないこと: 参照先が実在し、画面のタブが全部説明されている."""

    @staticmethod
    def _docs():
        import glob
        return sorted(glob.glob(os.path.join(REPO, "docs", "*.md"))) + [os.path.join(REPO, "README.md")]

    def test_referenced_files_exist(self):
        import re
        pat = re.compile(r"(?<![\w/])((?:docs|tools|tests|exercises|answers|data|web)/[\w\-.]+\.[a-z]+)")
        missing = []
        for path in self._docs():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for ref in set(pat.findall(text)):
                if "*" in ref or ref.endswith((".tsv.po",)):
                    continue
                if not os.path.exists(os.path.join(REPO, ref)):
                    missing.append(f"{os.path.relpath(path, REPO)} → {ref}")
        self.assertEqual(missing, [])

    def test_markdown_links_resolve(self):
        import re
        broken = []
        for path in self._docs():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for target in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", text):
                full = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if not os.path.exists(full):
                    broken.append(f"{os.path.relpath(path, REPO)} → {target}")
        self.assertEqual(broken, [])

    def test_every_diagnosis_arrow_is_explained(self):
        """check が出し得る → の行が、全部 docs/10 の「診断の → の行の読み方」にあること (#63).

        道具に → が増えたのに説明が増えていない、を防ぐ。boku2.py の say("→ …") を拾って照合する."""
        import re
        with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
            src = fh.read()
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        # `say("→ …")` だけでなく、**文字列そのものが → で始まるもの全部**を拾う (#178)。
        # 知らせの文を組み立てる助けの関数 (`dropped_note`) に移すと、`say(` の形では
        # 見つからず、**新しい → が説明の無いまま増える**。見張りは道具の書き方に
        # 合わせるのではなく、出る言葉のほうを見る
        arrows = re.findall(r'(?:say\(|return \(|print\(|lines\.append\(|^\s+)f?"→ ([^"{]+)',
                            src, re.M)
        self.assertGreaterEqual(len(arrows), 8, arrows)
        keys = set()
        for head in arrows:
            key = re.split(r"[。、(:]", head)[0].strip()[:14]      # 行頭の言い回しで照合
            keys.add(key)
            self.assertTrue(key in doc, f"docs/10 に説明が無い → の行: {head}")
        # ブラウザの要約の → も、同じ言い回しで、CLI に無いものを増やしていないこと (#64)
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        js_arrows = re.findall(r'lines\.push\([`"]→ ([^`"$]+)', app)
        self.assertGreaterEqual(len(js_arrows), 6, js_arrows)
        for head in js_arrows:
            key = re.split(r"[。、(:]", head)[0].strip()[:14]
            self.assertIn(key, keys, f"ブラウザだけにある → の行 (CLI と docs/10 に合わせる): {head}")

    def test_the_numbers_written_in_the_docs_are_the_real_ones(self):
        """**文書に書いた数が、道具の実際の数と合っていること** (#191).

        #184 は「→ は 12 種類」(本当は 24)、#185 は「タブ 11 枚」の裏付け無し、
        #190 は `broken.py` の「5 通り」(本当は 6) —— **同じ数を 3 回別々に直して**
        いました。今回まとめて掃いたら、**README の「5 通り」がまだ残っていました**
        (#184 で exercises を、#190 で broken.py を直したのに、README は 2 回とも
        見落とし)。1 か所ずつ直す限り、次も必ずどこかに残ります。

        数は**道具から取って**、文書のほうを回ります。

        **docs/09 の記録欄は見ません。** あそこはその時どうだったかの記録で、
        「ヘッドレス 15」は書いた時点では本当のことでした。**過去の記録を
        今の数に書き換えるのは、記録を壊すこと**です。見るのは生きている文章だけ。
        """
        import glob
        import importlib.util
        import re
        import unittest as ut

        def load(path, name):
            spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        mk = load("tools/make_boku2_sample.py", "mk_numbers")
        e2e = load("tests/e2e/run_all.py", "e2e_numbers")
        with open(os.path.join(REPO, "web", "index.html"), encoding="utf-8") as fh:
            tabs = len(re.findall(r'role="tab" data-tab="\w+"', fh.read()))
        tests_here = ut.TestLoader().loadTestsFromModule(sys.modules[__name__]).countTestCases()

        #: (文書での書かれ方, 本当の数, 何の数か)
        claims = [
            (r"壊し方は (\d+) 通り", len(mk.DAMAGE), "--break の壊し方"),
            (r"などで (\d+) 通り", len(mk.DAMAGE), "--break の壊し方"),
            (r"やること \((\d+) 通り", len(mk.DAMAGE), "--break の壊し方"),
            (r"(\d+) 通りとも", len(mk.DAMAGE), "--break の壊し方"),
            (r"タブは (\d+) 枚", tabs, "画面のタブ"),
            (r"文字表は全部で (\d+) 字", boku2.FONT_GLYPHS, "文字表の字数"),
            (r"1 行 (\d+) 字", boku2.FONT_COLS, "1 行の字数"),
            (r"刻み (\d+) ドット", boku2.FONT_CELL, "文字の刻み"),
            (r"先頭 (\d+) 件の `\.msg`", boku2.MSG_CHECK_FILES, "check が中身まで開く .msg の数"),
            (r"ヘッドレス (\d+)", len(e2e.CHECKS), "ヘッドレスの検査"),
            (r"テスト (\d+) 件", tests_here, "Python の検査"),
        ]

        def living(path):
            """その文書の**生きている部分**。docs/09 は記録欄より前だけ."""
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if os.path.basename(path).startswith("09-"):
                return text.split("## 進め方 (自走ループの記録欄)")[0]
            return text

        docs = (sorted(glob.glob(os.path.join(REPO, "docs", "*.md")))
                + [os.path.join(REPO, "README.md"),
                   os.path.join(REPO, "exercises", "README.md")])
        self.assertGreaterEqual(len(docs), 8, "文書を拾えていない")
        for pattern, want, label in claims:
            found = [(p, m.group(0), int(m.group(1)))
                     for p in docs for m in re.finditer(pattern, living(p))]
            # **0 件で緑にしない。** 書き方が変わって当たらなくなったら、
            # 数が合っているのではなく**見ていない**だけになる
            self.assertTrue(found, f"{label} ({pattern}) の書かれ方が文書に 1 件も無い")
            for path, text, got in found:
                self.assertEqual(got, want,
                                 f"{os.path.relpath(path, REPO)} の「{text}」は "
                                 f"{label} {want} と合っていません")

    def test_the_arrow_table_is_the_one_place_to_look_things_up(self):
        """**docs/10 の → の表が、道具の出す → と 1 対 1 で揃っていること** (#184).

        #63 の上の検査は「docs/10 の**どこかに**あるか」しか見ていませんでした。
        そのため `maps` / `unpack` / `text` の → 6 種類は、下の「困ったとき」の表に
        しか無いのに緑のまま。ところが表の前書きは「`→` は次の 12 種類」と言い切り、
        課題 9 は「`→` の行はこの表で引け」と指示しています。**引けない行がある**。

        ここでは 3 方向から見ます:

        * 道具が出す → は、どれも**この表に**行がある (どこかに、ではない)
        * 表の行は、どれも道具が実際に出す (消した → の説明が残っていない)
        * 前書きの「N 種類」が、表の行数と合っている

        行と → は、**行の固定部分が → の中にこの順で出るか**で突き合わせます
        (行は `N` や `…` で数字や名前を伏せた要約なので、文字どおりには一致しない)。
        """
        import re

        def norm(s):
            return re.sub(r"\s+", " ", s.replace("**", "").replace("`", "")).strip()

        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        body = doc.split("### 診断の `→` の行の読み方")[1].split("\n\n## ")[0]
        rows = [norm(ln.split("|")[1]) for ln in body.splitlines()
                if ln.startswith("| ") and not ln.startswith("| ---")
                and not ln.startswith("| `→` の行")]

        # 道具の → は、隣り合う文字列をつないだ**全文**で見る。先頭だけだと
        # 「[フォント] 幅が …」の 2 種類 (狭すぎる / 広すぎる) が見分けられない。
        # **取り出す側だけでなく校正の側も見る** (#186)。前書きは「どの道具のものでも」
        # と言っているのに、見ていたのは boku2.py だけだった
        one = re.compile(r'\s*f?"((?:[^"\\]|\\.)*)"')
        arrows = set()
        for tool in ("boku2.py", "proofread.py", "compare_tsv.py"):
            with open(os.path.join(REPO, "tools", tool), encoding="utf-8") as fh:
                src = fh.read()
            for m in re.finditer(
                    r'(?:say\(|return \(|print\(|lines\.append\(|^\s+)(?=f?"(?:\\n)?→ )',
                    src, re.M):
                pos, parts = m.end(), []
                while True:
                    s = one.match(src, pos)
                    if not s:
                        break
                    parts.append(s.group(1))
                    pos = s.end()
                joined = re.sub(r"\{[^}]*\}", "…", "".join(parts)).replace("\\n", "")
                arrows.add(norm(joined.split("→ ", 1)[1]))

        self.assertGreaterEqual(len(arrows), 20, "→ を拾えていない (0 件なら必ず一致する)")
        self.assertGreaterEqual(len(rows), 20, "表の行を拾えていない")

        def fixed_parts(row):
            return [c.strip() for c
                    in re.split(r"…|(?<= )N(?= |%|$)|(?<= )M(?= |$)", row)
                    if len(c.strip()) >= 2]

        def fits(row, arrow):
            at = 0
            for c in fixed_parts(row):
                i = arrow.find(c, at)
                if i < 0:
                    return False
                at = i + len(c)
            return True

        used = set()
        for arrow in sorted(arrows):
            hit = [r for r in rows if fits(r, arrow)]
            self.assertTrue(hit, f"この → を引ける行が表にありません: {arrow[:60]}")
            self.assertEqual(len(hit), 1,
                             f"1 つの → に表の行が {len(hit)} つ当たります: {arrow[:40]}")
            used |= set(hit)
        for row in rows:
            self.assertTrue(row in used, f"道具が出さない → の行が表に残っています: {row[:60]}")

        # 前書きの数え上げ。ここが合っていないと、読者は表を最後まで読まない
        said = re.search(r"全部で (\d+) 種類", body)
        self.assertTrue(said, "前書きが → の数を言っていません")
        self.assertEqual(int(said.group(1)), len(rows),
                         f"前書きは {said.group(1)} 種類、表は {len(rows)} 行")

        # 課題 9 の「N 通りの壊し方」も、実際の --break の選択肢と合っていること
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mk_sample_arrows", os.path.join(REPO, "tools", "make_boku2_sample.py"))
        mk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mk)
        with open(os.path.join(REPO, "exercises", "README.md"), encoding="utf-8") as fh:
            ex = fh.read()
        ex9 = ex.split("## 課題 9")[1].split("\n## ")[0]
        kinds = re.search(r"壊し方は (\d+) 通り", ex9)
        self.assertTrue(kinds, "課題 9 が壊し方の数を言っていません")
        self.assertEqual(int(kinds.group(1)), len(mk.DAMAGE),
                         f"課題 9 は {kinds.group(1)} 通り、--break は {len(mk.DAMAGE)} 通り")
        for kind in mk.DAMAGE:
            self.assertTrue(f"--break {kind}" in ex9,
                            f"課題 9 に --break {kind} の行がありません")

    def test_the_road_ends_at_the_tsv_and_the_docs_agree(self):
        """docs/10 が道の終わりを言い、README がそれと食い違わないこと (#78).

        手順が 3 (校正) で終わったあと何をするのかが書かれておらず、一方 README は
        「実データへの入れ直しまで」と読める書き方だった。docs/01〜03 の自作データの
        話なのだが、僕の夏休み 2 から来た読者には実物への入れ直しがあるように見える。
        素人をいちばん間違った方向へ送る食い違いなので、検査で止める。
        """
        howto = open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8").read()
        readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
        self.assertTrue("## 4. ここで終わり" in howto, "docs/10 に道の終わりの節が無い")
        self.assertIn("入れ直す手順は\n用意していません", howto.replace("\r", ""),
                      "docs/10 が「入れ直しは無い」と言い切っていない")
        self.assertIn("docs/05", howto.split("## 4. ここで終わり")[1].split("## ")[0],
                      "終わりの節から権利面 (docs/05) に繋がっていない")
        # README の「入れ直し」は、必ず自作の練習データ限定だと分かる形で書くこと
        self.assertTrue("入れ直しができるのは自作の練習データに対してだけ" in readme,
                        "README の冒頭が実物への入れ直しがあるように読める")
        for line in readme.splitlines():
            if "入れ直す" in line and "insert_text.py" in line:
                nearby = readme[max(0, readme.index(line) - 200):readme.index(line) + 400]
                self.assertTrue("練習データ" in nearby or "SCRIPT" in nearby or "work/" in nearby,
                                f"入れ直しの行が練習データの話だと分からない: {line}")

    def test_every_proofread_rule_is_explained(self):
        """proofread.py が出し得る rule 名が、全部 docs/04 の「検査の一覧」にあること (#65)."""
        import re
        with open(os.path.join(REPO, "tools", "proofread.py"), encoding="utf-8") as fh:
            src = fh.read()
        with open(os.path.join(REPO, "docs", "04-校正とQA.md"), encoding="utf-8") as fh:
            doc = fh.read()
        rules = set(re.findall(r'"(?:ERROR|WARN|INFO)",\s*"([a-z_]+)"', src))
        rules |= set(re.findall(r'\("(?:ERROR|WARN)",\s*"([a-z_]+)"\)', src))
        rules |= set(re.findall(r'entry\.get\([^)]*\),\s*"([a-z_]+)"', src))
        self.assertGreaterEqual(len(rules), 11, sorted(rules))
        table = doc.split("### 検査の一覧", 1)[1].split("\n## ", 1)[0]
        for rule in sorted(rules):
            self.assertIn(f"`{rule}`", table, f"docs/04 の検査の一覧に無い rule: {rule}")
        # 逆に、表にあるのに道具が出さない名前も無いこと (名前が変わったら表も変える)
        for name in re.findall(r"^\| `([a-z_]+)` \|", table, re.M):
            if name == "rule":
                continue                                   # 見出しの行
            self.assertIn(name, rules, f"表にあるが道具が出さない rule: {name}")

    @staticmethod
    def code_only(js: str) -> str:
        """コメントを取り除いた JavaScript を返す (#93).

        「画面にこの文言があるか」をファイル全体への部分一致で見ていたので、
        **同じ文言がコメントにも書いてある所**では、実際に出る側だけを書き換えても
        検査が通った (`文字表に無い番号` は説明のコメントと本物の両方にあった)。
        コメントは画面に出ない。出る側だけを見る。

        取りこぼすと検査が落ちる向きに倒れるので、多少荒くても安全側。
        """
        out, i, n = [], 0, len(js)
        while i < n:
            two = js[i:i + 2]
            if two == "/*":
                end = js.find("*/", i + 2)
                i = n if end < 0 else end + 2
            elif two == "//":
                end = js.find("\n", i)
                i = n if end < 0 else end
            else:
                out.append(js[i])
                i += 1
        return "".join(out)

    def test_the_comment_stripper_works(self):
        """コメントを外す処理そのものを確かめる (これが壊れると上の検査が素通しになる)."""
        js = 'a = "見える";\n/* 隠れる */ b = `出る`; // 行コメントも隠れる\n'
        code = self.code_only(js)
        self.assertIn("見える", code)
        self.assertIn("出る", code)
        self.assertNotIn("隠れる", code)

    def test_manual_mentions_the_screen_features(self):
        """画面にある主要な物 (要約の行、目盛りの色、ページ送り、入れ物の入れ子…) が、
        説明書 docs/07 にも書いてあること。画面だけ増えて説明書が古くなるのを防ぐ (#61).

        画面側はコメントを外してから探す。コメントに同じ文言が残っていると、
        実際に出る文言を変えても気づけない (#93 で踏んだ)。
        """
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            manual = fh.read()
        # 画面に出る文言は app.js と index.html のどちらにもあり得る。両方から
        # コメントを外して繋ぐ (「報告用の要約」は index.html のボタンで、app.js には
        # コメントとしてしか無かった。app.js だけ見ていて素通ししていた #93)
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = self.code_only(fh.read())
        with open(os.path.join(REPO, "web", "index.html"), encoding="utf-8") as fh:
            app += re.sub(r"<!--.*?-->", "", fh.read(), flags=re.S)
        pairs = [   # (画面の文言 (app.js にあること), 説明書の言い回し)
            ("文字表に無い番号", "文字表に無い番号"),
            ("橙の枠", "橙の枠"),
            ("ページ送り", "ページ送り"),
            ("フォルダの規則: 2 通り (stack / flag) で一致", "フォルダの規則: 2 通り (stack / flag) で一致"),
            ("これはマップの入れ物です", "入れ物の中の入れ物"),
            ("報告用の要約", "報告用の要約"),
            ("校正用の TSV をコピー", "校正用の TSV をコピー"),
            ("文字の番号を重ねる", "文字の番号を重ねる"),
        ]
        for on_screen, in_manual in pairs:
            # assertIn は失敗すると app.js 全体を出してしまうので assertTrue で見る
            self.assertTrue(on_screen in app,
                            f"画面側の文言が変わった (コメントではなく実際に出る所): {on_screen}")
            self.assertTrue(in_manual in manual, f"docs/07 に無い: {in_manual}")

    def test_every_tab_is_documented(self):
        """**画面のタブが、docs/07 の「タブの一覧」に、画面と同じ順で 1 行ずつあること** (#185).

        前は docs/07 の**全文**からタブ名を探していました。それだと「タイル」は
        本文に 22 回、「文字列」は 15 回出てくる普通の言葉なので、
        **タブの説明が 1 行も無くても緑**になります (実際、11 枚のうち 5 枚は
        どこにも説明がありませんでした)。#184 で踏んだのと同じ「全文から語句を
        探さない」の 4 度目。

        並び順まで見るのは、素人が画面と表を**左から突き合わせる**からです。
        """
        import re
        with open(os.path.join(REPO, "web", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        tabs = re.findall(r'role="tab" data-tab="\w+"[^>]*>([^<]+)<', html)
        self.assertGreaterEqual(len(tabs), 10)
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertTrue("## タブの一覧" in doc, "docs/07 に「タブの一覧」の節がありません")
        table = doc.split("## タブの一覧", 1)[1].split("\n## ", 1)[0]
        listed = [ln.split("|")[1].strip() for ln in table.splitlines()
                  if ln.startswith("| ") and not ln.startswith("| ---")
                  and not ln.startswith("| タブ ")]
        self.assertTrue(listed, "「タブの一覧」に行がありません (0 行なら必ず一致する)")
        # 画面と同じ順・同じ数。多い / 少ない / 入れ替わりが、そのまま差として出る
        self.assertEqual(listed, tabs,
                         f"画面のタブと「タブの一覧」が違います\n  画面: {tabs}\n  一覧: {listed}")
        # 「使いどき」の欄が空のまま足されていないこと (名前だけ並べても引けない)
        for ln in table.splitlines():
            if ln.startswith("| ") and not ln.startswith("| ---") and not ln.startswith("| タブ "):
                cells = [c.strip() for c in ln.strip("|").split("|")]
                self.assertEqual(len(cells), 3, f"欄の数が違う行: {ln[:40]}")
                self.assertGreaterEqual(len(cells[2]), 6, f"「使いどき」が空に近い行: {cells[0]}")

    def test_terms_are_unified(self):
        """利用者向けの文書と画面では、同じものを同じ言葉で呼ぶ."""
        forbidden = {
            "インデックス": "索引", "アーカイブ": "入れ物", "コンテナ": "入れ物",
            "グリフ表": "文字表", "文字リスト": "文字表", "ダイアログ": "会話",
            "エクストラクト": "取り出す", "アンパック": "切り分け",
        }
        targets = [os.path.join(REPO, "docs", "07-構造探査台.md"),
                   os.path.join(REPO, "docs", "10-僕夏2の手順.md"),
                   os.path.join(REPO, "web", "index.html")]
        hits = []
        for path in targets:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for bad, good in forbidden.items():
                if bad in text:
                    hits.append(f"{os.path.relpath(path, REPO)}: 「{bad}」→「{good}」")
        self.assertEqual(hits, [])
        # 文字表という言葉は、docs/01 の文字テーブルとの関係を docs/10 で一度は説明している
        with open(targets[1], encoding="utf-8") as fh:
            self.assertIn("文字テーブル", fh.read())

    def test_readme_quickstart_actually_runs(self):
        """README の「3 分で一周する」のコマンドを、書いてあるとおりに順に実行して通ること.

        コマンドを書き換えたのに README が古いまま、という食い違いを機械で見張る。
        `open`/`python3 tests/` の行は飛ばし、work/ に書くものはそのまま work/ に書く."""
        import re
        import shlex
        import subprocess
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        block = re.search(r"## 3 分で一周する.*?```bash\n(.*?)```", text, re.S)
        self.assertIsNotNone(block)
        cmds, cur = [], ""
        for line in block.group(1).split("\n"):
            line = line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            cur += line.rstrip("\\") + (" " if line.endswith("\\") else "")
            if not line.endswith("\\"):
                cmds.append(cur.strip())
                cur = ""
        ran = 0
        for cmd in cmds:
            if not cmd.startswith("python3 tools/") or "make_viewer.py" in cmd:
                continue                                    # ブラウザで開くだけの生成物は飛ばす
            # README は shell 前提 (パイプ・* の展開)。python3 だけ、このテストの python に差し替える
            shell_cmd = shlex.quote(sys.executable) + cmd[len("python3"):]
            res = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True, cwd=REPO)
            ok = (0, 1) if "proofread.py" in cmd else (0,)   # 校正は指摘があると 1 で終わる (題材が不具合入り)
            self.assertIn(res.returncode, ok, f"{cmd}\n{res.stdout[-800:]}\n{res.stderr[-800:]}")
            ran += 1
        self.assertGreaterEqual(ran, 12)

    def test_recipe_commands_actually_run(self):
        """docs/10 の bash ブロックのコマンドを、`実物/` を練習データに置き換えて順に実行する.

        手順書のコマンドが本当に通ることを見張る (#35 の README 版)。"""
        import re
        import shlex
        import subprocess
        import make_boku2_sample
        text = ""
        for name in (os.path.join("docs", "10-僕夏2の手順.md"), os.path.join("exercises", "README.md")):
            with open(os.path.join(REPO, name), encoding="utf-8") as fh:
                text += fh.read() + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "BOKU2SAMPLE")
            make_boku2_sample.build_sample(sample)
            subst = {
                "実物/": sample + "/",
                "work/BOKU2SAMPLE": sample,
                "work/OUT": f"{tmp}/OUT", "work/all.tsv": f"{tmp}/all.tsv",
                " OUT/": f" {tmp}/OUT/", " OUT ": f" {tmp}/OUT ",
                "-f font.txt": f"-f {sample}/font.txt",
                "fontlist font.txt": f"fontlist {sample}/font.txt",
                "table font.txt": f"table {sample}/font.txt",
                " all.tsv": f" {tmp}/all.tsv",
                "font_chars.txt": f"{tmp}/font_chars.txt",
                "boku2.tbl": f"{tmp}/boku2.tbl",
                "system.msg --table": f"{tmp}/OUT/system/system.msg --table",
            }
            ran = 0
            # 僕夏2 の部分だけ (課題 8 と docs/10)。練習用フォーマットの課題 1〜7 は README 側で見張る
            blocks = [b for b in re.findall(r"```bash\n(.*?)```", text, re.S)
                      if "boku2" in b or "BOKU2SAMPLE" in b]
            for block in blocks:
                cur = ""
                for line in block.split("\n"):
                    line = line.split("#", 1)[0].rstrip()
                    if not line.strip():
                        continue
                    cur += line.rstrip("\\") + (" " if line.endswith("\\") else "")
                    if line.endswith("\\"):
                        continue
                    cmd, cur = cur.strip(), ""
                    if not cmd.startswith("python3 tools/") or "make_boku2_sample.py" in cmd:
                        continue
                    for k, v in subst.items():
                        cmd = cmd.replace(k, v)
                    shell_cmd = shlex.quote(sys.executable) + cmd[len("python3"):]
                    res = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True, cwd=REPO)
                    self.assertEqual(res.returncode, 0, f"{cmd}\n{res.stdout[-800:]}\n{res.stderr[-800:]}")
                    ran += 1
            self.assertGreaterEqual(ran, 5)
            self.assertTrue(os.path.exists(os.path.join(tmp, "all.tsv")))

    def test_recipe_commands_use_existing_tools(self):
        import re
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        tools = set(re.findall(r"python3 (tools/[\w]+\.py)", doc))
        self.assertIn("tools/boku2.py", tools)
        self.assertIn("tools/make_boku2_sample.py", tools)
        for t in tools:
            self.assertTrue(os.path.exists(os.path.join(REPO, t)), t)


class TestCheckFontTable(unittest.TestCase):
    """check が、フォルダに font.txt があればその出来具合 (使われている番号のうち無い数) を出すこと (#67)."""

    def test_the_glyph_count_covers_the_whole_text_not_just_msg(self):
        """**文字表の判定は、本文ぜんぶを見て言うこと** (#231).

        `check` は `.msg` だけを見て「この範囲は全部読める」と言っていました。
        ところがこの作品の本文は、`.msg` のほかに**入れ物** (日記・保存画面・
        出来事・釣り) と **MAP の物語の会話**にあり、量はそちらのほうが多い。
        練習データですら、`.msg` に出てこない番号が入れ物に 21・会話に 21 あり、
        いちばん大きい番号は 91 ではなく 165 でした。**足りない文字表に
        太鼓判を押す**形なので、いちばん高くつく外れ方です。
        """
        import io
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            # 1. **材料が弱くないこと**を先に確かめる。3 つの出どころが同じ番号しか
            #    使っていなければ、union にしてもしなくても同じ数になり、検査は通る
            idx = open(os.path.join(folder, "BOKU2.IDX"), "rb").read()
            img_path = os.path.join(folder, "BOKU2.IMG")
            entries = boku2.read_dfi(idx, os.path.getsize(img_path))
            msg_used, box_used = set(), set()
            with open(img_path, "rb") as img:
                for e in entries:
                    base = os.path.basename(e["path"]).lower()
                    if not (base.endswith(".msg") or base in boku2.TEXT_CONTAINERS):
                        continue
                    img.seek(e["at"])
                    got = boku2.used_numbers_of(img.read(e["len"]), e["path"])
                    (msg_used if base.endswith(".msg") else box_used).update(got)
            map_used = boku2.scan_map_folder(os.path.join(folder, "MAP"))["used"]
            self.assertTrue(box_used - msg_used,
                            "材料が弱い: 入れ物に .msg と違う番号が 1 つも無い")
            self.assertTrue(map_used - msg_used,
                            "材料が弱い: MAP の会話に .msg と違う番号が 1 つも無い")
            everything = msg_used | box_used | map_used
            self.assertGreater(max(everything), max(msg_used),
                               "材料が弱い: いちばん大きい番号が .msg の中にある")

            # 2. `check` が出す数が、3 つを合わせた数であること
            out = io.StringIO()
            boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertTrue(f"本文で使われている番号 {len(everything)} 種" in got,
                            f"合わせた {len(everything)} 種で言っていない (.msg だけなら "
                            f"{len(msg_used)} 種)")
            self.assertTrue(f"(.msg {len(msg_used)} / 入れ物 {len(box_used)} / "
                            f"MAP の会話 {len(map_used)})" in got,
                            "どこから来た番号かの内訳が出ていない")
            # 3. いちばん大きい番号も、合わせたほうで言うこと (#230 の判定がこれで決まる)
            self.assertTrue(f"使われている文字番号の最大は {max(everything)}" in got,
                            f"最大が合わせた {max(everything)} になっていない")

            # 4. **MAP が無いときは、0 種ではなく「診ていない」**と言うこと。
            #    0 は「見た結果 0」に読めるが、ここは「見ていない」。意味が違う
            import shutil
            shutil.rmtree(os.path.join(folder, "MAP"))
            out = io.StringIO()
            boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertTrue("MAP は診ていない" in got,
                            "MAP が無いのに「診ていない」と言っていない")
            self.assertFalse("MAP の会話 0" in got,
                             "診ていないものを 0 種と書いている")

    def test_font_table_progress_line(self):
        import io
        import boku2
        import make_boku2_sample
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            out = io.StringIO()
            boku2.check(folder, out=out)
            self.assertRegex(out.getvalue(), r"\[文字表\] font\.txt: \d+ 字 / 本文で使われている番号 \d+ 種 \(\.msg \d+ / 入れ物 \d+ / MAP の会話 \d+\) のうち文字表に無い 0 種。この範囲は全部読める")
            # 文字表を先頭 3 字に削ると、無い番号が出る
            fp = os.path.join(folder, "font.txt")
            with open(fp, encoding="utf-8") as fh:
                full = fh.read()
            with open(fp, "w", encoding="utf-8") as fh:
                fh.write("".join(boku2.parse_glyph_table(full)[:3]))
            out = io.StringIO()
            boku2.check(folder, out=out)
            self.assertRegex(out.getvalue(), r"文字表に無い [1-9]\d* 種 \(例: \d+")
            self.assertIn("docs/10 の手順 3", out.getvalue())
            # 無ければ「まだ無い」
            os.remove(fp)
            out = io.StringIO()
            rc = boku2.check(folder, out=out)
            self.assertIn("[文字表] font.txt はまだ無い", out.getvalue())
            self.assertEqual(rc, 0)                         # 文字表の有無は「問題」には数えない


class TestCrcFileCrossCheck(unittest.TestCase):
    """`BOKU2.CRC` で、切り分けを**ゲーム自身の検査値**と突き合わせること (#239).

    実物には索引 (`BOKU2.IDX`) とは別に `BOKU2.CRC` があり、公開ソース
    (UNPACK.py の `getCRCdict`) が実際に読んでいます。中身は

      見出し 20 バイト: 項目数 / 名前の並びの位置 / 長さ / 検査値の並びの位置 / 長さ
      名前の並び: 1 項目 0x20 バイト (u16 ? / u16 検査値の番号 / u16 種別 / u16 番号 / 名前)
      検査値: 各ファイルの**先頭 0x80 バイト**の CRC-16 (CCITT-FALSE)

    これで 2 つのことが外から確かめられます: **ファイルの数**(索引とは別の出どころ)
    と、**切り分けた位置と中身**。実物が届いた日にいちばん強い裏付けになります。
    """

    def test_the_crc_matches_the_known_check_value(self):
        """CRC-16 の実装が世の中の値と合っていること (ここが違えば全部無意味)."""
        import boku2

        # CRC-16/CCITT-FALSE の決まった確かめ方 ("123456789" → 0x29B1)
        self.assertEqual(boku2.crc16_ccitt(b"123456789"), 0x29B1)
        self.assertEqual(boku2.crc16_ccitt(b""), 0xFFFF)

    def test_a_file_that_is_not_that_shape_is_not_read(self):
        """形が合わないものを**当てずっぽうで読まない** (別の版・別の作品)."""
        import boku2

        import struct

        for bad in (b"", b"\x00" * 8, b"\xff" * 64, bytes(range(64))):
            self.assertIsNone(boku2.read_crc_file(bad), f"読めないはずのものを読んだ: {bad[:8]!r}")

        # **長さだけ見ていては足りない。** 中の数がおかしいものも断ること
        body = b"\0" * 200
        cases = {
            "項目 0 件": struct.pack("<5I", 0, 20, 32, 52, 4) + body,
            "項目が多すぎる": struct.pack("<5I", 10 ** 6, 20, 32, 52, 4) + body,
            "名前の置き場が見出しの中": struct.pack("<5I", 1, 4, 32, 52, 4) + body,
            "検査値の長さが奇数": struct.pack("<5I", 1, 20, 32, 52, 5) + body,
            "名前の並びが項目数に足りない": struct.pack("<5I", 4, 20, 32, 52, 4) + body,
        }
        for why, raw in cases.items():
            self.assertIsNone(boku2.read_crc_file(raw), f"{why}: 読めないはずのものを読んだ")
        # **読めるものは読めること** (全部断るだけなら上の検査は意味が無い)
        ok = struct.pack("<5I", 1, 20, boku2.CRC_ENTRY, 20 + boku2.CRC_ENTRY, 2) + body
        self.assertIsNotNone(boku2.read_crc_file(ok), "正しい形を読めていない")

    def test_check_uses_it_and_says_both_things(self):
        """`check` が**数**と**中身**の両方を突き合わせて言うこと."""
        import io
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            crc_path = os.path.join(folder, "BOKU2.CRC")
            self.assertTrue(os.path.exists(crc_path), "練習データに BOKU2.CRC が無い")

            out = io.StringIO()
            rc = boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertEqual(rc, 0, f"無事な練習データが問題ありになった:\n{got[-500:]}")
            self.assertTrue("[検査値] BOKU2.CRC:" in got, f"検査値の行が無い:\n{got[-500:]}")
            self.assertTrue("同じ数" in got, f"数を突き合わせていない:\n{got[-500:]}")
            self.assertTrue("件すべて合いました" in got, f"中身を突き合わせていない:\n{got[-500:]}")

            # **1 つずらしたら気づくこと。** 索引も本体もそのままなので、
            # ここまでの段は全部通る。検査値だけが食い違いを見つける
            make_boku2_sample.damage(folder, "crc")
            out = io.StringIO()
            rc = boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertEqual(rc, 1, f"検査値が食い違うのに問題なしで終わった:\n{got[-500:]}")
            self.assertTrue("検査値が 1 件合いません" in got, f"食い違いを言っていない:\n{got[-500:]}")
            # **どのファイルで、どちらの値か**まで言うこと (報告の材料になる)
            self.assertTrue("はこちら 0x" in got and "検査値ファイル 0x" in got,
                            f"両方の値を出していない:\n{got[-500:]}")

    def test_a_count_that_disagrees_is_said(self):
        """**数が合わないときに言うこと** (中身が全部合っていても言う).

        項目数はこちらの切り分けとは別の所から出る数です。合わないなら、
        索引の読み方かこの数え方のどちらかが外れているということで、
        中身の検査値がたまたま合っていても、そこは別の話になります。
        """
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            with open(os.path.join(folder, "BOKU2.CRC"), "rb") as fh:
                crc = boku2.read_crc_file(fh.read())
            with open(os.path.join(folder, "BOKU2.IDX"), "rb") as fh:
                idx = fh.read()
            img_path = os.path.join(folder, "BOKU2.IMG")
            entries = boku2.read_dfi(idx, os.path.getsize(img_path))
            self.assertEqual(crc["n"], len(entries), "材料が弱い: もともと数が合っていない")
            with open(img_path, "rb") as img:
                # 1 件少なく数えた形 (索引の読み方が違えば普通に起きる)
                lines, problems = boku2.crc_report(crc, entries[:-1], img, len(entries))
            got = "\n".join(lines)
            self.assertGreaterEqual(problems, 1, f"数が合わないのに問題に数えていない:\n{got}")
            self.assertTrue("合いません" in got, f"合わないと言っていない:\n{got}")
            self.assertTrue(str(crc["n"]) in got and str(len(entries) - 1) in got,
                            f"両方の数を出していない:\n{got}")

    def test_names_can_come_from_the_crc_file(self):
        """**索引の名前が読めなくても、検査値ファイルから名前を付けられる** (#240).

        社長の実物は名前が付かず `#0 #1 …` のままでした (docs/09 の #1・#3)。
        名前は検査値ファイルにも入っていて、そこは索引とは別の場所なので、
        片方が読めなくてももう片方から出ることがあります。

        並び順が索引と同じかどうかは実物でしか決まらないので、**1 件ずつ
        検査値で裏を取り**、合った項目だけ名前を使います。
        """
        import subprocess
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            # **索引の名前を最後まで潰す** (社長の実物と同じ形)
            make_boku2_sample.damage(folder, "allnames")
            idx = os.path.join(folder, "BOKU2.IDX")
            img = os.path.join(folder, "BOKU2.IMG")
            crc = os.path.join(folder, "BOKU2.CRC")

            def run(out, *extra):
                return subprocess.run(
                    [sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                     "unpack", idx, img, out, *extra],
                    capture_output=True, text=True, cwd=REPO)

            plain = run(os.path.join(tmp, "A"))
            self.assertTrue("#0" in plain.stdout,
                            f"材料が弱い: 名前が潰れていない:\n{plain.stdout[:300]}")

            got = run(os.path.join(tmp, "B"), "--names-from-crc", crc)
            # 名前が付いたので、**「名前が付かなかった」の知らせは出ない** (出たら嘘)
            self.assertEqual(got.returncode, 0,
                             f"名前が付いたのに問題ありで終わった:\n{got.stderr[:400]}")
            self.assertFalse("名前が付かなかったファイル" in got.stderr,
                             f"名前が付いたのにそう言っていない:\n{got.stderr[:400]}")
            self.assertTrue("検査値ファイルの名前を 20 件当てました" in got.stderr,
                            f"当てた件数を言っていない:\n{got.stderr[:400]}")
            self.assertTrue("diary.bin" in got.stdout,
                            f"名前が付いていない:\n{got.stdout[:300]}")
            self.assertFalse("最初の名前: #0" in got.stdout,
                             f"番号のままの名前が残っている:\n{got.stdout[:300]}")
            self.assertTrue(os.path.exists(os.path.join(tmp, "B", "diary.bin")),
                            "その名前でファイルが落ちていない")

            # **中身が違えば名前を使わない。** 並び順が違う実物では、ここで止まる
            with open(crc, "rb") as fh:
                raw = bytearray(fh.read())
            head = struct.unpack_from("<5I", raw, 0)
            for k in range(len(boku2.read_dfi(open(idx, "rb").read(),
                                              os.path.getsize(img)))):
                at = head[3] + k * 2
                struct.pack_into("<H", raw, at, struct.unpack_from("<H", raw, at)[0] ^ 0xFFFF)
            bad = os.path.join(tmp, "bad.crc")
            with open(bad, "wb") as fh:
                fh.write(raw)
            res = run(os.path.join(tmp, "C"), "--names-from-crc", bad)
            self.assertTrue("1 件も当たりませんでした" in res.stderr,
                            f"当たらなかったと言っていない:\n{res.stderr[:400]}")
            self.assertTrue("#0" in res.stdout,
                            f"裏が取れないのに名前を使っている:\n{res.stdout[:300]}")

    def test_two_files_with_the_same_name_are_both_kept(self):
        """**名前がぶつかっても、ファイルを 1 つも落とさない** (#245).

        検査値ファイルの名前は**フォルダの付かないファイル名だけ**です。実物は
        116 のフォルダに 1951 件あるので、別のフォルダの同名ファイルが必ず
        重なります。そのまま使うと `unpack` が**後の 1 件で前の 1 件を黙って
        上書き**します (20 件を切り分けたのに 19 個しか落ちなかった)。
        索引の名前を読むときと同じ `~2` を付けて、両方残します。
        """
        import struct
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            crc_path = os.path.join(folder, "BOKU2.CRC")
            with open(crc_path, "rb") as fh:
                raw = bytearray(fh.read())
            dir_start = struct.unpack_from("<5I", raw, 0)[1]
            for i in (1, 2):                       # 2 件を同じ名前にする
                at = dir_start + i * boku2.CRC_ENTRY + 8
                raw[at:at + boku2.CRC_ENTRY - 8] = b"\0" * (boku2.CRC_ENTRY - 8)
                raw[at:at + len(b"same.bin")] = b"same.bin"
            with open(crc_path, "wb") as fh:
                fh.write(raw)
            make_boku2_sample.damage(folder, "allnames")

            out = os.path.join(tmp, "OUT")
            n, unnamed, dropped, paths = boku2.unpack(
                os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG"),
                out, crc_path)
            landed = [f for f in os.listdir(out) if os.path.isfile(os.path.join(out, f))]
            # **切り分けた数と、落ちたファイルの数が合うこと**
            self.assertEqual(len(landed), n,
                             f"{n} 個に切り分けたのに {len(landed)} 個しか落ちていない "
                             f"(名前がぶつかって上書きされた): {sorted(landed)}")
            self.assertTrue("same.bin" in landed and "same.bin~2" in landed,
                            f"ぶつかった 2 件が両方残っていない: {sorted(landed)}")
            # **材料が弱くないこと**: 本当に同じ名前を 2 件作れている
            self.assertEqual(sum(1 for p in paths if p.startswith("same.bin")), 2,
                             f"材料が弱い: 同じ名前が 2 件になっていない: {paths[:5]}")

            # **黙って名前を変えないこと。** 理由まで言う (索引の名前がぶつかった
            # ときとは原因が違う —— こちらは「フォルダが付かない」から)
            import subprocess
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "boku2.py"), "unpack",
                 os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG"),
                 os.path.join(tmp, "OUT2"), "--names-from-crc", crc_path],
                capture_output=True, text=True, cwd=REPO)
            said = res.stdout + res.stderr
            self.assertTrue("名前がぶつかったので" in said,
                            f"名前を変えたことを言っていない:\n{said[:500]}")
            self.assertTrue("フォルダが付かない" in said,
                            f"なぜぶつかるのかを言っていない:\n{said[:500]}")

    def test_check_points_at_that_way_out(self):
        """名前が読めないときに、`check` がその道を**教える**こと (#240)."""
        import io
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            # 名前が読めているうちは言わない (毎回出ると読まれなくなる)
            out = io.StringIO()
            boku2.check(folder, out=out)
            self.assertFalse("--names-from-crc" in out.getvalue(),
                             "名前が読めているのに逃げ道を案内している")

            make_boku2_sample.damage(folder, "allnames")
            out = io.StringIO()
            boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertTrue("この検査値ファイルに名前が" in got,
                            f"名前があることを言っていない:\n{got[-600:]}")
            self.assertTrue("--names-from-crc" in got,
                            f"打つべきコマンドを出していない:\n{got[-600:]}")

    def test_the_two_name_lists_are_compared(self):
        """**同じものが 2 か所に書いてある**ので、突き合わせること (#241).

        索引にも検査値ファイルにも名前が入っています。両方読めたなら、
        食い違いはどちらかの読み方が違うということ。索引も本体も検査値も
        読めてしまうので、**名前を比べなければ誰も気づけません**。
        """
        import io
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            out = io.StringIO()
            boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertTrue("名前も 20 件そろっています" in got,
                            f"そろっていると言っていない:\n{got[-500:]}")

            make_boku2_sample.damage(folder, "crcname")
            out = io.StringIO()
            rc = boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertEqual(rc, 1, f"名前が食い違うのに問題なしで終わった:\n{got[-500:]}")
            self.assertTrue("名前が 1 件食い違います" in got, f"食い違いを言っていない:\n{got[-500:]}")
            # **どちらが何と言っているか**まで出すこと (報告の材料になる)
            self.assertTrue("索引は diary.bin" in got, f"索引側の名前を出していない:\n{got[-500:]}")
            self.assertTrue("検査値ファイルは XXX" in got,
                            f"検査値ファイル側の名前を出していない:\n{got[-500:]}")

    def test_the_name_check_is_not_fooled_by_case_or_our_own_marks(self):
        """**読み方の間違いでないものを、食い違いに数えない** (#241).

        大文字小文字の違いと、同じ名前を見分けるためにこちらが付けた `~2` は、
        どちらも索引の読み方が外れている証拠ではありません。ここで鳴ると、
        本当の食い違いが埋もれます。
        """
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            with open(os.path.join(folder, "BOKU2.CRC"), "rb") as fh:
                crc = boku2.read_crc_file(fh.read())
            with open(os.path.join(folder, "BOKU2.IDX"), "rb") as fh:
                idx = fh.read()
            img_path = os.path.join(folder, "BOKU2.IMG")
            entries = boku2.read_dfi(idx, os.path.getsize(img_path))

            # 1. 大文字にしても食い違いに数えない
            crc["names"] = [n.upper() for n in crc["names"]]
            with open(img_path, "rb") as img:
                lines, problems = boku2.crc_report(crc, entries, img, len(entries))
            got = "\n".join(lines)
            self.assertEqual(problems, 0, f"大文字小文字で鳴っている:\n{got}")
            self.assertTrue("名前も" in got and "そろっています" in got, got)

            # 2. こちらが付けた `~2` も外して比べる
            marked = [dict(e) for e in entries]
            marked[0]["path"] = marked[0]["path"] + "~2"
            with open(img_path, "rb") as img:
                lines, problems = boku2.crc_report(crc, marked, img, len(entries))
            self.assertEqual(problems, 0, f"`~2` で鳴っている:\n" + "\n".join(lines))

    def test_names_are_not_compared_when_the_order_is_wrong(self):
        """**並び順が違うときに、名前の食い違いで騒がない** (#242).

        並びが違えば名前が食い違うのは当たり前で、原因は 1 つ上の行 (検査値が
        合わない) にあります。実物は 1951 件あるので、ここで鳴らすと何千件もの
        「食い違い」が出て、**本当の手がかりが埋もれます**。
        """
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            with open(os.path.join(folder, "BOKU2.CRC"), "rb") as fh:
                crc = boku2.read_crc_file(fh.read())
            with open(os.path.join(folder, "BOKU2.IDX"), "rb") as fh:
                idx = fh.read()
            img_path = os.path.join(folder, "BOKU2.IMG")
            entries = boku2.read_dfi(idx, os.path.getsize(img_path))

            # 名前を全部ずらす (並び順が 1 つずれた形)
            shifted = dict(crc)
            shifted["names"] = crc["names"][1:] + crc["names"][:1]
            # **材料が弱くないこと**: ずらせば名前は本当に食い違う
            _, diff, _ = boku2.compare_crc_names(shifted, entries)
            self.assertGreater(diff, 0, "材料が弱い: ずらしても名前が食い違わない")

            # 1. 検査値が合っているうちは、名前の食い違いをちゃんと言う
            with open(img_path, "rb") as img:
                lines, problems = boku2.crc_report(shifted, entries, img, len(entries))
            got = "\n".join(lines)
            self.assertGreaterEqual(problems, 1, f"名前の食い違いを言っていない:\n{got}")
            self.assertTrue("名前が" in got and "食い違います" in got, got)

            # 2. 検査値が合わないなら、名前の話はしない (原因は 1 つ上にある)
            broken = dict(shifted)
            broken["crcs"] = [v ^ 0xFFFF for v in crc["crcs"]]
            with open(img_path, "rb") as img:
                lines, problems = boku2.crc_report(broken, entries, img, len(entries))
            got = "\n".join(lines)
            self.assertTrue("検査値が" in got and "合いません" in got,
                            f"検査値の食い違いを言っていない:\n{got}")
            self.assertFalse("名前が" in got and "食い違います" in got,
                             f"並びが違うのに名前の食い違いでも騒いでいる:\n{got}")

    def test_it_says_when_there_is_none(self):
        """無いときは「無い」と言い、**診ていない段**に数えること (#175)."""
        import io
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            os.remove(os.path.join(folder, "BOKU2.CRC"))
            out = io.StringIO()
            rc = boku2.check(folder, out=out)
            got = out.getvalue()
            self.assertEqual(rc, 0, "BOKU2.CRC が無いのは「問題」ではない")
            self.assertTrue("[検査値] BOKU2.CRC は無い" in got, f"無いと言っていない:\n{got[-400:]}")
            self.assertTrue("検査値との突き合わせ (BOKU2.CRC が無い)" in got,
                            f"診ていない段に数えていない:\n{got[-400:]}")


class TestEverySharedJudgementIsReached(unittest.TestCase):
    """両側にある判定が、**実際に呼ばれる所まで**検査を通っていること (#236).

    #234・#235 で同じ穴を 2 度踏んだ: 判定の関数は画面と一括処理で 1 字まで
    突き合わせているのに、**その関数に何を渡しているか**、そもそも
    **呼ばれるのか**を見ている検査が無い。要約から判定が静かに消えても全部緑。

    1 つずつ検査を足すやり方は #189・#190・#191 で何度も破れている
    (一覧を手で並べると、足した人が書き忘れる)。ここでは**一覧を作らない**:

    1. 対になっている関数を**機械で数える** —— `tools/boku2.py` の `def 名前`
       と `web/app.js` の `function 名前` が snake / camel で対応するもの
    2. 練習データと `--break` の全部を `check` に通し、**実際に呼ばれた関数**を数える
    3. 呼ばれなかったものは、**理由を書いて下に置く**。書けないなら、
       それは「誰も通していない判定」なので通す材料を足すこと

    これで、新しく対の判定を足した人は「`check` で通る材料」か「通らない理由」の
    どちらかを必ず書くことになる。
    """

    #: `check` からは呼ばれないと分かっているもの と、**その理由**。
    #: 理由の書けるものだけをここに置く (ONLY_CLI と同じ決まり)
    NOT_FROM_CHECK = {
        "block_stats":
            "`guess_kind_note` の最後の枝 (先頭が知らない形で、中身の性質でしか"
            "言えないとき) からしか呼ばれない。練習データの `.msg` は 56〜80 バイト"
            "しかなく、この分類は `tile` (言うことなし) にしかならないので、"
            "実物でしか通らない。枝そのものは "
            "test_the_unreadable_bytes_are_named_when_they_can_be が直接見ている",
    }

    @staticmethod
    def _camel(name: str) -> list:
        head, *rest = name.split("_")
        camel = head + "".join(w.capitalize() for w in rest)
        return [camel, "boku" + camel[0].upper() + camel[1:]]

    def shared_names(self) -> list:
        """両側にある関数の名前 (一括処理側の呼び方) を機械で拾う."""
        import re

        with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
            py = set(re.findall(r"^def ([a-z_][a-z0-9_]*)\(", fh.read(), re.M))
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            js = set(re.findall(r"^function ([A-Za-z][A-Za-z0-9_]*)\(", fh.read(), re.M))
        return sorted(n for n in py if any(c in js for c in self._camel(n)))

    def test_check_reaches_every_shared_judgement(self):
        import io
        import boku2
        import make_boku2_sample

        names = self.shared_names()
        # **0 件で緑にしない。** 拾い方が壊れたら「全部通っている」に化ける
        self.assertGreaterEqual(len(names), 15,
                                f"両側にある関数を {len(names)} 件しか拾えない: {names}")

        seen = set()
        original = {}
        for n in names:
            original[n] = getattr(boku2, n)

            def wrap(name, func):
                def call(*a, **k):
                    seen.add(name)
                    return func(*a, **k)
                return call

            setattr(boku2, n, wrap(n, original[n]))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                # 練習データそのまま + **壊し方の全部** (道具の一覧を正にする)
                for kind in [None] + sorted(make_boku2_sample.DAMAGE):
                    folder = os.path.join(tmp, "S" + (kind or "ok"))
                    make_boku2_sample.build_sample(folder)
                    if kind:
                        make_boku2_sample.damage(folder, kind)
                    boku2.check(folder, out=io.StringIO())
                # 文字表を 2 通り壊した版 (#235 と同じ材料)
                folder = os.path.join(tmp, "Sfont")
                make_boku2_sample.build_sample(folder)
                table = os.path.join(folder, "font.txt")
                with open(table, encoding="utf-8") as fh:
                    rows = [ln for ln in fh.read().replace("\r", "").split("\n") if ln]
                rows[1] = rows[1][:-1]
                rows[-1] = rows[-1] + "??"
                with open(table, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(rows) + "\n")
                boku2.check(folder, out=io.StringIO())
        finally:
            for n, f in original.items():
                setattr(boku2, n, f)

        missed = sorted(set(names) - seen - set(self.NOT_FROM_CHECK))
        self.assertEqual(missed, [],
                         "check で一度も呼ばれない両側の判定があります。"
                         "通る材料 (--break の壊し方など) を足すか、"
                         "NOT_FROM_CHECK に理由を書いてください:\n  "
                         + "\n  ".join(missed))
        # 逃がした分が**本当にまだ呼ばれない**こと。呼ばれるようになったのに
        # 残しておくと、そこだけ見張りの外になる (ONLY_CLI と同じ)
        for name, why in self.NOT_FROM_CHECK.items():
            self.assertTrue(name in names,
                            f"NOT_FROM_CHECK に両側の関数でない名前がある: {name}")
            self.assertFalse(name in seen,
                             f"{name} は check から呼ばれるようになりました。"
                             f"NOT_FROM_CHECK から外してください (理由: {why[:40]}…)")


class TestDamageDrill(unittest.TestCase):
    """診断の読み方の練習 (make_boku2_sample.py --break …) が、意図した → の行を出すこと."""

    EXPECT = {
        "idx": "DFI でないので",
        "name": "名前が付かないファイルが多い",
        # 名前が 1 つも付かない吸い出し。**形で探す道** (#172・#173) が通ること。
        # ここが黙って飛ぶと、本文もフォントも診られないまま最後まで進んでしまう
        "allnames": "中身の形",
        "msg": "読めない .msg の例: system/system.msg",
        "font": "TIM2 として読めません",
        "map": "入れ物として読めないファイルの例",
        # 索引だけ正しくて中身が空の吸い出し (#218)。形式の話をする前に、
        # **中身が無いこと**を言えているか
        "empty": "本体の中身がほとんど空です",
        # 文字番号が文字表 1656 字に収まらない形 (#230)。読み方そのものが違う合図
        "bignum": "この作品の文字表 1656 字",
        # 切り分けとゲーム自身の検査値が食い違う形 (#239)
        "crc": "検査値が 1 件合いません",
        # 2 か所に書いてある名前が食い違う形 (#241)
        "crcname": "名前が 1 件食い違います",
    }

    def test_each_damage_kind_is_diagnosed(self):
        import io
        import boku2
        import make_boku2_sample
        # **道具の一覧を正にする** (#190)。ここが自前の辞書だけを回っていたので、
        # `--break` に壊し方が増えても、この検査は**黙って増えないまま**だった。
        # #189 と同じ型 (一覧を写すと、写した側が古くなっても誰も気づかない)
        self.assertEqual(set(self.EXPECT), set(make_boku2_sample.DAMAGE),
                         "--break の壊し方と、ここで待ち受けている行がずれています")
        for kind in sorted(make_boku2_sample.DAMAGE):
            want = self.EXPECT[kind]
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                folder = os.path.join(tmp, "S")
                make_boku2_sample.build_sample(folder)
                note = make_boku2_sample.damage(folder, kind)
                self.assertTrue(note)
                out = io.StringIO()
                rc = boku2.check(folder, out=out)
                self.assertEqual(rc, 1, f"{kind}: 問題なしになった\n{out.getvalue()}")
                # assertIn は落ちたとき診断の全文を吐く。何がまずいのか読めなくなる
                got = out.getvalue()
                self.assertTrue(want in got, f"{kind}: 「{want}」が出ていない\n{got[-500:]}")
                self.assertTrue("→" in got, f"{kind}: → の行が 1 本も無い\n{got[-500:]}")
                # どの壊れ方でも締めの行まで出ること。途中で止める壊れ方 (idx) だけ
                # 締めが無く、道具が落ちたのか診た結果なのか分からなかった (#73)
                self.assertTrue("== 結果:" in got, f"{kind}: 締めの行が無い\n{got[-500:]}")
                self.assertTrue("この出力ごと報告してください" in got,
                                f"{kind}: 報告の促しが無い\n{got[-500:]}")

    def test_stopping_early_says_what_was_not_checked(self):
        """途中で止めたときは、この先を診ていないことまで書くこと (#73)."""
        import io
        import boku2
        import make_boku2_sample
        # 1. 索引と本体が無い (フォルダの指定違い。素人が最初に踏む)
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            self.assertEqual(boku2.check(tmp, out=out), 1)
            self.assertIn("索引と本体が見つからないのでここで止めました", out.getvalue())
            self.assertIn("この先 (本体・.msg・フォント・MAP) は診ていません", out.getvalue())
        # 2. 索引が DFI でない (別の版か、ファイルの取り違え)
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            make_boku2_sample.damage(folder, "idx")
            out = io.StringIO()
            self.assertEqual(boku2.check(folder, out=out), 1)
            self.assertIn("索引が読めないのでここで止めました", out.getvalue())
            self.assertIn("この先 (本体・.msg・フォント・MAP) は診ていません", out.getvalue())
            # 診ていない段の行を、さも診たかのように出していないこと
            self.assertNotIn("[フォント]", out.getvalue())
            self.assertNotIn("[MAP]", out.getvalue())
        # 壊し方の一覧と選択肢が一致していること (README に書く名前がずれないように)
        self.assertEqual(set(make_boku2_sample.DAMAGE), set(self.EXPECT))


class TestDamagedData(unittest.TestCase):
    """壊れたデータでも、診断と抽出が追跡表示 (Traceback) で止まらないこと.

    実物は練習データと違う所が必ずある。索引・本体・MAP のどれかを壊した状態で
    check と text を走らせ、「読めない」と報告するか空を返すかのどちらかであること
    (Python の例外で落ちないこと) を、乱数の種を固定して何通りも確かめる."""

    MUTATIONS = ("truncate", "flip", "zero", "empty", "garbage_head")

    @staticmethod
    def mutate(data: bytes, how: str, rnd) -> bytes:
        if not data:
            return data
        if how == "truncate":
            return data[:rnd.randrange(0, len(data))]
        if how == "flip":
            b = bytearray(data)
            for _ in range(max(1, len(b) // 100)):
                i = rnd.randrange(len(b))
                b[i] ^= rnd.randrange(1, 256)
            return bytes(b)
        if how == "zero":
            b = bytearray(data)
            a = rnd.randrange(len(b))
            n = rnd.randrange(1, min(len(b) - a, 512) + 1)
            b[a:a + n] = b"\0" * n
            return bytes(b)
        if how == "empty":
            return b""
        if how == "garbage_head":
            return bytes(rnd.randrange(256) for _ in range(min(64, len(data)))) + data[64:]
        raise AssertionError(how)

    def test_check_and_text_survive_damage(self):
        import io
        import random
        import shutil
        import boku2
        import make_boku2_sample

        with tempfile.TemporaryDirectory() as tmp:
            clean = os.path.join(tmp, "clean")
            make_boku2_sample.build_sample(clean)
            targets = ["BOKU2.IDX", "BOKU2.IMG"] + [os.path.join("MAP", n) for n in sorted(os.listdir(os.path.join(clean, "MAP")))]
            allowed = (ValueError, struct.error, OSError)      # 入口 (main) が 1 行で報告する種類
            seed = 0
            verdicts = {0: 0, 1: 0}
            for target in targets:
                for how in self.MUTATIONS:
                    for _ in range(8):
                        seed += 1
                        rnd = random.Random(seed)
                        folder = os.path.join(tmp, f"case{seed}")
                        shutil.copytree(clean, folder)
                        p = os.path.join(folder, target)
                        with open(p, "rb") as fh:
                            data = fh.read()
                        with open(p, "wb") as fh:
                            fh.write(self.mutate(data, how, rnd))
                        label = f"seed {seed}: {target} を {how}"
                        with self.subTest(case=label):
                            out = io.StringIO()
                            try:
                                rc = boku2.check(folder, out=out)
                            except allowed:
                                rc = 1
                            except Exception as exc:                   # noqa: BLE001
                                self.fail(f"{label}: check が {type(exc).__name__}: {exc}")
                            self.assertIn(rc, (0, 1), label)
                            verdicts[rc] += 1
                            # 抽出も同じ: 読めないなら空、壊れているなら報告される種類の例外
                            files = [os.path.join(folder, "MAP", n) for n in sorted(os.listdir(os.path.join(folder, "MAP")))]
                            out_dir = os.path.join(folder, "OUT")
                            try:
                                boku2.unpack(os.path.join(folder, "BOKU2.IDX"), os.path.join(folder, "BOKU2.IMG"), out_dir)
                                files += boku2.expand_inputs([out_dir])
                            except allowed:
                                pass
                            for f in files:
                                try:
                                    boku2.text_rows(f, None)
                                    boku2.text_rows(f, list("あいうえおかきくけこ"), keep_voice=True)
                                except allowed:
                                    pass
                                except Exception as exc:               # noqa: BLE001
                                    self.fail(f"{label}: text_rows({os.path.relpath(f, folder)}) が {type(exc).__name__}: {exc}")
                        shutil.rmtree(folder)
            # 壊し方が効いていること (全部「問題なし」なら試験になっていない)
            self.assertGreater(verdicts[1], len(targets) * len(self.MUTATIONS), verdicts)


class TestBrowserDamagedData(unittest.TestCase):
    """ブラウザ側の各読み取りも、壊れたデータで例外を投げないこと (tests/test_fuzz.mjs)."""

    def test_browser_parsers_survive_damage(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        res = subprocess.run([node, os.path.join(REPO, "tests", "test_fuzz.mjs")],
                             capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


class TestOtherPythons(unittest.TestCase):
    """手元にある別の版の Python でも、道具が構文エラーなく動くこと.

    社長の Windows の Python は、ここで走らせている版と違うかもしれない。PATH にある
    python3.X を全部探し、道具を構文チェックして、練習データの診断を通す."""

    def test_tools_run_on_every_installed_python(self):
        import glob as globmod
        import shutil
        import subprocess
        import make_boku2_sample
        found = {}
        for d in os.environ.get("PATH", "").split(os.pathsep):
            for p in globmod.glob(os.path.join(d, "python3.[0-9]*")):
                base = os.path.basename(p)
                if base.endswith("-config") or base in found:
                    continue
                found[base] = p
        others = {k: v for k, v in found.items()
                  if k != f"python{sys.version_info.major}.{sys.version_info.minor}"}
        if not others:
            self.skipTest("別の版の Python が見つかりません")
        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            tools = sorted(globmod.glob(os.path.join(REPO, "tools", "*.py")))
            for name, exe in sorted(others.items()):
                with self.subTest(python=name):
                    res = subprocess.run([exe, "-m", "py_compile", *tools], capture_output=True, text=True)
                    self.assertEqual(res.returncode, 0, f"{name}: {res.stderr[-800:]}")
                    res = subprocess.run([exe, os.path.join(REPO, "tools", "boku2.py"), "check", sample],
                                         capture_output=True, text=True, encoding="utf-8", cwd=REPO)
                    self.assertEqual(res.returncode, 0, f"{name}: {res.stderr[-800:]}")
                    self.assertIn("問題なし", res.stdout, name)


class TestWindowsConsole(unittest.TestCase):
    """Windows のコンソールやリダイレクト (cp932) でも、道具が UnicodeEncodeError で落ちないこと.

    PYTHONIOENCODING=cp932 で標準出力を Shift-JIS にして走らせる。cp932 に無い文字
    (hexdump.py の «» など) は ? に置き換わって続く。"""

    def _run(self, *args):
        import subprocess
        env = dict(os.environ, PYTHONIOENCODING="cp932")
        return subprocess.run([sys.executable, *args], capture_output=True, cwd=REPO, env=env)

    def test_tools_survive_cp932_stdout(self):
        if not os.path.exists(os.path.join(REPO, "work", "SCRIPT.BIN")):
            self.skipTest("work/SCRIPT.BIN がありません (make_sample.py)")
        res = self._run(os.path.join(REPO, "tools", "hexdump.py"), "work/SCRIPT.BIN", "--message", "2")
        self.assertEqual(res.returncode, 0, res.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"UnicodeEncodeError", res.stderr)
        import make_boku2_sample
        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(sample)
            res = self._run(os.path.join(REPO, "tools", "boku2.py"), "check", sample)
            self.assertEqual(res.returncode, 0, res.stderr.decode("utf-8", "replace"))
            self.assertIn("問題なし".encode("cp932"), res.stdout)


class TestTim2(unittest.TestCase):
    """TIM2 の組み立て (tools/make_tim2.py) と、ブラウザ側の読み取りの突き合わせ."""

    def test_builder_layout(self):
        import make_tim2
        data, px = make_tim2.font_sheet(rows=2, cols=17, cell=23)
        self.assertEqual(data[:4], b"TIM2")
        total, clut_size, image_size, header_size, n_colors = __import__("struct").unpack_from("<IIIHH", data, 0x10)
        self.assertEqual(image_size, 17 * 23 * 2 * 23)
        self.assertEqual(n_colors, 256)
        self.assertEqual(clut_size, 256 * 4)
        self.assertEqual(len(data), 0x10 + total)
        # 画素は見出しの直後にそのまま並ぶ (8bit 索引)
        self.assertEqual(list(data[0x10 + header_size: 0x10 + header_size + 40]), px[:40])

    def test_browser_decodes_every_format(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        res = subprocess.run([node, os.path.join(REPO, "tests", "test_tim2.mjs")],
                             capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


class TestBokuMsgInBrowser(unittest.TestCase):
    """僕の夏休み 2 の .msg 読み (件数 + 位置表 + 2 バイトの並び)."""

    def test_msg(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        res = subprocess.run([node, os.path.join(REPO, "tests", "test_bokumsg.mjs")],
                             capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)

        # ブラウザが書き出した TSV を、そのまま校正ツールが読めること (工程がつながる)
        tsv = os.path.join(REPO, "work", "MSG_EXPORT.tsv")
        rows = scrp.read_tsv(tsv)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["original"], "かき<BR>く")
        res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "proofread.py"), tsv,
                              "--no-font-check"], capture_output=True, text=True, cwd=REPO)
        self.assertIn(res.returncode, (0, 1), res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stderr)


class TestMsgLengthField(unittest.TestCase):
    """位置表 8 バイト刻みの後ろ 4 バイト = その項目のバイト長 (#71).

    公開ソースの書き出し側で確認した形。鵜呑みにせず、次の位置から出した長さと
    突き合わせてから使う。合っていれば最後の項目を詰め物ごと読まずに済む。
    """

    @staticmethod
    def build(entries, len_of, pad):
        n = len(entries)
        tab = 4 + n * 8
        head = struct.pack("<I", n)
        body, p = b"", tab
        for e in entries:
            head += struct.pack("<II", p if e else 0, len_of(e) if e else 0)
            body += struct.pack(f"<{len(e)}H", *e)
            p += len(e) * 2
        return head + body + b"\xcd" * pad

    def setUp(self):
        self.entries = [[0, 1, 0x8000], [2, 3, 4, 0x8000], [5, 0x8000]]

    def test_correct_length_trims_the_padding(self):
        info: dict = {}
        items = boku2.parse_msg(self.build(self.entries, lambda e: len(e) * 2, 8), 8, info)
        self.assertIsNotNone(items)
        self.assertEqual(info.get("len_field"), "ok")
        self.assertEqual(items[2]["codes"], [5, 0x8000])

    def test_a_field_that_is_not_the_length_is_ignored(self):
        info: dict = {}
        items = boku2.parse_msg(self.build(self.entries, lambda e: 0x1234, 8), 8, info)
        self.assertIsNotNone(items)
        self.assertEqual(info.get("len_field"), "ng")
        # 位置だけで読むので、最後の項目は詰め物まで含む (今までどおりで壊れない)
        self.assertEqual(len(items[2]["codes"]), 6)

    def test_zero_fields_are_not_trusted(self):
        info: dict = {}
        items = boku2.parse_msg(self.build(self.entries, lambda e: 0, 8), 8, info)
        self.assertIsNotNone(items)
        self.assertEqual(info.get("len_field"), "ng")

    def test_four_byte_stride_has_no_length_field(self):
        n = 2
        entries = [[0, 0x8000], [1, 0x8000]]
        tab = 4 + n * 4
        head, body, p = struct.pack("<I", n), b"", tab
        for e in entries:
            head += struct.pack("<I", p)
            body += struct.pack(f"<{len(e)}H", *e)
            p += len(e) * 2
        info: dict = {}
        self.assertIsNotNone(boku2.parse_msg(head + body, 4, info))
        self.assertNotIn("len_field", info)

    def test_the_practice_data_writes_a_real_length(self):
        """練習データが実物と同じ形になっていること (#56 と同じ種類の思い込みを防ぐ)."""
        import subprocess

        path = os.path.join(REPO, "work", "BOKU2SAMPLE", "BOKU2.IMG")
        if not os.path.exists(path):
            self.skipTest("練習データがありません")
        out = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                              "check", os.path.join(REPO, "work", "BOKU2SAMPLE")],
                             capture_output=True, text=True)
        self.assertIn("位置表の長さの欄: 合う", out.stdout)
        self.assertNotIn("合わない 0 件 (", out.stdout)
        line = next(ln for ln in out.stdout.splitlines() if "長さの欄" in ln)
        self.assertIn("合わない 0 件", line, line)


class TestGlyphDraftInBrowser(unittest.TestCase):
    """文字表の下書き (フォント画像の形から候補の字を当てる)."""

    def test_glyph_draft(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        res = subprocess.run([node, os.path.join(REPO, "tests", "test_glyphdraft.mjs")],
                             capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


class TestFontGridInBrowser(unittest.TestCase):
    """番号振りの列数が 1 行 23 字と合うかの照合 (tests/test_fontgrid.mjs、#166)."""

    def test_font_grid_mismatch(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        res = subprocess.run([node, os.path.join(REPO, "tests", "test_fontgrid.mjs")],
                             capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


class TestSniffInBrowser(unittest.TestCase):
    """名前の無いファイルに中身から見当を付ける sniffKind."""

    def test_sniff(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        res = subprocess.run([node, os.path.join(REPO, "tests", "test_sniff.mjs")],
                             capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


class TestBrowserEndToEnd(unittest.TestCase):
    """構造探査台を実際にブラウザで操作する検査 (tests/e2e/)。playwright が無ければ skip."""

    def test_all_scenarios(self):
        import subprocess
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright がありません")
        res = subprocess.run([sys.executable, os.path.join(REPO, "tests", "e2e", "run_all.py")],
                             capture_output=True, text=True, cwd=REPO, timeout=1500)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("NG ", res.stdout)
        self.assertNotIn("skip:", res.stdout, "playwright はあるのに skip している")


class TestNothingIsQuietlyLeftOut(unittest.TestCase):
    """「走らせる物の一覧」が、実際に在る物と一致していること (#82).

    e2e は CHECKS の並び、node 側は run_tests.py の中の呼び出しが一覧になっている。
    どちらも手書きなので、検査を足して一覧に入れ忘れると、**ファイルは在るのに
    誰も走らせない**。落ちるわけではないので、緑のまま気づかない (#81 と同じ形)。
    """

    def test_every_e2e_file_is_in_checks(self):
        e2e = os.path.join(REPO, "tests", "e2e")
        found = {n[:-3] for n in os.listdir(e2e)
                 if n.endswith(".py") and n not in ("common.py", "run_all.py")}
        sys.path.insert(0, e2e)
        try:
            import run_all
        finally:
            sys.path.remove(e2e)
        self.assertEqual(found, set(run_all.CHECKS),
                         "tests/e2e/ にあるのに CHECKS に無い (または逆)")

    @staticmethod
    def code_strings(path: str) -> set:
        """その Python の**説明文でない**文字列だけを集める (#136).

        「名前がファイルの中に出てくるか」で見ていたので、`.mjs` の名前が
        説明文に出てくるだけで通っていた。実際に呼んでいる所を消しても、
        説明文が残っていれば緑のまま (壊して確かめて分かった)。
        `ast` で読んで、docstring は除き、それ以外の文字列だけを見る。
        """
        import ast

        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docs.add(id(first.value))
        return {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs}

    def test_the_docstring_trap_is_actually_closed(self):
        """説明文の中の名前を拾わないこと (この検査の土台) (#136).

        ここが緩いと上の検査が空振りする。実際、直す前は
        `test_fuzz.mjs` を呼ぶ行を消して**説明文だけ残しても通った**。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('"""説明文の中の only_in_doc.mjs"""\n'
                         "def f():\n"
                         '    """ここも説明文 also_doc.mjs"""\n'
                         '    return run("real_call.mjs")  # ここは本物\n')
            got = type(self).code_strings(path)
        self.assertIn("real_call.mjs", got, "本物の呼び出しを拾えていない")
        self.assertNotIn("説明文の中の only_in_doc.mjs", got, "module の説明文を拾っている")
        self.assertNotIn("ここも説明文 also_doc.mjs", got, "関数の説明文を拾っている")

    def test_every_node_test_is_called_from_here(self):
        """`.mjs` を**呼んでいる**こと。説明文に名前があるだけでは通さない."""
        tests_dir = os.path.join(REPO, "tests")
        strings = self.code_strings(os.path.join(tests_dir, "run_tests.py"))
        names = sorted(n for n in os.listdir(tests_dir) if n.endswith(".mjs"))
        self.assertGreaterEqual(len(names), 5, f"node の検査が {len(names)} 件しか見つからない")
        for name in names:
            # assertIn は落ちると**文字列を全部**吐く (44KB 出た)。#96・#115・#129 と同じ形
            self.assertTrue(name in strings,
                            f"tests/{name} を run_tests.py が呼んでいない "
                            f"(説明文に名前があるだけでは通しません)")


class TestE2eFixturesFromAScratchTree(unittest.TestCase):
    """取得したままの木 (work/ が無い) で、e2e の練習データが作れること (#82).

    まっさらな取得直後は work/ が空で、make_archive.py は make_sample.py の出力を
    材料にする。一覧の順序と依存が抜けていたため e2e は**取得直後には動かず**、
    しかも失敗の中身 (「先に make_sample.py を実行してください」) は
    check=True + capture_output に呑まれて traceback だけが出ていた。
    2 度目からは前回の残りで動くので、緑に見えてしまう。
    """

    def test_fixtures_build_in_a_tree_without_work(self):
        import shutil

        e2e = os.path.join(REPO, "tests", "e2e")
        sys.path.insert(0, e2e)
        try:
            import run_all
        finally:
            sys.path.remove(e2e)
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(os.path.join(REPO, "tools"), os.path.join(tmp, "tools"))
            # 取得したままの木にある物はすべて写す。exercises/ は make_viewer.py が
            # 題材にする (#110)。work/ だけが無い状態を作るのがこの検査の趣旨
            for extra in ("data", "web", "exercises"):
                src = os.path.join(REPO, extra)
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(tmp, extra))
            os.makedirs(os.path.join(tmp, "work"), exist_ok=True)
            problem = run_all.build_fixtures(tmp)
            self.assertIsNone(problem, problem)
            for marker, _cmd in run_all.FIXTURES:
                self.assertTrue(os.path.exists(os.path.join(tmp, marker)), marker)

    def test_a_broken_generator_reports_its_own_words(self):
        """道具が落ちたら、その道具が出した言葉がそのまま返ること (traceback ではなく)."""
        import shutil

        e2e = os.path.join(REPO, "tests", "e2e")
        sys.path.insert(0, e2e)
        try:
            import run_all
        finally:
            sys.path.remove(e2e)
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tools"))
            first = run_all.FIXTURES[0][1][0]
            with open(os.path.join(tmp, first), "w", encoding="utf-8") as fh:
                fh.write("import sys\nprint('ここに理由が出る', file=sys.stderr)\nsys.exit(1)\n")
            problem = run_all.build_fixtures(tmp)
            self.assertIsNotNone(problem, "落ちたのに問題なしと言っている")
            self.assertIn("ここに理由が出る", problem)
            self.assertNotIn("Traceback", problem)


class TestDisassemblerInBrowser(unittest.TestCase):
    """ブラウザ側の逆アセンブラも同じ答えと突き合わせる."""

    def test_browser_decoder_matches(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        script = os.path.join(REPO, "tests", "test_disasm.mjs")
        problem = ensure_practice("work/BOOT.ELF", "make_elf.py")
        if problem:
            self.skipTest(problem)
        res = subprocess.run([node, script], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


#: 公開ソース (Hilltop Works) の置き場。リポジトリには**入れていない**ので、
#: 無い環境ではこの一群を飛ばす。BOKU2_PUBLIC_SRC で場所を変えられる
PUBLIC_SRC = os.environ.get("BOKU2_PUBLIC_SRC",
                            "/home/user/hilltopworks/bokunonatsuyasumi2")


class TestAgainstThePublicSource(unittest.TestCase):
    """公開ソースの読み取り部を**実際に走らせて**、こちらの答えと比べる (#107).

    #104〜#106 で画面と CLI をそろえたので、この 2 つは同じ答えしか出さなくなった。
    残る危険は「**どちらも同じように間違っている**」型で、自分どうしを比べても
    絶対に見つからない。docs/09 が「公開ソースで確認した」と書いている形式は、
    今まで**ソースを読んで**確かめただけだった。読み違いはそのまま残る。

    そこで、向こうの `readMSG` / `convertRawToText` / `readFont` を実行時に
    取り出して、こちらの合成データに対して走らせる。**向こうのコードもデータも
    このリポジトリには入れない** (置き場が無ければ飛ばす)。学ぶのは形式だけ、
    という約束 (docs/05) はそのまま。

    出る文字列の見た目は違って当たり前 (向こうは終端を {KEY_ERROR:0x8000} と
    出し、詰め物もそのまま見せる)。**同じ意味に直してから**比べる。
    """

    @classmethod
    def setUpClass(cls):
        import re

        if not os.path.isdir(PUBLIC_SRC):
            raise unittest.SkipTest(f"公開ソースが無い ({PUBLIC_SRC})")
        msg_py = os.path.join(PUBLIC_SRC, "MSG.py")
        res_py = os.path.join(PUBLIC_SRC, "resource.py")
        for path in (msg_py, res_py):
            if not os.path.isfile(path):
                raise unittest.SkipTest(f"公開ソースに {os.path.basename(path)} が無い")

        def take(src, name):
            """関数 1 つ分の原文を切り出す (丸ごと import すると PIL や numpy を要求する)."""
            m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.S | re.M)
            if not m:
                raise unittest.SkipTest(f"公開ソースに {name} が見つからない (作りが変わった)")
            return m.group(0)

        import types

        with open(msg_py, encoding="utf-8") as fh:
            src = fh.read()
        with open(res_py, encoding="utf-8") as fh:
            rsrc = fh.read()
        res = types.ModuleType("resource")
        # resource.py 全体は PIL / numpy を要求するので、要る読み取り関数だけ取り出す
        for helper in ("readInt", "readShort", "ReadString"):
            exec(take(rsrc, helper), res.__dict__)
        cls.ns = {"resource": res, "MSG_MODE": 0, "MAP_MODE": 1,
                  "OFFSET_ONLY_MODE": 2, "EXTRACTION": 0}
        for name in ("readMSG", "convertRawToText", "readFont"):
            exec(take(src, name), cls.ns)

        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        cls.sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        cls.unpacked = os.path.join(REPO, "work", "public_check")
        import shutil
        import subprocess
        shutil.rmtree(cls.unpacked, ignore_errors=True)
        for args in (["unpack", os.path.join(cls.sample, "BOKU2.IDX"),
                      os.path.join(cls.sample, "BOKU2.IMG"), cls.unpacked],
                     ["maps", os.path.join(cls.sample, "MAP"),
                      "-o", os.path.join(cls.unpacked, "maps")]):
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py")] + args,
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise unittest.SkipTest(f"練習データを用意できない: {r.stdout[-200:]}")
        with open(os.path.join(cls.sample, "font.txt"), encoding="utf-8") as fh:
            cls.table = fh.read()
        cls.glyphs = boku2.parse_glyph_table(cls.table)
        import warnings
        with warnings.catch_warnings():
            # 向こうの readFont はファイルを閉じない。こちらで直す話ではないので黙らせる
            warnings.simplefilter("ignore", ResourceWarning)
            cls.dict = cls.ns["readFont"](os.path.join(cls.sample, "font.txt"), 0, 0)

    @staticmethod
    def canon_theirs(text: str) -> str:
        """向こうの書き方を、意味だけの形にそろえる.

        向こうは制御コードを辞書に入れていないので `{KEY_ERROR:0x8000}` と出る。
        終端はそこで切る (詰め物 0xCDCD はその後ろにしか出ない)。
        """
        import re

        end = text.find("{KEY_ERROR:0x8000}")
        if end >= 0:
            text = text[:end]
        text = text.replace("{KEY_ERROR:0x8001}", "<BR>")
        text = re.sub(r"\{WAIT=0x([0-9a-f]+)\}\\n", lambda m: f"<WAIT:{int(m.group(1), 16)}>", text)
        return text.replace("{BREAK}\\n", "<BREAK>")

    @staticmethod
    def canon_ours(text: str) -> str:
        import re

        text = text.replace("{END}", "").replace("\n", "<BR>")
        text = re.sub(r"\{WAIT (\d+)\}", lambda m: f"<WAIT:{m.group(1)}>", text)
        return text.replace("{BREAK}<BR>", "<BREAK>")

    def compare(self, path: str, mode: int, stride: int, alt: bool = False):
        """1 ファイルを両方に読ませ、項目の切れ目と本文を突き合わせる。比べた項目数を返す."""
        with open(path, "rb") as fh:
            b = fh.read()
        theirs = self.ns["readMSG"](path, 0, len(b), mode)
        ours = boku2.parse_msg(b, stride)
        self.assertIsNotNone(ours, f"{os.path.basename(path)} をこちらが読めない")
        self.assertEqual(len(theirs), len(ours),
                         f"{os.path.basename(path)}: 項目数が違う "
                         f"(公開ソース {len(theirs)} / こちら {len(ours)})")
        n = 0
        for i, (raw, item) in enumerate(zip(theirs, ours)):
            if boku2.voice_id(item["codes"]):
                continue                      # 音声の番号。向こうは解釈せず生で出す
            self.assertEqual(len(raw), len(item["codes"]) * 2,
                             f"{os.path.basename(path)}[{i}]: 項目の長さが違う")
            got = self.canon_ours(boku2.decode(item["codes"], self.glyphs, tags=False, alt=alt))
            want = self.canon_theirs(self.ns["convertRawToText"](self.dict, raw, alt))
            self.assertEqual(got, want,
                             f"{os.path.basename(path)}[{i}] の本文が公開ソースと違う")
            n += 1
        return n

    def their_paths(self, idx_bytes: bytes, rec_count: int, names_at: int) -> list[str]:
        """公開ソースの unpackIMG を、書き出しだけ差し替えて走らせ、作られる道筋を返す.

        道筋を決める所 (フォルダの積み方) はそのまま向こうのコードが動く。
        """
        import io
        import re
        import types

        with open(os.path.join(PUBLIC_SRC, "UNPACK.py"), encoding="utf-8") as fh:
            up = fh.read()

        def take(src, name):
            m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.S | re.M)
            if not m:
                raise unittest.SkipTest(f"公開ソースに {name} が無い")
            return m.group(0)

        written: list[str] = []

        class FakeOut:
            def write(self, b):
                pass

            def close(self):
                pass

        def fake_open(path, mode="r", *a, **k):
            if "w" in mode:
                written.append(path)
                return FakeOut()
            return io.BytesIO(idx_bytes if "idx" in os.path.basename(path).lower()
                              else b"\0" * (1 << 16))

        ns = {"resource": self.ns["resource"], "open": fake_open, "log": lambda m: None,
              "os": types.SimpleNamespace(path=os.path, makedirs=lambda *a, **k: None),
              "INDEX_PATH": "boku2.idx", "IMG_PATH": "boku2.img", "IMG_RIP_DIR": "R",
              "DIR_START": 0x10, "IDX_ENTRY_SIZE": 0x10,
              "NUM_IDX_ENTRIES": rec_count, "FILENAMES_START": names_at}
        for name in ("getFileNames", "getIDX", "createDirPath", "unpackIMG"):
            exec(take(up, name), ns)
        ns["unpackIMG"]()
        return [p[2:] if p.startswith("R/") else p for p in written]

    @staticmethod
    def build_index(shape):
        """[(is_dir, more, name)] から DFI の索引バイト列を作る."""
        import struct

        recs = b"DFI\0" + b"\0" * 12
        for i, (is_dir, more, _n) in enumerate(shape):
            recs += struct.pack("<HHIII", 1 if is_dir else 0, more, 0, 1 + i, 16)
        names = b"".join(n.encode("ascii") + b"\0" for _, _, n in shape)
        return recs + names, len(shape), len(recs)

    def test_the_folder_rule_matches_the_public_source(self):
        """フォルダの閉じ方 (stack / flag) の決着 (#108).

        docs/09 の「未解決」に長く残っていた唯一の形式の疑問。**実物でしか
        決まらない**と書いてきたが、公開ソースの `unpackIMG` を合成索引に対して
        走らせれば、少なくとも**向こうがどちらの規則で動いているか**は決まる。

        小さな形を総当たりし、2 つの規則が食い違う形では毎回 flag と一致すること、
        そして**食い違う形が実際に見つかっていること** (0 件なら何も確かめていない)
        を見る。
        """
        import itertools

        total = diff = rejected = 0
        for combo in itertools.product([(1, 0), (1, 1), (0, 0), (0, 1)], repeat=5):
            shape = [(1, 1, "/")] + [(d, m, f"{'d' if d else 'f'}{i}")
                                     for i, (d, m) in enumerate(combo)]
            if not any(not d for d, _, _ in shape[1:]):
                continue
            idx, n, at = self.build_index(shape)
            ours_stack = [e["path"] for e in boku2.read_dfi(idx, 1 << 30, "stack")]
            ours_flag = [e["path"] for e in boku2.read_dfi(idx, 1 << 30, "flag")]
            try:
                theirs = self.their_paths(idx, n, at)
            except (AssertionError, IndexError):
                # 向こうが「索引が壊れている」と断る形 (フォルダの積みが途中で
                # 底を突く)。そもそも成り立たない索引なので比べる相手にならない
                rejected += 1
                continue
            total += 1
            self.assertEqual(theirs, ours_flag,
                             f"公開ソースと flag が違う: {combo}\n"
                             f"  彼ら {theirs}\n  flag {ours_flag}")
            if ours_stack != ours_flag:
                diff += 1
        print(f"\n    フォルダ規則: 比べた形 {total} 通り "
              f"(うち stack と flag が食い違う {diff} 通り) / 向こうが断った形 {rejected} 通り")
        self.assertGreater(total, 100, f"試した形が {total} 通りしかない")
        self.assertGreater(diff, 0,
                           "stack と flag が食い違う形が 1 つも出ていない。"
                           "それでは「flag と一致」に意味が無い (形の作り方を見直す)")

    def test_the_default_rule_is_the_one_with_evidence(self):
        """既定は flag。実物で動いている実装と同じ側にしておく (#108).

        画面側は `tests/test_index.mjs` が **2 通りで答えの違う索引を読ませて**
        確かめる。ここで `rule = rule || "flag"` の字を探していたが、
        それはソースを見ているだけで、通らない枝でも通る (#137)。
        こちら (CLI) は既定値そのものを見るので、字ではなく本物。
        """
        import inspect

        sig = inspect.signature(boku2.read_dfi)
        self.assertEqual(sig.parameters["rule"].default, "flag",
                         "CLI の既定が flag でない")
        # 既定で呼んだときに flag と同じ答えになること (署名だけでなく中身も見る)
        idx = self.mismatching_index()
        default = [e["path"] for e in boku2.read_dfi(idx, 40 * 2048)]
        as_flag = [e["path"] for e in boku2.read_dfi(idx, 40 * 2048, "flag")]
        as_stack = [e["path"] for e in boku2.read_dfi(idx, 40 * 2048, "stack")]
        self.assertNotEqual(as_flag, as_stack, "この索引では 2 通りが同じ答え (検査にならない)")
        self.assertEqual(default, as_flag, "既定が flag の答えになっていない")

    @staticmethod
    def mismatching_index() -> bytes:
        """stack と flag で道筋の変わる索引 (A の中に B と C。#108 の形)."""
        import struct

        recs = [(1, 1, "/"), (1, 0, "A"), (1, 1, "B"), (0, 0, "b0.bin"),
                (1, 0, "C"), (0, 0, "c0.bin")]
        recs += [(0, 0 if i == 7 else 1, f"r{i}.bin") for i in range(8)]
        body, lba = b"", 0
        for is_dir, more, _ in recs:
            if is_dir:
                body += struct.pack("<HHIII", 1, more, 0, 0, 0)
            else:
                body += struct.pack("<HHIII", 0, more, 0, lba, 2048)
                lba += 1
        names = b"".join(n.encode("ascii") + b"\0" for _, _, n in recs)
        return b"DFI\0" + struct.pack("<III", len(recs), 0, 0) + body + names

    def their_clut_order(self, n: int, image_format: int, linear: bool = False) -> dict:
        """公開ソース TIM2.py のパレット並び替えの式を**そのまま動かして**、置換を取り出す.

        向こうは 16×16 の升目に置く形で書いてある。こちらは番号→番号の関数
        (csm1Index) なので、式を動かして (行, 列) を番号に直してから比べる。
        """
        with open(os.path.join(PUBLIC_SRC, "TIM2.py"), encoding="utf-8") as fh:
            src = fh.read()
        try:
            start = src.index("            for x in range(n_palette_entries):")
            # 同じ行が 2 回出る (内側の else と外側の else)。2 つ目まで含めないと
            # 並び替えをしない側が丸ごと落ちる (最初これで空振りした)
            mark = "CLUT_array[x//16][x%16] = (red, green, blue, alpha)"
            first = src.index(mark, start)
            end = src.index("\n", src.index(mark, first + 1)) + 1
        except ValueError:
            self.skipTest("公開ソース TIM2.py の作りが変わった")
        body = "\n".join(ln[12:] if ln.startswith(" " * 12) else ln
                         for ln in src[start:end].split("\n"))

        cells = {}

        class Row:
            def __init__(self, i):
                self.i = i

            def __setitem__(self, j, v):
                cells[self.i * 16 + j] = v[0]

        class Grid:
            def __getitem__(self, i):
                return Row(i)

        class Feed:
            def __init__(self):
                self.k = 0

            def read(self, _n):
                v = self.k
                self.k += 1
                return v.to_bytes(4, "little")

        exec(body, {"n_palette_entries": n, "TIM2_file": Feed(), "CLUT_array": Grid(),
                    "palette": [], "linear_CLUT": linear, "image_format": image_format})
        return cells

    def test_the_palette_reorder_matches_the_public_source(self):
        """8bit 索引のパレット並び替え (CSM1) が公開ソースと同じ置換であること (#109).

        ここは docs/09 が「GS の CSM1 並び替えつき」とだけ書いてきた所で、
        いちばん間違えても気づきにくい (色がずれるだけで、絵は出る)。
        """
        ours = self.ours_csm1()          # 画面の本物を動かす (書き写すと壊しても気づかない)
        theirs = self.their_clut_order(256, 5)
        self.assertEqual(len(theirs), 256, f"向こうが置いたのは {len(theirs)} 色")
        self.assertTrue(any(theirs[p] != p for p in range(256)),
                        "並び替えが起きていない。これでは一致に意味が無い")
        for p in range(256):
            self.assertEqual(theirs[p], ours[p], f"{p} 番の置き場が違う")

    @staticmethod
    def ours_csm1() -> list:
        """web/app.js の csm1Index を**そのまま**動かして 0..255 の対応を得る (#109).

        最初はこの式を Python で書き写していた。画面側を壊しても検査が通ってしまい、
        #103 で踏んだ「書き写した定数は古くなる」と同じ穴だった。本物を動かす。
        """
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');"
            "const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('/* @extract-start tim2 */');"
            "const b=s.indexOf('/* @extract-end tim2 */');"
            "if(a<0||b<0){console.error('no tim2 block');process.exit(2);}"
            "const m=new Function('u16le','u32le',s.slice(a,b)+'\\nreturn {csm1Index};')"
            "(()=>0,()=>0);"
            "console.log(JSON.stringify([...Array(256).keys()].map(m.csm1Index)));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("csm1Index を取り出せない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    def test_the_four_bit_palette_is_not_reordered_on_either_side(self):
        """4bit 索引では並び替えない。こちらも imageType 5 のときだけ並び替える."""
        theirs = self.their_clut_order(256, 4)
        self.assertEqual(len(theirs), 256)
        self.assertTrue(all(theirs[p] == p for p in range(256)),
                        "向こうは 4bit でも並び替えていた")
        js = self.read_app_js()
        self.assertIn("pic.imageType !== 5", js,
                      "画面側が 8bit 索引だけに絞っていない")

    def test_the_extra_padding_rule_matches(self):
        """見出しの前に 0x70 の空きが入る条件 (#109).

        公開ソースは「形式の欄が 1」のほかに「+8 の値が 0x4001A0」でも空きを見込む。
        こちらは前者しか見ていなかった。見出しの位置を外すと幅も画素の種類も全部ずれる。
        """
        import struct

        head = b"TIM2" + bytes([4, 0]) + struct.pack("<H", 1)
        self.assertEqual(boku2.tim2_header_at(head + struct.pack("<I", 0x4001A0), 0, 0), 0x80,
                         "0x4001A0 のときに 0x70 の空きを見込んでいない")
        self.assertEqual(boku2.tim2_header_at(head + struct.pack("<I", 0), 0, 0), 0x10)
        self.assertEqual(boku2.tim2_header_at(head + struct.pack("<I", 0), 0, 1), 0x80,
                         "形式の欄が 1 のときの空きが消えている")
        # 画面側は本物を動かして確かめる。文字を探すだけだと、比べる値を変えられても通る
        self.assertEqual(self.browser_header_at(0x4001A0), 0x80,
                         "画面側が 0x4001A0 のときに 0x70 の空きを見込んでいない")
        self.assertEqual(self.browser_header_at(0), 0x10,
                         "画面側が余計に空きを入れている")

    @staticmethod
    def browser_header_at(word: int) -> int:
        """形式の欄 0 で +8 が `word` の TIM2 を画面の parseTim2 に読ませ、見出しの位置を返す."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');"
            "const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('/* @extract-start tim2 */');"
            "const b=s.indexOf('/* @extract-end tim2 */');"
            "const u16le=(x,p)=>x[p]|(x[p+1]<<8);"
            "const u32le=(x,p)=>((x[p]|(x[p+1]<<8)|(x[p+2]<<16)|(x[p+3]<<24))>>>0);"
            "const m=new Function('u16le','u32le',s.slice(a,b)+'\\nreturn {parseTim2};')(u16le,u32le);"
            f"const W={word};"
            "const buf=new Uint8Array(0x200);"
            "buf.set([0x54,0x49,0x4D,0x32,4,0,1,0],0);"
            "buf[8]=W&0xFF;buf[9]=(W>>8)&0xFF;buf[10]=(W>>16)&0xFF;buf[11]=(W>>24)&0xFF;"
            # 見出しを 0x10 と 0x80 の両方に置き、どちらを読んだかを幅で見分ける
            "const put=(p,w)=>{buf[p+12]=48;buf[p+14]=16;buf[p+18]=3;buf[p+19]=5;"
            "buf[p+20]=w&0xFF;buf[p+21]=(w>>8)&0xFF;buf[p+22]=8;};"
            "put(0x10,111);put(0x80,222);"
            "const r=m.parseTim2(buf,0);"
            "console.log(JSON.stringify(r&&r.pictures[0]?r.pictures[0].width:null));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("parseTim2 を動かせない: " + res.stdout + res.stderr)
        width = json.loads(res.stdout)
        return {111: 0x10, 222: 0x80}.get(width, width)

    @staticmethod
    def read_app_js() -> str:
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_glyph_table_is_the_same_size_as_theirs(self):
        """向こうの font.txt は 1656 字。docs が言う 72 行 × 23 列の外からの裏付け."""
        path = os.path.join(PUBLIC_SRC, "font.txt")
        if not os.path.isfile(path):
            self.skipTest("公開ソースに font.txt が無い")
        with open(path, encoding="utf-8") as fh:
            chars = fh.read().replace("\n", "")
        self.assertEqual(len(chars), 72 * boku2.FONT_COLS,
                         f"公開ソースの文字表が {len(chars)} 字 "
                         f"(こちらの見立ては 72 × {boku2.FONT_COLS})")

    def test_the_menu_msg_reads_the_same(self):
        n = self.compare(os.path.join(self.unpacked, "system", "system.msg"), 0, 8)
        self.assertGreaterEqual(n, 4, f"比べた項目が {n} 件しかない")

    def test_the_alt_break_menu_reads_the_same(self):
        """0x8002 を引数の無いページ送りとして読むファイル (ALT_BREAK_FILES)."""
        n = self.compare(os.path.join(self.unpacked, "system", "submenu", "item", "item_info.msg"), 0, 8, alt=True)
        self.assertGreaterEqual(n, 2, f"比べた項目が {n} 件しかない")

    def test_the_copied_file_name_lists_still_match_the_public_source(self):
        """**公開ソースから書き写した名前の一覧が、ずれていないこと** (#189).

        `ALT_BREAK_FILES` の 7 つは、`0x8002` の読み方が変わるファイルの名前です。
        名前が 1 字でもずれると、そのファイルだけ `0x8002` を「待ち時間 + u16」で
        読み、**ページ送りのたびに次の 1 字を飛ばします**。出力は日本語のままなので、
        見ても分かりません。試しに `turi_info.msg` を `turi_info_TYPO.msg` に
        変えてみたら、**387 件のテストが全部通りました** —— 振る舞いの検査は
        `item_info.msg` だけで代表させていて、名前そのものは誰も見ていなかった。

        名前の一覧は**向こうが正解**なので、向こうから読み取って比べます。
        画面 (`web/app.js`) の写しも同時に見ます。
        """
        import re

        msg_py = os.path.join(PUBLIC_SRC, "MSG.py")
        if not os.path.isfile(msg_py):
            self.skipTest(f"公開ソースに MSG.py が無い ({msg_py})")
        with open(msg_py, encoding="utf-8", errors="replace") as fh:
            public = fh.read()

        def names_in(text, var):
            m = re.search(re.escape(var) + r"\s*=\s*(?:new Set\()?[\[(]([^\])]*)", text)
            self.assertTrue(m, f"{var} の一覧を読み取れない")
            return {n.lower() for n in re.findall(r'"([^"]+\.\w+)"', m.group(1))}

        theirs = names_in(public, "ALT_NEWLINE_FILES")
        self.assertEqual(len(theirs), 7,
                         f"公開ソースの一覧が {len(theirs)} 件 (読み取り方が壊れた)")
        self.assertEqual(set(boku2.ALT_BREAK_FILES), theirs,
                         "boku2.py の ALT_BREAK_FILES が公開ソースとずれています")

        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        self.assertEqual(names_in(app, "ALT_BREAK_FILES"), theirs,
                         "web/app.js の ALT_BREAK_FILES が公開ソースとずれています")

        # 入れ物の一覧は**書き写しではなく要約** (向こうは `fish\\img\\…` のような道)。
        # そこで「向こうのどこかに同じ名前がある」ことと、画面と CLI が
        # **同じ並び**であること (#104 で報告の並びがずれた) を見る
        unpack_py = os.path.join(PUBLIC_SRC, "UNPACK.py")
        if os.path.isfile(unpack_py):
            with open(unpack_py, encoding="utf-8", errors="replace") as fh:
                paths = fh.read()
            for name in boku2.TEXT_CONTAINERS:
                self.assertTrue(name in paths.lower() or name in public.lower(),
                                f"{name} は公開ソースのどの一覧にもありません")
        m = re.search(r"const TEXT_CONTAINERS = \[([^\]]*)\]", app)
        self.assertTrue(m, "web/app.js の TEXT_CONTAINERS を読み取れない")
        self.assertEqual(re.findall(r'"([^"]+)"', m.group(1)),
                         list(boku2.TEXT_CONTAINERS),
                         "画面と CLI で入れ物の一覧の並びが違います (#104)")

    @staticmethod
    def index_from_paths(paths: list) -> tuple[bytes, bytes]:
        """道の一覧から DFI の索引を組む (各フォルダの最後の項目だけ「続く」を 0 にする)."""
        tree, root = [(True, "/", 1)], {}
        for p in paths:
            cur = root
            for d in p.split("/")[:-1]:
                cur = cur.setdefault(d, {})
            cur[p.split("/")[-1]] = None

        def walk(items):
            keys = list(items)
            for i, k in enumerate(keys):
                tree.append((items[k] is not None, k, 0 if i == len(keys) - 1 else 1))
                if items[k] is not None:
                    walk(items[k])

        walk(root)
        recs, blob = [], b""
        for is_dir, _name, more in tree:
            if is_dir:
                recs.append((1, more, 0, 0))
            else:
                recs.append((0, more, len(blob) // 2048, 64))
                blob += b"\0" * 2048
        idx = b"DFI\0" + struct.pack("<I", 0x100) + b"\0" * 8
        for kind, more, lba, size in recs:
            idx += struct.pack("<HHIII", kind, more, 0, lba, size)
        idx += b"".join(n.encode() + b"\0" for _, n, _ in tree)
        return idx, blob

    def test_the_known_name_area_start_matches_our_record_reading(self):
        """実物の**名前の置き場 0x8140** が、こちらの読み方と噛み合うこと (#222).

        英語化パッチの公開ソースは `FILENAMES_START = 0x8140` と決め打ちしています。
        こちらは「見出し 16 バイト + 16 バイト刻みのレコード」と読んでいるので、
        (0x8140 - 16) / 16 = **2067 件**。社長の吸い出しで出た **1951 ファイル**
        (#1・#3) との差 116 がフォルダの数にあたります。

        **割り切れること自体が裏付け**です。見出しの大きさや刻みが 1 でも違えば、
        0x8140 は半端な件数になります。
        """
        import re

        unpack_py = os.path.join(PUBLIC_SRC, "UNPACK.py")
        if not os.path.isfile(unpack_py):
            self.skipTest("公開ソースに UNPACK.py が無い")
        with open(unpack_py, encoding="utf-8", errors="replace") as fh:
            m = re.search(r"FILENAMES_START\s*=\s*(0x[0-9A-Fa-f]+|\d+)", fh.read())
        self.assertTrue(m, "FILENAMES_START を読み取れない (向こうの作りが変わった)")
        theirs = int(m.group(1), 0)
        self.assertEqual(theirs, boku2.KNOWN_NAMES_AT,
                         f"公開ソースの値が 0x{theirs:X} に変わっています "
                         f"(boku2.py は 0x{boku2.KNOWN_NAMES_AT:X})")
        self.assertEqual((theirs - 16) % 16, 0,
                         "こちらの読み方 (見出し 16 + 16 バイト刻み) だと半端な件数になる")
        self.assertEqual((theirs - 16) // 16, 2067, "レコード件数の見立てが変わった")

    def test_a_real_sized_index_is_told_whether_the_names_start_where_they_should(self):
        """実物なみの索引で、**名前の置き場が合っているか**を言うこと (#222).

        実物で名前が付かなかった (#1・#3) とき、社長には比べる相手がありませんでした。
        0x8140 という**外から来た数**があれば、その場で「レコードの読み方がずれている」
        と分かります。練習データのような小さい索引では**言わない** ——
        実物の値を持ち出しても雑音にしかならないので。
        """
        import io as _io

        def build(names_at: int) -> tuple[bytes, bytes]:
            n = (names_at - 16) // 16
            idx = bytearray(b"DFI\0" + struct.pack("<I", 0x100) + b"\0" * 8)
            blob = bytearray()
            for i in range(n):
                idx += struct.pack("<HHIII", 0, 1 if i < n - 1 else 0, 0,
                                   len(blob) // 2048, 64)
                blob += b"\0" * 2048
            idx += b"".join(("f%04d.bin" % i).encode() + b"\0" for i in range(n))
            return bytes(idx), bytes(blob)

        def run(idx: bytes, blob: bytes) -> str:
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, "BOKU2.IDX"), "wb") as fh:
                    fh.write(idx)
                with open(os.path.join(tmp, "BOKU2.IMG"), "wb") as fh:
                    fh.write(blob)
                buf = _io.StringIO()
                boku2.check(tmp, out=buf)
                return buf.getvalue()

        ok = run(*build(boku2.KNOWN_NAMES_AT))
        self.assertIn("公開ソースが決め打ちしている値と同じです", ok, ok[:800])

        off = run(*build(boku2.KNOWN_NAMES_AT - 16))
        self.assertIn("レコードの読み方がずれている疑い", off, off[:800])
        self.assertIn("-1.0 件ぶん", off, f"ずれの大きさを言っていない:\n{off[:800]}")

        # **練習データでは言わない** (小さい索引に実物の値を持ち出さない)
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if not problem:
            buf = _io.StringIO()
            boku2.check(os.path.join(REPO, "work", "BOKU2SAMPLE"), out=buf)
            self.assertNotIn("0x8140", buf.getvalue(),
                             "小さい索引にも実物の値を持ち出している")

    def test_the_two_folder_rules_disagree_on_the_real_shape(self):
        """**実物のフォルダの形では、2 通りの規則が食い違う** (#221).

        docs/09 の未解決その 2 (フォルダの閉じ方) は「3 段以上まとめて閉じるときだけ
        答えが変わる」ので、いつまでも机上の話に見えていました。ところが公開ソースに
        書いてある**実物の .msg 24 本の道**でその形を組むと、**21 本で食い違います**
        (`system/namemsg/namemsg.msg` と `namemsg/namemsg.msg`)。

        つまり `check` の「フォルダの規則」の行は、実物では**必ず意味を持つ**。
        ここが一致したまま通ることは期待できないので、食い違ったときの
        **決め方**まで道具が言う必要がある (それも下で見る)。
        """
        import re

        msg_py = os.path.join(PUBLIC_SRC, "MSG.py")
        if not os.path.isfile(msg_py):
            self.skipTest("公開ソースに MSG.py が無い")
        with open(msg_py, encoding="utf-8", errors="replace") as fh:
            m = re.search(r"IMG_MSG_FILES\s*=\s*\[(.*?)\]", fh.read(), re.S)
        self.assertTrue(m, "IMG_MSG_FILES を読み取れない (向こうの作りが変わった)")
        paths = [re.sub(r"/+", "/", p.replace("\\", "/"))
                 for p in re.findall(r'"([^"]+)"', m.group(1))]
        self.assertGreaterEqual(len(paths), 20, f"道を {len(paths)} 本しか拾えない")
        self.assertTrue(any(p.count("/") >= 3 for p in paths),
                        "材料が弱い: 3 段以上の道が 1 本も無い")

        idx, blob = self.index_from_paths(paths)
        by_rule = {r: [e["path"] for e in boku2.read_dfi(idx, len(blob), rule=r)]
                   for r in ("stack", "flag")}
        differ = [(a, b) for a, b in zip(by_rule["stack"], by_rule["flag"]) if a != b]
        # **落ちたときに 24 本を 2 回並べない** (読めない失敗文は直せない失敗文)
        self.assertTrue(differ,
                        f"実物の形 ({len(paths)} 本) で 2 通りが一致した "
                        "(もしそうなら朗報。この検査ごと書き直すこと)")
        # **この組み方では stack がそのまま道を再現する**。組み方そのものが
        # stack 寄りなので、これは「stack が正しい」の証明ではない (#221)
        wrong = [f"{a} (組んだのは {b})" for a, b in zip(by_rule["stack"], paths) if a != b]
        self.assertEqual(wrong, [], "組んだ道を stack でも再現できない:\n  " + "\n  ".join(wrong[:3]))

    def test_the_tool_says_how_to_decide_which_folder_rule_is_right(self):
        """食い違ったときに、**決め方**まで言うこと (#221).

        「この行ごと報告してください」だけだと、社長は報告して待つしかない。
        実物の道は公開ソースに書いてあるので、**その場で決められる**。
        """
        import io as _io
        import re

        msg_py = os.path.join(PUBLIC_SRC, "MSG.py")
        if not os.path.isfile(msg_py):
            self.skipTest("公開ソースに MSG.py が無い")
        with open(msg_py, encoding="utf-8", errors="replace") as fh:
            m = re.search(r"IMG_MSG_FILES\s*=\s*\[(.*?)\]", fh.read(), re.S)
        paths = [re.sub(r"/+", "/", p.replace("\\", "/"))
                 for p in re.findall(r'"([^"]+)"', m.group(1))]
        idx, blob = self.index_from_paths(paths)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "BOKU2.IDX"), "wb") as fh:
                fh.write(idx)
            with open(os.path.join(tmp, "BOKU2.IMG"), "wb") as fh:
                fh.write(blob)
            buf = _io.StringIO()
            boku2.check(tmp, out=buf)        # `out` を渡す (既定は取り込み時の stdout)
            out = buf.getvalue()
        self.assertIn("フォルダの規則が 2 通りで食い違う", out, out[-600:])
        self.assertIn("決め方:", out, f"決め方を言っていない:\n{out[-600:]}")
        self.assertIn("フォルダ付きの道", out, f"何を見れば決まるかを言っていない:\n{out[-600:]}")

    def test_the_practice_tree_uses_the_real_paths(self):
        """練習データの道筋が、**実物の道筋**と同じであること (#220).

        公開ソースの `UNPACK.py` と `MSG.py` には、実物の中の道がそのまま
        書いてあります (`system\\saveload.bin`、`data\\map\\evt\\on_mem_event.bin`、
        `fish\\img\\fish_on_mem.bin`、`system\\submenu\\item\\item_info.msg`)。

        こちらの練習データはそこを**平らに**置いていました。いちばん確かめたいのは
        フォルダの入れ子の復元 (stack / flag) なのに、**実物より浅い形でしか
        試していなかった**ことになります。道筋を実物に合わせたので、
        ここで「向こうの一覧に同じ道がある」ことを見張ります。
        """
        import re

        want = {}
        for name in ("UNPACK.py", "MSG.py"):
            path = os.path.join(PUBLIC_SRC, name)
            if not os.path.isfile(path):
                self.skipTest(f"公開ソースに {name} が無い")
            with open(path, encoding="utf-8", errors="replace") as fh:
                for raw in re.findall(r'"([A-Za-z0-9_\\~]+\.\w+)"', fh.read()):
                    if "~" in raw:               # 向こうが展開した先のフォルダ
                        continue
                    # 同じ名前が**道つき**と**名前だけ**の両方で出てくる
                    # (`MENU_FONT_FILES` は名前だけ)。道の深いほうを採る
                    path = re.sub(r"/+", "/", raw.replace("\\", "/")).lower()
                    key = os.path.basename(path)
                    if path.count("/") >= want.get(key, "").count("/"):
                        want[key] = path

        base = os.path.join(REPO, "work", "BOKU2SAMPLE")
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        with open(os.path.join(base, "BOKU2.IDX"), "rb") as fh:
            idx = fh.read()
        ours = [e["path"].lower()
                for e in boku2.read_dfi(idx, os.path.getsize(os.path.join(base, "BOKU2.IMG")))]

        checked, bad = 0, []
        for path in ours:
            real = want.get(os.path.basename(path))
            if real is None:                     # 向こうの一覧に無い名前は見ない
                continue
            checked += 1
            # 向こうの一覧には作業フォルダの頭 (`img_rip_edits/…`) が付くものがある。
            # **こちらの道が向こうの道の末尾になっている**ことを見る
            if not ("/" + real).endswith("/" + path):
                bad.append(f"{path} (実物は {real})")
        self.assertGreaterEqual(checked, 5,
                                f"突き合わせた道が {checked} 本しかない (拾い方が壊れた)")
        self.assertEqual(bad, [], "練習データの道筋が実物と違います:\n  " + "\n  ".join(bad))

    def test_the_map_conversation_tables_read_the_same(self):
        """マップの会話は「表の一覧 + 4 バイト刻み」。こちらの 12 バイト項目の読みを外から確かめる."""
        path = os.path.join(self.unpacked, "maps", "M_A01000", "1.bin")
        with open(path, "rb") as fh:
            b = fh.read()
        tables = boku2.parse_tables(b)
        self.assertTrue(tables, "こちらが表の一覧として読めない")
        total = 0
        for t in tables:
            theirs = self.ns["readMSG"](path, t["off"], t["size"], 1)   # MAP_MODE
            self.assertEqual(len(theirs), len(t["msg"] or []),
                             f"表 {t['i']}: 項目数が違う (公開ソース {len(theirs)})")
            for raw, item in zip(theirs, t["msg"]):
                if boku2.voice_id(item["codes"]):
                    continue
                got = self.canon_ours(boku2.decode(item["codes"], self.glyphs, tags=False))
                want = self.canon_theirs(self.ns["convertRawToText"](self.dict, raw))
                self.assertEqual(got, want, f"表 {t['i']} の本文が公開ソースと違う")
                total += 1
        self.assertGreaterEqual(total, 4, f"比べた項目が {total} 件しかない")

    def their_unpack_map(self):
        """公開ソースの `unpackMap` を切り出して返す (入れ物の切り分け側)."""
        import re
        import types

        path = os.path.join(PUBLIC_SRC, "UNPACK.py")
        if not os.path.isfile(path):
            self.skipTest("公開ソースに UNPACK.py が無い")
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        m = re.search(r"^def unpackMap\(.*?(?=^def |\Z)", src, re.S | re.M)
        if not m:
            self.skipTest("公開ソースに unpackMap が見つからない (作りが変わった)")
        ns = {"os": os, "resource": self.ns["resource"], "log": lambda *a, **k: None}
        exec(m.group(0), ns)
        return ns["unpackMap"]

    def parts_theirs(self, unpack_map, data: bytes, type_: int) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "m.bin")
            with open(src, "wb") as fh:
                fh.write(data)
            out = os.path.join(tmp, "out")
            unpack_map(src, out, type_)
            got = {}
            if os.path.isdir(out):
                for name in os.listdir(out):
                    with open(os.path.join(out, name), "rb") as fh:
                        got[int(name.split(".")[0])] = fh.read()
            return got

    def parts_ours(self, data: bytes) -> tuple[int | None, dict]:
        r = boku2.parse_map_rec(data)
        if not r:
            return None, {}
        rec, items = r
        return rec, {it["i"]: data[it["at"]:it["at"] + it["len"]] for it in items if it["len"]}

    def test_the_container_reading_matches_their_unpacker(self):
        """入れ物の切り分けが、公開ソースの `unpackMap` と同じ部品を出すこと (#126).

        docs/09 の表で「MAP の入れ物」は**合成データのみ**が根拠だった行。
        向こうの `unpackMap` は実物で動いているので、突き合わせれば根拠が 1 段上がる。
        """
        unpack_map = self.their_unpack_map()
        targets = [os.path.join(self.sample, "MAP", n)
                   for n in sorted(os.listdir(os.path.join(self.sample, "MAP")))]
        # **道筋は実物に合わせてある** (#220)。公開ソースの UNPACK.py に出てくる道
        targets += [os.path.join(self.unpacked, *n.split("/")) for n in
                    ("diary.bin", "system/saveload.bin",
                     "data/map/evt/on_mem_event.bin", "fish/img/fish_on_mem.bin")]
        compared = 0
        for path in targets:
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            rec, ours = self.parts_ours(data)
            self.assertTrue(ours, f"{os.path.basename(path)} を入れ物として読めない")
            theirs = self.parts_theirs(unpack_map, data, 0 if rec == 12 else 1)
            self.assertEqual(ours, theirs,
                             f"{os.path.basename(path)}: 部品が公開ソースと違う "
                             f"(こちら {sorted(ours)} / 向こう {sorted(theirs)})")
            compared += 1
        self.assertGreaterEqual(compared, 5, f"比べた入れ物が {compared} 個しかない")

    def test_a_container_whose_first_u32_is_a_type_id_still_reads(self):
        """先頭の u32 が項目数でなく種別 ID でも読めること (#126).

        向こうの `unpackMap` はそこを `header_ID` (「たいてい (いつも?) 0xE」) として
        読み捨て、項目は +4 から**最初の位置まで**並んでいるものとして回す。
        数はどこにも書いていない。こちらは項目数として読んでいたので、
        値が 1〜64 の外に出ると「入れ物ではない」と言っていた。
        向こうの道具は実物で動いているほうなので、こちらが折れる。
        """
        unpack_map = self.their_unpack_map()
        parts = [b"A" * 32, b"B" * 48, b"C" * 16]

        def build(ident: int, head: int = 0x80) -> bytes:
            out, off = struct.pack("<I", ident), head
            for p in parts:
                out += struct.pack("<II", off, len(p))
                off += (len(p) + 15) // 16 * 16
            out += b"\0" * (head - len(out))
            return out + b"".join(p + b"\0" * ((16 - len(p) % 16) % 16) for p in parts)

        for ident in (0xE, 3, 100, 0, 0xFFFF):
            data = build(ident)
            theirs = self.parts_theirs(unpack_map, data, 1)
            self.assertEqual(len(theirs), 3,
                             f"前提が崩れた: 公開ソースが 先頭 {ident} を {len(theirs)} 個と読む")
            _, ours = self.parts_ours(data)
            self.assertEqual(ours, theirs, f"先頭 u32 が {ident} の入れ物で食い違う")

    def test_no_part_is_dropped_when_the_first_u32_is_smaller_than_the_table(self):
        """**先頭の数で止めて部品を落とさないこと** (#217).

        実物の見出しは `[u32 0xE][u32 0x80]` で、公開ソースの `unpackMap` は
        先頭を種別 ID として読み捨て、表は **+4 から「最初の位置」まで**回す。
        4〜0x80 に 8 バイト刻みで並ぶのは **15 項目**なので、0xE = 14 を項目数と
        して読むこちらは、**15 個目が中身入りでも黙って落とす**。
        落ちたことは誰にも分からない (残りの部品はちゃんと出るので)。

        ここでは向こうの `unpackMap` を実際に走らせて、同じ数になることを見る。
        """
        unpack_map = self.their_unpack_map()
        n, head = 15, 0x80
        parts = [bytes([0x50 + i]) * 64 for i in range(n)]
        out, off = bytearray(struct.pack("<I", 0xE)), head
        for p in parts:
            out += struct.pack("<II", off, len(p))
            off += 64
        out += b"\0" * (head - len(out))
        data = bytes(out) + b"".join(parts)

        theirs = self.parts_theirs(unpack_map, data, 1)
        # **材料が弱くないこと**: 先頭の数 (14) は本当に項目数より小さい
        self.assertLess(struct.unpack_from("<I", data, 0)[0], n, "材料が弱い")
        self.assertEqual(len(theirs), n,
                         f"前提が崩れた: 公開ソースが {len(theirs)} 個と読む")
        _, ours = self.parts_ours(data)
        self.assertEqual(sorted(ours), sorted(theirs),
                         f"部品を落としている (こちら {len(ours)} 個 / 向こう {len(theirs)} 個)")


class TestTheBorrowedNumbers(unittest.TestCase):
    """公開ソースから**借りてきた数**が、向こうの今の値と合っていること (#223).

    この一式の形の読み方には、向こうのソースから取った数がいくつも入っています
    (位置表の刻み 8 / 4、入れ物の項目の刻み 8 / 12、名前の置き場 0x8140、
    1 行 23 字、刻み 22 ドット)。**数が合っていることは、誰も見ていませんでした** ——
    #222 で 0x8140 を突き合わせたのが最初で、残りは「同じ値を書いてある」だけ。

    借りた数は**どこから借りたか**まで書いて、ここで実際にその場所を読みます。
    向こうが値を直したら (あるいはこちらが写し間違えたら) 落ちます。
    """

    #: (こちらの名前, 読み取る先, 読み取り方, 何の数か)
    BORROWED = (
        ("MSG_STRIDE", "MSG.py", r"MSG_MODE:\s*\n\s*f\.seek\(offset \+ 4 \+ x\*(0x[0-9A-Fa-f]+)",
         ".msg の位置表の刻み"),
        ("MAP_MSG_STRIDE", "MSG.py",
         r"MAP_MODE or mode == OFFSET_ONLY_MODE:\s*\n\s*f\.seek\(offset \+ 4 \+ x\*(0x[0-9A-Fa-f]+)",
         "マップの中の会話の刻み"),
        ("MAP_ENTRY_ALT", "UNPACK.py", r"if type == 0:\s*\n\s*entry_size = (0x[0-9A-Fa-f]+)",
         "入れ物の項目の刻み (12 バイト側)"),
        ("MAP_ENTRY", "UNPACK.py", r"else:\s*\n\s*entry_size = (\d+)\s*\n",
         "入れ物の項目の刻み (8 バイト側)"),
        ("KNOWN_NAMES_AT", "UNPACK.py", r"FILENAMES_START\s*=\s*(0x[0-9A-Fa-f]+)",
         "名前の置き場"),
        ("FONT_COLS", "reprint.py", r"N_COLUMNS\s*=\s*(\d+)", "1 行の字数"),
        ("FONT_CELL", "reprint.py", r"CELL_WIDTH\s*=\s*(\d+)", "文字の刻み (ドット)"),
    )

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(PUBLIC_SRC):
            raise unittest.SkipTest(f"公開ソースが無い ({PUBLIC_SRC})")

    def test_every_borrowed_number_still_matches_the_public_source(self):
        import re

        for name, where, how, what in self.BORROWED:
            with self.subTest(name):
                path = os.path.join(PUBLIC_SRC, where)
                self.assertTrue(os.path.isfile(path), f"公開ソースに {where} が無い")
                with open(path, encoding="utf-8", errors="replace") as fh:
                    m = re.search(how, fh.read())
                self.assertTrue(m, f"{where} から {what} を読み取れない (向こうの作りが変わった)")
                theirs = int(m.group(1), 0)
                self.assertEqual(getattr(boku2, name), theirs,
                                 f"{what}: こちらは {getattr(boku2, name)}、"
                                 f"公開ソース ({where}) は {theirs}")

    def test_the_list_is_not_empty_or_trivially_passing(self):
        """借りた数が**本当にその値でないと困る**こと (素通し防止).

        値を取り違えても誰も落ちないなら、この検査は飾り。1 つずつ変えてみて、
        **少なくともどこかの検査が落ちる**ことを見る…のは重いので、
        ここでは「借りた数が全部違う値である」ことだけ見る
        (同じ値ばかりなら、突き合わせても当たり前になる)。
        """
        self.assertGreaterEqual(len(self.BORROWED), 5)
        values = [getattr(boku2, n) for n, _w, _h, _t in self.BORROWED]
        self.assertGreaterEqual(len(set(values)), 4,
                                f"借りた数が {sorted(set(values))} しかない (突き合わせが緩い)")


class TestTheCitationsPointAtSomethingReal(unittest.TestCase):
    """文書が挙げる**出典そのもの**を、公開ソースと突き合わせる (#165).

    #107 の検査は公開ソースの**関数を走らせて**答えを比べます。それで形式の
    読み違いは防げますが、文書の書き方は防げません。docs は随所で
    「`MSG.py` の `ALT_NEWLINE_FILES`」のように **どのファイルの何** を挙げて
    います。社長がその名前で公開ソースを開いて**見つからなければ**、そこから先の
    話は確かめようがありません。

    これは絵空事ではなく、docs/11 自身が 「数字 1 つでも出典を 2 つ以上で
    突き合わせる」「出典の行番号まで追い直した」と書いている文書です。
    ところが **その出典の書き方だけは、今まで一度も機械で見ていません**でした。

    実際、この検査を書くために手で確かめたとき、私は 2 回間違えました。
    `grep ... | head -4` で `reprint.py` の行が画面から落ち「`N_COLUMNS` は
    別のファイルにある」と早合点し、`BUGGED_LINES` を `[...]` の形で探して
    「無い」と読みました (本当は `{...}` の辞書)。**手で読むと間違える**という
    証拠がその場で 2 つ出たので、検査にします。

    公開ソースはリポジトリに**入れていない**ので、置き場が無ければ飛ばします。
    """

    #: 文書の「`ファイル名` の `名前`」という書き方
    CITE = r"`([A-Za-z_0-9]+\.(?:py|java|txt|asm))`\s*の\s*`([A-Za-z_0-9]+)`"
    #: 上の書き方で挙げている出典は、今この数だけある。**減ったら探し方が壊れた合図**
    CITE_MIN = 4

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(PUBLIC_SRC):
            raise unittest.SkipTest(f"公開ソースが無い ({PUBLIC_SRC})")

    @staticmethod
    def doc_paths() -> list:
        base = os.path.join(REPO, "docs")
        return [os.path.join(base, n) for n in sorted(os.listdir(base)) if n.endswith(".md")]

    @classmethod
    def doc_text(cls) -> str:
        out = []
        for path in cls.doc_paths():
            with open(path, encoding="utf-8") as fh:
                out.append(fh.read())
        return "\n".join(out)

    @staticmethod
    def public(name: str) -> str:
        with open(os.path.join(PUBLIC_SRC, name), encoding="utf-8") as fh:
            return fh.read()

    def test_every_named_constant_is_in_the_file_the_doc_names(self):
        """「`ファイル` の `名前`」と書いたら、その名前がそのファイルに在ること."""
        import re

        found, bad = [], []
        for path in self.doc_paths():
            rel = os.path.relpath(path, REPO)
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    for m in re.finditer(self.CITE, line):
                        fname, name = m.group(1), m.group(2)
                        found.append(f"{rel}:{i} {fname} の {name}")
                        # **こちらの道具の名前なら、こちらのファイルを見る** (#216)。
                        # 「`proofread.py` の `line_width`」まで公開ソースを探しに行くと、
                        # 自分の道具の話を書いた瞬間に落ちる。相手を取り違えた検査は、
                        # 直し方も間違って教える
                        ours = os.path.join(REPO, "tools", fname)
                        if os.path.isfile(ours):
                            with open(ours, encoding="utf-8") as fh2:
                                if not re.search(rf"\b{re.escape(name)}\b", fh2.read()):
                                    bad.append(f"{rel}:{i} tools/{fname} に {name} という名前が無い")
                            continue
                        src = os.path.join(PUBLIC_SRC, fname)
                        if not os.path.isfile(src):
                            bad.append(f"{rel}:{i} 公開ソースにも tools/ にも {fname} が無い")
                        elif not re.search(rf"\b{re.escape(name)}\b", self.public(fname)):
                            bad.append(f"{rel}:{i} {fname} に {name} という名前が無い")
        # 出典を 1 つも拾えないまま緑になるのを防ぐ
        self.assertGreaterEqual(len(found), self.CITE_MIN,
                                f"出典の書き方を {len(found)} 件しか拾えない (探し方が壊れた)")
        self.assertEqual(bad, [], "文書の出典が公開ソースに無い:\n  " + "\n  ".join(bad))

    def test_the_font_grid_numbers_are_the_public_sources_own(self):
        """升目の数字が、挙げた行の**書かれ方そのまま**で公開ソースに在ること.

        #158 の検査は文書どうし・道具との食い違いを見ます。こちらは
        **その大もと**が本当にそう書いてあるかを見ます。
        """
        import boku2

        src = self.public("reprint.py")
        for line in (f"N_COLUMNS = {boku2.FONT_COLS}", f"CELL_WIDTH = {boku2.FONT_CELL}"):
            self.assertTrue(line in src,
                            f"reprint.py に「{line}」の行が無い (出典の書き方が変わった)")
        # 三つ目の証人。**文字表の各行の幅**が、そのまま 1 行の字数
        rows = [r for r in self.public("font.txt").split("\n") if r]
        widths = {len(r) for r in rows}
        self.assertGreater(len(rows), 60, f"文字表が {len(rows)} 行しかない")
        self.assertEqual(widths, {boku2.FONT_COLS},
                         f"文字表の行の幅が {sorted(widths)} (1 行 {boku2.FONT_COLS} 字のはず)")

    def test_the_assembly_note_is_quoted_word_for_word(self):
        """docs/11 が `asm_notes.txt` から引いた字句が、そのまま在ること.

        書き落とし (`char%17`) も **在ることが前提の話** なので、一緒に見る。
        向こうが直したら、docs/11 の「書き落とし」の説明は成り立たなくなる。
        """
        import re

        notes = self.public("asm_notes.txt")
        # **文書が引いている字句のほうを集める**。こちら側に写しを持つと、
        # 写しと出典だけが合っていて文書は野放し、という素通しになる (#150)
        pat = re.compile(r"`([^`\n]+)`|「([^」\n]+)」")
        found, bad = [], []
        for path in self.doc_paths():
            rel = os.path.relpath(path, REPO)
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    if "asm_notes" not in line:
                        continue
                    for m in pat.finditer(line):
                        q = m.group(1) or m.group(2)
                        if "char" not in q and not re.fullmatch(r"[+*]0x[0-9a-fA-F]+", q):
                            continue
                        found.append(f"{rel}:{i} {q}")
                        if q not in notes:
                            bad.append(f"{rel}:{i} asm_notes.txt に「{q}」が無い")
        self.assertGreaterEqual(len(found), 10,
                                f"引用を {len(found)} 件しか拾えない (探し方が壊れた)")
        self.assertEqual(bad, [], "文書の引用が asm_notes.txt に無い:\n  " + "\n  ".join(bad))
        # 書き落とし (`char%17`) は **在ることが前提の話**。向こうが直したら
        # 文書の「書き落とし」の説明ごと成り立たなくなる
        self.assertTrue("char%17" in notes, "asm_notes.txt から書き落としが消えた")
        self.assertTrue("char%0x17" in notes, "asm_notes.txt から正しい側の行が消えた")

    def test_the_lookalike_examples_really_use_the_kanji(self):
        """docs/04 の「形が同じで別の字」の実例が、公開ソースの綴りどおりであること.

        docs/04 は買い物と虫相撲の語を挙げて「全部 `一` (漢数字)」と言い切って
        います。**言い切りの根拠は向こうの綴り**なので、1 文字ずつ確かめる。
        """
        import re

        src = self.public("MSG.py")
        with open(os.path.join(REPO, "docs", "04-校正とQA.md"), encoding="utf-8") as fh:
            doc = fh.read()
        # **言い切っている一文だけ**を見る。文書ぜんぶから鉤括弧を拾うと、
        # 「ゲーム」のような地の文まで実例に数えてしまう (#151 と同じ形)
        claim = "**全部 `一` (漢数字)** です"
        self.assertTrue(claim in doc, f"docs/04 に「{claim}」の言い切りが無い")
        head = doc[:doc.index(claim)]
        sentence = head[head.rindex("。") + 1:] if "。" in head else head
        words = re.findall(r"「([^」\n]{2,20})」", sentence)
        self.assertGreaterEqual(len(words), 5,
                                f"言い切った一文から実例を {len(words)} 語しか拾えない: {words}")
        bad = []
        for w in words:
            if "ー" in w:
                bad.append(f"「{w}」は長音記号 (U+30FC) で書いてある")
            elif w not in src:
                bad.append(f"「{w}」が MSG.py に無い (綴りが違う)")
        self.assertEqual(bad, [], "docs/04 の実例が公開ソースと合わない:\n  " + "\n  ".join(bad))

    def test_the_glyph_numbers_match_the_public_table(self):
        """docs/04 の字の番号 (`ー` 1589 / `一` 279) が、文字表から出る番号と合うこと."""
        import re

        import boku2

        with open(os.path.join(REPO, "docs", "04-校正とQA.md"), encoding="utf-8") as fh:
            doc = fh.read()
        want = {m.group(1): int(m.group(2))
                for m in re.finditer(r"`(.)` \(番号 (\d+)\)", doc)}
        self.assertEqual(set(want), {"一", "ー"},
                         f"番号を書いた字を拾えない: {want}")
        rows = [r for r in self.public("font.txt").split("\n") if r]
        for ch, num in sorted(want.items()):
            spot = [(r, row.find(ch)) for r, row in enumerate(rows) if ch in row]
            self.assertEqual(len(spot), 1, f"文字表に `{ch}` が {len(spot)} 個ある")
            r, c = spot[0]
            self.assertEqual(r * boku2.FONT_COLS + c, num,
                             f"`{ch}` は {r} 行 {c} 列 = 番号 "
                             f"{r * boku2.FONT_COLS + c} で、文書の {num} と違う")


class TestEveryTagTheExtractorWritesIsUnderstood(unittest.TestCase):
    """取り出す側が書く記号を、測る側が全部知っていること (#105).

    `boku2.py` はメニュー 7 ファイル (ALT_BREAK_FILES: item_info.msg など) の
    0x8002 を **`<BREAK>`** (引数の無いページ送り) と書き出す。ところが
    `proofread.py` は `<WAIT>` と `<CLEAR>` でしかページを割らず、
    `scrp.split_lines` は `<BR>` でしか行を割っていなかった。

    結果、`あみ<BREAK>むしをつかまえる` は **1 行 (幅 10)** として測られていた。
    本当は 2 行 (幅 2 と 8)。道具の中でいちばん本業に近い所 —— 行数と幅の検査 ——
    が、実物のメニュー文で黙って外れる状態だった。しかも `proofread` は
    ERROR 0 件で通るので、**通ったことが安心の根拠にならない**。

    取り出し側の記号の一覧を原本から取って、測る側が全部割れることを見る。
    """

    def test_the_break_tag_splits_lines_and_pages(self):
        self.assertEqual(scrp.split_lines("あみ<BREAK>むしをつかまえる"),
                         ["あみ", "むしをつかまえる"], "<BREAK> で行が割れていない")
        self.assertEqual(proofread.pages_of("あみ<BREAK>むしをつかまえる"),
                         [["あみ"], ["むしをつかまえる"]], "<BREAK> でページが割れていない")

    def test_the_widths_are_measured_per_line(self):
        """1 行にまとめて数えると、幅の検査がすり抜ける."""
        wide = [scrp.display_width(scrp.strip_tags(l))
                for l in scrp.split_lines("あみ<BREAK>むしをつかまえる")]
        self.assertEqual(wide, [2.0, 8.0], f"行ごとの幅が {wide}")

    def test_no_tag_is_left_unknown(self):
        """boku2.py が書ける記号を原本から数え上げ、割る側が知っているか見る.

        新しい記号を足したときに、この検査ごと考え直させるのが狙い。
        """
        import re

        with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
            src = fh.read()
        # decode_text が tags=True のときに出す記号だけを拾う
        block = src[src.index("def decode("):src.index("def parse_glyph_table")]
        names = {t for t in re.findall(r'"<([A-Z]+)[:>]', block)}
        self.assertEqual(names, {"BR", "BREAK", "WAIT", "VOICE"},
                         f"boku2.py の記号が変わっている: {sorted(names)}。"
                         "増えたなら、行・ページ・幅のどれとして扱うかを決めてここに書く")
        # 引数の形も原本に合わせる (<WAIT:0A> は 2 桁、<VOICE:01234567> は 8 桁)
        samples = {"BR": "<BR>", "BREAK": "<BREAK>",
                   "WAIT": "<WAIT:0A>", "VOICE": "<VOICE:01234567>"}
        # (1) どの記号も、幅には数えない。1 つでも素通りすると幅の検査がずれる
        for name, tag in samples.items():
            self.assertEqual(scrp.strip_tags(f"あ{tag}い"), "あい",
                             f"{tag} がタグとして扱われず、幅に数えられている")
        # (2) 行を割るのは <BR> と <BREAK>。<WAIT> は行の途中、<VOICE> は目印
        for name, tag in samples.items():
            want = ["あ", "い"] if name in ("BR", "BREAK") else [f"あ{tag}い"]
            self.assertEqual(scrp.split_lines(f"あ{tag}い"), want,
                             f"{tag} の行の割り方が違う")
        # (3) ページを割るのは <WAIT> と <BREAK>
        for name, tag in samples.items():
            pages = proofread.pages_of(f"あ{tag}い")
            want = 2 if name in ("WAIT", "BREAK") else 1
            self.assertEqual(len(pages), want, f"{tag} のページの割り方が違う ({pages})")


class TestTheDelaySlotIsMarkedOnBothSides(unittest.TestCase):
    """遅延スロットの印が、画面と CLI の両方に出ること (#103).

    docs/08 は「分岐の次の 1 命令は必ず実行される」を MIPS でいちばん引っかかる
    癖として教え、**知らないと混乱します**とまで書いている。画面はその行に
    「← 遅延スロット」と出していたが、**CLI は何も出していなかった**。
    docs/08 が教える `elfdump.py --disasm` を叩いた人は、自分で目で探すことになる。

    #99 と同じ「片側にだけある判定」で、そのときは画面が遅れていた。今度は逆。
    どちらが遅れても分かるように、**両方に同じ ELF を読ませて印の付く番地を
    突き合わせる**。
    """

    def slots_from_cli(self, count: int) -> list[str]:
        import re
        import subprocess

        res = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "elfdump.py"),
             os.path.join(REPO, "work", "BOOT.ELF"), "--disasm", "--count", str(count)],
            capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return [m for m in re.findall(r"^([0-9A-F]{8}) .*← 遅延スロット", res.stdout, re.M)]

    def test_the_cli_marks_the_instruction_after_a_branch(self):
        problem = ensure_practice("work/BOOT.ELF", "make_elf.py")
        if problem:
            self.skipTest(problem)
        got = self.slots_from_cli(64)
        # 練習用 ELF は jal 2 つと jr 1 つを踏む。印が 1 つも出ないのが最悪の壊れ方
        self.assertTrue(got, "CLI が遅延スロットに印を付けていない")
        self.assertIn("00100040", got,
                      f"jr $ra の次の行に印が無い (付いたのは {got})")

    def test_the_lines_quoted_in_the_doc_really_come_out(self):
        """docs/08 が例に出している逆アセンブルの行を、実際に出して突き合わせる.

        #102 の宿題 (文書が載せている出力例を測り直す) をこの文書で行う。
        docs/08 は `0010003C jr $ra` のような番地入りの行を 4 行載せていて、
        練習用 ELF の中身が変われば全部ずれる。番地・命令・オペランドだけ見て、
        右側に付けてある日本語の説明は見ない (説明の書き方は自由でよい)。
        """
        import re

        problem = ensure_practice("work/BOOT.ELF", "make_elf.py")
        if problem:
            self.skipTest(problem)
        import subprocess

        res = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "elfdump.py"),
             os.path.join(REPO, "work", "BOOT.ELF"), "--disasm", "--count", "64"],
            capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        real = {}
        for at, mn, ops in re.findall(r"^([0-9A-F]{8})  [0-9A-F]{8}  (\S+)\s*(.*)$",
                                      res.stdout, re.M):
            real[at] = (mn, re.split(r"\s{2,}|←", ops)[0].strip())

        with open(os.path.join(REPO, "docs", "08-コードを読む.md"), encoding="utf-8") as fh:
            doc = fh.read()
        # 機械語の桁は**あってもなくてもよい**。#164 で docs/08 を道具の出力どおりに
        # 直したとき、この桁が増えてここが落ちた (命令を機械語と読み違えていた)
        quoted = re.findall(r"^([0-9A-F]{8})  (?:[0-9A-F]{8}  )?(\S+)\s+(.*)$", doc, re.M)
        self.assertTrue(quoted, "docs/08 に逆アセンブルの例が無い")
        for at, mn, ops in quoted:
            want = (mn, re.split(r"\s{2,}|←", ops)[0].strip())
            self.assertIn(at, real, f"docs/08 の {at} が出力に無い")
            self.assertEqual(real[at], want,
                             f"docs/08 の {at} は「{want[0]} {want[1]}」と書いてあるが、"
                             f"実際は「{real[at][0]} {real[at][1]}」")

    def test_the_two_sides_mark_the_same_addresses(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        problem = ensure_practice("work/BOOT.ELF", "make_elf.py")
        if problem:
            self.skipTest(problem)
        res = subprocess.run(
            [node, os.path.join(REPO, "tests", "test_disasm.mjs"), "--slots", "64"],
            capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        ui = res.stdout.split()
        self.assertTrue(ui, "画面側が遅延スロットを 1 つも出していない")
        self.assertEqual(self.slots_from_cli(64), ui,
                         "遅延スロットの印が画面と CLI で食い違う")


class TestTheWayQuotesAreMatched(unittest.TestCase):
    """引用を実際の出力に当てはめる書き方そのものを確かめる (#208).

    `doc_shape` は今、**3 か所**の見張り (docs/10 の引用・e2e の画面の引用・
    「報告するとき」の引用) が使っている。ここが緩むと 3 つとも同時に緩み、
    しかも**緑のまま**になる。#207 で見つけた「`ANSI` の N まで数にしてしまう」
    穴は、まさにそれが 1 年近く緑だった例なので、当て方自体に検査を付ける。
    """

    def test_a_number_written_as_a_letter_matches_a_number(self):
        for quote, text in (("名前が付いた N 件", "名前が付いた 20 件"),
                            ("合う N 件 / 合わない M 件", "合う 4 件 / 合わない 0 件"),
                            ("文字表に無い K 種", "文字表に無い 12 種"),
                            ("索引は N 個", "索引は 1,951 個")):
            with self.subTest(quote):
                self.assertRegex(text, doc_shape(quote))

    def test_a_letter_inside_a_word_is_left_alone(self):
        """`ANSI` の N を数に変えない。変えると**何を書いても当たらない**引用になる."""
        for quote, text in (("この TSV は ANSI で保存されています",
                             "注意: この TSV は ANSI で保存されています"),
                            ("[MAP] 1 番が会話だった N 件", "[MAP] 1 番が会話だった 2 件")):
            with self.subTest(quote):
                self.assertRegex(text, doc_shape(quote))

    def test_the_spaces_around_an_ellipsis_are_not_required(self):
        """`…` の前後の空白は読みやすさのためのもので、出力には無いことがある."""
        self.assertRegex("font.txt: 187 字 / 上の .msg で使われている番号 34 種のうち文字表に無い 0 種",
                         doc_shape("font.txt: N 字 / … 文字表に無い K 種"))

    def test_it_does_not_match_just_anything(self):
        """**緩すぎないこと。** ここが素通しだと、上の 3 つが全部無意味になる."""
        for quote, text in (("名前が付いた N 件", "名前が付いた ぜんぶ"),
                            ("[フォント] … N×M ドット", "[フォント] 幅×高さ ドット"),
                            ("合う N 件 / 合わない M 件", "合う 4 件 / 合わない")):
            with self.subTest(quote):
                self.assertNotRegex(text, doc_shape(quote))


class TestEveryQuotedOutputInTheDocsIsReal(unittest.TestCase):
    """docs/10 が「道具がこう言う」と引用した文言を、**全部まとめて**確かめる (#130).

    docs/10 は「見る 1 点」の表の下でこう約束している:

        表の「見る 1 点」は、**道具が実際にその言葉で出力する**ものだけにしてあります

    ところが #123・#127・#129 と 3 回続けて、引用のほうが古くなっていた。
    そのたびに 1 件ずつ検査を足してきたが、**足したものしか守られない**。
    ここでは表と「困ったとき」から引用を全部拾い出し、道具を実際に走らせて作った
    出力の山に、1 つずつ当てる。新しく引用を書けば、その場で守りが付く。

    `N` は数、`…` は途中の省略として当てる。画面だけが出す文言 (索引の「候補なし」など)
    は走らせずに `web/app.js` の**コメントを除いたコード**に当てる。

    **この当て方には穴がある (#131)。** 画面は文言を組み立てて出すので、
    `候補 ${n} 件` と ` (${top.known} 形式として読みました)` のように分かれていると、
    ソースをいくら探しても組み上がった形は見つからない。
    docs/10 の「1. 画面で確かめる」が「…と出れば」と書いている文言は、
    **描かれた文字**で見るしかないので `tests/e2e/sample.py` が持っている。
    ここで見るのは、そこに載らない CLI 側とベタ書きの文言。
    """

    #: 「N 個中 M 個」のような引用は数を当てはめて探す。当て方は
    #: `tests/e2e/common.py` の `doc_shape` **1 か所**に置いてある (#208)。
    #: ここと e2e の 2 か所に同じ当て方を書いていたせいで、#207 で直したはずの
    #: 「`N` をどこでも数にしてしまう」穴が、e2e 側にそのまま残っていた

    @classmethod
    def setUpClass(cls):
        import subprocess

        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        if ensure_practice("work/SCRIPT.BIN", "make_sample.py"):
            raise unittest.SkipTest("練習データ (SCRIPT.BIN) が作れない")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ran_tools = set()
        cls.corpus = cls.build_corpus(cls.tmp.name, cls.ran_tools)
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            cls.app = TestDocs.code_only(fh.read())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def build_corpus(tmp: str, ran: set) -> str:
        """道具を一通り走らせて、出た文字を全部つなげる (正常系も異常系も).

        `ran` に**実際に呼んだ**道具の名前を入れる。ソースを読んで数えると
        `if False:` で囲っただけの呼び出しまで数えてしまう (壊して確かめて気づいた #133)。
        """
        import subprocess

        def run(*args):
            ran.add(os.path.basename(args[0]))
            r = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=REPO)
            return r.stdout + r.stderr

        tool = lambda n: os.path.join(REPO, "tools", n)
        at = lambda *p: os.path.join(tmp, *p)
        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        font = os.path.join(sample, "font.txt")
        out = [run(tool("boku2.py"), "check", sample)]
        for how in ("idx", "name", "msg", "font", "map"):     # 課題 9 の 5 通り
            run(tool("make_boku2_sample.py"), "--break", how, "--out", at("BR" + how))
            out.append(run(tool("boku2.py"), "check", at("BR" + how)))
        os.makedirs(at("void"), exist_ok=True)
        # 検査値を全部反転させた表 (並び順が合わない形。名前は 1 件も当たらない)
        import struct as _struct
        with open(os.path.join(sample, "BOKU2.CRC"), "rb") as fh:
            _raw = bytearray(fh.read())
        _at = _struct.unpack_from("<5I", _raw, 0)[3]
        for _k in range((len(_raw) - _at) // 2):
            _p = _at + _k * 2
            _struct.pack_into("<H", _raw, _p, _struct.unpack_from("<H", _raw, _p)[0] ^ 0xFFFF)
        with open(at("flipped.crc"), "wb") as fh:
            fh.write(_raw)
        # 名前を 2 件そろえた表 (フォルダが付かないので実物では必ずぶつかる)
        with open(os.path.join(sample, "BOKU2.CRC"), "rb") as fh:
            _dup = bytearray(fh.read())
        _dir = _struct.unpack_from("<5I", _dup, 0)[1]
        for _i in (1, 2):
            _p = _dir + _i * boku2.CRC_ENTRY + 8
            _dup[_p:_p + boku2.CRC_ENTRY - 8] = b"\0" * (boku2.CRC_ENTRY - 8)
            _dup[_p:_p + len(b"same.bin")] = b"same.bin"
        with open(at("samename.crc"), "wb") as fh:
            fh.write(_dup)
        # 置き換えた後で初めてぶつかる 2 通り (#246)。`a:b.bin` は `_` にすると
        # `a_b.bin` と同じになり、`sys` はフォルダにもファイルにも使われている
        import make_boku2_sample as _mk
        _cidx, _cimg, _ = _mk.build_dfi([
            (True, 1, "/", None),
            (True, 1, "sys", None),
            (False, 1, "a:b.bin", b"A" * 32),
            (False, 0, "a_b.bin", b"B" * 32),       # ここで sys を閉じて根に戻る
            (False, 1, "sys", b"C" * 32),
            (False, 0, "tail.bin", b"D" * 32),
        ])
        with open(at("clash.idx"), "wb") as fh:
            fh.write(_cidx)
        with open(at("clash.img"), "wb") as fh:
            fh.write(_cimg)
        out += [
            run(tool("boku2.py"), "unpack", at("clash.idx"), at("clash.img"), at("OUTclash")),
            # **索引の名前も検査値の名前も読めて、一致する形** (#247)。いちばん普通の
            # 形なのに、この道でしか出ない知らせがある
            run(tool("boku2.py"), "unpack", os.path.join(sample, "BOKU2.IDX"),
                os.path.join(sample, "BOKU2.IMG"), at("OUTcrc0"),
                "--names-from-crc", os.path.join(sample, "BOKU2.CRC")),
            run(tool("boku2.py"), "check", at("void")),
            run(tool("boku2.py"), "unpack", os.path.join(sample, "BOKU2.IDX"),
                os.path.join(sample, "BOKU2.IMG"), at("OUT")),
            run(tool("boku2.py"), "maps", os.path.join(sample, "MAP"), "-o", at("OUT", "maps")),
            # 名前の置き場を壊した索引で unpack も走らせる。**名前が付かなかった**
            # ときの知らせは、この道でしか出ない (#161)。`check` だけ走らせていたので、
            # docs/10 がそれを引用した瞬間に「道具も画面も言わない」で落ちた
            run(tool("boku2.py"), "unpack", at("BRname", "BOKU2.IDX"),
                at("BRname", "BOKU2.IMG"), at("OUTname")),
            # **検査値ファイルから名前を付ける道** (#240)。読めない検査値ファイルと、
            # 並び順が合わない検査値ファイルの 2 通りは、この道でしか知らせが出ない
            run(tool("boku2.py"), "unpack", at("BRname", "BOKU2.IDX"),
                at("BRname", "BOKU2.IMG"), at("OUTcrc1"),
                "--names-from-crc", os.path.join(sample, "font.txt")),
            run(tool("boku2.py"), "unpack", at("BRname", "BOKU2.IDX"),
                at("BRname", "BOKU2.IMG"), at("OUTcrc2"),
                "--names-from-crc", at("flipped.crc")),
            # 名前がぶつかる表 (#245)。実物はフォルダが 116 あるので必ず起きる
            run(tool("boku2.py"), "unpack", at("BRname", "BOKU2.IDX"),
                at("BRname", "BOKU2.IMG"), at("OUTcrc3"),
                "--names-from-crc", at("samename.crc")),
            run(tool("boku2.py"), "maps", at("void"), "-o", at("m0")),
            run(tool("boku2.py"), "maps", at("nosuch") + "/*.BIN", "-o", at("m2")),
        ]
        with open(at("void", "X.BIN"), "wb") as fh:            # 入れ物でないファイル
            fh.write(bytes(range(256)) * 2)
        out.append(run(tool("boku2.py"), "maps", at("void"), "-o", at("m1")))
        with open(at("part.txt"), "w", encoding="utf-8") as fh:
            fh.write("あい\n")
        os.makedirs(at("sj"), exist_ok=True)
        with open(at("sj", "x.msg"), "wb") as fh:
            fh.write("\0".join(["はい", "いいえ"]).encode("cp932") + b"\0")
        os.makedirs(at("nofiles"), exist_ok=True)
        out += [
            run(tool("boku2.py"), "text", at("OUT"), "-f", font, "-o", at("a.tsv")),
            run(tool("boku2.py"), "text", at("OUT"), "-f", at("part.txt"), "-o", at("b.tsv")),
            run(tool("boku2.py"), "text", at("sj"), "-f", font, "-o", at("c.tsv")),
            run(tool("boku2.py"), "text", at("nofiles"), "-o", at("d.tsv")),
            run(tool("boku2.py"), "used", at("nofiles")),
        ]
        for name in ("h.tsv", "h2.tsv"):
            with open(at(name), "w", encoding="utf-8") as fh:
                fh.write("id\toriginal\ttranslation\n")
        open(at("blank.txt"), "w").close()
        qa = os.path.join(REPO, "exercises", "qa_target.tsv")
        enc = os.path.join(REPO, "work", "MSG_ENC.BIN")
        out += [
            run(tool("proofread.py"), qa),
            run(tool("proofread.py"), at("h.tsv")),
            run(tool("proofread.py"), qa, "--font-chars", at("blank.txt")),
            run(tool("compare_tsv.py"), at("h.tsv"), at("h2.tsv")),
            # うまくいった側も要る。失敗の道しか走らせないと「全部一致しました」が
            # 山に入らず、そこを引用した文書が**通らない**ことに気づけない (#133)
            run(tool("compare_tsv.py"), qa, qa),
            run(tool("dump_text.py"), os.path.join(sample, "BOKU2.IDX")),
            run(tool("insert_text.py"), at("a.tsv"), "-o", at("z.bin"),
                "--original", os.path.join(REPO, "work", "SCRIPT.BIN")),
        ]
        # **「注意:」の道も走らせる** (#207)。この 4 つは異常系の中でも特殊な材料が
        # 要るので、上の流れでは一度も出ていなかった。出ない文言を docs/10 が
        # 引用した瞬間に「道具も画面も言わない」で落ちる
        with open(at("ansi.tsv"), "wb") as fh:                 # ANSI で保存した TSV
            fh.write("id\toriginal\ttranslation\nr0\tかわで?をとる\tかわで?をとる\n".encode("cp932"))
        with open(at("hurt.tsv"), "w", encoding="utf-8") as fh:  # 訳文だけ ? に潰れた
            fh.write("id\toriginal\ttranslation\nr0\tぼくの\u2661なつやすみ\tぼくの?なつやすみ\n")
        with open(at("nums.tsv"), "w", encoding="utf-8") as fh:  # 文字表なしで取り出した形
            fh.write("id\toriginal\ttranslation\n")
            for i in range(10):
                fh.write(f"r{i}\t[{i}][{i + 1}][{i + 2}]\t[{i}][{i + 1}][{i + 2}]\n")
        with open(at("few.tsv"), "w", encoding="utf-8") as fh:   # 一部だけ番号が残った形
            fh.write("id\toriginal\ttranslation\n")
            for i in range(9):
                fh.write("r%d\tぼくのなつやすみ\tぼくのなつやすみ\n" % i)
            fh.write("r9\tぼくの[900]やすみ\tぼくの[900]やすみ\n")
        # 名前に使えない字がある索引 (unpack が `_` に変える) と、同じ名前が出る索引
        odd = at("odd")
        os.makedirs(odd, exist_ok=True)
        # 4 つ目は 1 つ目と**同じ名前**。`~2` を付けて区別した、の道はここでしか出ない
        names = ["a:b.msg", "c?d.bin", "e*f.tm2", "a:b.msg"]
        blob, recs = bytearray(), []
        for i, _n in enumerate(names):
            recs.append((len(blob) // 2048, 64))
            blob += bytes([i + 1]) * 64 + bytes(2048 - 64)
        head = bytearray(b"DFI\0" + struct.pack("<III", len(names), 0, 0))
        for sector, ln in recs:
            head += struct.pack("<HHIII", 0, 0, 0, sector, ln)
        for n in names:
            head += n.encode() + b"\0"
        with open(at("odd.IDX"), "wb") as fh:
            fh.write(bytes(head))
        with open(at("odd.IMG"), "wb") as fh:
            fh.write(bytes(blob))
        # 入れ物の大きさ (#210): 数え方が合う行と合わない行を 1 つずつ入れて、
        # 「N 行は数えられませんでした」の道も通す
        with open(at("room.tsv"), "w", encoding="utf-8") as fh:
            fh.write("id\tsize\toriginal\ttranslation\n")
            fh.write("a\t12\tはじめから\tゲームをさいしょからはじめる\n")
            fh.write("b\t7\tはじめから\tはじめから\n")
        # 列の入れ替えの道 (#226): size に合うのが訳文の側ばかりになる形
        with open(at("swap.tsv"), "w", encoding="utf-8") as fh:
            fh.write("id\tsize\toriginal\ttranslation\n")
            for i in range(6):
                one = f"げんぶん{i}ばんめのせりふ。"
                fh.write(f"r{i}\t{(len(one) + 1) * 2}\tやく{i}\t{one}\n")
        out.append(run(tool("proofread.py"), at("swap.tsv"), "--no-font-check"))
        # id の事故の道 (#225): 重なった id と、空欄の id
        with open(at("ids.tsv"), "w", encoding="utf-8") as fh:
            fh.write("id\toriginal\ttranslation\n")
            fh.write("r0\tあさ\tあさ\nr1\tひる\tひる\nr1\tよる\tよる\n\tゆうがた\tゆうがた\n")
        out.append(run(tool("proofread.py"), at("ids.tsv"), "--no-font-check"))
        # 行をまたぐ知らせの道 (#224): 1 行ずれと、行数オーバーが大半を占める形
        with open(at("slip.tsv"), "w", encoding="utf-8") as fh:
            fh.write("id\toriginal\ttranslation\n")
            lines = [f"せりふ{i}ばんめです。" for i in range(12)]
            for i, one in enumerate(lines):
                fh.write(f"r{i}\t{one}\t{lines[i - 1] if i else one}\n")
        with open(at("tall.tsv"), "w", encoding="utf-8") as fh:
            fh.write("id\toriginal\ttranslation\n")
            tall = "あ<BR>い<BR>う<BR>え<BR>お"
            for i in range(8):
                fh.write(f"r{i}\t{tall}\tやくぶん{i}<BR>にぎょうめ<BR>さんぎょうめ<BR>よんぎょうめ\n")
        out += [
            run(tool("proofread.py"), at("slip.tsv"), "--no-font-check"),
            run(tool("proofread.py"), at("tall.tsv"), "--no-font-check"),
            run(tool("proofread.py"), at("room.tsv"), "--no-font-check"),
            run(tool("proofread.py"), at("ansi.tsv"), "--no-font-check"),
            run(tool("proofread.py"), at("hurt.tsv"), "--no-font-check"),
            run(tool("proofread.py"), at("nums.tsv"), "--no-font-check"),
            run(tool("proofread.py"), at("few.tsv"), "--no-font-check"),
            run(tool("boku2.py"), "unpack", at("odd.IDX"), at("odd.IMG"), at("oddout")),
        ]
        if os.path.isfile(enc):                      # 課題 1・3 の道具 (docs/10 が名指しする)
            tbl = os.path.join(REPO, "answers", "custom.tbl")
            with open(at("part.tbl"), "w", encoding="utf-8") as fh:
                with open(tbl, encoding="utf-8") as src:
                    lines = src.read().splitlines()
                fh.write("\n".join(l for i, l in enumerate(lines) if i % 3))
            out += [
                run(tool("hexdump.py"), enc, "--struct"),
                run(tool("hexdump.py"), enc, "--table", tbl),
                run(tool("hexdump.py"), enc, "--table", at("part.tbl"), "--message", "0"),
            ]
        return "\n".join(out)

    def quoted(self) -> list[tuple[str, str]]:
        """docs/10 の 2 つの表から、引用した文言を (どの表, 文言) で拾う."""
        import re

        got = []
        head = self.doc.split("| | やること | 見る 1 点")[1].split("\n\n")[0]
        for line in head.splitlines():
            cells = [c.strip() for c in line.split("|")]
            if len(cells) > 4 and cells[1] not in ("", "---"):
                got += [("見る 1 点", q) for q in re.findall(r"「([^」]+)」", cells[3])]
        table = self.doc.split("## 困ったとき")[1].split("###")[0]
        for line in table.splitlines():
            cells = [c.strip() for c in line.split("|")]
            if len(cells) > 2 and cells[1] not in ("", "---", "症状"):
                got += [("困ったとき", q) for q in re.findall(r"「([^」]+)」", cells[1])]
        # **欄を消すなの表** (#227)。表計算の事故 4 つを 1 か所にまとめた所で、
        # 「道具の言い方」の欄に引用がある。ここも歩かないと、まとめた瞬間に古くなる
        keep = self.doc.split("### `id` と `size` の欄は消さないでください")
        if len(keep) > 1:
            table = next(c for c in keep[1].split("\n\n") if c.lstrip().startswith("| 起きる事故"))
            for line in table.splitlines():
                cells = [c.strip() for c in line.split("|")]
                if len(cells) > 4 and cells[1] not in ("", "---", "起きる事故"):
                    got += [("欄を消すな", q) for q in re.findall(r"「([^」]+)」", cells[3])]
        return got

    def where(self, phrase: str) -> str | None:
        import re

        pattern = doc_shape(phrase)
        if re.search(pattern, self.corpus):
            return "CLI"
        return "画面" if re.search(pattern, self.app) else None

    def test_the_corpus_really_contains_output(self):
        """前提の確認: 走らせた結果がちゃんと集まっていること.

        道具が全部こけていると山が空になり、下の検査は「見つからない」だらけで
        落ちる**が**、逆に山が巨大なゴミだと何にでも当たってしまう。
        目印になる行がいくつか入っているかを見る。
        """
        self.assertGreater(len(self.corpus), 3000, "走らせた結果が少なすぎる")
        for mark in ("== 診断:", "== 結果:", "使った設定"):
            self.assertIn(mark, self.corpus, f"山に「{mark}」が無い (道具が動いていない)")

    def test_every_quoted_phrase_is_something_a_tool_prints(self):
        quotes = self.quoted()
        self.assertGreaterEqual(len(quotes), 15,
                                f"引用を {len(quotes)} 個しか拾えていない (拾い方が壊れた)")
        # **表ごとに拾えていること** (#227)。まとめた表を足しても、拾い方が
        # その表に届いていなければ **0 件で緑**になる (足した回に実際に踏んだ)
        for where in ("見る 1 点", "困ったとき", "欄を消すな"):
            self.assertTrue(any(w == where for w, _q in quotes),
                            f"「{where}」の表から 1 つも拾えていない")
        missing = [(where, q) for where, q in quotes if self.where(q) is None]
        self.assertEqual(missing, [],
                         "docs/10 が引用しているのに、道具も画面も言わない文言: "
                         + " / ".join(f"[{w}]「{q}」" for w, q in missing))

    def test_the_corpus_runs_every_tool_the_doc_names(self):
        """docs/10 がコマンドで名指しする道具は、全部この山で走らせること (#133).

        山に無い道具の文言は、引用しても**当たらない**。それは落ちる向きなので
        安全だが、「当たらない = 文書が古い」と読み違える (#131 で一度読み違えた)。
        もっと悪いのは、山が薄いまま「21 個とも当たった」を裏付けだと思うこと。
        **docs/10 に新しい道具のコマンドを書いたら、山にも足す**を機械で縛る。
        """
        import re

        named = set(re.findall(r"tools/(\w+\.py)", self.doc))
        self.assertGreaterEqual(len(named), 3,
                                f"docs/10 から道具名を {len(named)} 個しか拾えない (拾い方が壊れた)")
        # **実際に呼んだ**名前で見る。ソースを読むと `if False:` で囲った呼び出しも数える
        self.assertEqual(named - self.ran_tools, set(),
                         f"docs/10 が名指しするのに山で走らせていない道具: "
                         f"{sorted(named - self.ran_tools)}")

    def test_the_success_paths_are_in_the_corpus_too(self):
        """うまくいった側の文言も山に入っていること.

        異常系だけ走らせた山は、正常系の引用を「無い」と言ってしまう。
        `exercises/README.md` が引用している 2 つで確かめる。
        """
        for phrase in ("全部一致しました", "足すのは 1 行ではなく"):
            self.assertIn(phrase, self.corpus,
                          f"うまくいった側の「{phrase}」が山に入っていない")

    def test_a_phrase_nobody_prints_is_caught(self):
        """この検査自体が効くこと。実在しない文言は見つからないと言うこと."""
        self.assertIsNone(self.where("そんなことは誰も言いません"),
                          "実在しない文言まで「ある」と言っている")
        self.assertEqual(self.where("問題なし"), "CLI", "実在する文言を見つけられない")


class TestTheHeaderIsNotText(unittest.TestCase):
    """見出しとポインタ表のバイトを「文字表に足せ」と言わないこと (#129).

    課題 3 は「表に無いバイト」の一覧を見て、その値を文字表に足していく課題。
    ところが一覧は**窓の中の全部**を数えていた。`work/MSG_ENC.BIN` の本文は
    0xAC から始まり、既定の窓 (先頭 256 バイト) はほとんどが見出しとポインタ表。
    そのため **完成した答えの表 `answers/custom.tbl` を渡しても**
    「9 種類 / 98 個 足りない」と出て、言われたとおりに足すと
    文字でない値が 8 つ表に混ざる。

    ここでは「答えの表なら本文の中で 0 個」と「外の分は足せと言わない」を見る。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/MSG_ENC.BIN", "make_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        cls.enc = os.path.join(REPO, "work", "MSG_ENC.BIN")
        cls.tbl = os.path.join(REPO, "answers", "custom.tbl")

    def run_dump(self, *args) -> str:
        import subprocess

        r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"),
                            self.enc, "--table", self.tbl, *args],
                           capture_output=True, text=True, cwd=REPO)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def test_the_answer_table_leaves_nothing_to_add(self):
        """答えの表なら、既定の窓でも「足すもの」は 0 件であること."""
        out = self.run_dump()
        self.assertIn("本文の中には 0 個", out, out[-500:])
        self.assertNotIn("に「16 進=文字」の行を足します", out,
                         "答えの表なのに、まだ足せと言っている")

    def test_the_header_bytes_are_named_but_not_counted_as_missing(self):
        """見出し側の分は、数だけ報せて「足すもの」には数えないこと."""
        out = self.run_dump()
        self.assertIn("そこは文字ではないので", out, out[-500:])
        # 本文の外にある値 (ポインタの 0x00 など) を「足す値」として挙げていないこと
        self.assertNotIn("  0x00  ", out, "ポインタ表の 0x00 を足す値として挙げている")

    def test_the_body_range_really_starts_after_the_pointer_table(self):
        """前提の確認: 本文は見出し + ポインタ表のあとから始まる.

        ここが崩れると上の 2 件は空振りする (既定の窓が本文だけになる)。
        """
        import hexdump

        bodies = hexdump.body_ranges(self.enc)
        self.assertTrue(bodies, "SCRP として本文の範囲が取れない")
        archive = scrp.read_archive(self.enc)
        head_end = 0x10 + archive.count * 4
        # 本文はポインタ表の**直後**から始まる (0x10 + 39×4 = 0xAC。課題 1 の 4 つ目の問い)
        self.assertGreaterEqual(min(a for a, _ in bodies), head_end,
                                "本文がポインタ表と重なっている (前提が崩れた)")
        self.assertLess(head_end, 256, "既定の窓に見出しが入らない (この検査が空振りする)")

    def test_a_missing_byte_inside_the_body_is_still_reported(self):
        """本文の中の不足は今までどおり出ること (消しすぎていないこと)."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(self.tbl, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            part = os.path.join(tmp, "part.tbl")
            with open(part, "w", encoding="utf-8") as fh:
                fh.write("\n".join(l for i, l in enumerate(lines) if i % 3))
            import subprocess
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"),
                                self.enc, "--table", part, "--message", "0"],
                               capture_output=True, text=True, cwd=REPO)
        out = r.stdout + r.stderr
        self.assertIn("表に無いバイトが", out, out[-400:])
        self.assertIn("行を足します", out, "本文の中の不足なのに足せと言わない")

    def test_the_answer_table_can_really_be_regenerated(self):
        """`answers/README.md` の「消しても復元できます」を確かめる (#129).

        消さずに、別の場所へ書き出して committed のものと突き合わせる。
        """
        import subprocess

        with open(os.path.join(REPO, "answers", "README.md"), encoding="utf-8") as fh:
            self.assertTrue("消しても復元できます" in fh.read(),
                            "README がこの約束をしていない (検査の前提)")
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "make_sample.py"),
                                "--out", os.path.join(tmp, "work"),
                                "--answers", os.path.join(tmp, "answers")],
                               capture_output=True, text=True, cwd=REPO)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            made = os.path.join(tmp, "answers", "custom.tbl")
            self.assertTrue(os.path.isfile(made), "make_sample.py が custom.tbl を書かない")
            with open(made, encoding="utf-8") as fh:
                fresh = fh.read()
        with open(self.tbl, encoding="utf-8") as fh:
            committed = fh.read()
        self.assertEqual(fresh, committed,
                         "作り直した custom.tbl が、置いてあるものと違う")

    def test_a_file_that_is_not_scrp_keeps_the_old_report(self):
        """SCRP でないファイルでは、範囲が分からないので今までどおり全部数える."""
        import subprocess

        problem = ensure_practice("work/FONT.BIN", "make_sample.py")
        if problem:
            self.skipTest(problem)
        r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"),
                            os.path.join(REPO, "work", "FONT.BIN"),
                            "--table", self.tbl, "--length", "64"],
                           capture_output=True, text=True, cwd=REPO)
        out = r.stdout + r.stderr
        self.assertIn("表に無いバイトが", out, out[-400:])
        self.assertNotIn("本文の中に", out, "SCRP でないのに本文の範囲を語っている")


class TestTheQaAnswerKeyIsTrue(unittest.TestCase):
    """課題 5・6 の答え (`answers/qa_answers.md`) を、道具に通して確かめる (#128).

    この答えは**書いてある文章**でしかなかった。検査を足したり題材をいじったりすれば
    黙って古くなる (#127 で検査を 1 つ足したばかり)。そこで答えの中身を読み取って、
    実際に `proofread.py` と `insert_text.py` にかけ、書いてある数と突き合わせる。

    実際、この形で 2 つ見つかった:

    * **修正例に id 24 が抜けていた。** そのとおりに直すと ERROR が 1 件残る。
      しかも id 24 は「数字は字面では気づけない」の**見本として挙げている当の行**。
    * **「容量にも収まります」が条件不足だった。** `ERROR` の行だけ直すと
      3 バイトはみ出す。余白は 2 バイト以下しかなく、浮かせる分は `WARN` の側にある。
    """

    @classmethod
    def setUpClass(cls):
        import proofread

        cls.pf = proofread
        cls.rows = scrp.read_tsv(os.path.join(REPO, "exercises", "qa_target.tsv"))
        cls.rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")
        cls.gloss = proofread.load_glossary(os.path.join(REPO, "data", "glossary.tsv"))
        cls.chars = proofread.load_font_chars(os.path.join(REPO, "data", "font_chars.txt"))
        with open(os.path.join(REPO, "answers", "qa_answers.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()

    def findings(self, rows):
        tw = self.pf.tag_widths_of(self.rules, 0.0)
        out = []
        for r in rows:
            out += self.pf.check_row(r, self.rules, self.gloss, self.chars, tw)
        return out + self.pf.check_consistency(rows, lambda rid: None)

    def fix_list(self) -> dict:
        """答えの「課題 6 の修正例」の中身を読み取る."""
        import re

        block = self.doc.split("```")[1]
        fix = {}
        for line in block.strip().splitlines():
            m = re.match(r"(\d+)\s+(.*)$", line.strip())
            if m:
                fix[m.group(1)] = m.group(2)
        return fix

    def applied(self, only_ids=None) -> list:
        fix = self.fix_list()
        rows = [dict(r) for r in self.rows]
        for r in rows:
            if r["id"] in fix and (only_ids is None or r["id"] in only_ids):
                r["translation"] = fix[r["id"]]
        return rows

    def bytes_of(self, rows) -> int | None:
        """入れ直したときの大きさ。容量に収まらなければ None."""
        import subprocess

        problem = ensure_practice("work/SCRIPT.BIN", "make_sample.py")
        if problem:
            self.skipTest(problem)
        with tempfile.TemporaryDirectory() as tmp:
            tsv, out = os.path.join(tmp, "f.tsv"), os.path.join(tmp, "S.BIN")
            scrp.write_tsv(tsv, rows)
            r = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "insert_text.py"), tsv,
                 "-o", out, "--original", os.path.join(REPO, "work", "SCRIPT.BIN")],
                capture_output=True, text=True, cwd=REPO)
            return os.path.getsize(out) if r.returncode == 0 else None

    def test_the_table_matches_what_the_tool_actually_reports(self):
        """答えの表の id と検出項目が、実際の出力と 1 対 1 で合うこと."""
        import collections
        import re

        actual = collections.defaultdict(collections.Counter)
        for f in self.findings(self.rows):
            actual[f.row_id][f.rule] += 1
        claimed = {}
        for line in self.doc.splitlines():
            m = re.match(r"\| (\d+) \| .*? \| (.+?) \| ", line)
            if not m:
                continue
            c = collections.Counter()
            for rm in re.finditer(r"`([a-z_]+)`(?: ×(\d+))?", m.group(2)):
                c[rm.group(1)] += int(rm.group(2) or 1)
            claimed[m.group(1)] = c
        self.assertEqual(dict(claimed), {k: dict(v) for k, v in actual.items()},
                         "答えの表と実際の指摘が食い違う")
        # 前書きの「20 行 / 27 件」も数え直す
        self.assertIn(f"{len(actual)} 行に不具合", self.doc, "行数の書き方が実際と違う")
        self.assertIn(f"指摘は {len(self.findings(self.rows))} 件", self.doc,
                      "指摘の件数が実際と違う")

    def test_following_the_fix_list_really_clears_every_error(self):
        """修正例のとおりに直すと ERROR が 0 になること (id 24 の抜けで 1 件残っていた)."""
        errs = [f for f in self.findings(self.applied()) if f.severity == "ERROR"]
        self.assertEqual(errs, [], f"修正例に従っても ERROR が残る: "
                                   f"{[(f.row_id, f.rule) for f in errs]}")

    def test_fixing_only_the_errors_does_not_fit_and_the_docs_say_so(self):
        """`ERROR` の行だけ直すと容量に入らないこと。答えがその数を書いていること."""
        err_ids = {f.row_id for f in self.findings(self.rows) if f.severity == "ERROR"}
        rows = self.applied(only_ids=err_ids)
        self.assertEqual([f for f in self.findings(rows) if f.severity == "ERROR"], [],
                         "ERROR の行を直したのに ERROR が残る (前提が崩れた)")
        self.assertIsNone(self.bytes_of(rows),
                          "ERROR の行だけで容量に収まってしまう (答えの説明のほうが古い)")
        self.assertTrue("3 バイト超過" in self.doc, "はみ出す量が答えに書いていない")

    def test_the_byte_counts_in_the_answer_are_measured(self):
        """答えが挙げる 2,118 / 2,120 を測り直す."""
        size = self.bytes_of(self.applied())
        self.assertIsNotNone(size, "修正例のとおりに直しても容量に入らない")
        self.assertIn(f"{size:,} バイト", self.doc, f"修正例の大きさは {size:,} バイト")

        rows = self.applied()
        for r in rows:                       # WARN も残らず直した状態
            if r["id"] == "38":
                r["translation"] = "いいえ"
        both = self.bytes_of(rows)
        self.assertEqual([f for f in self.findings(rows) if f.severity != "ERROR"], [],
                         "全部直したのに WARN が残る (前提が崩れた)")
        self.assertIsNotNone(both, "全部直しても容量に入らない")
        self.assertIn(f"{both:,} バイト", self.doc, f"全部直すと {both:,} バイト")


class TestTheLessonDocsRunTopToBottom(unittest.TestCase):
    """docs/01・02・03・06・08 のコマンドを、**書いてある順に本当に打って**通ること (#143).

    今まで見張っていたのは 3 つ:
    README の「3 分で一周する」(実際に走らせる)、docs/10 と課題 8 の僕夏 2 の部分
    (実際に走らせる)、そして全文書の**書き方**の検査 (#80・#81。穴埋めが残っていないか、
    使うファイルの作り方が前に書いてあるか)。

    抜けていたのが**この 5 つの本文のコマンドを実際に走らせること**。
    書き方の検査は「入力を作る行が前にあるか」しか見ないので、
    **前の行が後の行の題材を書き換えてしまう**型は素通りする。実際そうなっていた:
    docs/03 の「同じ場所を指すポインタ」の実演が `make_sample.py --pool-duplicates` で
    `work/SCRIPT.BIN` を共有ありの形に置き換え、その下の
    「入れ直すと元とバイト単位で一致する」が**容量オーバーで落ちていた**。
    """

    DOCS = ("01-文字テーブル.md", "02-相対検索.md", "03-ポインタテーブル.md",
            "06-画面で確かめる.md", "08-コードを読む.md")

    #: 走らせない行 (外の道具を入れる、検査そのもの、比較のための shell)
    SKIP = ("pip ", "node ", "python3 tests/")

    @staticmethod
    def commands(path: str) -> list:
        """(コマンド, 空振りしてよいか) を、書いてある順に返す.

        「空振りしてよい」は**文書がそう書いているとき**だけ認める。
        相対検索は仮定を変えて何度も回す道具なので、`docs/02` には
        **わざと当たらない**例がある。文書が「ヒット 0 件」と断っていれば、
        終了コード 1 は書いてあるとおりの結果。断りが消えたら落ちる。
        """
        import re

        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        out = []
        # 後ろは**先読み**で見る。取り込むと次のブロックを飲んでしまう
        # 最初のブロックより前の**地の文**に書いてある下ごしらえも拾う。
        # docs/02 は「先に `python3 tools/make_sample.py`」を引用の形で書いていて、
        # ```bash の中だけ見ていると前提を落とす (最初そう書いて、この文書だけ落ちた)
        for pre in re.findall(r"`(python3 tools/[\w./ -]+)`", text.split("```", 1)[0]):
            out.append((pre.strip(), False))
        for m in re.finditer(r"```bash\n(.*?)```(?=(.{0,400}))", text, re.S):
            block, after = m.group(1), m.group(2)
            # **文書が結果を見せている**ときだけ、終了コード 1 を認める。
            # 「ヒット 0 件」と断っている行 (docs/02 の外れる仮定) と、
            # すぐ下にエラーを引用している行 (docs/03 の容量オーバーの実演)。
            shown = after.split("```")[1] if after.count("```") >= 2 else ""
            may_miss = ("ヒット 0 件" in after.split("```")[0]
                        or shown.lstrip().startswith("エラー:"))
            cur = ""
            for line in block.splitlines():
                line = line.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                cur += line.rstrip("\\") + (" " if line.endswith("\\") else "")
                if not line.endswith("\\"):
                    out.append((cur.strip(), may_miss))
                    cur = ""
        return out

    def test_each_doc_runs_in_order_in_a_clean_tree(self):
        """1 文書ずつ、取得したままの木で頭から流す.

        文書ごとに木を作り直す。**前の文書の後片付けに頼らない**ためで、
        読む人が 1 つの文書だけ開いても同じことが起きる。
        """
        import shlex
        import shutil
        import subprocess

        ran = 0
        for name in self.DOCS:
            path = os.path.join(REPO, "docs", name)
            cmds = [(c, miss) for c, miss in self.commands(path)
                    if not any(c.startswith(s) for s in self.SKIP)]
            self.assertGreaterEqual(len(cmds), 3,
                                    f"{name} からコマンドを {len(cmds)} 本しか拾えない")
            with tempfile.TemporaryDirectory() as tmp:
                tree = os.path.join(tmp, "t")
                shutil.copytree(REPO, tree, ignore=shutil.ignore_patterns(
                    "work", ".git", "__pycache__", "*.pyc"))
                for cmd, may_miss in cmds:
                    if not cmd.startswith(("python3 ", "cp ", "cmp ")):
                        continue
                    shell = cmd.replace("python3 ", shlex.quote(sys.executable) + " ", 1)
                    res = subprocess.run(shell, shell=True, capture_output=True,
                                         text=True, cwd=tree)
                    if may_miss and res.returncode == 1:
                        ran += 1                        # 文書が「0 件」と断っている行
                        continue
                    self.assertEqual(res.returncode, 0,
                                     f"docs/{name} を上から打つと止まる:\n  {cmd}\n"
                                     f"{(res.stdout + res.stderr)[-500:]}")
                    ran += 1
        self.assertGreaterEqual(ran, 25, f"走らせたコマンドが {ran} 本しかない")

    def test_the_capacity_error_docs_quote_is_the_real_one(self):
        """docs/03 が引用している容量エラーの数字を、その場で測り直す (#143).

        引用は「2,128 バイトで、上限 2,120 バイトを 8 バイト超えています」だったが、
        実際は 2,124 / 4 バイト。題材が変われば動く数なので、走らせて突き合わせる。
        """
        import shutil
        import subprocess

        with open(os.path.join(REPO, "docs", "03-ポインタテーブル.md"), encoding="utf-8") as fh:
            doc = fh.read()
        with tempfile.TemporaryDirectory() as tmp:
            tree = os.path.join(tmp, "t")
            shutil.copytree(REPO, tree, ignore=shutil.ignore_patterns(
                "work", ".git", "__pycache__", "*.pyc"))
            subprocess.run([sys.executable, "tools/make_sample.py"],
                           capture_output=True, cwd=tree, check=True)
            res = subprocess.run(
                [sys.executable, "tools/insert_text.py", "exercises/qa_target.tsv",
                 "-o", "work/BAD.BIN", "--original", "work/SCRIPT.BIN"],
                capture_output=True, text=True, cwd=tree)
        out = res.stdout + res.stderr
        line = next((l.strip() for l in out.splitlines() if l.startswith("エラー:")), "")
        self.assertTrue(line, f"容量エラーが出ない (前提が崩れた):\n{out[-300:]}")
        self.assertTrue(line in doc,
                        f"docs/03 の引用が今と違う。この行に書き換える → 「{line}」")


class TestExerciseNineSendsYouToTheRightTable(unittest.TestCase):
    """課題 9 が引けと言う表に、出る `→` の行が本当に載っていること (#142).

    課題 9 は 5 通りに壊して `→` の行を読む課題で、手順 2 が
    「docs/10 の**『困ったとき』**の表で対応する症状を探す」と言っていた。
    ところが `→` の行は**1 本もそこに無い**。載っているのは、その少し下の
    **「診断の `→` の行の読み方」** のほう。「困ったとき」は
    `→` **以外**で道具が言うこと (「候補なし」「1 行もありません」など) を引く表。

    課題のとおりにすると、19 行の表を端から見て自分の行が見つからず、
    「documented されていない」か「壊し方を間違えた」と思うことになる。
    5 通りとも実際に走らせて、名指しの表に載っていることを確かめる。
    """

    BREAKS = ("idx", "name", "msg", "font", "map")

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        cls.trouble = doc.split("## 困ったとき")[1].split("### 診断の")[0]
        cls.arrows = doc.split("### 診断の `→` の行の読み方")[1]
        with open(os.path.join(REPO, "exercises", "README.md"), encoding="utf-8") as fh:
            cls.ex = fh.read().split("## 課題 9")[1]

    def arrows_for(self, how: str) -> tuple[list, str]:
        """1 通り壊して `check` を走らせ、(→ の行, 結果の行) を返す."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, how)
            made = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "make_boku2_sample.py"),
                 "--break", how, "--out", folder], capture_output=True, text=True, cwd=REPO)
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "boku2.py"), "check", folder],
                capture_output=True, text=True, cwd=REPO)
        lines = [l for l in res.stdout.splitlines() if l.startswith("→")]
        verdict = next((l for l in res.stdout.splitlines() if l.startswith("== 結果")), "")
        self.assertEqual(res.returncode, 1, f"{how}: 終了コードが 1 でない\n{res.stdout[-300:]}")
        return lines, verdict

    @staticmethod
    def looks_up(line: str, table: str) -> bool:
        """`→` の行が表に載っているか.

        表の側は具体名を `…` で省いて書く (「[フォント] … は TIM2 として読めません」)。
        **表の見出しを型にして**行に当てる。行の側を切り刻むと、
        省略された所で当たらない (最初そう書いて font の行だけ外した)。
        """
        body = line[2:].strip()
        for row in table.splitlines():
            if not row.startswith("|"):
                continue
            head = row.strip().strip("|").split("|")[0].strip()
            if len(head) < 8 or set(head) <= set("-: "):
                continue
            pattern = ".*".join(re.escape(p) for p in head.split("…") if p.strip())
            if pattern and re.search(pattern, body):
                return True
        return False

    def test_the_exercise_names_the_table_that_has_the_lines(self):
        """課題が名指しする表の名前が、`→` の行を載せているほうであること."""
        self.assertIn("診断の `→` の行の読み方", self.ex,
                      "課題 9 が `→` の表を名指ししていない")

    def test_every_arrow_line_is_in_that_table_and_not_the_other(self):
        found = 0
        for how in self.BREAKS:
            lines, verdict = self.arrows_for(how)
            self.assertTrue(lines, f"{how}: → の行が 1 本も出ない (課題の確認が崩れる)")
            self.assertIn("確認事項", verdict, f"{how}: 結果が確認事項になっていない ({verdict})")
            for line in lines:
                self.assertTrue(self.looks_up(line, self.arrows),
                                f"{how}: 「診断の → の行の読み方」に無い行: {line[:60]}")
                self.assertFalse(self.looks_up(line, self.trouble),
                                 f"{how}: 「困ったとき」にも載っている (表の役割が混ざった): {line[:60]}")
                found += 1
        self.assertGreaterEqual(found, 5, f"確かめた → の行が {found} 本しかない")

    def test_the_index_break_stops_the_diagnosis_as_the_exercise_says(self):
        """課題が言う「idx だけはそこで止まる」が本当であること."""
        _, verdict = self.arrows_for("idx")
        self.assertIn("ここで止めました", verdict, f"止まると言っていない: {verdict}")
        self.assertIn("そこで診断が止まる", self.ex, "課題が「止まる」と書いていない")


class TestEveryRuleInTheTableDoesWhatItSays(unittest.TestCase):
    """docs/04 の検査一覧が、実際の動きと合っていること (#139).

    `untranslated` の欄には「**訳文が原文と同じ (未訳の疑い)**」と書いてあった。
    ところが実際に出るのは「**訳文の欄が空**」のときだけで、
    原文と同じ行では**何も出ない**。読んだ人は、取り出したままの 19 行に
    WARN が並ぶと思って待つが、いつまでも来ない。

    行ごとに出さないのは正しい判断 (取り出し直後は全行がそうなので、
    同じ WARN が何十件も並んで本当の指摘が埋もれる)。まとめて注意書き 1 つに
    してある。**間違っていたのは文書のほう**なので文書を直した。
    """

    @classmethod
    def setUpClass(cls):
        import proofread

        cls.pf = proofread
        cls.rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")
        with open(os.path.join(REPO, "docs", "04-校正とQA.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()

    def fired(self, original: str, translation: str) -> set:
        row = {"id": "0", "original": original, "translation": translation}
        found = self.pf.check_row(row, self.rules, [], None,
                                  self.pf.tag_widths_of(self.rules, 0.0))
        return {f.rule for f in found}

    def test_untranslated_fires_on_an_empty_cell_not_on_a_copy(self):
        self.assertIn("untranslated", self.fired("こんにちは。", ""),
                      "訳文が空なのに untranslated が出ない")
        self.assertNotIn("untranslated", self.fired("こんにちは。", "こんにちは。"),
                         "原文と同じだけで untranslated を出している (文書の言い分が正しくなる)")
        self.assertNotIn("untranslated", self.fired("こんにちは。", "やあ。"),
                         "訳してあるのに untranslated が出ている")

    def test_the_table_describes_the_real_condition(self):
        """一覧の `untranslated` の欄が、空欄のことだと分かる書き方であること."""
        import re

        rows = [ln for ln in self.doc.splitlines()
                if ln.startswith("| `untranslated` |") and "手作業に残る部分" in ln]
        self.assertEqual(len(rows), 1, f"docs/04 の一覧に untranslated の行が {len(rows)} 本")
        self.assertIn("空", rows[0], f"空欄のことだと書いていない: {rows[0]}")
        self.assertNotIn("原文と同じ", rows[0],
                         f"出ない条件を書いている: {rows[0]}")

    def test_the_doc_says_where_the_copy_case_is_reported_instead(self):
        """「原文と同じ」がどこに出るのかを書いてあること (読者の期待を宙に浮かせない)."""
        self.assertTrue("行ごとの指摘にしていません" in self.doc,
                        "「原文と同じ」を行ごとに出さない旨が書いていない")

    #: docs/04 の一覧が「これを見ている」と言っている条件を、1 つずつ作ったもの。
    #: **書いてある条件で、書いてある検査が出る**ことを見る (#140)
    PROBES = {
        "placeholder": ("<VAR:00>さん、こんにちは。", "さん、こんにちは。"),
        "control": ("はい。<WAIT>", "はい。"),
        "line_width": ("みじかい。", "あ" * 25 + "。"),
        "line_count": ("みじかい。", "あ<BR>い<BR>う<BR>え"),
        "kinsoku": ("あいう<BR>えお。", "あいう<BR>。えお"),
        "halfwidth": ("３００ギル", "300ギル"),
        "font": ("とびら。", "薔薇。"),
        "glossary": ("薬草をつかう。", "やくそうをつかう。"),
        "notation": ("……そっか。", "また来てね〜"),      # 〜 は U+301C (禁止)

        "empty": ("セーブしますか？<WAIT>", "<WAIT>"),
        "untranslated": ("セーブしますか？", ""),
        "number": ("８５０ギル", "８５ギル"),
    }

    def test_each_rule_fires_on_the_condition_the_table_describes(self):
        """一覧の「何を見ているか」どおりの入力で、その検査が出ること (#140).

        #139 で入れたのは「13 個がどれも出せること」までで、
        **書いてある条件で出るか**は見ていなかった。実際、`empty` の欄には
        「訳文が空」と書いてあったが、空欄で出るのは `untranslated` のほう。
        `empty` は**タグしか残っていない**ときに出る。
        """
        gloss = self.pf.load_glossary(os.path.join(REPO, "data", "glossary.tsv"))
        chars = self.pf.load_font_chars(os.path.join(REPO, "data", "font_chars.txt"))
        tw = self.pf.tag_widths_of(self.rules, 0.0)
        for rule, (original, translation) in self.PROBES.items():
            row = {"id": "0", "original": original, "translation": translation}
            got = {f.rule for f in self.pf.check_row(row, self.rules, gloss, chars, tw)}
            self.assertIn(rule, got,
                          f"一覧が言う条件で {rule} が出ない (出たのは {sorted(got)})")

    def test_the_line_break_tag_is_not_compared_and_the_table_says_so(self):
        """`<BR>` は照合しないこと。一覧がそう書いてあること (#140).

        一覧の `control` の欄は「`<BR>` `<WAIT:xx>` などの制御タグが原文と
        一致するか」と、**`<BR>` を先頭に挙げていた**。実際は `BR` / `CLEAR` / `LT`
        は照合から外してある (行の折り返しは校正者の仕事で、結果は
        `line_width` / `line_count` が見る)。読んだ人は `<BR>` を消したら
        出ると思うが、出ない。
        """
        gloss = self.pf.load_glossary(os.path.join(REPO, "data", "glossary.tsv"))
        tw = self.pf.tag_widths_of(self.rules, 0.0)

        def fired(original, translation):
            row = {"id": "0", "original": original, "translation": translation}
            return {f.rule for f in self.pf.check_row(row, self.rules, gloss, None, tw)}

        self.assertNotIn("control", fired("あいう<BR>えお。", "あいうえお。"),
                         "<BR> を消したのに control が出る (一覧の元の書き方が正しくなる)")
        self.assertNotIn("control", fired("あいうえお。", "あい<BR>うえお。"),
                         "<BR> を増やしたのに control が出る")
        # 見ている側は出ること (照合そのものが死んでいない)
        self.assertIn("control", fired("はい。<WAIT>", "はい。"),
                      "<WAIT> を消しても control が出ない (照合が働いていない)")
        # 同じ名前の行が要約の表にもある。**一覧のほう** (5 列) だけを見る (#139 で踏んだ)
        rows = [ln for ln in self.doc.splitlines()
                if ln.startswith("| `control` | WARN |") and "変数と文字数" in ln]
        self.assertEqual(len(rows), 1, f"docs/04 の一覧に control の行が {len(rows)} 本")
        self.assertIn("は見ません", rows[0], f"見ないタグがあると書いていない: {rows[0]}")

    def test_the_empty_row_describes_tags_only(self):
        """`empty` の欄が「タグしか残っていない」と書いてあること."""
        rows = [ln for ln in self.doc.splitlines()
                if ln.startswith("| `empty` | ERROR |") and "手作業に残る部分" in ln]
        self.assertEqual(len(rows), 1, f"docs/04 の一覧に empty の行が {len(rows)} 本")
        self.assertIn("タグしか残っていない", rows[0], f"条件が違う: {rows[0]}")

    def rule_tables(self) -> tuple[dict, dict]:
        """docs/04 の 2 つの表を {検査名: 行} で返す (要約は 3 列、一覧は 5 列)."""
        import re

        summary, detail = {}, {}
        for line in self.doc.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            m = re.match(r"`([a-z_]+)`$", cells[0])
            if not m or m.group(1) == "rule":      # 一覧の見出し行
                continue
            if len(cells) == 3:
                summary[m.group(1)] = cells
            elif len(cells) == 5:
                detail[m.group(1)] = cells
        return summary, detail

    def test_the_two_tables_list_the_same_rules_with_the_same_weight(self):
        """要約の表と一覧の表が、同じ 14 個を同じ重さで挙げていること (#141).

        同じ検査が 2 つの表に出てくるので、片方だけ直すと食い違う。
        実際 `control` は一覧だけ `<BR>` を見ると書いてあり (#140)、
        `halfwidth` は要約だけ半角カナに触れていた。
        """
        summary, detail = self.rule_tables()
        self.assertEqual(sorted(summary), sorted(detail),
                         f"2 つの表で挙げている検査が違う "
                         f"(要約だけ: {sorted(set(summary) - set(detail))} / "
                         f"一覧だけ: {sorted(set(detail) - set(summary))})")
        self.assertEqual(len(detail), 14, f"一覧が {len(detail)} 個 (書き方が変わった)")
        for name in sorted(detail):
            s, d = summary[name][1], detail[name][1]
            self.assertEqual(s.split(" (")[0].strip(), d.split(" (")[0].strip(),
                             f"{name} の重さが違う: 要約「{s}」/ 一覧「{d}」")

    def test_half_width_kana_is_caught_and_both_tables_say_so(self):
        """半角カナも `halfwidth` で出ること。2 つの表とも触れていること (#141).

        一覧は「半角の英数記号」としか書いていなかったが、道具は
        **半角カナを別のメッセージで**拾う。しかも半角カナは課題 5 の
        仕込み (id 16) そのもので、教材の中で一番出てくる型。
        """
        tw = self.pf.tag_widths_of(self.rules, 0.0)

        def messages(translation):
            row = {"id": "0", "original": "あ", "translation": translation}
            return [f.message for f in self.pf.check_row(row, self.rules, [], None, tw)
                    if f.rule == "halfwidth"]

        kana, alnum = messages("ﾀﾁは回復した"), messages("300ギル")
        self.assertTrue(kana, "半角カナで halfwidth が出ない")
        self.assertTrue(alnum, "半角英数で halfwidth が出ない")
        self.assertNotEqual(kana, alnum, "半角カナと半角英数のメッセージが同じ")
        summary, detail = self.rule_tables()
        for label, cells in (("要約", summary["halfwidth"]), ("一覧", detail["halfwidth"])):
            self.assertIn("半角カナ", cells[2], f"{label}の表が半角カナに触れていない: {cells[2]}")

    def test_every_rule_in_the_table_can_actually_fire(self):
        """一覧に載っている 14 個が、どれも出せること (出ない検査を載せない).

        `untranslated` は題材では一度も出ないので、表にあって実際は
        死んでいる、ということが起こりうる。1 つずつ出させて確かめる。
        """
        import re

        table = self.doc.split("| `rule` | 重さ |")[1].split("\n\n")[0]
        listed = re.findall(r"^\| `([a-z_]+)` \|", table, re.M)
        self.assertEqual(len(listed), 14, f"一覧が {len(listed)} 個 (書き方が変わった)")
        rows = scrp.read_tsv(os.path.join(REPO, "exercises", "qa_target.tsv"))
        gloss = self.pf.load_glossary(os.path.join(REPO, "data", "glossary.tsv"))
        chars = self.pf.load_font_chars(os.path.join(REPO, "data", "font_chars.txt"))
        tw = self.pf.tag_widths_of(self.rules, 0.0)
        seen = set()
        for row in rows:
            seen |= {f.rule for f in self.pf.check_row(row, self.rules, gloss, chars, tw)}
        seen |= {f.rule for f in self.pf.check_consistency(rows, lambda rid: None)}
        # 題材で出ないものは、その場で条件を作って出させる
        seen |= self.fired("こんにちは。", "")                      # untranslated
        # room は `size` の欄がある行でしか働かない (題材の TSV には無い)
        seen |= {f.rule for f in self.pf.check_row(
            {"id": "0", "size": "12", "original": "はじめから",
             "translation": "ゲームをさいしょからはじめる"},
            self.rules, [], None, self.pf.tag_widths_of(self.rules, 0.0))}
        missing = [r for r in listed if r not in seen]
        self.assertEqual(missing, [], f"一覧にあるのに出せない検査: {missing}")


class TestKanjiHidingInKatakana(unittest.TestCase):
    """カタカナに紛れた形の似た漢字を拾う検査 (#127).

    長音記号 `ー` (U+30FC) と漢数字の `一` (U+4E00) は、小さいビットマップ
    フォントでは見分けがつかない。**英語化パッチの公開ソースに、取り出した
    日本語がそのまま残っていて**、買い物メニューが「コ一ヒ一牛乳」、
    虫相撲が「グレ一ト」「ハリケ一ン」になっている (`MENU_TEXT_EXCEPTIONS` /
    `BUGGED_LINES`)。製品側の誤字か、文字表を作るときの取り違えかは、
    手元のデータでは決められない。どちらにせよ目では気づけないので機械に見張らせる。

    検査の値打ちは**取りこぼさないこと**と**正しい日本語で鳴らないこと**の両方で
    決まる。片方だけ見ても意味がないので、両方を数える。
    """

    #: 公開ソースの MENU_TEXT_EXCEPTIONS / BUGGED_LINES にそのまま出てくる形
    BAD = ["グレ一ト", "ハリケ一ン", "コ一ヒ一牛乳", "ジェットサイダ一",
           "チュ一チュ一アイス", "ベ一スボ一ルバ一", "ボ一ルガム", "クリ一ム",
           "ベビ一スタ一ラ一メン", "北極バ一", "虫交換ノ一ト"]
    #: 鳴ってはいけない、正しい日本語
    GOOD = ["コーヒーを一杯", "メートル一本", "ビール一杯だけ", "テスト二回目",
            "カード一枚", "ジェットサイダー", "グレート", "一日中あそんだ",
            "力をこめる", "口をひらく", "セリカ。", "ジュースを一本<BR>買った",
            "アイス、一つ", "ゲーム", "スタート"]

    @classmethod
    def setUpClass(cls):
        import proofread

        cls.pf = proofread
        cls.rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")

    def fires(self, text: str) -> bool:
        row = {"id": "0", "original": text, "translation": text}
        found = self.pf.check_row(row, self.rules, [], None,
                                  self.pf.tag_widths_of(self.rules, 0.0))
        return any(f.rule == "notation" and "似た漢字" in f.message for f in found)

    def test_it_catches_every_real_example_from_the_public_source(self):
        missed = [t for t in self.BAD if not self.fires(t)]
        self.assertEqual(missed, [], f"公開ソースに実在する形を拾えない: {missed}")

    def test_it_stays_quiet_on_correct_japanese(self):
        noisy = [t for t in self.GOOD if self.fires(t)]
        self.assertEqual(noisy, [], f"正しい日本語で鳴っている: {noisy}")

    def test_it_is_quiet_on_everything_already_in_the_repo(self):
        """手持ちの本文と教材で 1 件も鳴らないこと (鳴ったら誤検出かこちらの誤字)."""
        import glob

        hits = []
        for path in ("exercises/qa_target.tsv", "work/BOKU2SAMPLE/answer.tsv"):
            full = os.path.join(REPO, path)
            if not os.path.isfile(full):
                continue
            for row in scrp.read_tsv(full):
                for col in ("original", "translation"):
                    if row.get(col) and self.fires(row[col]):
                        hits.append(f"{path} id={row.get('id')} {col}")
        for path in glob.glob(os.path.join(REPO, "docs", "*.md")):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            # docs/04 と docs/10 は**わざと**実例を載せているので除く
            if os.path.basename(path).startswith(("04-", "09-", "10-")):
                continue
            for line in text.splitlines():
                if self.fires(line):
                    hits.append(f"{os.path.basename(path)}: {line[:40]}")
        self.assertEqual(hits, [], f"鳴ってはいけない所で鳴った: {hits[:5]}")

    def test_the_examples_really_are_in_the_public_source(self):
        """BAD の 11 個が本当に公開ソースに書いてあること (#127).

        「実在する形で測った」が検査の値打ちの土台なので、そこを書き写しで
        済ませない。公開ソースが無い環境では飛ばす (中身は取り込まない)。
        """
        msg_py = os.path.join(PUBLIC_SRC, "MSG.py")
        if not os.path.isfile(msg_py):
            self.skipTest(f"公開ソースが無い ({PUBLIC_SRC})")
        with open(msg_py, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        missing = [t for t in self.BAD if t not in src]
        self.assertEqual(missing, [], f"公開ソースに無い形を実例として挙げている: {missing}")

    def test_the_docs_say_where_the_example_came_from(self):
        """実例の出どころを書いてあること。作り話にしない."""
        with open(os.path.join(REPO, "docs", "04-校正とQA.md"), encoding="utf-8") as fh:
            doc = fh.read()
        for phrase in ("MENU_TEXT_EXCEPTIONS", "コ一ヒ一牛乳", "どちらとも決められません"):
            self.assertTrue(phrase in doc, f"docs/04 に「{phrase}」が無い")


class TestNothingIsNotAPass(unittest.TestCase):
    """「0 件だから合格」を言わせない (#123).

    課題 6 と docs/10 の「やり切ったかどうか」は、どちらも
    **`proofread.py` が ERROR 0 件**・**往復の突き合わせが一致**を合格条件にしている。
    ところがどちらも、**何も検査していないとき**にその条件を満たしてしまった:

    * 見出しだけの TSV → 「0 行をチェック: ERROR 0 件」で終了コード 0
    * 両方が空の突き合わせ → 「全部一致しました」で終了コード 0
    * **0 字の文字表を渡すと、フォント検査が黙って止まる**。実機で □ になる字を
      当てる検査で、実物では文字表を作りかけの段階で渡すことになるので、
      いちばん踏みやすい。他の検査は普通に動くので出力も普通に見える
    * `boku2.py used` が 0 種でも「この番号だけ書き出せば読める」と言う

    `boku2.py text` は同じ形 (0 行) を「文言が 1 行も見つかりませんでした」+
    終了コード 1 で断っていた。そちらに揃えた。ここでは 4 つとも、
    **終了コードが 0 でないこと**と、合格の言葉を出さないことを見る。
    """

    def cli(self, tool, *args):
        import subprocess

        res = subprocess.run([sys.executable, os.path.join(REPO, "tools", tool), *args],
                             capture_output=True, text=True, cwd=REPO)
        return res.returncode, res.stdout + res.stderr

    def header_only(self, tmp, name="empty.tsv"):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("id\toriginal\ttranslation\n")
        return path

    def test_proofread_refuses_a_tsv_with_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self.cli("proofread.py", self.header_only(tmp))
        self.assertEqual(rc, 1, f"0 行の TSV が合格になった:\n{out}")
        self.assertNotIn("ERROR 0 件", out, "0 行なのに「ERROR 0 件」と言っている")
        self.assertIn("1 行もありません", out, out)

    def test_compare_refuses_when_nothing_was_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.header_only(tmp, "a.tsv")
            b = self.header_only(tmp, "b.tsv")
            rc, out = self.cli("compare_tsv.py", a, b)
        self.assertEqual(rc, 1, f"0 行同士の突き合わせが合格になった:\n{out}")
        self.assertNotIn("全部一致しました", out, "何も比べていないのに「全部一致」")

    def test_compare_refuses_when_ignore_removed_everything(self):
        """--ignore で全部除いたときも、比べた行は 0 になる."""
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"id": "0", "original": "はい", "translation": "はい"}]
            a, b = os.path.join(tmp, "a.tsv"), os.path.join(tmp, "b.tsv")
            scrp.write_tsv(a, rows)
            scrp.write_tsv(b, rows)
            rc, out = self.cli("compare_tsv.py", a, b, "--ignore", "はい")
        self.assertEqual(rc, 1, f"全部除いたのに合格になった:\n{out}")
        self.assertNotIn("全部一致しました", out, "全部除いたのに「全部一致」")

    def test_an_empty_font_table_does_not_silently_turn_the_check_off(self):
        """0 字の文字表で、フォント検査が黙って止まらないこと.

        止まっていることに気づけるかどうかが要点なので、
        **同じ TSV を空でない文字表にかけると指摘が出る**ことも一緒に見る。
        そうでないと「元から指摘が無い題材」を見て通ってしまう。
        """
        bad = "扉には薔薇の紋章が刻まれている。<WAIT>"          # 薔薇紋章刻 が data/font_chars.txt に無い
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.tsv")
            scrp.write_tsv(path, [{"id": "0", "original": bad, "translation": bad}])
            blank = os.path.join(tmp, "blank.txt")
            open(blank, "w", encoding="utf-8").close()

            rc_ok, out_ok = self.cli("proofread.py", path)                       # 既定の文字表
            self.assertIn("ERROR font", out_ok,
                          "題材にフォント外の字が無い。この検査が空振りしている")

            rc, out = self.cli("proofread.py", path, "--font-chars", blank)
            self.assertEqual(rc, 1, f"0 字の文字表が合格になった:\n{out}")
            self.assertNotIn("ERROR 0 件", out, "フォント検査が止まったまま「ERROR 0 件」")
            self.assertIn("0 字", out, out)

            # 外したいときの道 (--no-font-check) は残っていること
            rc_off, out_off = self.cli("proofread.py", path, "--font-chars", blank,
                                       "--no-font-check")
            self.assertEqual(rc_off, 0, f"--no-font-check で通らない:\n{out_off}")

    def test_used_refuses_when_it_found_no_glyph_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self.cli("boku2.py", "used", tmp)
        self.assertEqual(rc, 1, f"0 種なのに合格になった:\n{out}")
        self.assertNotIn("この番号だけ書き出せば", out,
                         "書き出す番号が無いのに「この番号だけ書き出せば読める」")

    def test_a_range_that_never_uses_the_glyph_table_is_not_called_readable(self):
        """文字番号を 1 つも使わない範囲を「文字表で全部読めました」と言わないこと (#124).

        保存画面の一部は Shift-JIS で、文字表を引かない。そこだけを `text` に渡すと
        「使われている番号 0 種」で**全部読めた**ことになっていた。
        docs/10 の 55 分の行は、この文言を文字表が仕上がった印として見ろと言っている。
        """
        sjis = "\0".join(["セーブしますか？", "はい", "いいえ"]).encode("cp932") + b"\0"
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "x.msg"), "wb") as fh:
                fh.write(sjis)
            font = os.path.join(tmp, "font.txt")
            with open(font, "w", encoding="utf-8") as fh:
                fh.write("あいうえお\n")
            rc, out = self.cli("boku2.py", "text", tmp, "-f", font,
                               "-o", os.path.join(tmp, "o.tsv"))
            with open(os.path.join(tmp, "o.tsv"), encoding="utf-8-sig") as fh:
                got = fh.read()
        self.assertEqual(rc, 0, out)
        self.assertTrue("セーブしますか？" in got,
                        "Shift-JIS の行が取り出せていない (前提が崩れた)")
        self.assertNotIn("文字表で全部読めました", out,
                         "文字表を一度も引いていないのに「全部読めました」と言っている")
        self.assertIn("試せていません", out, out)
        # docs/10 の 55 分の行と「困ったとき」が、この 3 つ目の結果を知っていること
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertTrue("文字表は試せていません" in doc,
                        "docs/10 が 3 つ目の結果 (試せていません) を書いていない")

    def test_the_browser_says_the_same_thing_for_a_table_it_never_used(self):
        """画面の言い分けが CLI と同じ 4 通りで、0 種を ok と言わないこと.

        判定そのものは web/app.js の `bokuGlyphVerdict` を **node で動かして**
        確かめる (tests/test_bokumsg.mjs)。ここでは画面と CLI が同じ言葉を
        持っていることだけを見る。
        """
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertTrue("function bokuGlyphVerdict(" in js,
                        "画面側の言い分けが関数になっていない (書き写した検査になる)")
        self.assertTrue("文字表は試せていない" in js,
                        "画面に「試せていない」の文言が無い")
        with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
            py = fh.read()
        self.assertTrue("文字表は試せていない" in py and "文字表は試せていません" in py,
                        "CLI 側 (check / text) のどちらかに文言が無い")

    def test_maps_refuses_when_it_split_nothing(self):
        """入れ物が 0 個なら「0 個の入れ物から 0 個の部品」で終わらないこと (#125).

        docs/10 の 25 分の行は「0 個でないこと」を**人に見張らせていた**。
        道具が言えることを人の目に任せない。
        """
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self.cli("boku2.py", "maps", tmp, "-o", os.path.join(tmp, "o"))
        self.assertEqual(rc, 1, f"0 個なのに合格になった:\n{out}")
        self.assertIn("入れ物が 1 つも見つかりませんでした", out, out)
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertTrue("入れ物が 1 つも見つかりませんでした" in doc,
                        "docs/10 の「困ったとき」にこの断り文句が無い")

    def test_the_folder_rule_agreement_says_how_much_it_compared(self):
        """「2 通りで一致」に分母が付くこと。入れ子が無ければ「試せていない」と言う (#125).

        フォルダの規則は docs/09 の表でまだ「確かめていない」側にあり、
        docs/10 はこの行を報告の決め手に挙げている。入れ子の無い索引では
        2 通りは必ず同じ答えを出すので、分母の無い「一致」は裏付けに見えてしまう。
        """
        import io

        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        with open(os.path.join(sample, "BOKU2.IDX"), "rb") as fh:
            idx = fh.read()
        size = os.path.getsize(os.path.join(sample, "BOKU2.IMG"))

        tested = boku2.dfi_rule_tested(idx, size)
        in_folder = sum(1 for e in boku2.read_dfi(idx, size, "flag") if "/" in e["path"])
        self.assertEqual(tested, in_folder, "突き合わせた件数がフォルダの中のファイル数と違う")
        self.assertGreater(tested, 0, "練習データに入れ子が無い (前提が崩れた)")

        out = io.StringIO()
        boku2.check(sample, out=out)
        self.assertIn(f"フォルダの中のファイル {tested} 件で突き合わせた", out.getvalue(),
                      "「一致」に分母が付いていない")

        # 入れ子がまったく無い索引を作って、「一致」と言わないことを見る
        flat = self.flat_dfi()
        # まずこの索引が本当に読めること。読めない索引なら 0 件は当たり前で、検査にならない
        self.assertEqual(len(boku2.read_dfi(flat, 6 * 2048, "flag")), 6,
                         "作った平らな索引が読めていない (この検査が空振りする)")
        self.assertEqual(boku2.dfi_rule_mismatch(flat, 6 * 2048), [], "入れ子が無いのに食い違う")
        self.assertEqual(boku2.dfi_rule_tested(flat, 6 * 2048), 0,
                         "入れ子が無いのに「突き合わせた」と数えている")

    def flat_dfi(self) -> bytes:
        """入れ子がまったく無い DFI 索引 (根にファイルが並ぶだけ)."""
        names = [f"f{i}.bin" for i in range(6)]
        recs = [(1, 1, 0, 0)] + [(0, 0 if i == 5 else 1, i, 2048) for i in range(6)]
        body = b"".join(struct.pack("<HHIII", d, more, 0, lba, ln) for d, more, lba, ln in recs)
        name_blob = b"/\0" + b"".join(n.encode("ascii") + b"\0" for n in names)
        return b"DFI\0" + struct.pack("<III", len(recs), 0, 0) + body + name_blob

    def test_the_docs_quote_the_refusals_word_for_word(self):
        """docs/10 の「困ったとき」が挙げる断り文句が、道具の出力に本当にあること.

        #120〜#122 は 3 回続けて「文書が書いた実行結果が実際と違う」だった。
        書いたその場で突き合わせる。
        """
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            doc = fh.read()
        with tempfile.TemporaryDirectory() as tmp:
            blank = os.path.join(tmp, "blank.txt")
            open(blank, "w", encoding="utf-8").close()
            rows = [{"id": "0", "original": "はい", "translation": "はい"}]
            real = os.path.join(tmp, "t.tsv")
            scrp.write_tsv(real, rows)
            outs = [
                self.cli("proofread.py", self.header_only(tmp))[1],
                self.cli("proofread.py", real, "--font-chars", blank)[1],
                self.cli("compare_tsv.py", self.header_only(tmp, "a.tsv"),
                         self.header_only(tmp, "b.tsv"))[1],
            ]
        for phrase in ("検査する行が 1 行もありません", "が 0 字です",
                       "突き合わせた行が 1 行もありません"):
            # assertIn だと docs/10 を丸ごと吐く (#96・#115 で 2 度踏んだ)
            self.assertTrue(phrase in doc, f"docs/10 の「困ったとき」に「{phrase}」の行が無い")
            self.assertTrue(any(phrase in o for o in outs),
                            f"docs/10 が書いている「{phrase}」を道具が出さない")

    def test_the_tool_that_already_got_this_right_still_does(self):
        """`boku2.py text` の断り方が手本。これが緩むと揃える先が消える."""
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self.cli("boku2.py", "text", tmp, "-o", os.path.join(tmp, "o.tsv"))
        self.assertEqual(rc, 1, f"0 行なのに合格になった:\n{out}")
        self.assertIn("1 行も見つかりませんでした", out, out)


class TestExerciseSevenIsDoable(unittest.TestCase):
    """課題 7 の前提が本当かを確かめる (#122).

    課題 7 は「`--var-width 6` を付けると **id 2** が仕様違反として出る
    (14 + 6 = 20 > 18)」を導入に使い、そこから `--name-width` を作らせる。
    ところが **id 2 では出ない**。id 2 の訳文は `<VAR:00>` が `あなた` に
    置き換わっている行 (課題 5 の `placeholder` の仕込み) なので、差し込む変数が
    無く 17 文字にしかならない。校正が見るのは訳文の側。
    20 文字になるのは**原文**で、そちらは検査台でしか見られない。

    実際に出るのは **id 22** (13 → 19)。導入でつまずくと課題そのものに入れないので、
    例を id 22 に直した。ここではその例と、課題が「もう用意してある」と言っている
    材料が本当にあるかを見る。
    """

    @classmethod
    def setUpClass(cls):
        import proofread

        cls.pf = proofread
        cls.rows = scrp.read_tsv(os.path.join(REPO, "exercises", "qa_target.tsv"))
        cls.rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")
        cls.gloss = proofread.load_glossary(os.path.join(REPO, "data", "glossary.tsv"))
        cls.chars = proofread.load_font_chars(os.path.join(REPO, "data", "font_chars.txt"))
        with open(os.path.join(REPO, "exercises", "README.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()

    def fired(self, row_id: str, var_width: float) -> set:
        row = next(r for r in self.rows if r["id"] == row_id)
        tw = self.pf.tag_widths_of(self.rules, var_width)
        return {f.rule for f in self.pf.check_row(row, self.rules, self.gloss, self.chars, tw)}

    def test_the_example_row_really_fires_only_with_the_flag(self):
        """課題が挙げる行が、`--var-width` を付けたときだけ幅で引っかかること."""
        import re

        m = re.search(r"`id (\d+)` の `<VAR:00>.*?` が\n`line_width` で出ます", self.doc)
        self.assertTrue(m, "課題 7 が例の id を挙げていない")
        rid = m.group(1)
        self.assertNotIn("line_width", self.fired(rid, 0.0),
                         f"id {rid} は --var-width 無しでも幅で引っかかる (例にならない)")
        self.assertIn("line_width", self.fired(rid, 6.0),
                      f"id {rid} が --var-width 6 でも幅で引っかからない")

    def test_the_document_warns_that_id_2_does_not_fire(self):
        """id 2 では出ないことと、その理由が書いてあること."""
        self.assertTrue("**`id 2` では出ません。**" in self.doc,
                        "id 2 で出ないという断りが無い")
        self.assertNotIn("line_width", self.fired("2", 6.0),
                         "id 2 が幅で引っかかる (文書の断りのほうが古い)")

    def test_the_widths_the_document_quotes_are_measured(self):
        """13 → 19 と、原文が 20 になることを測り直す."""
        import re

        m = re.search(r"\((\d+) 文字 \+ 名前 (\d+) 文字 = (\d+) 文字 > (\d+)\)", self.doc)
        self.assertTrue(m, "課題 7 の計算が読み取れない")
        plain, name, total, limit = (int(x) for x in m.groups())
        row = next(r for r in self.rows if r["id"] == "22")
        bare = scrp.display_width(row["translation"], self.pf.tag_widths_of(self.rules, 0.0))
        wide = scrp.display_width(row["translation"], self.pf.tag_widths_of(self.rules, float(name)))
        self.assertEqual(bare, plain, f"名前なしの幅が {bare} (文書は {plain})")
        self.assertEqual(wide, total, f"名前ありの幅が {wide} (文書は {total})")
        self.assertEqual(float(limit), self.rules["line_max_width"], "上限が違う")
        # 原文が 20 文字になる、という別の主張も測る
        two = next(r for r in self.rows if r["id"] == "2")
        first = scrp.split_lines(two["original"])[0]
        self.assertEqual(scrp.display_width(first, self.pf.tag_widths_of(self.rules, 6.0)), 20.0,
                         "id 2 の原文が 20 文字にならない")

    def test_the_parts_the_exercise_promises_are_there(self):
        """「もう用意してある」と言っている材料が本当にあること.

        これが無いと課題に入れない: 任意のタグ名で幅を渡せること / 話者名の表 /
        設定から既定値を読む書き方。
        """
        self.assertEqual(scrp.display_width("<NAME:01>あいう", {"NAME": 4}), 7.0,
                         "display_width が NAME の幅を数えられない (課題 7 が成り立たない)")
        self.assertEqual(scrp.display_width("<NAME:01>あいう", {}), 3.0,
                         "幅を渡さないときに数えてしまっている")
        names = self.pf.load_names(os.path.join(REPO, "data", "names.tsv"))
        self.assertTrue(names, "話者名の表が読めない")
        self.assertGreaterEqual(max(len(v) for v in names.values()), 2,
                                "auto の材料になる名前が短すぎる")
        # 設定から既定を読む書き方 (var_width) が手本として残っていること
        import inspect

        src = inspect.getsource(self.pf.tag_widths_of)
        self.assertIn('rules.get("var_width"', src, "設定から読む手本が消えている")


class TestExerciseTwoRuleOfThumbIsTrue(unittest.TestCase):
    """課題 2 の「`82 xx` は仮名」が、題材で本当かを数える (#121).

    課題 2 は要点として「`82 xx` がひらがな、`F0` が改行、`FF` が終端」を
    **目で覚えろ**と言っていた。前 2 つと最後は正しいが、**`82` は仮名だけでは
    ない**。Shift-JIS では `82 4F`〜`82 9A` が全角の英数字で、題材にも
    `３００` (id 3) や `８５０` (id 24) が入っている。どちらも課題 5・6 で
    もう一度出てくる行なので、「82 なら仮名」と覚えたまま手で読むと数字を外す。

    覚え違いは道具では捕まらない (道具は正しく読む) ので、**文書の側**を直した。
    ここでは文書が挙げている数と範囲を数え直す。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/SCRIPT.BIN", "make_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        arch = scrp.read_archive(os.path.join(REPO, "work", "SCRIPT.BIN"))
        cls.kana, cls.alnum, cls.alnum_ids = 0, 0, set()
        for idx in range(arch.count):
            raw = arch.raw_block(idx)
            i = 0
            while i < len(raw):
                b = raw[i]
                if b in (0xF0, 0xF2, 0xFF):          # 改行 / 待ち / 終端
                    i += 1
                    continue
                if b == 0xF1:                        # 話者名 (引数 1 バイト)
                    i += 2
                    continue
                if 0x81 <= b <= 0x9F or 0xE0 <= b <= 0xEF:
                    if b == 0x82:
                        trail = raw[i + 1]
                        if 0x9F <= trail <= 0xF1:
                            cls.kana += 1
                        elif 0x4F <= trail <= 0x9A:
                            cls.alnum += 1
                            cls.alnum_ids.add(idx)
                    i += 2
                else:
                    i += 1
        with open(os.path.join(REPO, "exercises", "README.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()

    def test_the_document_admits_that_82_is_not_always_kana(self):
        self.assertTrue("`82` なら必ず仮名、ではありません" in self.doc,
                        "課題 2 が「82 なら仮名」と言い切ったままになっている")

    def test_the_counts_in_the_document_are_measured(self):
        import re

        m = re.search(r"前者が (\d+) 回、後者が (\d+) 回", self.doc)
        self.assertTrue(m, "課題 2 に数が書かれていない")
        self.assertEqual(int(m.group(1)), self.kana,
                         f"仮名が {self.kana} 回 (文書は {m.group(1)} 回)")
        self.assertEqual(int(m.group(2)), self.alnum,
                         f"全角英数が {self.alnum} 回 (文書は {m.group(2)} 回)")

    def test_the_ids_the_document_names_really_contain_them(self):
        """文書が例に挙げた id に、本当に全角英数が入っていること."""
        import re

        named = {int(x) for x in re.findall(r"id (\d+) の `[０-９]+`", self.doc)}
        self.assertTrue(named, "課題 2 が例の id を挙げていない")
        self.assertTrue(named <= self.alnum_ids,
                        f"文書が挙げた id {sorted(named)} のうち "
                        f"{sorted(named - self.alnum_ids)} には全角英数が無い")

    def test_the_boundary_in_the_document_is_the_real_one(self):
        """境目 (9F) が本当の境目であること。ここを外すと覚え直しになる."""
        self.assertEqual("ぁ".encode("cp932").hex().upper(), "829F")
        self.assertEqual("ん".encode("cp932").hex().upper(), "82F1")
        self.assertEqual("０".encode("cp932").hex().upper(), "824F")
        self.assertEqual("ｚ".encode("cp932").hex().upper(), "829A")
        self.assertTrue("`82 9F`〜`82 F1`" in self.doc, "かなの範囲が違う")
        self.assertTrue("`82 4F`〜`82 9A`" in self.doc, "全角英数の範囲が違う")


class TestExerciseThreeCanActuallyBeFinished(unittest.TestCase):
    """課題 3 を手順どおりにやると、道具の言うとおりに足せば読めること (#120).

    課題 3 は「手順 2 の出力の『表に無いバイト』が足すべきものの全部」と
    書いていた。実際にやってみると**足りなかった**。id 0 を読むのに要るのは
    `B3` `B2` の 2 つと、**2 バイトの漢字 7 種類** (E03E E03D E055 …)。
    ところが道具は「表に無いバイトが 3 種類」としか言わず、`E0` を 1 つと数えていた。
    素人が `E0=漢` のように 1 行足しても、当然まだ読めない。

    道具に「続くバイトが毎回違う → 前半の疑い。足すのは N 行」と言わせ、
    課題の文もそれに合わせた。ここでは**言われたとおりに足すと本当に読めるか**を
    通しで確かめる。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/MSG_ENC.BIN", "make_sample.py")
        if problem:
            raise unittest.SkipTest(problem)

    @staticmethod
    def table_of(path):
        out = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    out[k.strip().upper()] = v
        return out

    def test_the_tool_says_how_many_rows_to_add(self):
        """「1 行ではなく N 行」を、続くバイトの数から言うこと."""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            guess = os.path.join(tmp, "guess.tbl")
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "relative_search.py"),
                                os.path.join(REPO, "work", "MSG_ENC.BIN"),
                                "--search", "ここは", "--derive", guess],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"),
                                os.path.join(REPO, "work", "MSG_ENC.BIN"),
                                "--table", guess, "--message", "0"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = r.stdout
        self.assertIn("表に無いバイト", out, "足りないバイトの一覧が出ていない")
        self.assertIn("続くバイトが毎回違う", out,
                      "2 バイトの前半らしさを言っていない:\n" + out[-400:])
        self.assertIn("足すのは 1 行ではなく", out,
                      "何行足せばよいかを言っていない:\n" + out[-400:])

    def test_adding_exactly_what_the_tool_names_finishes_the_exercise(self):
        """道具が挙げたものを (答えの表から) 足すと、id 0 が最後まで読めること.

        「言われたとおりに足したのに読めない」が起きないことの確認。
        """
        import re
        import subprocess
        import tempfile

        answers = self.table_of(os.path.join(REPO, "answers", "custom.tbl"))
        with tempfile.TemporaryDirectory() as tmp:
            guess = os.path.join(tmp, "guess.tbl")
            subprocess.run([sys.executable, os.path.join(REPO, "tools", "relative_search.py"),
                            os.path.join(REPO, "work", "MSG_ENC.BIN"),
                            "--search", "ここは", "--derive", guess],
                           capture_output=True, text=True, check=True)
            out = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"),
                                  os.path.join(REPO, "work", "MSG_ENC.BIN"),
                                  "--table", guess, "--message", "0"],
                                 capture_output=True, text=True).stdout
            # 道具が名指しした値だけを拾う (1 バイトの行と、4 桁で挙がった組)
            singles = re.findall(r"^  0x([0-9A-F]{2})  \d+ 回", out, re.M)
            pairs = re.findall(r"([0-9A-F]{4})", out.split("足すのは 1 行ではなく")[-1]) \
                if "足すのは 1 行ではなく" in out else []
            add = []
            for key in [p for p in pairs] + [s for s in singles]:
                if key in answers:
                    add.append((key, answers[key]))
            self.assertTrue(add, f"足す材料が拾えない (singles={singles} pairs={pairs})")
            with open(guess, "a", encoding="utf-8") as fh:
                for key, ch in add:
                    fh.write(f"{key}={ch}\n")
            done = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hexdump.py"),
                                   os.path.join(REPO, "work", "MSG_ENC.BIN"),
                                   "--table", guess, "--message", "0"],
                                  capture_output=True, text=True).stdout
        self.assertNotIn("表に無いバイト", done,
                         "言われたとおりに足したのに、まだ読めない字が残っている:\n"
                         + done[-500:])


class TestTheExerciseAnswersAreTrue(unittest.TestCase):
    """課題 5・6 の答え (answers/qa_answers.md) が、道具の出す結果と合っていること (#119).

    #118 で「答えを持つ仕掛けは、答え自体を検査しないと間違いを増幅する置き場になる」
    と書いた。その目でもう 1 つの答え —— 仕込んだ不具合の一覧 —— を見る。

    答えは 3 つのことを言い切っている: **20 行に仕込んだ**、**指摘は 27 件**、
    そして行ごとに**どの検査が拾うか**。どれも `proofread.py` を走らせれば
    確かめられるのに、確かめる仕掛けが無かった (既存の検査は「仕込んだ行と
    指摘の出た行が同じ集合か」までで、**検査の名前までは見ていない**)。

    行ごとの検査名がずれると、課題 7 (道具を直す) の題材そのものが狂う。
    """

    @classmethod
    def setUpClass(cls):
        import proofread

        cls.rows = scrp.read_tsv(os.path.join(REPO, "exercises", "qa_target.tsv"))
        rules = proofread.load_rules(os.path.join(REPO, "data", "rules.json"), "ja")
        gloss = proofread.load_glossary(os.path.join(REPO, "data", "glossary.tsv"))
        chars = proofread.load_font_chars(os.path.join(REPO, "data", "font_chars.txt"))
        cls.fired: dict[str, set] = {}
        cls.count = 0
        for row in cls.rows:
            hits = proofread.check_row(row, rules, gloss, chars)
            if hits:
                cls.fired.setdefault(row["id"], set()).update(f.rule for f in hits)
            cls.count += len(hits)
        # 行をまたぐ検査 (同じ原文に別の訳。#86 でここを見落とした)
        for f in proofread.check_consistency(cls.rows, lambda r: 0):
            cls.fired.setdefault(f.row_id, set()).add(f.rule)
            cls.count += 1
        with open(os.path.join(REPO, "answers", "qa_answers.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()

    def doc_rows(self) -> dict:
        """答えの表を {id: そこに書いてある検査名の集合} で返す."""
        import re

        out = {}
        for line in self.doc.split("\n"):
            if not re.match(r"^\|\s*\d+\s*\|", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out[cells[0]] = set(re.findall(r"`([a-z_]+)`", cells[2]))
        return out

    def test_the_stated_counts_are_right(self):
        """「20 行に仕込んで指摘は 27 件」を数え直す."""
        import re

        m = re.search(r"(\d+) 行に不具合を仕込んでいます \(指摘は (\d+) 件\)", self.doc)
        self.assertTrue(m, "答えの冒頭の言い方が変わっている")
        rows_said, found_said = int(m.group(1)), int(m.group(2))
        self.assertEqual(len(self.fired), rows_said,
                         f"指摘の出た行が {len(self.fired)} 行 (答えは {rows_said} 行)")
        self.assertEqual(self.count, found_said,
                         f"指摘が {self.count} 件 (答えは {found_said} 件)")
        self.assertEqual(len(self.doc_rows()), rows_said,
                         "表の行数が冒頭の数と違う")

    def test_every_row_names_the_checks_that_actually_fire(self):
        """行ごとの検査名が、実際に発火するものと一致すること."""
        said = self.doc_rows()
        self.assertTrue(said, "答えの表を読めない")
        wrong = []
        for rid, want in said.items():
            got = self.fired.get(rid, set())
            if want != got:
                wrong.append(f"id {rid}: 答え={sorted(want)} 実際={sorted(got)}")
        self.assertFalse(wrong, "答えの検査名が実際と違う:\n  " + "\n  ".join(wrong))

    def test_the_answer_and_the_planting_agree(self):
        """答えの表の id と、仕込みの定義 (plant_errors) の id が同じであること."""
        import plant_errors

        planted = {str(rid) for rid, *_ in plant_errors.PLANTED}
        self.assertEqual(set(self.doc_rows()), planted,
                         "答えの表と仕込みの定義で id が違う")


class TestAllFiveTextPlacesAreInThePractice(unittest.TestCase):
    """docs/09 が挙げる 5 種類の文言の置き場が、練習データに全部あること (#116).

    docs/09 の「現在地」は文言の置き場を 5 種類挙げている: MAP の会話 /
    `.msg` 8 バイト刻み / 出来事 4 バイト刻み / 見出しの無い並び / **Shift-JIS**。
    ところが練習データには **Shift-JIS のファイルが無かった**。`parse_sjis_list`
    そのものには単体の検査があるが、**通し (切り分け → text → TSV) では
    一度も通っていなかった**。素人が docs/10 をなぞっても、この形には出会えない。

    読み方の自動判定は「前の段が成功したら後ろは試さない」ので、**どの段が
    実際に使われるか**は題材しだい。題材に無い段は、順番の正しさも確かめられない。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        import subprocess
        import tempfile

        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        cls.tmp = tempfile.TemporaryDirectory()
        out = os.path.join(cls.tmp.name, "OUT")
        tsv = os.path.join(cls.tmp.name, "all.tsv")
        for args in (["unpack", os.path.join(sample, "BOKU2.IDX"),
                      os.path.join(sample, "BOKU2.IMG"), out],
                     ["text", out, "-f", os.path.join(sample, "font.txt"), "-o", tsv]):
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py")] + args,
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise unittest.SkipTest(f"練習データを通せない: {r.stdout[-200:]}")
        cls.rows = scrp.read_tsv(tsv)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_shift_jis_screen_text_comes_out(self):
        """保存画面の Shift-JIS が、文字表なしでそのまま TSV に出ること."""
        got = [r for r in self.rows if r["id"].startswith("saveload#")]
        self.assertTrue(got, "Shift-JIS の行が 1 つも出ていない "
                             f"(出た id: {sorted({r['id'].split(':')[0] for r in self.rows})})")
        self.assertEqual([r["original"] for r in got],
                         ["セーブしますか？", "はい", "いいえ"],
                         "Shift-JIS の本文が違う")

    def test_every_kind_of_place_is_represented(self):
        """5 種類が揃っていること。1 つでも欠けたら、その読み方は通しで無検査になる."""
        kinds = {r["id"].split(":")[0] for r in self.rows}
        want = {
            ".msg (8 バイト刻み)": "system",
            "メニュー (ページ送り)": "item_info",
            "入れ物の中の入れ物 (4 バイト刻み)": "fish_on_mem#1#2",
            "見出しの無い並び": "diary#0",
            "Shift-JIS": "saveload#2",
            "出来事 (位置だけの表)": "on_mem_event#2",
        }
        missing = [k for k, stem in want.items() if stem not in kinds]
        self.assertFalse(missing, f"練習データに無い置き場: {missing} (出た id: {sorted(kinds)})")

    def test_the_answer_key_matches_what_the_steps_produce(self):
        """docs/10 の「answer.tsv と一致すれば手順が正しく通っている」が本当であること (#118).

        文書はこう約束している ——「`work/BOKU2SAMPLE/answer.tsv` に取り出せるはずの
        全文があります。`all.tsv` の `original` と一致すれば、手順が正しく通っています」。
        ところが答えには**音声の番号 (`<VOICE:…>`) の行**まで入っていて、`text` は
        それを既定で落とすので、**どうやっても一致しなかった** (答え 33 行 /
        取り出し 31 行)。本文はすべて合っていたので、外れていたのは答えの側。

        素人がここで詰まると「自分の手順が悪い」と思って戻ってしまう。
        答え合わせは**合うときに合う**のでなければ意味が無い。
        """
        import subprocess
        import tempfile

        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        with tempfile.TemporaryDirectory() as tmp:
            out, maps = os.path.join(tmp, "OUT"), os.path.join(tmp, "maps")
            tsv = os.path.join(tmp, "all.tsv")
            # docs/10 の「2. 一括で取り出す」に書いてある 3 つのコマンドそのまま
            for args in (["unpack", os.path.join(sample, "BOKU2.IDX"),
                          os.path.join(sample, "BOKU2.IMG"), out],
                         ["maps", os.path.join(sample, "MAP"), "-o", maps],
                         ["text", out, maps, "-f", os.path.join(sample, "font.txt"), "-o", tsv]):
                r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py")] + args,
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            got = {row["id"]: row["original"] for row in scrp.read_tsv(tsv)}
        want = {row["id"]: row["original"]
                for row in scrp.read_tsv(os.path.join(sample, "answer.tsv"))}
        self.assertTrue(want, "answer.tsv が空")
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        self.assertFalse(missing, f"答えにあって取り出しに無い: {missing}")
        self.assertFalse(extra, f"取り出しにあって答えに無い: {extra}")
        wrong = [k for k in want if want[k] != got[k]]
        self.assertFalse(wrong, "本文が違う: "
                         + "; ".join(f"{k}: {want[k]!r} ≠ {got[k]!r}" for k in wrong[:3]))

    def test_the_answer_key_leaves_out_the_voice_rows(self):
        """答えに音声の行が混ざっていないこと (混ざると上の一致が壊れる)."""
        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        rows = scrp.read_tsv(os.path.join(sample, "answer.tsv"))
        voice = [r["id"] for r in rows if r["original"].startswith("<VOICE:")]
        self.assertFalse(voice, f"答えに音声の行がある: {voice}")
        # ただし題材の側には音声が入っていること (落とす処理が働いた証拠になる)
        import make_boku2_sample

        self.assertTrue(any(t.startswith("<VOICE:")
                            for tables in make_boku2_sample.MAPS.values()
                            for table in tables for t in table),
                        "題材に音声の行が無い。落とす処理が働いたか分からない")

    def test_all_four_containers_are_present(self):
        """公開ソースが挙げる文言の入れ物 4 つが練習データに全部あること (#117).

        `check` の「見つからない」が空になるのが目印。1 つでも欠けていると、
        その入れ物の読み方は通しで一度も走らない。
        """
        import subprocess

        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        res = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"),
                              "check", sample], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        line = next((ln for ln in res.stdout.split("\n") if ln.startswith("[入れ物]")), "")
        self.assertTrue(line, "check に [入れ物] の行が無い")
        for name in boku2.TEXT_CONTAINERS:
            self.assertIn(name, line, f"練習データに {name} が無い: {line}")
        self.assertNotIn("見つからない", line, f"まだ欠けている入れ物がある: {line}")

    def test_the_sample_builder_says_the_same(self):
        """答えの一覧にも Shift-JIS が入っていること (答え合わせが片手落ちにならない)."""
        import make_boku2_sample

        self.assertIn("SAVELOAD", dir(make_boku2_sample), "練習データの作り手に文言が無い")
        self.assertEqual(make_boku2_sample.SAVELOAD, ["セーブしますか？", "はい", "いいえ"])


class TestTheStrideIsChosenByContent(unittest.TestCase):
    """位置表の刻みを「先に読めた方」ではなく「中身が良い方」で選ぶこと (#115).

    `.msg` の読み方は 8 バイト刻み → 4 バイト刻み → 見出しの無い並び → Shift-JIS の
    順に試す。**前の段が間違って成功すると、後ろは試されない。**
    8 バイト刻みは 4 バイト刻みのファイルでも通ることがあり (位置表の後ろに隙間が
    あって、そこが増える偶数の並びに見える場合)、そのとき項目の切れ目が本文の
    途中に来て**文がぶつ切り**になる。

    見分ける手がかりは「終わりの印 (0x8000) で終わっている項目の割合」。
    入れ物の刻み (parse_map_rec) が「部品が多く取れる方を採る」のと同じ考え方で、
    こちらにだけ無かった。
    """

    @staticmethod
    def four_stride_that_also_reads_as_eight() -> bytes:
        """4 バイト刻みなのに 8 バイト刻みとしても通るファイル (作為的に組む).

        位置表の後ろに隙間を空け、8 刻みが「位置」として読む所に増える偶数を置く。
        """
        n, body_at = 4, 64
        offs = [body_at, body_at + 8, body_at + 16, body_at + 24]
        head = struct.pack("<I", n) + b"".join(struct.pack("<I", o) for o in offs)
        gap = bytearray(body_at - len(head))
        struct.pack_into("<I", gap, 20 - len(head), 200)
        struct.pack_into("<I", gap, 28 - len(head), 208)
        body = (struct.pack("<H", 0x0041) * 3 + struct.pack("<H", 0x8000)) * 4
        return head + bytes(gap) + body + b"\0" * 160

    def test_the_trap_really_is_a_trap(self):
        """まず「8 でも読めてしまう」ことを確かめる。読めないなら以下に意味が無い."""
        b = self.four_stride_that_also_reads_as_eight()
        self.assertIsNotNone(boku2.parse_msg(b, 8), "8 バイト刻みで読めない (罠が成立していない)")
        self.assertIsNotNone(boku2.parse_msg(b, 4), "4 バイト刻みで読めない (作り方が違う)")

    def test_the_better_reading_wins(self):
        b = self.four_stride_that_also_reads_as_eight()
        info: dict = {}
        items = boku2.pick_msg(b, info)
        self.assertEqual(info.get("stride"), 4,
                         f"刻みの選び方が違う ({info.get('both_strides')})")
        self.assertEqual([it["at"] for it in items], [64, 72, 80, 88],
                         "項目の切れ目が本文の途中に来ている")

    def test_the_practice_menu_still_reads_as_eight(self):
        """`item_info.msg` は 8 でも 4 でも読めるが、正しいのは 8.

        直しすぎ (何でも 4 にする) を止める側の検査。
        """
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        import shutil
        import subprocess
        import tempfile

        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "boku2.py"), "unpack",
                                os.path.join(sample, "BOKU2.IDX"),
                                os.path.join(sample, "BOKU2.IMG"), tmp],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            path = os.path.join(tmp, "system", "submenu", "item", "item_info.msg")
            self.assertTrue(os.path.exists(path), "item_info.msg が出てこない")
            with open(path, "rb") as fh:
                b = fh.read()
        self.assertIsNotNone(boku2.parse_msg(b, 4), "4 でも読める前提が崩れている")
        info: dict = {}
        items = boku2.pick_msg(b, info)
        self.assertEqual(info.get("stride"), 8,
                         f"正しい 8 を選べていない ({info.get('both_strides')})")
        with open(os.path.join(sample, "font.txt"), encoding="utf-8") as fh:
            glyphs = boku2.parse_glyph_table(fh.read())
        got = [boku2.decode(it["codes"], glyphs, tags=False, alt=True) for it in items]
        self.assertEqual(got, ["あみ{BREAK}\nむしをつかまえる{END}",
                               "つりざお{BREAK}\nさかなをつる{END}"],
                         "メニューの文がぶつ切りになっている")

    def test_both_sides_use_the_same_rule(self):
        """画面側にも同じ選び方があること (#104 で懲りた「片側だけ」の再発を止める)."""
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        # assertIn は落ちたとき app.js を丸ごと吐く (#96)。自前の文言で出す
        self.assertTrue("function bokuEndsWell" in js, "画面側に良し悪しの目安が無い")
        self.assertTrue("score > best.score" in js, "画面側が中身で選んでいない")


class TestTheBlindSpotOfTheImageFinderIsDocumented(unittest.TestCase):
    """docs/07 に書いた「取りこぼす絵」の表を、実際に測り直す (#114).

    「見つけた絵」は (1) 分類が タイル (2) 縦の相関が小さい、の順に見る。
    **(1) で落ちた絵は (2) がどれだけ良くても拾われない。** 実際に落ちるのは
    階調のなだらかな絵で、色数が多く隣との差が小さいと「波形」になる。
    合成した階調の絵は縦の相関 0.73 (しきい値 0.82 より小さい = 絵の条件を
    満たす) なのに、測る前に落ちている。

    これは直さずに**文書に書く**ことにした。その代わり、書いた数字が本当かを
    ここで見張る。

    #147 で表の見張り方そのものを点検したら、**3 行のうち 1 行目しか見ていなかった**。
    2 行目・3 行目は「合成データで測ると」と書いてあるのに**作り方がどこにも無く**、
    誰にも再現できない数字だった。実際 2 行目の「平均差 23.2 / 相関 0.91」は、
    上の `gradient` で 23.2 を出す粒 (36) では相関 0.98 で、種 64 通り ×
    粒 256 通りを総当たりしても 23.2 と 0.91 が揃う組は 1 つも無かった。
    docs/07 に粒の値を書き、3 行すべてをここで測り直すようにした。
    """

    #: docs/07 の表と同じ形の絵を作る種。noise が隣どうしの差を決める
    @staticmethod
    def gradient(noise: int, seed: int = 7) -> list:
        r = seed & 0xFFFFFFFF
        out = []
        for y in range(256):
            for x in range(64):
                r = (r * 1103515245 + 12345) & 0xFFFFFFFF
                out.append(((x * 4 + y * 3) + ((r >> 16) & 0xFF) % (noise + 1)) & 0xFF)
        return out

    @staticmethod
    def probe(byte_list) -> dict:
        """web/app.js の blockStats / classifyStats / lagRatio / tileScore を動かす."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const SJIS_LEAD'),e=s.indexOf('function guessTileShape');"
            "if(a<0||e<0){console.error('no funcs');process.exit(2);}"
            "const m=new Function(s.slice(a,e)+"
            "'\\nreturn {blockStats,classifyStats,lagRatio,tileScore};')();"
            f"const v=new Uint8Array({json.dumps(byte_list)});"
            "const st=m.blockStats(v.subarray(0,4096));"
            "console.log(JSON.stringify({cls:m.classifyStats(st),meanDiff:st.meanDiff,"
            "ratio:m.lagRatio(v.subarray(0,4096),64),found:!!m.tileScore(v,0,4096)}));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("測れない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    #: docs/07 の表の分類の欄と、classifyStats の返り値の対応
    CLS_WORDS = {"波形": "wave", "高エントロピー": "high", "タイル": "tile"}

    def doc_rows(self) -> list:
        """docs/07 の「取りこぼす絵」の表を (粒, 平均差, 分類, 相関) で返す.

        粒 (ざらつき) の欄は #147 で足した。**作り方が書いていない数字は
        誰にも確かめられない**ので、この欄が無ければ読み取りごと失敗させる。
        """
        import re

        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        # **見出しの形**で切る。素の言葉で切ると、本文中の同じ言葉 (節への
        # 差し込みリンクなど) に当たって別の場所を読む (#149 で実際に踏んだ)
        head = "### この見つけ方が取りこぼす絵"
        self.assertTrue(head in doc, "docs/07 に取りこぼしの節が無い")
        body = doc.split(head, 1)[1].split("\n###", 1)[0]
        rows = []
        for line in body.split("\n"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 5 and re.match(r"^\d+", cells[0]):
                ratio = re.search(r"(\d\.\d\d)", cells[3])
                rows.append((int(re.match(r"^(\d+)", cells[0]).group(1)),
                             float(re.match(r"^([\d.]+)", cells[1]).group(1)),
                             cells[2], float(ratio.group(1)) if ratio else None))
        return rows

    def threshold(self) -> float:
        import re

        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            return float(re.search(r"const TILE_RATIO_MAX = ([\d.]+);", fh.read()).group(1))

    def test_every_row_of_the_table_is_measured(self):
        """表の**全行**を測り直す (#147 まで 1 行目しか見ていなかった).

        書いてある粒でそのまま作り直せること自体が、この検査の値打ち。
        """
        rows = self.doc_rows()
        self.assertEqual(len(rows), 3, f"docs/07 の表から {len(rows)} 行しか読めない")
        for noise, want_diff, want_cls, want_ratio in rows:
            got = self.probe(self.gradient(noise))
            self.assertIn(want_cls, self.CLS_WORDS, f"知らない分類「{want_cls}」")
            self.assertEqual(got["cls"], self.CLS_WORDS[want_cls],
                             f"ざらつき {noise}: 分類が {got['cls']} (docs/07 は {want_cls})")
            self.assertEqual(round(got["meanDiff"], 1), want_diff,
                             f"ざらつき {noise}: 平均差が {got['meanDiff']:.1f} "
                             f"(docs/07 は {want_diff})")
            self.assertEqual(round(got["ratio"], 2), want_ratio,
                             f"ざらつき {noise}: 縦の相関が {got['ratio']:.2f} "
                             f"(docs/07 は {want_ratio})")
            self.assertFalse(got["found"],
                             f"ざらつき {noise}: 出ないはずの絵が拾われた (表を書き換えること)")

    def test_only_the_first_row_is_a_real_blind_spot(self):
        """1 行目だけが「分類さえ緩めれば拾える」絵であること (#147).

        表の主張の核はここ。2・3 行目は相関もしきい値を超えているので、
        分類を緩めても拾えない —— **取りこぼしと呼べるのは 1 行目だけ**。
        docs/07 にそう書いたので、その言い分をここで支える。
        """
        rows = self.doc_rows()
        cut = self.threshold()
        first = self.probe(self.gradient(rows[0][0]))
        self.assertLess(first["ratio"], cut,
                        "1 行目の相関がしきい値より大きい (表の前提が崩れている)")
        for noise, _diff, _cls, _ratio in rows[1:]:
            got = self.probe(self.gradient(noise))
            self.assertGreater(got["ratio"], cut,
                               f"ざらつき {noise} の相関がしきい値より小さい。"
                               f"分類を緩めれば拾えるので、docs/07 の"
                               f"「本当の取りこぼしは 1 行目だけ」が嘘になる")

    def test_a_sharp_image_is_still_found(self):
        """輪郭のある絵 (ドット絵・フォント) は今までどおり拾えること."""
        problem = ensure_practice("work/FONT.BIN", "make_sample.py")
        if problem:
            self.skipTest(problem)
        with open(os.path.join(REPO, "work", "FONT.BIN"), "rb") as fh:
            got = self.probe(list(fh.read(8192)))
        self.assertEqual(got["cls"], "tile", f"フォントの分類が {got['cls']}")
        self.assertTrue(got["found"], "フォントが絵として拾われなくなった")


class TestLooseningTheWaveFilterIsMeasured(unittest.TestCase):
    """docs/07 の「緩めたら音声が軒並み絵になる」を測る (#147).

    取りこぼし (なだらかな絵) を直さない**理由**として、docs/07 は
    「ここを緩めて波形も測りにいくと、今度は音声ファイルが軒並み『絵』として
    出てきます」と書いていた。設計判断の根拠なのに、一度も測っていなかった。

    測ったら**練習データの音 (`BGM.ADP`) はいちばん小さい相関でも 7.94** で、
    しきい値 0.82 の 10 倍近く離れていた。緩めても絵にはならない。
    危ないのは**周期が候補の刻み (8・16・24・32・64・128) と合う音だけ**で、
    23 バイト周期の音は 1.02 で出てこない。

    理由もはっきりしている。なめらかな波は局所的にはただの坂なので、
    間隔を空けた差は**間隔に比例して**大きくなる。だから相関は間隔そのもの
    (いちばん短い候補なら 8) に近づく。低い音ほど安全側。

    「軒並み」を測った事実に書き換えたので、その事実をここで見張る。
    """

    #: 表のうち、練習データから作るもの。それ以外は周期を指定した合成音
    SAMPLE_ROW = "`BGM.ADP`"

    @staticmethod
    def sine(period: int, size: int = 4096) -> list:
        """docs/07 の表と同じ合成音。`pseudo_wave` と同じ形で周期だけ変える.

        JS の `Math.round` は常に大きいほうへ丸めるので `floor(x + 0.5)` で揃える
        (Python の `round` は偶数丸めで、境目の値がずれる)。
        """
        import math

        out = []
        for i in range(size):
            v = (math.sin(i / period * 2 * math.pi) * 0.6
                 + math.sin(i / 1331 * 2 * math.pi) * 0.4)
            out.append(math.floor((v + 1) * 127.5 + 0.5) & 0xFF)
        return out

    @staticmethod
    def measure(byte_list) -> dict:
        """web/app.js の classifyStats と lagRatio をそのまま動かし、最小の相関を返す."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const SJIS_LEAD'),e=s.indexOf('function guessTileShape');"
            "if(a<0||e<0){console.error('no funcs');process.exit(2);}"
            "const m=new Function(s.slice(a,e)+"
            "'\\nreturn {blockStats,classifyStats,lagRatio,TILE_LAGS,TILE_RATIO_MAX};')();"
            # 48KB を丸ごと渡すと argv の上限に当たる。測るのは先頭の窓だけ
            f"const v=new Uint8Array({json.dumps(list(byte_list[:4096]))});"
            "let best=9;for(const l of m.TILE_LAGS){const r=m.lagRatio(v,l);if(r<best)best=r;}"
            "console.log(JSON.stringify({cls:m.classifyStats(m.blockStats(v)),"
            "ratio:best,cut:m.TILE_RATIO_MAX}));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("測れない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    def doc_rows(self) -> list:
        """docs/07 の「緩めたら何が起きるのか」の表を (見出し, 分類, 相関, 出るか) で返す."""
        import re

        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        # 見出しの形で切る (素の言葉だと本文のリンクに当たる。#149)
        head = "### では、緩めたら何が起きるのか"
        self.assertTrue(head in doc, "docs/07 に「緩めたら何が起きるのか」の節が無い")
        body = doc.split(head, 1)[1].split("\n###", 1)[0]
        rows = []
        for line in body.split("\n"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # 分類の欄が分類の言葉になっている行だけを拾う。見出しに数字がある行だけを
            # 拾うと、`BGM.ADP` の行が黙って抜け落ちる (#147 で一度そうなった)
            if len(cells) == 4 and cells[1].strip("*") in ("波形", "高エントロピー", "タイル"):
                ratio = re.search(r"(\d+\.\d\d)", cells[2])
                self.assertTrue(ratio, f"相関を読めない行: {line!r}")
                rows.append((cells[0], cells[1].strip("*"), float(ratio.group(1)),
                             "出る" in cells[3].replace("出ない", "")))
        return rows

    def bytes_for(self, label: str) -> list:
        """表の見出しから、測る相手のバイト列を作る."""
        import re

        if self.SAMPLE_ROW in label:
            import make_iso

            return list(make_iso.pseudo_wave(48 * 1024))
        m = re.match(r"^(\d+) バイト", label)
        self.assertTrue(m, f"表の見出しから周期を読めない: {label!r}")
        return self.sine(int(m.group(1)))

    def test_every_row_of_the_loosening_table_is_measured(self):
        rows = self.doc_rows()
        self.assertEqual(len(rows), 6, f"表から {len(rows)} 行しか読めない")
        self.assertTrue(any(self.SAMPLE_ROW in r[0] for r in rows),
                        "練習データの音の行が表から読めていない (この節の中心)")
        for label, want_cls, want_ratio, want_found in rows:
            got = self.measure(self.bytes_for(label))
            self.assertEqual(got["cls"], "wave",
                             f"{label}: 分類が {got['cls']} (音として扱えていない)")
            self.assertEqual(want_cls, "波形", f"{label}: docs/07 の分類が「{want_cls}」")
            self.assertEqual(round(got["ratio"], 2), want_ratio,
                             f"{label}: 相関が {got['ratio']:.2f} (docs/07 は {want_ratio})")
            self.assertEqual(got["ratio"] < got["cut"], want_found,
                             f"{label}: 分類を外したときに絵として出るかが docs/07 と逆")

    def test_the_practice_audio_would_not_become_a_picture(self):
        """節の中心。練習データの音は、緩めても絵にならない (しきい値の何倍も離れている)."""
        import make_iso

        got = self.measure(list(make_iso.pseudo_wave(48 * 1024)))
        self.assertEqual(got["cls"], "wave")
        self.assertGreater(got["ratio"], got["cut"] * 5,
                           f"練習データの音の相関が {got['ratio']:.2f} まで下がった。"
                           f"docs/07 の「しきい値の 10 倍近く離れています」を書き直すこと")

    def test_a_lag_matched_tone_really_would_slip_through(self):
        """逆側。刻みに合う音は本当にすり抜ける (だから波形の分類には値打ちがある).

        ここが落ちるなら、波形の分類は**何も守っていない**ことになり、
        なだらかな絵の取りこぼしを我慢する理由が無くなる。
        """
        got = self.measure(self.sine(16))
        self.assertLess(got["ratio"], got["cut"],
                        "刻みに合う音もすり抜けない。波形の分類が守っている相手が"
                        "いなくなったので、docs/07 の判断ごと考え直すこと")

    def test_a_smooth_wave_lands_near_the_shortest_lag(self):
        """なめらかな波の相関が「間隔そのもの」に近づくという説明が本当であること."""
        got = self.measure(self.sine(128))
        self.assertGreater(got["ratio"], 6,
                           f"なめらかな波の相関が {got['ratio']:.2f}。"
                           f"docs/07 の「いちばん短い候補 (8) でも 8 前後」の説明が崩れた")


class TestTheDisassemblyInTheDocIsReal(unittest.TestCase):
    """docs/08 が見せている逆アセンブルと `SYSTEM.CNF` が、本物であること (#164).

    #163 で作った網は「コマンドの**すぐ下**」の出力しか見ない。docs/08 は
    命令を 2 行だけ抜き出して説明に使うので、その網に掛からなかった。
    しかも docs/08 は **「上の 2 行は練習用の `work/BOOT.ELF` から実際に出る
    ものです」と言い切って**いた。

    確かめたら、**命令とアドレスは本物だが、行の形は違った**。道具は

        0010003C  03E00008  jr       $ra
        00100040  27BD0020  addiu    $sp, $sp, 0x20   ← 遅延スロット

    と機械語の桁と自前の注釈を出すのに、文書は桁を省いて注釈を自分の言葉に
    差し替えていた。**打った人の画面と違う。** 嘘ではないが、
    「実際に出るもの」と言うなら**そのまま写す**のが筋。

    `SYSTEM.CNF` も 4 行あるのに 3 行しか載せていなかった
    (`HDDUNITPOWER = NICHDD` が抜けていた)。

    直したので、**道具が出すとおりか**をここで見張る。文書の行は
    そのまま出力に含まれていなければいけない。
    """

    #: (docs/08 のブロックの目印, その行を出すコマンド)
    BLOCKS = [
        ("0010003C", ["tools/elfdump.py", "work/BOOT.ELF", "--disasm", "--count", "64"]),
        ("0010000C", ["tools/elfdump.py", "work/BOOT.ELF", "--disasm", "--count", "64"]),
    ]

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/BOOT.ELF", "make_elf.py")
        if problem:
            raise unittest.SkipTest(problem)
        with open(os.path.join(REPO, "docs", "08-コードを読む.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()

    @staticmethod
    def run_tool(argv: list) -> str:
        # **`run` という名前にしない。** unittest.TestCase.run を
        # 覆ってしまい、検査そのものが走らなくなる (#164 で踏んだ)
        import subprocess

        res = subprocess.run([sys.executable, *argv], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError(f"{argv} が落ちた: {(res.stdout + res.stderr)[-200:]}")
        return res.stdout

    def block_with(self, mark: str) -> list:
        """目印の行を含む ``` ブロックの中身を行で返す."""
        import re

        for m in re.finditer(r"```\w*\n(.*?)\n```", self.doc, re.S):
            if mark in m.group(1):
                return [ln.rstrip() for ln in m.group(1).splitlines() if ln.strip()]
        return []

    def test_every_shown_instruction_line_is_printed_verbatim(self):
        bad = []
        for mark, argv in self.BLOCKS:
            lines = self.block_with(mark)
            self.assertTrue(lines, f"docs/08 に {mark} のブロックが無い (書き方が変わった)")
            real = self.run_tool(argv)
            for line in lines:
                if line not in real:
                    bad.append(f"docs/08 [{mark}] の行が出力に無い:\n"
                               f"      文書: {line!r}")
        self.assertEqual(bad, [], "docs/08 の逆アセンブルが実際と違う:\n  " + "\n  ".join(bad))

    def test_the_delay_slot_note_is_the_tools_own_words(self):
        """遅延スロットの注釈は、**道具が出す言葉**であること.

        文書が自分で書いた矢印だと、打った人の画面に出ない。
        """
        lines = self.block_with("0010003C")
        self.assertTrue(any("← 遅延スロット" in ln for ln in lines),
                        f"道具の注釈が載っていない: {lines}")
        real = self.run_tool(self.BLOCKS[0][1])
        self.assertTrue("← 遅延スロット" in real, "道具が遅延スロットの注釈を出さなくなった")

    def test_the_boot_config_block_matches_the_real_file(self):
        """`SYSTEM.CNF` のブロックが、練習用イメージに入る中身と同じであること."""
        import make_iso

        lines = self.block_with("BOOT2")
        self.assertTrue(lines, "docs/08 に SYSTEM.CNF のブロックが無い")
        real = [ln for ln in make_iso.SYSTEM_CNF.replace("\r\n", "\n").split("\n") if ln]
        self.assertEqual(lines, real,
                         "docs/08 の SYSTEM.CNF が実物と違う "
                         f"(文書 {len(lines)} 行 / 実物 {len(real)} 行)")

    def test_the_lui_pair_shows_what_the_tool_resolves(self):
        """`lui`/`addiu` の組で、**道具が解いた指し先**まで載っていること.

        手で復元する方法だけを教えて、道具がやってくれることを伏せない。
        """
        lines = self.block_with("0010000C")
        self.assertTrue(any("→ 0x" in ln for ln in lines),
                        f"道具が出す指し先が載っていない: {lines}")


class TestTheOutputUnderACommandIsRealOutput(unittest.TestCase):
    """コマンドの**すぐ下**に置いた出力ブロックが、本当にその出力であること (#163).

    #130 は docs/10 の引用を「どこかの道具が言うか」で見ていた。
    こちらは**どのコマンドの出力か**まで縛る。教材は

        ```bash
        python3 tools/hexdump.py work/SCRIPT.BIN --message 2
        ```
        ```
        0000013D  F1 01 …
        ```

    という形で「打てばこう出る」と約束している。ここが古いと、
    **初めて読む人が最初の 1 分で「話が違う」と思う**。

    docs/01 と docs/02 は**いちばん最初に読む教材**なのに、
    中身を確かめる検査が一つも無かった (通しで打てるか、Windows で読めるかの
    構造的な検査だけ)。手で確かめたら 4 組とも合っていたので、そのまま固定する。

    **組にするのは「すぐ下」に置かれたものだけ。** 間に文章があるブロックは
    式や語の一覧のことがあり、近さで機械的に結ぶと外れる。
    拾い方を賢くするより、**書き方の決まり (すぐ下に置く) を頼りにする** (#151)。
    """

    #: 出力に混ざる「その場のもの」。ここだけは違ってよい
    VOLATILE = (
        re.compile(r"/tmp/[^\s'\"]+"),
        re.compile(r"work/[^\s'\"]*\.(?:tbl|tsv|bin|BIN)"),
    )

    FENCE = re.compile(r"```(\w*)\n(.*?)\n```", re.S)

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/SCRIPT.BIN", "make_sample.py")
        if problem:
            raise unittest.SkipTest(problem)

    @classmethod
    def pairs(cls, text: str) -> list:
        """```bash のすぐ下にある出力ブロックを (コマンド, 出力) で返す."""
        out, blocks = [], list(cls.FENCE.finditer(text))
        for a, b in zip(blocks, blocks[1:]):
            if a.group(1) != "bash" or b.group(1) not in ("", "text"):
                continue
            if text[a.end():b.start()].strip():     # 間に文章があれば組にしない
                continue
            out.append((a.group(2).strip(), b.group(2)))
        return out

    def all_pairs(self) -> list:
        got = []
        base = os.path.join(REPO, "docs")
        for name in sorted(os.listdir(base)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(base, name), encoding="utf-8") as fh:
                for cmd, body in self.pairs(fh.read()):
                    got.append((f"docs/{name}", cmd, body))
        return got

    @staticmethod
    def run_lines(cmd: str) -> str:
        """書いてあるコマンドを、書いてあるまま走らせて出力を返す.

        `\\` の行継続と、`#` のコメントだけ外す。**引数はいじらない** ——
        いじると「書いてあるとおりに打った人」と違うものを測ることになる。
        """
        import subprocess

        joined = cmd.replace("\\\n", " ")
        out = []
        for line in joined.splitlines():
            line = re.sub(r"\s+#.*$", "", line).strip()
            if not line.startswith("python3 "):
                continue
            argv = [sys.executable] + line.split()[1:]
            res = subprocess.run(argv, capture_output=True, text=True, cwd=REPO)
            out.append(res.stdout + res.stderr)
        return "\n".join(out)

    def test_the_pairs_are_found(self):
        """前提: 組がちゃんと拾えていること (0 組なら下の検査は素通り)."""
        got = self.all_pairs()
        self.assertGreaterEqual(len(got), 4,
                                f"コマンドと出力の組を {len(got)} 組しか拾えていない")
        for doc in ("docs/01-文字テーブル.md", "docs/02-相対検索.md"):
            self.assertTrue(any(d == doc for d, _c, _b in got),
                            f"{doc} から組を拾えていない")

    def test_every_line_under_a_command_really_comes_out(self):
        """出力ブロックの各行が、そのコマンドの出力に実際にあること."""
        bad = []
        for doc, cmd, body in self.all_pairs():
            real = self.run_lines(cmd)
            if not real.strip():
                bad.append(f"{doc}: コマンドが何も出さない [{cmd.splitlines()[0]}]")
                continue
            # その場で変わるもの (一時フォルダの道筋など) は、両側から同じように落とす
            real_cmp = real
            for pat in self.VOLATILE:
                real_cmp = pat.sub("", real_cmp)
            for line in body.splitlines():
                want = line.strip()
                if not want:
                    continue
                for pat in self.VOLATILE:
                    want = pat.sub("", want)
                if want and want not in real_cmp:
                    bad.append(f"{doc} [{cmd.splitlines()[0][:50]}]\n"
                               f"      書いてある: {line.strip()[:70]!r}\n"
                               f"      実際の出力にこの行が無い")
        self.assertEqual(bad, [], "コマンドの下の出力が実際と違う:\n  "
                                  + "\n  ".join(bad))


class TestSplittingSurvivesAStrayFile(unittest.TestCase):
    """関係ないファイル 1 つで、切り分けが全部止まらないこと (#161).

    `maps` はフォルダの中を順に切り分けるが、入れ物でないファイルに当たると
    **例外を投げてそこで終わって**いた。吸い出した `MAP/` には
    **`.DS_Store` (Mac) や `Thumbs.db` (Windows) がほぼ必ず混ざる**。
    しかも先頭が `.` の名前は一覧にも出ないので、
    **身に覚えのないファイル名のエラーだけ**が残る。

    並び順で結果が変わるのが最悪だった: `readme.txt` なら本物 2 つは切り分けて
    から止まる。`.DS_Store` は先に来るので、**1 つも切り分けられない**。
    実測したとおり。

    直したあとは、入れ物でないものを**飛ばして名前を言い**、残りは切り分ける。
    言葉は docs/10 の「困ったとき」に載っている「入れ物ではありません」のまま
    (#130 の引用の検査が見張っている)。

    `unpack` にも同じ目を向けた。こちらは**名前が付かなかった数**を言わなかった。
    docs/10 は「`#0012.tm2` のように番号だけなら名前の読み取りに失敗している」と
    人に見張らせているのに、道具は数を出していなかった。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        cls.sample = os.path.join(REPO, "work", "BOKU2SAMPLE")

    def maps_with(self, stray: str, tmp: str):
        """MAP/ を写して `stray` という名前のごみを 1 つ混ぜ、maps を走らせる."""
        import shutil
        import subprocess

        src = os.path.join(self.sample, "MAP")
        dst = os.path.join(tmp, "MAP")
        shutil.copytree(src, dst)
        with open(os.path.join(dst, stray), "w", encoding="utf-8") as fh:
            fh.write("これは MAP のファイルではありません\n")
        out = os.path.join(tmp, "out")
        res = subprocess.run([sys.executable, "tools/boku2.py", "maps", dst, "-o", out],
                             capture_output=True, text=True, cwd=REPO)
        made = sorted(os.listdir(out)) if os.path.isdir(out) else []
        return res, made

    def test_a_stray_file_does_not_stop_the_rest(self):
        """並び順によらず、本物は全部切り分けられること.

        **`.DS_Store` を必ず試す。** 名前の順で先に来るので、
        止まる作りだと 1 つも出来ない —— いちばん痛い形。
        """
        for stray in (".DS_Store", "Thumbs.db", "readme.txt"):
            with tempfile.TemporaryDirectory() as tmp:
                res, made = self.maps_with(stray, tmp)
                self.assertEqual(res.returncode, 0,
                                 f"{stray}: 終了コード {res.returncode} "
                                 f"{(res.stdout + res.stderr)[-200:]!r}")
                self.assertEqual(made, ["M_A01000", "M_A02000"],
                                 f"{stray}: 切り分けられたのが {made}")
                self.assertTrue("入れ物ではありません" in res.stderr,
                                f"{stray}: 飛ばしたことを言っていない "
                                f"{res.stderr[-200:]!r}")
                self.assertTrue(stray in res.stderr,
                                f"{stray}: 飛ばした名前が出ていない {res.stderr[-200:]!r}")

    def test_the_count_is_of_real_boxes_not_of_files_given(self):
        """「N 個の入れ物から」の N が、**入れ物だった数**であること.

        渡した数で言うと、飛ばしたごみまで入れ物に数えてしまう。
        """
        import re

        with tempfile.TemporaryDirectory() as tmp:
            res, _made = self.maps_with(".DS_Store", tmp)
            m = re.search(r"(\d+) 個の入れ物から", res.stdout)
            self.assertTrue(m, f"入れ物の数が出ていない: {res.stdout!r}")
            self.assertEqual(int(m.group(1)), 2,
                             f"入れ物が {m.group(1)} 個 (本物は 2 個。"
                             f"渡した 3 個を数えていないか)")

    def test_all_stray_still_fails(self):
        """入れ物が 1 つも無ければ、今までどおり終了コード 1 で、名前も言うこと.

        飛ばす作りにしたせいで「何も出来ていないのに成功」になっては困る。
        """
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "MAP")
            os.makedirs(junk)
            for name in (".DS_Store", "note.txt"):
                with open(os.path.join(junk, name), "w", encoding="utf-8") as fh:
                    fh.write("ごみ\n")
            res = subprocess.run([sys.executable, "tools/boku2.py", "maps", junk,
                                  "-o", os.path.join(tmp, "out")],
                                 capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 1, "全部ごみなのに成功で終わった")
            self.assertTrue("1 つも見つかりませんでした" in res.stderr, res.stderr[-200:])
            self.assertTrue("入れ物ではありません" in res.stderr, res.stderr[-200:])
            self.assertTrue("note.txt" in res.stderr,
                            f"どれが入れ物でなかったかを言っていない: {res.stderr[-200:]!r}")

    def test_unpack_reports_how_many_got_no_name(self):
        """`unpack` が、名前の付かなかった数を言うこと."""
        import struct
        import subprocess

        import boku2

        with tempfile.TemporaryDirectory() as tmp:
            # 名前の置き場を壊した索引を作る (make_boku2_sample.py の "name" と同じ狙い)
            with open(os.path.join(self.sample, "BOKU2.IDX"), "rb") as fh:
                idx = bytearray(fh.read())
            # レコードの直後 (0x1C0) から名前が並ぶ。そこを 0 で埋めれば名前が読めない
            img = os.path.join(self.sample, "BOKU2.IMG")
            before = boku2.read_dfi(bytes(idx), os.path.getsize(img))
            self.assertTrue(before, "題材の索引が読めない")
            named = sum(1 for e in before
                        if not os.path.basename(e["path"]).startswith("#"))
            self.assertEqual(named, len(before), "壊す前から名前が欠けている")
            idx[0x1C0:] = b"\0" * (len(idx) - 0x1C0)
            broken = os.path.join(tmp, "B.IDX")
            with open(broken, "wb") as fh:
                fh.write(bytes(idx))
            res = subprocess.run(
                [sys.executable, "tools/boku2.py", "unpack", broken,
                 img, os.path.join(tmp, "out")],
                capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stderr[-200:])
            self.assertTrue("名前が付かなかったファイル" in res.stderr,
                            f"名前の付かない数を言っていない: {res.stderr[-300:]!r}")

    def test_unpack_says_nothing_when_every_name_is_read(self):
        """名前が全部読めたときは黙ること (毎回言うと、言葉が効かなくなる)."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            res = subprocess.run(
                [sys.executable, "tools/boku2.py", "unpack",
                 os.path.join(self.sample, "BOKU2.IDX"),
                 os.path.join(self.sample, "BOKU2.IMG"), os.path.join(tmp, "out")],
                capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, res.stderr[-200:])
            self.assertFalse("名前が付かなかったファイル" in res.stderr,
                             f"全部読めたのに知らせが出た: {res.stderr[-300:]!r}")


class TestTextSaysWhatItDidNotTake(unittest.TestCase):
    """`text` が「出なかったもの」を言うこと (#160).

    #159 で踏んだ穴: ファイルを**並べて**渡すと、渡し損ねた分は当然出ないのに、
    最後の行は「文字表で全部読めました」。文字表の話しかしていないので嘘では
    ないが、**12 行しか出ていない画面と 31 行出ている画面が同じ顔をしている。**

    道具は渡されなかったファイルを知らない —— **知ろうとしていなかっただけ**。
    渡されたのがファイルばかりなら、その共通の親を辿れば「拾えたはずのもの」が
    数えられる。フォルダを渡したときは全部辿るので、何も言わない。

    もう 1 つ、**読める形なのに 1 行も出なかったファイル**も名前ごと言う。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        cls.sample = os.path.join(REPO, "work", "BOKU2SAMPLE")

    def unpacked(self, tmp: str) -> str:
        """練習データを切り分けた OUT/ を作る (手順 6 と同じ形)."""
        import subprocess

        out = os.path.join(tmp, "OUT")
        for argv in ([sys.executable, "tools/boku2.py", "unpack",
                      os.path.join(self.sample, "BOKU2.IDX"),
                      os.path.join(self.sample, "BOKU2.IMG"), out],
                     [sys.executable, "tools/boku2.py", "maps",
                      os.path.join(self.sample, "MAP"), "-o", os.path.join(out, "maps")]):
            res = subprocess.run(argv, capture_output=True, text=True, cwd=REPO)
            self.assertEqual(res.returncode, 0, f"下ごしらえで落ちた: {res.stderr[-200:]}")
        return out

    def run_text(self, args: list, tmp: str):
        import subprocess

        argv = [sys.executable, "tools/boku2.py", "text", *args,
                "-f", os.path.join(self.sample, "font.txt"),
                "-o", os.path.join(tmp, "all.tsv")]
        return subprocess.run(argv, capture_output=True, text=True, cwd=REPO)

    def test_listing_files_warns_about_the_ones_left_out(self):
        """#159 そのもの。並べて渡すと、渡し損ねた分を名前ごと知らせる."""
        import glob
        import re

        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpacked(tmp)
            listed = sorted(glob.glob(os.path.join(out, "system", "*.msg")))
            # 深い所の 1 つも混ぜる (#220 で道筋を実物に合わせたので、`system/*.msg` は 1 個だけ)
            listed += sorted(glob.glob(os.path.join(out, "system", "submenu", "item", "*.msg")))
            listed += sorted(glob.glob(os.path.join(out, "maps", "*", "1.bin")))
            self.assertGreater(len(listed), 3, "並べる材料が足りない (題材が変わった)")
            res = self.run_text(listed, tmp)
            self.assertEqual(res.returncode, 0, res.stderr[-300:])
            self.assertTrue("渡されなかった" in res.stderr,
                            f"渡し損ねを知らせていない: {res.stderr[-300:]!r}")
            # 名前まで出ること。数だけだと、何を足せばよいか分からない
            for name in ("diary.bin", "namemsg.msg"):
                self.assertTrue(name in res.stderr,
                                f"{name} が知らせに出ていない: {res.stderr[-300:]!r}")
            # **数も見る。** 数を見ないと、探す範囲が広がりすぎても気づけない
            # (関係ないフォルダまで数えて「50 個あります」と言っても緑になる)
            m = re.search(r"読めるファイルが (\d+) 個", res.stderr)
            self.assertTrue(m, f"知らせに数が入っていない: {res.stderr[-300:]!r}")
            self.assertEqual(int(m.group(1)), 6,
                             f"渡し損ねが {m.group(1)} 個 (OUT の中では 6 個のはず)")

    def test_passing_the_folder_says_nothing(self):
        """正しい渡し方では黙ること (毎回言うと、言葉が効かなくなる).

        フォルダだけのときと、**フォルダとファイルが混じった**ときの両方を見る。
        混じったときも黙るのが決まりで、ここを見ないと
        「ファイルばかりのときだけ」という条件が外れても気づけない。
        """
        import glob

        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpacked(tmp)
            one = sorted(glob.glob(os.path.join(out, "system", "*.msg")))[0]
            for args, what in (([out], "フォルダだけ"), ([out, one], "フォルダとファイル")):
                res = self.run_text(args, tmp)
                self.assertEqual(res.returncode, 0, res.stderr[-300:])
                self.assertFalse("渡されなかった" in res.stderr,
                                 f"{what}を渡したのに知らせが出た: {res.stderr[-300:]!r}")

    def test_used_warns_the_same_way_as_text(self):
        """**`used` にも同じ見張りを付ける** (#232).

        `text` は #160 から「渡されなかったファイルが N 個」と言っていましたが、
        `used` は言っていませんでした。**文字表を作る手順が見ているのは `used` の
        側**です (docs/10 の手順 3)。ファイルを並べて渡すと、練習データですら
        68 種が 15 種に、いちばん大きい番号が 165 から 87 になります。それでも
        `used` は「フォント画像のこの番号だけ書き出せば本文は読める」と
        言い切っていた —— **足りない文字表がそのままできあがる**形です。
        """
        import glob
        import re
        import subprocess

        def run_used(args):
            return subprocess.run([sys.executable, "tools/boku2.py", "used", *args],
                                  capture_output=True, text=True, cwd=REPO)

        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpacked(tmp)
            listed = sorted(glob.glob(os.path.join(out, "system", "*.msg")))
            self.assertTrue(listed, "並べる材料が無い (題材が変わった)")

            whole = run_used([out])
            part = run_used(listed)
            self.assertEqual(whole.returncode, 0, whole.stderr[-300:])
            self.assertEqual(part.returncode, 0, part.stderr[-300:])

            def kinds(res):
                m = re.search(r"# (\d+) 種 \(最大 (\d+)\)", res.stderr)
                self.assertTrue(m, f"種と最大が出ていない: {res.stderr[-300:]!r}")
                return int(m.group(1)), int(m.group(2))

            all_kinds, all_top = kinds(whole)
            part_kinds, part_top = kinds(part)
            # **材料が弱くないこと。** 並べても同じ数になるなら、何を言っても検査は通る
            self.assertGreater(all_kinds, part_kinds,
                               "材料が弱い: 並べて渡しても種類が減らない")
            self.assertGreater(all_top, part_top,
                               "材料が弱い: 並べて渡してもいちばん大きい番号が変わらない")

            # 1. 並べて渡したら、知らせと**言い方の断り**が出ること
            self.assertTrue("渡されなかった" in part.stderr,
                            f"used が渡し損ねを知らせていない: {part.stderr[-400:]!r}")
            self.assertTrue("渡したファイルの中だけ" in part.stderr,
                            f"部分的な数だと断っていない: {part.stderr[-400:]!r}")
            # 2. **間違った太鼓判を押さないこと。**「この番号だけ書き出せば読める」も、
            #    「2 枚目は要りません」も、部分的な数の上では言ってはいけない
            for lie in ("この番号だけ書き出せば本文は読める", "2 枚目の画像は要りません"):
                self.assertFalse(lie in part.stderr,
                                 f"部分的な数で「{lie}」と言っている: {part.stderr[-400:]!r}")
            # 3. フォルダを渡したら黙って、今までどおり言い切ること
            self.assertFalse("渡されなかった" in whole.stderr,
                             f"フォルダを渡したのに知らせが出た: {whole.stderr[-300:]!r}")
            self.assertTrue("この番号だけ書き出せば本文は読める" in whole.stderr,
                            f"言い切っていない: {whole.stderr[-300:]!r}")
            self.assertTrue("使われている文字番号の最大は" in whole.stderr,
                            f"最大の意味を言っていない: {whole.stderr[-300:]!r}")

    def test_the_search_does_not_wander_outside(self):
        """探す範囲が、渡したファイルの共通の親より**上に登らない**こと.

        1 段上げると、隣に置いてある関係ないデータまで「拾い漏れ」に数える。
        知らせが騒がしくなるだけでなく、**本当の拾い漏れが埋もれる**。
        """
        import glob

        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpacked(tmp)
            # OUT の隣に、読める形の別データを置く (別の作品の作業場のつもり)
            other = os.path.join(tmp, "OTHER")
            os.makedirs(other)
            with open(os.path.join(other, "yosomono.msg"), "wb") as fh:
                fh.write(struct.pack("<I", 0))
            # **共通の親が OUT になるように**、別々の下位フォルダから並べる。
            # 片方のフォルダだけだと共通の親が OUT/system になり、
            # 1 段上げても OUT 止まりで、この検査が働かない
            listed = sorted(glob.glob(os.path.join(out, "system", "*.msg")))
            listed += sorted(glob.glob(os.path.join(out, "maps", "*", "1.bin")))
            self.assertEqual(os.path.commonpath([os.path.abspath(f) for f in listed]),
                             os.path.abspath(out), "共通の親が OUT になっていない")
            res = self.run_text(listed, tmp)
            self.assertTrue("渡されなかった" in res.stderr, "知らせが出ていない (前提が崩れた)")
            self.assertFalse("yosomono.msg" in res.stderr,
                             f"隣のフォルダまで数えている: {res.stderr[-300:]!r}")

    def test_the_folder_form_really_takes_more(self):
        """知らせの前提。フォルダのほうが実際に多く拾えること.

        ここが同じなら、上の知らせは**言うだけ無駄**になる。
        """
        import glob
        import re

        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpacked(tmp)
            listed = sorted(glob.glob(os.path.join(out, "system", "*.msg")))
            listed += sorted(glob.glob(os.path.join(out, "maps", "*", "1.bin")))
            few = self.run_text(listed, tmp).stdout
            many = self.run_text([out], tmp).stdout
            n_few = int(re.search(r"(\d+) 行", few).group(1))
            n_many = int(re.search(r"(\d+) 行", many).group(1))
            self.assertGreater(n_many, n_few,
                               f"フォルダで {n_many} 行 / 並べて {n_few} 行 (差が無い)")

    def test_a_readable_shape_with_no_text_is_named(self):
        """読める形なのに 1 行も出なかったファイルを、名前ごと言うこと."""
        import struct

        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpacked(tmp)
            # 件数 0 の .msg。形は .msg だが本文が 1 行も無い
            hollow = os.path.join(out, "system", "hollow.msg")
            with open(hollow, "wb") as fh:
                fh.write(struct.pack("<I", 0))
            res = self.run_text([out], tmp)
            self.assertEqual(res.returncode, 0, res.stderr[-300:])
            self.assertTrue("1 行も出なかった" in res.stderr,
                            f"空のファイルを知らせていない: {res.stderr[-400:]!r}")
            self.assertTrue("hollow.msg" in res.stderr,
                            f"名前が出ていない: {res.stderr[-400:]!r}")

    def test_the_notice_does_not_change_the_result(self):
        """知らせを足しても、行数も終了コードも変わらないこと."""
        import glob

        with tempfile.TemporaryDirectory() as tmp:
            out = self.unpacked(tmp)
            listed = sorted(glob.glob(os.path.join(out, "system", "*.msg")))
            res = self.run_text(listed, tmp)
            self.assertEqual(res.returncode, 0)
            with open(os.path.join(tmp, "all.tsv"), encoding="utf-8-sig") as fh:
                body = [ln for ln in fh.read().split("\n") if ln.strip()]
            self.assertGreater(len(body), 1, "TSV が見出しだけになった")
            self.assertTrue("渡されなかった" in res.stderr, "知らせが出ていない (前提が崩れた)")


class TestTheFontGridIsTheSameEverywhere(unittest.TestCase):
    """フォントの升目の数字が、全部の文書で道具と揃っていること (#158).

    実物のフォントは **1 行 23 字 (0x17)、刻み 22 ドット (0x16)**。
    描かれる枠は 23 ドットで 1 ドット重なる。出典は英語化パッチの公開ソース
    `reprint.py` の `N_COLUMNS = 23` / `CELL_WIDTH = 22` と `asm_notes.txt`。

    **`asm_notes.txt` には `char%17` という書き落としがあります** (同じ文書の
    別の行は `char%0x17`)。10 進の 17 と読むと升目がずれ、**文字表が丸ごと
    別物になります**。しかも出てくる字は日本語に見えるので、目では気づけません。

    ところが `docs/10` —— **社長が実物を相手に読む唯一の手順書** —— の
    「文字の番号を重ねる」の行が、まさに `(1 文字 23、1 行 17)` でした。
    刻みに枠の 23 を使い、列数に書き落としの 17 を使う、**二重に間違い**。
    同じ文書の別の行 (困ったときの表) は正しく「1 行 23 字を 22 ドット刻みで」と
    書いてあり、**docs/10 が自分と食い違って**いました。

    #99 でこの取り違えを見つけたときは「案内文・docs/09・docs/11 を直した」と
    記録してある —— **docs/10 だけ漏れていた。** 手で直して回ると 1 つ漏れる。
    #152 と同じで、**漏れないようにするには仕掛けにする**しかない。
    """

    #: (文書の書き方, 道具のどの定数と合うべきか)
    PATTERNS = [
        (r"1 行 (\d+) 字", "FONT_COLS"),
        (r"刻み (\d+) ドット", "FONT_CELL"),
        (r"(\d+) ドット刻み", "FONT_CELL"),
        (r"1 文字 (\d+)", "FONT_CELL"),
    ]

    #: 実機の書き落としをそのまま書いてしまった形。見つけたら必ず落とす
    MISREADINGS = ["1 行 17"]

    @staticmethod
    def docs() -> list:
        base = os.path.join(REPO, "docs")
        return [os.path.join(base, n) for n in sorted(os.listdir(base)) if n.endswith(".md")]

    def test_every_doc_agrees_with_the_tool(self):
        import re

        import boku2

        want = {"FONT_COLS": boku2.FONT_COLS, "FONT_CELL": boku2.FONT_CELL}
        found, bad = 0, []
        for path in self.docs():
            rel = os.path.relpath(path, REPO)
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    for pat, key in self.PATTERNS:
                        for m in re.finditer(pat, line):
                            found += 1
                            if int(m.group(1)) != want[key]:
                                bad.append(f"{rel}:{i} 「{m.group(0)}」 "
                                           f"({key} は {want[key]}): {line.strip()[:70]}")
        # **見つけた記述が 0 件でも緑になる**ので、数も見る (#134 と同じ形)
        self.assertGreater(found, 8, f"升目の記述が {found} 件しか見つからない (探し方が壊れた)")
        self.assertEqual(bad, [], "文書と道具で升目の数字が違う:\n  " + "\n  ".join(bad))

    def test_the_known_misreading_is_nowhere(self):
        """`asm_notes.txt` の書き落とし (10 進の 17) を、どの文書も書いていないこと."""
        bad = []
        for path in self.docs():
            rel = os.path.relpath(path, REPO)
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    for word in self.MISREADINGS:
                        if word in line:
                            bad.append(f"{rel}:{i} 「{word}」: {line.strip()[:70]}")
        self.assertEqual(bad, [], "実機の書き落とし (0x17 を 10 進の 17 と読んだ形) がある:\n  "
                                  + "\n  ".join(bad))

    def test_the_screen_says_the_same_numbers(self):
        """画面の案内文も同じ数字であること (文書だけ直して画面が古い、を防ぐ)."""
        import re

        import boku2

        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        m = re.search(r"1 行 (\d+) 字、刻み (\d+) ドット", js)
        self.assertTrue(m, "web/app.js の案内文から升目の数字を読めない")
        self.assertEqual((int(m.group(1)), int(m.group(2))),
                         (boku2.FONT_COLS, boku2.FONT_CELL),
                         f"画面の案内文が {m.group(0)} (道具は "
                         f"{boku2.FONT_COLS} 字 / {boku2.FONT_CELL} ドット)")
        # 目盛りの既定値そのものも刻みと同じであること (#99 でここを直した)
        d = re.search(r'mk\("1 文字の幅", "tim2cw", (\d+)\), '
                      r'chIn = mk\("1 文字の高さ", "tim2ch", (\d+)\)', js)
        self.assertTrue(d, "目盛りの既定値を読めない (書き方が変わった)")
        self.assertEqual((int(d.group(1)), int(d.group(2))),
                         (boku2.FONT_CELL, boku2.FONT_CELL),
                         f"目盛りの既定値が {d.group(1)}×{d.group(2)} "
                         f"(刻みは {boku2.FONT_CELL})")

    def test_the_two_handover_docs_do_not_contradict(self):
        """docs/09 の「次の一手」と docs/10 の手順が、升目について食い違わないこと.

        2 つとも社長が読む手順で、**内容が重なっている**。片方だけ直すと、
        読んだほうによって違うことをする。
        """
        import re

        said = {}
        for name in ("09-調査ログと引き継ぎ.md", "10-僕夏2の手順.md"):
            with open(os.path.join(REPO, "docs", name), encoding="utf-8") as fh:
                doc = fh.read()
            block = [ln for ln in doc.split("\n")
                     if "文字の番号を重ねる" in ln or "1 行 23 字" in ln]
            nums = set()
            for line in block:
                nums |= {int(x) for x in re.findall(r"1 行 (\d+) 字", line)}
                nums |= {int(x) for x in re.findall(r"刻み (\d+) ドット", line)}
            said[name] = nums
        for name, nums in said.items():
            self.assertTrue(nums, f"docs/{name} から升目の数字を読めない (書き方が変わった)")
        self.assertEqual(said["09-調査ログと引き継ぎ.md"], said["10-僕夏2の手順.md"],
                         f"docs/09 と docs/10 で升目の言い分が違う: {said}")


class TestThePeriodGuessIsCounted(unittest.TestCase):
    """繰り返しの周期の見当を、正解の分かる 8 通りで数える (#154).

    「タイル」タブの **「見当を付ける」** ボタンは `guessPeriods` の **1 位**を
    1 タイルのバイト数として使っていた。数えたら **1/8**。外し方が 2 つあった。

    1. **周期が無くても 1 位が出る。** 並べ替えれば必ず先頭は決まるので、
       乱数でも「繰り返しの周期が強い順: 96 バイト …」と出ていた。
       候補どうしの差は 1% 程度 —— 並びは中身ではなく誤差だった。
       #148 の「黙って 0 件」や #150 の「当てはめた結果」と同じ形で、
       **根拠が無いのに結論だけ出る。**
    2. **本物の周期があっても、その倍数が同じ点を取る。** 8 バイト周期のデータでは
       16・32・64… も同じだけ合うので、1 位はしばしば 128 や 256 になる。
       それを 1 タイルの大きさとして使うと、**絵が何枚も 1 枚に潰れて**出る。

    直したのは 2 段。**強さ**で「そもそも周期があるか」を見て、あるときは
    **自分の倍数もすべて良い側に入っている最小の刻み**を採る。8/8 になった。
    """

    #: (名前, 正解の刻み。0 は「周期なしと言うのが正解」, 作り方の JS)
    CASES = [
        ("合成タイル 8 バイト", 8, "glyphs(8)"),
        ("合成タイル 16 バイト", 16, "glyphs(16)"),
        ("合成タイル 32 バイト", 32, "glyphs(32)"),
        ("合成タイル 64 バイト", 64, "glyphs(64)"),
        ("合成タイル 128 バイト", 128, "glyphs(128)"),
        ("FONT.BIN (実物)", 32, "new Uint8Array(fs.readFileSync('work/FONT.BIN'))"),
        ("乱数", 0, "noise(32768)"),
        ("ゼロ埋め", 0, "new Uint8Array(32768)"),
    ]

    #: 作り方。**1 タイルごとに絵が違う**のが肝 (同じ絵を並べると、どんな見方でも
    #: 当たってしまって検査にならない)。端に余白があるところだけが共通
    MAKE_JS = (
        "function noise(n,seed){let r=(seed||1)>>>0;const o=new Uint8Array(n);"
        "for(let i=0;i<n;i++){r=(Math.imul(r,1103515245)+12345)>>>0;o[i]=(r>>>16)&0xFF;}"
        "return o;}"
        "function glyphs(per,n){n=n||256;let r=7>>>0;"
        "const rnd=()=>{r=(Math.imul(r,1103515245)+12345)>>>0;return (r>>>16)&0xFF;};"
        "const o=new Uint8Array(per*n);"
        "for(let t=0;t<n;t++)for(let i=0;i<per;i++){"
        "const e=(i<per/8||i>=per-per/8);o[t*per+i]=e?0:rnd();}return o;}"
    )

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/FONT.BIN", "make_sample.py")
        if problem:
            raise unittest.SkipTest(problem)

    @classmethod
    def measure(cls, how: str) -> dict:
        """web/app.js の bestPeriod と guessPeriods をそのまま動かす."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('function guessPeriods');"
            "const e=s.indexOf('/* ======================= 4. 構造の推定');"
            "if(a<0||e<0){console.error('no period funcs');process.exit(2);}"
            "const m=new Function(s.slice(a,e)+"
            "'\\nreturn {guessPeriods,bestPeriod,PERIOD_MIN_STRENGTH};')();"
            + cls.MAKE_JS
            + f"const v={how};"
            "const got=m.bestPeriod(v);const raw=m.guessPeriods(v);"
            "console.log(JSON.stringify({lag:got.lag,strength:got.strength,"
            "raw:raw.length?raw[0].lag:0,cut:m.PERIOD_MIN_STRENGTH}));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("測れない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    def doc_rows(self) -> list:
        """docs/07 の表を (データ, 正解, 強さ, 直す前, 直したあと) で返す."""
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        head = "### 繰り返しの周期の見当"
        self.assertTrue(head in doc, "docs/07 に周期の見当の節が無い")
        body = doc.split(head, 1)[1].split("\n###", 1)[0]
        out = []
        for line in body.split("\n"):
            if not line.startswith("|"):
                if out:
                    break
                continue
            cells = [c.strip().strip("*").replace("`", "") for c in
                     line.strip().strip("|").split("|")]
            if len(cells) != 5 or "---" in cells[1] or cells[0] == "データ":
                continue
            out.append(cells)
        return out

    def test_every_case_is_read_correctly(self):
        wrong = []
        for name, want, how in self.CASES:
            got = self.measure(how)
            if got["lag"] != want:
                wrong.append(f"{name}: 正解 {want} / 見当 {got['lag']} "
                             f"(強さ {got['strength']:.2f})")
        self.assertEqual(wrong, [], "周期の見当が外れている:\n  " + "\n  ".join(wrong))

    def test_the_table_in_the_doc_is_measured(self):
        """docs/07 の表を行ごとに測り直す.

        #154 で壊して確かめていて気づいた —— **表の数字を誰も読んでいなかった**。
        「直したあと」の欄を書き換えても検査が通ってしまう。#147 で学んだのと
        同じことを、書いた当日にやりかけていた。
        """
        rows = self.doc_rows()
        self.assertEqual(len(rows), len(self.CASES),
                         f"docs/07 の表から {len(rows)} 行しか読めない")
        by_name = {c[0]: c for c in self.CASES}
        for name, want, strength, before, after in rows:
            self.assertIn(name, by_name, f"docs/07 の「{name}」に対応する例がこの検査に無い")
            _n, real_want, how = by_name[name]
            said_want = 0 if want == "周期なし" else int(want)
            self.assertEqual(said_want, real_want,
                             f"{name}: docs/07 の正解が {want} (この検査は {real_want})")
            got = self.measure(how)
            self.assertEqual(round(got["strength"], 2), float(strength),
                             f"{name}: 強さが {got['strength']:.2f} (docs/07 は {strength})")
            self.assertEqual(got["raw"], int(before),
                             f"{name}: 1 位が {got['raw']} (docs/07 は {before})")
            said_after = 0 if after == "周期なし" else int(after)
            self.assertEqual(got["lag"], said_after,
                             f"{name}: 見当が {got['lag']} (docs/07 は {after})")

    def test_the_old_way_really_was_worse(self):
        """直す前のやり方 (1 位をそのまま採る) では当たらなかったこと.

        「直して良くなった」と言うには、**直す前が悪かったこと**も測っておく。
        ここが当たるようになったら、直しはもう要らないということ。
        """
        hits = 0
        for _name, want, how in self.CASES:
            got = self.measure(how)
            if want and got["raw"] == want:
                hits += 1
        self.assertLessEqual(hits, 2,
                             f"1 位をそのまま採る昔のやり方が {hits} 件当たる。"
                             f"直しの前提が変わったので #154 の言い分を見直すこと")

    def test_no_period_is_said_plainly_instead_of_ranking_noise(self):
        """周期が無いものには 0 を返すこと (誤差の並びを順位として出さない)."""
        for name, want, how in self.CASES:
            if want:
                continue
            got = self.measure(how)
            self.assertEqual(got["lag"], 0, f"{name} に周期 {got['lag']} を出した")
            self.assertLess(got["strength"], got["cut"],
                            f"{name} の強さが {got['strength']:.3f} (しきい値未満のはず)")

    def test_a_real_period_is_far_above_the_threshold(self):
        """本物の周期は、しきい値すれすれではなくはっきり上にあること.

        すれすれなら、しきい値をいじっただけで答えが変わる = 測れていない。
        """
        weak = []
        for name, want, how in self.CASES:
            if not want:
                continue
            got = self.measure(how)
            if got["strength"] < got["cut"] * 1.5:
                weak.append(f"{name}: 強さ {got['strength']:.2f} (しきい値 {got['cut']})")
        self.assertEqual(weak, [], "しきい値すれすれの例がある:\n  " + "\n  ".join(weak))

    def test_the_screen_says_when_it_cannot_tell(self):
        """画面側が「見当を付けられなかった」と言うようになっていること."""
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        for word in ("はっきりした繰り返しは見つかりませんでした",
                     "はっきりした繰り返しが見つからないので、見当を付けられませんでした"):
            self.assertTrue(word in js, f"web/app.js に「{word}」が無い")


class TestTheKindGuessIsCountedOnKnownFiles(unittest.TestCase):
    """名前の無いファイルへの見当を、**正解の分かる 20 件**で数える (#153).

    `tests/test_sniff.mjs` は前からあったが、見ているのは
    **「書いた規則のとおりに動くか」**だけで、**「その見当が当たっているか」**は
    一度も見ていなかった。#151 と同じ形の穴。

    練習用の `BOKU2SAMPLE` は `make_boku2_sample.py` が組み立てるので、
    **どのファイルが何なのかが分かっている**。20 件で数えたら **11 件**しか
    当たらなかった。外したのは、よりによって探している当のもの:

    - `.msg` 4 件と入れ物 4 件 —— docs/07 が「1951 個の中から**テキストを探す**」
      と書いている当の相手。**入れ物を指す呼び名が一つも無く**、
      全部「不明 (タイル・表など)」に落ちていた。
    - `bk_font.tms` —— 実物のフォントは 0x80 の前置きつき (docs/11) なのに、
      魔法数を**先頭だけ**で見ていたので取りこぼしていた。英語化パッチの
      公開ソースは `findTIM2s` でファイル全体から探している。向こうに倣った。

    直して 20/20。入れ物の判定は**埋まった TIM2 より先**に見る ——
    中に絵が 1 枚あるだけで「画像」と名づけると、
    **同じ入れ物に入っている文章が見えなくなる**。

    正解は**名前の拡張子ではなく、組み立て方**から取る。`diary.bin` の `.bin` は
    種類を言っていない (中身は入れ物)。名前を信じると正解表のほうが間違う。
    """

    #: `make_boku2_sample.py` が `build_map` で組み立てたもの (= 入れ物)
    #: 道筋は実物に合わせてある (#220。公開ソースの UNPACK.py に出てくる道)
    CONTAINERS = {"diary.bin", "fish/img/fish_on_mem.bin",
                  "system/saveload.bin", "data/map/evt/on_mem_event.bin"}
    #: 中身がまるごとゼロのもの (名前は .bin だが「ゼロ埋め」が正しい)
    ALL_ZERO = {"readme.bin", "system/sys_end.bin", "data/data_end.bin"}

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            raise unittest.SkipTest(problem)

    @classmethod
    def entries(cls) -> list:
        """(path, 正解, 先頭 4KB, 長さ) の一覧。先頭 4KB は画面と同じ量."""
        import boku2

        base = os.path.join(REPO, "work", "BOKU2SAMPLE")
        with open(os.path.join(base, "BOKU2.IDX"), "rb") as fh:
            idx = fh.read()
        with open(os.path.join(base, "BOKU2.IMG"), "rb") as fh:
            img = fh.read()
        out = []
        for e in boku2.read_dfi(idx, len(img)):
            path = e["path"]
            if path in cls.CONTAINERS:
                want = "parts"
            elif path in cls.ALL_ZERO:
                want = "zero"
            else:
                ext = path.rsplit(".", 1)[-1]
                want = "tm2" if ext == "tms" else ext
            head = img[e["at"]:e["at"] + min(e["len"], 4096)]
            out.append((path, want, list(head), e["len"]))
        return out

    @staticmethod
    def guess(entries) -> list:
        """web/app.js の sniffKind を、画面と同じ渡し方で動かす."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        payload = [{"head": h, "size": n} for _p, _w, h, n in entries]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(payload, fh)
            tmp = fh.name
        try:
            prog = (
                "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
                "const a=s.indexOf('/* @extract-start sniff */'),"
                "b=s.indexOf('/* @extract-end sniff */');"
                "if(a<0||b<0){console.error('sniff の目印が無い');process.exit(2);}"
                "const m=new Function(s.slice(a,b)+'\\nreturn {sniffKind};')();"
                "const c0=s.indexOf('const SJIS_LEAD'),c1=s.indexOf('function guessPeriods');"
                "const c=new Function(s.slice(c0,c1)+"
                "'\\nreturn {blockStats,classifyStats};')();"
                f"const list=JSON.parse(fs.readFileSync({json.dumps(tmp)},'utf8'));"
                "console.log(JSON.stringify(list.map(e=>{"
                "const v=new Uint8Array(e.head);"
                "return m.sniffKind(v,c.classifyStats(c.blockStats(v)),e.size);})));"
            )
            res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
            if res.returncode != 0:
                raise AssertionError("見当を出せない: " + res.stdout + res.stderr)
            return json.loads(res.stdout)
        finally:
            os.unlink(tmp)

    def test_every_known_file_is_named_correctly(self):
        rows = self.entries()
        self.assertEqual(len(rows), 20, f"練習データが {len(rows)} 件 (題材が変わった)")
        got = self.guess(rows)
        wrong = [f"{p}: 正解 {w} / 見当 {g['ext']}"
                 for (p, w, _h, _n), g in zip(rows, got) if g["ext"] != w]
        self.assertEqual(wrong, [], "見当が外れている:\n  " + "\n  ".join(wrong))

    def test_the_text_containers_are_findable_by_name(self):
        """要点。**テキストを探している人が、絞り込み欄で見つけられること**.

        docs/07 は「絞り込み欄に `msg` と入れると、それだけが並びます」と書いて
        いるのに、名前の無いファイルには `msg` が一つも付かなかった。
        """
        rows = self.entries()
        got = self.guess(rows)
        named = [p for (p, _w, _h, _n), g in zip(rows, got) if g["ext"] == "msg"]
        self.assertEqual(len(named), 4, f"msg と名づけたのが {len(named)} 件: {named}")
        for path in named:
            self.assertTrue(path.endswith(".msg"), f"{path} を msg と名づけた (中身が違う)")

    def test_a_box_holding_one_picture_is_not_called_a_picture(self):
        """`diary.bin` は中に TIM2 を 1 枚抱えた入れ物。画像と名づけないこと.

        ここを取り違えると、**同じ入れ物に入っている日記の文章が見えなくなる**。
        """
        rows = [r for r in self.entries() if r[0] == "diary.bin"]
        self.assertEqual(len(rows), 1, "diary.bin が見つからない (題材が変わった)")
        self.assertTrue(bytes(rows[0][2]).find(b"TIM2") > 0,
                        "diary.bin に TIM2 が入っていない (この検査の前提が崩れた)")
        self.assertEqual(self.guess(rows)[0]["ext"], "parts",
                         "中に絵が 1 枚あるだけで画像と名づけている")

    def test_the_guess_is_never_marked_certain(self):
        """魔法数で決まったものだけが `sure`。見当に確定の顔をさせない."""
        rows = self.entries()
        got = self.guess(rows)
        for (path, _w, _h, _n), g in zip(rows, got):
            if g["sure"]:
                self.assertTrue(path.endswith(".tm2"),
                                f"{path} を確定扱いにした ({g['label']})")

    def test_the_new_rules_do_not_fire_on_other_data(self):
        """入れ物の判定が、関係ないデータに付かないこと (緩めた代償を測る)."""
        problem = ensure_practice("work/RINFOLT.iso", "make_iso.py")
        if problem:
            self.skipTest(problem)
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('/* @extract-start sniff */'),"
            "b=s.indexOf('/* @extract-end sniff */');"
            "const m=new Function(s.slice(a,b)+'\\nreturn {looksLikeParts};')();"
            "const iso=new Uint8Array(fs.readFileSync('work/RINFOLT.iso'));"
            "let tries=0,hits=0;"
            "for(let o=0;o+4096<=iso.length;o+=512){tries++;"
            "if(m.looksLikeParts(iso.subarray(o,o+4096),4096))hits++;}"
            "let r=12345>>>0;const rnd=new Uint8Array(4096*200);"
            "for(let i=0;i<rnd.length;i++){r=(Math.imul(r,1103515245)+12345)>>>0;"
            "rnd[i]=(r>>>16)&0xFF;}"
            "let rh=0,rt=0;for(let o=0;o+4096<=rnd.length;o+=4096){rt++;"
            "if(m.looksLikeParts(rnd.subarray(o,o+4096),4096))rh++;}"
            "console.log(JSON.stringify({tries,hits,rt,rh}));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, "測れない: " + res.stdout + res.stderr)
        got = json.loads(res.stdout)
        self.assertGreater(got["tries"], 300, "走査した窓が少なすぎる (前提が崩れた)")
        self.assertEqual(got["hits"], 0,
                         f"ISO の {got['tries']} 窓のうち {got['hits']} 窓を入れ物と見た")
        self.assertEqual(got["rh"], 0,
                         f"乱数の {got['rt']} 窓のうち {got['rh']} 窓を入れ物と見た")


class TestTheBitDepthGuessIsCounted(unittest.TestCase):
    """1 ドットのビット数の見当が、何通り当たるかを数える (#151).

    docs/07 は「外れることもあるので」とだけ書いて、**一度も数えていなかった**。
    数えたら 6 通り中 2 通りしか当たっていなかった。しかも外れ方が悪い。

    - **背景が 0 で埋まった 8bpp のスプライト** (市販ゲームでいちばん多い形) が
      平坦 85% で **1bpp** と判定され、1 辺まで倍に外れていた (16×16 → 32×32)。
      「`0x00` と `0xFF` が 3 割超なら 1bpp」しか見ていなかったため。
    - 疎なドット絵 (本物の 1bpp) は平坦 25% で条件を外し、色数 3 種から 4bpp に。

    分かれ目は平坦さではなく**隣のバイトとの跳ね方**だった。1bpp はビットの模様
    なので跳ねる (57.7 / 28.5)、絵は隣どうし似る (16.4 / 13.9 / 1.7 / 1.0)。
    間がはっきり空いているので、そこを条件に足して 4/6 まで戻した。

    **残る 2 つは直さない。** 4bpp を 2 画素ずつ詰めるとバイトの値は 256 種まで
    広がるので、色数では 8bpp と区別できない。逆も同じ。
    **バイトを見ただけでは決められない**ことを文書に書き、画面では見当だと
    分かるように出した。ここでは「当たりが 4/6 のまま」を見張る。
    """

    #: (名前, 1 タイルのバイト数, 正解の bpp, その絵を作る JS)
    CASES = [
        ("`FONT.BIN` (実物のフォント)", 32, 1,
         "new Uint8Array(fs.readFileSync('work/FONT.BIN'))"),
        ("疎なドット絵", 32, 1,
         "(()=>{const p=new Uint8Array(8192);for(let i=0;i<p.length;i++){"
         "const y=(i/2)|0;p[i]=(y%16<2||y%16>13)?0x00:((i&1)?0x3C:0x18);}return p;})()"),
        ("背景が 0 のスプライト", 256, 8,
         "(()=>{const p=new Uint8Array(8192);for(let i=0;i<p.length;i++){"
         "const x=i%64,y=(i/64)|0;p[i]=((x-32)**2+(y-32)**2<400)?"
         "(40+((x*3+y*5)&0x7F)):0;}return p;})()"),
        ("色数の多い絵", 256, 8,
         "(()=>{const p=new Uint8Array(8192);for(let i=0;i<p.length;i++)"
         "p[i]=(i*7+((i/64)|0)*13)&0xFF;return p;})()"),
        ("16 色の絵 (2 画素/バイト)", 128, 4,
         "(()=>{const p=new Uint8Array(8192);for(let i=0;i<p.length;i++){"
         "const x=i%32,y=(i/32)|0;p[i]=((((x+y)>>1)&15)<<4)|(((x*2+y)>>1)&15);}return p;})()"),
        ("32 色しか使っていない絵", 256, 8,
         "(()=>{const p=new Uint8Array(8192);for(let i=0;i<p.length;i++)"
         "p[i]=32+(((i%64)>>1)&31);return p;})()"),
    ]

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/FONT.BIN", "make_sample.py")
        if problem:
            raise unittest.SkipTest(problem)

    @staticmethod
    def measure(source: str, bytes_per_tile: int) -> dict:
        """web/app.js の guessTileShape をそのまま動かし、隣の平均差も返す."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const BPP1_JUMP'),e=s.indexOf('function refineRegion');"
            "if(a<0||e<0){console.error('no shape funcs');process.exit(2);}"
            "const m=new Function(s.slice(a,e)+'\\nreturn {guessTileShape};')();"
            f"const v={source};"
            f"console.log(JSON.stringify(m.guessTileShape(v,0,v.length,{bytes_per_tile})));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("見当を出せない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    def doc_table(self, heading: str, cols: int) -> list:
        """見出しの直後にある最初の表を返す (見出しの形で切る。#149 の教訓)."""
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        self.assertTrue(heading in doc, f"docs/07 に「{heading}」が無い")
        body = doc.split(heading, 1)[1].split("\n#", 1)[0]
        out = []
        for line in body.split("\n"):
            if not line.startswith("|"):
                if out:
                    break            # 表が終わったら、その先の別の表は読まない
                continue
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if len(cells) != cols or "---" in cells[1] or cells[0] == "絵":
                continue
            out.append(cells)
        return out

    def test_the_hit_rate_is_what_the_doc_says(self):
        """当たり外れの表を、行ごとに測り直す."""
        import re

        rows = self.doc_table("### 1 ドットのビット数の見当", 3)
        self.assertEqual(len(rows), 6, f"当たり外れの表から {len(rows)} 行しか読めない")
        by_name = {c[0]: c for c in self.CASES}
        hits = 0
        for name, want, said in rows:
            self.assertIn(name, by_name, f"docs/07 の「{name}」に対応する絵がこの検査に無い")
            _n, per_tile, real_bpp, src = by_name[name]
            self.assertEqual(f"{real_bpp}bpp", want,
                             f"{name}: docs/07 の正解が {want} (この検査は {real_bpp}bpp)")
            got = self.measure(src, per_tile)
            self.assertEqual(f"{got['bpp']}bpp", said,
                             f"{name}: 見当が {got['bpp']}bpp (docs/07 は {said})")
            if got["bpp"] == real_bpp:
                hits += 1
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        m = re.search(r"6 通りの絵で数えると (\d+) 通りしか当たりません", doc)
        self.assertTrue(m, "docs/07 が当たりの数を書いていない")
        self.assertEqual(hits, int(m.group(1)),
                         f"当たりが {hits} 通り (docs/07 は {m.group(1)} 通り)")

    def test_the_jump_numbers_in_the_doc_are_measured(self):
        """隣のバイトとの平均差の表。1bpp とそれ以外の間が空いていること."""
        rows = self.doc_table("#### 1bpp かどうかは、平坦さだけでは決められない", 2)
        self.assertEqual(len(rows), 6, f"跳ねの表から {len(rows)} 行しか読めない")
        by_name = {c[0]: c for c in self.CASES}
        ones, others = [], []
        for label, said in rows:
            # 2 つの表の見出しは**同じ言葉**に揃えてある。揃っていなければ落とす
            self.assertIn(label, by_name, f"「{label}」に対応する絵がこの検査に無い")
            _n, per_tile, real_bpp, src = by_name[label]
            got = self.measure(src, per_tile)
            self.assertEqual(round(got["meanJump"], 1), float(said),
                             f"{label}: 隣の平均差が {got['meanJump']:.1f} (docs/07 は {said})")
            (ones if real_bpp == 1 else others).append(got["meanJump"])
        self.assertGreater(min(ones), max(others),
                           f"1bpp {ones} とそれ以外 {others} が重なった。"
                           f"跳ねで分ける言い分が崩れたので docs/07 を書き直すこと")

    def test_a_zero_filled_sprite_is_no_longer_called_one_bit(self):
        """直した中心。背景で埋まった 8bpp が 1bpp にならず、1 辺も合うこと."""
        _n, per_tile, _bpp, src = [c for c in self.CASES if c[0] == "背景が 0 のスプライト"][0]
        got = self.measure(src, per_tile)
        self.assertEqual(got["bpp"], 8, "背景が 0 のスプライトがまた 1bpp になった")
        self.assertEqual((got["tw"], got["th"]), (16, 16),
                         f"1 辺が {got['tw']}×{got['th']} (256 バイト / 8bpp なら 16×16)")

    def test_the_doc_admits_four_and_eight_cannot_be_told_apart(self):
        """直さないと決めたことを、文書が言っていること (黙って外すのが最悪)."""
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        # assertIn は落ちたとき docs/07 を丸ごと吐く (#129・#136 で 2 度踏んだ)。
        # #151 でまた踏んだので、この形は使わない
        for word in ("バイトを見ただけでは、この 2 つは分けきれません",
                     "押せば「タイル」タブで手で切り替えられます"):
            self.assertTrue(word in doc, f"docs/07 に「{word}」が無い")


class TestPointerEvidenceIsNotCircular(unittest.TestCase):
    """ポインタ表の根拠が、当てはめた結果を数えていないこと (#150).

    docs/07 が「このツールでいちばん価値がある」と書いている機能の採点。
    根拠は 2 つあると書いてあり、そのひとつが **「表の直後から本文が始まっている」**。
    ところが基準の候補には**「1 件目がちょうど表の直後を指すような値」**が
    入っている。それが選ばれた候補では、ずれは必ず 0 ——
    **当てはめた結果を証拠として数えていた**。

    練習用イメージで数えたら、直す前は 40 件のうち 39 件にこの印が付き、
    そのうち 38 件が当てはめたものだった。**ほぼ全部に付く印は印ではない。**
    決定的なのは「ずれが 1〜64 の候補が 1 件も無い」こと。
    合わせていないのに惜しい、が一度も起きていない = ずれ 0 は作られた 0。

    直したこと: 合わせた基準では根拠に数えず注記に回し、代わりに
    **基準がセクタ境界 (2048 の倍数、0 は除く)** を根拠に足した。
    当てはめと独立なので印になる (40 件中 3 件)。
    確度「高」はちょうど 2 件のまま (#145) で、中身は
    「区切りバイト + セクタ境界」という**独立した 2 つ**に変わった。
    """

    @staticmethod
    def tables(source: str) -> list:
        """web/app.js の findPointerTables をそのまま動かす.

        `source` は JS の式で、`Uint8Array` を返すもの。
        """
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const u32le');"
            "const e=s.indexOf('/* ======================= タイル領域の自動検出');"
            "if(a<0||e<0){console.error('no pointer funcs');process.exit(2);}"
            "let src=s.slice(a,e).replace("
            "/\\/\\*\\* 範囲を絞って文字列を集める \\*\\/[\\s\\S]*?\\n}\\n/,'');"
            "const m=new Function('state',src+'\\nreturn {findPointerTables};')({});"
            f"const v={source};"
            "console.log(JSON.stringify(m.findPointerTables(v,v.length).map("
            "c=>({off:c.off,base:c.base,count:c.count,gap:c.gap,tableEnd:c.tableEnd,"
            "confidence:c.confidence,evidence:c.evidence,notes:c.notes}))));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("走らせられない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    #: docs/07 と同じ合成音 (make_iso.py の pseudo_wave と同じ式)
    WAVE_JS = ("(()=>{const w=new Uint8Array(48*1024);for(let i=0;i<w.length;i++){"
               "const v=Math.sin(i/23.0)*0.6+Math.sin(i/331.0)*0.4;"
               "w[i]=Math.trunc((v+1)*127.5)&0xFF;}return w;})()")
    ISO_JS = "new Uint8Array(fs.readFileSync('work/RINFOLT.iso'))"

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/RINFOLT.iso", "make_iso.py")
        if problem:
            raise unittest.SkipTest(problem)

    def section(self) -> str:
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        head = "### 当てはめた結果は証拠にならない"
        self.assertTrue(head in doc, "docs/07 に「当てはめた結果は証拠にならない」の節が無い")
        return doc.split(head, 1)[1].split("\n###", 1)[0]

    def doc_counts(self) -> dict:
        """節の表を {説明の一部: 件数} で返す."""
        import re

        out = {}
        for line in self.section().split("\n"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[1] and "---" not in cells[1]:
                m = re.search(r"\*?\*?(\d+)\*?\*?$", cells[1])
                if m:
                    out[cells[0].replace("*", "")] = int(m.group(1))
        return out

    def test_the_gap_is_never_merely_close(self):
        """要の一手。ずれが 1〜64 の候補が 1 件も無いこと.

        1 件でもあれば「ずれ 0 は当てはめて作った 0」という言い分が弱まる。
        逆にここが 0 のままなら、ずれという印は**当てはめの副産物**でしかない。
        """
        got = self.tables(self.ISO_JS)
        between = [c for c in got if 1 <= c["gap"] <= 64]
        self.assertEqual(between, [],
                         f"ずれが 1〜64 の候補が {len(between)} 件出た。"
                         f"docs/07 の表 (0 件) を書き直し、言い分も見直すこと")

    def test_a_fitted_base_never_earns_the_gap_evidence(self):
        """基準を当てはめた候補は、根拠ではなく注記になること."""
        got = self.tables(self.ISO_JS)
        fitted = [c for c in got if c["base"] not in (0, c["tableEnd"])]
        self.assertTrue(fitted, "当てはめた基準の候補が 1 件も無い (題材が変わった)")
        for c in fitted:
            self.assertEqual(c["gap"], 0, f"当てはめたのにずれが {c['gap']} (作りが変わった)")
            self.assertNotIn("表の直後から本文", c["evidence"],
                             f"0x{c['off']:x}: 当てはめた結果を根拠に数えている")
            self.assertTrue(any("当てはめた結果" in n for n in c["notes"]),
                            f"0x{c['off']:x}: 注記に残していない ({c['notes']})")

    def test_the_counts_in_the_doc_are_measured(self):
        got = self.tables(self.ISO_JS)
        said = self.doc_counts()
        self.assertEqual(len(said), 4, f"docs/07 の表から {len(said)} 行しか読めない")
        real = {
            "候補の総数": len(got),
            "ずれが 64 以下 (「表の直後から本文」の条件)": len([c for c in got if c["gap"] <= 64]),
            "そのうち、基準をそこに合わせた候補 (ずれは必ず 0)":
                len([c for c in got if c["gap"] <= 64 and c["base"] not in (0, c["tableEnd"])]),
            "ずれが 1〜64 の候補 (合わせていないのに惜しい)":
                len([c for c in got if 1 <= c["gap"] <= 64]),
        }
        self.assertEqual(said, real, "docs/07 の表と実測が違う")

    def test_the_sector_rule_is_the_discriminating_one(self):
        """セクタ境界が「40 件中 3 件」であること (効く印は、めったに付かない)."""
        import re

        got = self.tables(self.ISO_JS)
        aligned = [c for c in got if c["base"] and c["base"] % 2048 == 0]
        m = re.search(r"これが付くのは \*\*(\d+) 件\*\*だけです", self.section())
        self.assertTrue(m, "docs/07 がセクタ境界の件数を書いていない")
        self.assertEqual(len(aligned), int(m.group(1)),
                         f"セクタ境界が {len(aligned)} 件 (docs/07 は {m.group(1)} 件)")
        for c in aligned:
            self.assertIn("基準がセクタ境界", c["evidence"],
                          f"0x{c['off']:x}: セクタ境界なのに根拠に入っていない")

    def test_the_two_real_tables_stand_on_independent_evidence(self):
        """確度「高」は 2 件のまま、中身が独立した 2 つに変わったこと."""
        got = self.tables(self.ISO_JS)
        high = [c for c in got if c["confidence"] == "high"]
        self.assertEqual(len(high), 2, f"確度「高」が {len(high)} 件 (#145 の主張が崩れた)")
        for c in high:
            self.assertEqual(sorted(c["evidence"]),
                             sorted(["行き先の直前が区切りバイト", "基準がセクタ境界"]),
                             f"0x{c['off']:x} の根拠が {c['evidence']}")
            # 先頭の件数フィールドを 1 件目として飲み込んでいないこと (#150 の直し)
            self.assertEqual(c["count"], 39,
                             f"0x{c['off']:x} の件数が {c['count']} (39 件のはず)。"
                             f"件数だけで選ぶと header を 1 件多く飲み込む")

    def test_wave_data_never_gets_the_separator_evidence(self):
        """docs/07 の「波形は 1 番目の根拠を満たさない」を測る."""
        import re

        got = self.tables(self.WAVE_JS)
        sep = [c for c in got if "行き先の直前が区切りバイト" in c["evidence"]]
        self.assertEqual(sep, [], f"波形に区切りバイトの根拠が {len(sep)} 件付いた")
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        m = re.search(r"合成した音 48KB を読ませると候補は (\d+) 件出ます", doc)
        self.assertTrue(m, "docs/07 が波形の候補数を書いていない")
        self.assertEqual(len(got), int(m.group(1)),
                         f"波形の候補が {len(got)} 件 (docs/07 は {m.group(1)} 件)")


class TestTheMapLegendMatchesTheClassifier(unittest.TestCase):
    """docs/07 の色の凡例が、実際に動く `classifyStats` と同じことを言うか (#149).

    素人が構造探査台でいちばん最初に読む表なのに、**書いてある規則と順番が
    どちらも実物と違っていた**。

    - 順番: 文書は 灰・緑・水・藍・橙・桃 の順。実物は 灰・水・緑・橙・桃・**藍**。
      **藍は最後の受け皿**で、独自の条件を持たない。文書は 4 番目に置いて
      「上のどれでもなく、エントロピーが低い」と書いていたので、
      **波形も圧縮も藍に入る**ように読めた。
    - 橙: 文書は「隣のバイトとの平均差が小さい」だけ。実物は
      **エントロピーが 4.5 を超え、かつ**平均差が 24 未満。
      片側だけだと、ゼロに近い平べったいデータまで橙になる説明になる。
    - 桃: 実物には「標本が 192 バイト以上」がある。短い区画は桃にならない。

    書き写しでは追いつかないので、**行ごとに合成データを作って
    `classifyStats` に通し、その色になることを確かめる**。
    """

    #: 文書の色と、classifyStats の返り値の対応
    COLORS = {"灰": "zero", "水": "jp", "緑": "ascii",
              "橙": "wave", "桃": "high", "藍": "tile"}

    @staticmethod
    def make(kind: str) -> list:
        """その色になるはずの区画を作る。**素直な作り方だけを使う** (細工しない)."""
        import math

        if kind == "zero":
            return [0] * 500 + [1, 2, 3, 4]
        if kind == "ascii":
            return [0x41 + (i % 26) for i in range(512)]
        if kind == "jp":
            out = []
            for i in range(256):
                out += [0x82, 0xA0 + (i % 40)]      # ひらがなの範囲
            return out
        if kind == "wave":
            # 隣との差が小さく、値の種類は多い (エントロピーは高い)
            return [math.floor((math.sin(i / 40) + 1) * 127.5 + 0.5) & 0xFF
                    for i in range(2048)]
        if kind == "high":
            r, out = 12345, []
            for _ in range(2048):
                r = (r * 1103515245 + 12345) & 0xFFFFFFFF
                out.append((r >> 16) & 0xFF)
            return out
        if kind == "tile":
            # 輪郭のあるドット絵風。どの条件にも当てはまらない
            return [(0xFF if ((i >> 3) ^ (i >> 6)) & 1 else 0x00) for i in range(2048)]
        raise AssertionError(kind)

    @staticmethod
    def look(byte_list) -> dict:
        """分類と、そのとき各条件が立っていたかを返す.

        条件まで返すのは、**順番の検査が前提ごと崩れていないか**を見るため。
        「橙が先だから橙になった」と言うには、桃の条件も立っている必要がある。
        """
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const SJIS_LEAD'),e=s.indexOf('function guessPeriods');"
            "if(a<0||e<0){console.error('no funcs');process.exit(2);}"
            "const m=new Function(s.slice(a,e)+'\\nreturn {blockStats,classifyStats};')();"
            f"const v=new Uint8Array({json.dumps(byte_list)});"
            "const st=m.blockStats(v);const exp=8-255/(2*st.n*Math.LN2);"
            "console.log(JSON.stringify({cls:m.classifyStats(st),n:st.n,"
            "wave:st.entropy>4.5&&st.meanDiff<24,"
            "high:st.n>=192&&st.entropy>exp-0.3}));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("分類できない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    @classmethod
    def classify(cls, byte_list) -> str:
        return cls.look(byte_list)["cls"]

    def rows(self) -> list:
        """凡例を (順番, 色, 説明) で返す."""
        import re

        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            body = fh.read().split("## 全体マップの読み方", 1)[1].split("\n###", 1)[0]
        out = []
        for line in body.split("\n"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 4 and re.fullmatch(r"\d+", cells[0]):
                out.append((int(cells[0]), cells[1], cells[3]))
        return out

    def test_every_colour_in_the_legend_really_comes_out(self):
        """行ごとに区画を作って通す。書いた色にならなければ凡例が嘘."""
        rows = self.rows()
        self.assertEqual(len(rows), 6, f"凡例から {len(rows)} 行しか読めない")
        for _order, color, _how in rows:
            self.assertIn(color, self.COLORS, f"知らない色「{color}」")
            got = self.classify(self.make(self.COLORS[color]))
            self.assertEqual(got, self.COLORS[color],
                             f"{color} のはずの区画が {got} になった")

    def test_the_order_in_the_table_is_the_order_in_the_code(self):
        """順番も凡例のとおりであること。**藍が最後**が要点."""
        rows = self.rows()
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4, 5, 6], "順の欄が連番でない")
        self.assertEqual(rows[-1][1], "藍",
                         f"凡例の最後が「{rows[-1][1]}」。藍は最後の受け皿なので最後に置く")
        self.assertIn("上のどれにも当てはまらなかった", rows[-1][2],
                      "藍に独自の条件が書いてある (実物は受け皿で、条件を持たない)")

    def test_wave_wins_over_high_because_it_is_checked_first(self):
        """橙が桃より先、という順番が本当に効くこと.

        エントロピーは乱数並みなのに隣との差が小さい区画を作る。
        順番が入れ替われば桃になるので、この 1 件で順番を押さえられる。
        """
        import math

        smooth = [math.floor((math.sin(i / 9.0) + 1) * 127.5 + 0.5) & 0xFF
                  for i in range(4096)]
        got = self.look(smooth)
        # 前提: 橙と桃の**両方**が立っていること。片方しか立っていなければ
        # 「順番のおかげ」ではなく、ただ条件を満たしただけになる (素通しの検査)
        self.assertTrue(got["wave"] and got["high"],
                        f"この区画は橙と桃の両方には当てはまっていない ({got})。"
                        f"順番を確かめたことにならないので、作る区画を直すこと")
        self.assertEqual(got["cls"], "wave",
                         f"橙と桃の両方に当てはまる区画が {got['cls']} になった。"
                         f"橙より桃が先に見られている (凡例の順番を書き直すこと)")

    def test_a_short_block_is_never_pink(self):
        """桃には「標本が 192 バイト以上」が要る。短い乱数は桃にならない."""
        r, out = 4242, []
        for _ in range(160):
            r = (r * 1103515245 + 12345) & 0xFFFFFFFF
            out.append((r >> 16) & 0xFF)
        got = self.classify(out)
        self.assertNotEqual(got, "high",
                            "160 バイトの区画が桃になった (標本数の条件が消えている)")
        rows = self.rows()
        pink = [r for r in rows if r[1] == "桃"]
        self.assertEqual(len(pink), 1, "凡例に桃の行が 1 本無い")
        self.assertIn("192", pink[0][2], "凡例が桃の標本数の条件を書いていない")

    def test_the_practice_image_really_shows_all_six(self):
        """「6 種類がそのまま帯になって見えます」と、添えた内訳を確かめる."""
        import json
        import re
        import shutil
        import subprocess

        problem = ensure_practice("work/RINFOLT.iso", "make_iso.py")
        if problem:
            self.skipTest(problem)
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const SJIS_LEAD'),e=s.indexOf('function guessPeriods');"
            "const m=new Function(s.slice(a,e)+'\\nreturn {blockStats,classifyStats};')();"
            "const C=parseInt(/const MAP_CELLS = (\\d+)/.exec(s)[1],10);"
            "const b=new Uint8Array(fs.readFileSync('work/RINFOLT.iso'));"
            "const bs=Math.max(64,Math.ceil(b.length/C/64)*64);"
            "const n=Math.max(1,Math.ceil(b.length/bs)),sl=Math.max(256,bs);const t={};"
            "for(let i=0;i<n;i++){const o=i*bs;"
            "const c=m.classifyStats(m.blockStats(b.subarray(o,Math.min(b.length,o+sl))));"
            "t[c]=(t[c]||0)+1;}console.log(JSON.stringify({n,t}));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, "数えられない: " + res.stdout + res.stderr)
        got = json.loads(res.stdout)
        self.assertEqual(sorted(got["t"]), sorted(self.COLORS.values()),
                         f"6 種類そろっていない: {got['t']}")
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        m = re.search(r"全 ([\d,]+) 区画の内訳: ([^)]+)\)", doc)
        self.assertTrue(m, "docs/07 に内訳が書いていない (数字だけの主張は確かめられない)")
        self.assertEqual(int(m.group(1).replace(",", "")), got["n"],
                         f"区画の数が {got['n']} (docs/07 は {m.group(1)})")
        for part in m.group(2).split("・"):
            color, said = part.split()
            self.assertEqual(got["t"].get(self.COLORS[color], 0), int(said),
                             f"{color} が {got['t'].get(self.COLORS[color], 0)} 区画 "
                             f"(docs/07 は {said})")


class TestHowManyFalsePositivesSurvive(unittest.TestCase):
    """docs/07 の「誤検出について」の数字を測る (#148).

    節はこう書いていた —— 「このツールでは次の **2 段**で減らしています」
    (U+FFFD を含む並びを捨てる / よく使う範囲が 60% 未満なら捨てる)、
    「それでも 96KB の乱数から**数百件**は残ります」。測ったら 3 つとも外れていた。

    1. **段の数が違う。** 文字列側の条件は 4 つあり、その前に
       「圧縮・乱数・波形と分類された区画は走査しない」がある。
    2. **名指しした 2 つがほとんど効いていない。** 乱数に対して U+FFFD は
       20 件、よく使う範囲 60% は **1 件**しか落としていない。効いているのは
       節が書いていなかった**仮名の割合** (208 件)。
    3. **数が違う。** 既定の最低文字数 6 では**およそ 1,100 件**残る。
       「数百件」になるのは最低文字数を 8 にしたとき。

    そして画面のほうがもっと大事だった。乱数は区画ごと飛ばされるので
    **実際には 0 件**なのに、その跡がどこにも出ず「該当する文字列はありません。」
    とだけ出ていた (#148 で理由を出すようにした。画面側は tests/e2e/strings.py)。
    """

    #: 乱数の作り方。**書いた数字は作り方ごと残す** (#147 で踏んだ穴)。
    #: 96KB を JSON にして渡すと argv の上限に当たるので、node の側で作る
    NOISE_JS = (
        "function noise(size,seed){let r=seed>>>0;const o=new Uint8Array(size);"
        "for(let i=0;i<size;i++){r=(Math.imul(r,1103515245)+12345)>>>0;o[i]=(r>>>16)&0xFF;}"
        "return o;}"
    )
    NOISE_SIZE = 96 * 1024

    #: 文字列を捨てる条件。app.js の 1 行を、条件を 1 つ抜いた形に差し替える
    FULL = 'text.includes("\\uFFFD") || q.plaus < 0.6 || noKana || tooLatin'
    WITHOUT = {
        "仮名の割合": 'text.includes("\\uFFFD") || q.plaus < 0.6 || tooLatin',
        "U+FFFD": "q.plaus < 0.6 || noKana || tooLatin",
        "よく使う範囲": 'text.includes("\\uFFFD") || noKana || tooLatin',
        "英字の偏り": 'text.includes("\\uFFFD") || q.plaus < 0.6 || noKana',
    }

    @classmethod
    def run_js(cls, body: str, cond, seed: int = 12345) -> object:
        """web/app.js から文字列まわりを切り出して動かす (書き写さない)."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const SJIS_LEAD');"
            "const e=s.indexOf('\\n}\\n',s.indexOf('function scanUtf8'))+3;"
            "if(a<0||e<3){console.error('no string funcs');process.exit(2);}"
            # collectStrings は state を触るので外す (ここでは範囲を絞らずに測る)
            "let src=s.slice(a,e).replace("
            "/\\/\\*\\* 範囲を絞って文字列を集める \\*\\/[\\s\\S]*?\\n}\\n/,'');"
            f"const DROP={json.dumps(cls.FULL)};"
            f"const COND={json.dumps(cond)};"
            "if(!src.includes(DROP)){console.error('捨てる条件が見つからない');process.exit(3);}"
            "if(COND!==null)src=src.replace(DROP,COND);"
            "const m=new Function('state',src+"
            "'\\nreturn {scanStrings,scanUtf8,blockStats,classifyStats,jpQuality};')({});"
            + cls.NOISE_JS
            + f"const v=noise({cls.NOISE_SIZE},{seed});"
            + body
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("測れない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    @classmethod
    def count(cls, cond, min_chars: int = 6, seed: int = 12345) -> int:
        return cls.run_js(
            f"console.log(JSON.stringify(m.scanStrings(v,{min_chars}).length"
            f"+m.scanUtf8(v,{min_chars}).length));", cond, seed)

    def doc(self) -> str:
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            return fh.read().split("## 誤検出について", 1)[1].split("\n## ", 1)[0]

    def test_random_data_is_never_scanned_at_all(self):
        """1 段目。乱数は区画ごと「高エントロピー」になり、走査されない."""
        got = self.run_js(
            "const BS=1024,t={};"
            "for(let o=0;o<v.length;o+=BS)"
            "{const c=m.classifyStats(m.blockStats(v.subarray(o,o+BS)));t[c]=(t[c]||0)+1;}"
            "console.log(JSON.stringify(t));", None)
        self.assertEqual(list(got), ["high"],
                         f"乱数の区画の分類が {got} (「そもそも見ない」が効かなくなった)")
        self.assertEqual(got["high"], 96, f"96 区画のはずが {got['high']}")
        self.assertIn("96 区画すべてが「高エントロピー」", self.doc(),
                      "docs/07 が 1 段目を説明していない")

    def test_each_filter_drops_what_the_doc_says(self):
        """2 段目の表。条件を 1 つ抜いて、その条件だけが落としている件数を測る."""
        import re

        doc = self.doc()
        base = self.count(None)
        for name, without in self.WITHOUT.items():
            alone = self.count(without) - base
            rows = [ln for ln in doc.split("\n")
                    if ln.startswith("|") and name in ln]
            self.assertEqual(len(rows), 1, f"docs/07 に「{name}」の行が {len(rows)} 本")
            said = re.search(r"\*?\*?([\d,]+) 件\*?\*?", rows[0])
            self.assertTrue(said, f"件数を読めない行: {rows[0]}")
            self.assertEqual(alone, int(said.group(1).replace(",", "")),
                             f"「{name}」だけが落とす件数が {alone} 件 "
                             f"(docs/07 は {said.group(1)} 件)")

    def test_the_kana_rule_is_the_one_doing_the_work(self):
        """節の言い分「効いているのは仮名の割合だけ」を支える.

        ここが崩れたら表の並び順ごと書き直すことになるので、別建てで見る。
        """
        base = self.count(None)
        alone = {name: self.count(w) - base for name, w in self.WITHOUT.items()}
        best = max(alone, key=alone.get)
        self.assertEqual(best, "仮名の割合",
                         f"いちばん効いている条件が「{best}」になった。"
                         f"内訳 {alone}。docs/07 の説明を書き直すこと")
        self.assertGreater(alone["仮名の割合"], 5 * max(
            v for k, v in alone.items() if k != "仮名の割合"),
            f"仮名の割合の効きが他と並んだ。内訳 {alone}")

    def test_the_total_matches_the_doc(self):
        """「全部外すと 1,686 件、全部入れておよそ 1,100 件」を測り直す."""
        import re

        doc = self.doc()
        m = re.search(r"4 つ全部を外すと ([\d,]+) 件、全部入れて \*\*およそ ([\d,]+) 件", doc)
        self.assertTrue(m, "docs/07 から合計の主張を読めない (書き方が変わった)")
        said_off = int(m.group(1).replace(",", ""))
        said_on = int(m.group(2).replace(",", ""))
        self.assertEqual(self.count("false"), said_off,
                         f"全部外したときが {self.count('false')} 件 (docs/07 は {said_off})")
        # 「およそ」なので種を変えても持つ幅で見る。1 つの種に貼り付けない
        got = [self.count(None, seed=s) for s in (1, 12345, 20260914)]
        for n in got:
            self.assertLess(abs(n - said_on), said_on * 0.15,
                            f"乱数から残るのが {n} 件 (docs/07 は およそ {said_on} 件)。"
                            f"種を変えた結果は {got}")

    def test_the_doc_no_longer_claims_only_two_stages(self):
        """#148 の直しが戻っていないこと (「次の 2 段で減らしています」の形)."""
        doc = self.doc()
        self.assertNotIn("次の 2 段で\n減らしています", doc,
                         "「2 段」の書き方に戻っている (実際は区画の除外 + 4 つの条件)")
        for word in ("そもそも見ない", "仮名の割合"):
            self.assertTrue(word in doc, f"docs/07 に「{word}」の説明が無い")


class TestShortFilesAreNotCalledZeroFill(unittest.TestCase):
    """ゼロが 1 バイトも無いものを「ほとんどがゼロ埋め」と言わないこと (#113).

    `blockStats` は、末尾のゼロを除いた長さが 16 バイト未満のとき **`zeroRatio` を
    1 と決め打ち**していた。区画のほとんどが詰め物なら実際 1 に近いので気づき
    にくいが、**ファイルそのものが短いとき**は嘘になる。`01 02 03 04` の 4 バイトを
    「ほとんどがゼロ埋め」と言い切っていた。

    #112 で「判定には必ず根拠を出す」ようにしたので、この嘘は
    **「内訳: ゼロ埋め 100%」という証拠つき**で出るようになっていた。
    前の直しが、別の不具合の見た目を強めていた形。
    """

    @staticmethod
    def classify(byte_list) -> tuple:
        """web/app.js の blockStats / classifyStats をそのまま動かす."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('const SJIS_TRAIL'),b=s.indexOf('function guessPeriods');"
            "if(a<0||b<0){console.error('no stats funcs');process.exit(2);}"
            "const m=new Function(s.slice(a,b)+'\\nreturn {blockStats,classifyStats};')();"
            f"const v=new Uint8Array({json.dumps(byte_list)});"
            "const st=m.blockStats(v);"
            "console.log(JSON.stringify([st.zeroRatio, m.classifyStats(st)]));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("blockStats を動かせない: " + res.stdout + res.stderr)
        return tuple(json.loads(res.stdout))

    def test_a_tiny_file_with_no_zeros_is_not_zero_fill(self):
        ratio, cls = self.classify([1, 2, 3, 4])
        self.assertEqual(ratio, 0.0, "ゼロが無いのに zeroRatio が 0 でない")
        self.assertNotEqual(cls, "zero", "4 バイトの非ゼロを「ゼロ埋め」と分類している")

    def test_a_short_ascii_file_is_not_zero_fill(self):
        ratio, cls = self.classify(list(b"CONFIG=1"))
        self.assertEqual(ratio, 0.0)
        self.assertNotEqual(cls, "zero")

    def test_a_padding_block_is_still_zero_fill(self):
        """詰め物の区画 (中身が少しで末尾がゼロだらけ) は今までどおり「ゼロ埋め」."""
        ratio, cls = self.classify(list(range(1, 11)) + [0] * 2038)
        self.assertGreater(ratio, 0.9, f"詰め物のゼロ率が {ratio}")
        self.assertEqual(cls, "zero", "詰め物がゼロ埋めと判定されなくなった")

    def test_all_zeros_is_still_zero_fill(self):
        ratio, cls = self.classify([0] * 64)
        self.assertEqual(ratio, 1.0)
        self.assertEqual(cls, "zero")


class TestTheCorrelationNumbersAreMeasured(unittest.TestCase):
    """docs/07 の「縦の相関」の表を、実際に測り直す (#111).

    docs/07 は練習データの縦の相関を表にしている (フォント 0.59 / 独自コードの
    文章 0.99)。**しきい値 0.82 をまたぐかどうか**の根拠なので、数字がずれると
    「はっきり分かれます」という説明の足場が無くなる。

    #102 と同じ型の穴だが、こちらは**もっと悪い形**で見つかった。同じ測定値が
    `web/app.js` の説明文にも書いてあり、そちらは **0.56 / 0.97** と
    docs/07 (**0.59** / 0.97) と食い違っていた。実際に測ると 0.59 / 0.99 で、
    **両方とも古かった** (docs/07 は片方だけ合っていた)。書き写した数字は必ず腐る。
    """

    @classmethod
    def setUpClass(cls):
        problem = ensure_practice("work/FONT.BIN", "make_sample.py")
        if problem:
            raise unittest.SkipTest(problem)
        for name in ("FONT.BIN", "MSG_ENC.BIN"):
            if not os.path.exists(os.path.join(REPO, "work", name)):
                raise unittest.SkipTest(f"work/{name} がありません")

    @staticmethod
    def measure(name: str) -> float:
        """web/app.js の lagRatio を**そのまま**動かし、全窓での最小を返す.

        道具が使う関数を動かす (書き写さない。#103・#109 で 3 度踏んだ穴)。
        """
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node がありません")
        prog = (
            "const fs=require('fs');const s=fs.readFileSync('web/app.js','utf8');"
            "const a=s.indexOf('function meanGapDiff'),b=s.indexOf('function tileScore');"
            "if(a<0||b<0){console.error('no tile funcs');process.exit(2);}"
            "const m=new Function(s.slice(a,b)+'\\nreturn {lagRatio};')();"
            "const LAGS=[8,16,24,32,64,128],W=4096;"
            f"const buf=new Uint8Array(fs.readFileSync('work/{name}'));"
            "let best=9;"
            "for(let o=0;o+1024<=buf.length;o+=W){const w=buf.subarray(o,Math.min(o+W,buf.length));"
            "for(const l of LAGS){const r=m.lagRatio(w,l);if(r<best)best=r;}}"
            "console.log(JSON.stringify(best));"
        )
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, cwd=REPO)
        if res.returncode != 0:
            raise AssertionError("lagRatio を動かせない: " + res.stdout + res.stderr)
        return json.loads(res.stdout)

    def doc_row(self, needle: str) -> float:
        import re

        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        rows = [ln for ln in doc.split("\n") if ln.startswith("|") and needle in ln]
        self.assertEqual(len(rows), 1, f"docs/07 に「{needle}」の行が {len(rows)} 本")
        got = re.findall(r"\*?\*?(\d\.\d\d)\*?\*?", rows[0])
        self.assertEqual(len(got), 1, f"行から相関を 1 つ読めない: {rows[0]}")
        return float(got[0])

    def test_the_font_correlation_matches_the_doc(self):
        got = round(self.measure("FONT.BIN"), 2)
        self.assertEqual(got, self.doc_row("`FONT.BIN` (1bpp"),
                         f"docs/07 の表と実測が違う (実測 {got})")

    def test_the_text_correlation_matches_the_doc(self):
        got = round(self.measure("MSG_ENC.BIN"), 2)
        self.assertEqual(got, self.doc_row("`MSG_ENC.BIN` (独自"),
                         f"docs/07 の表と実測が違う (実測 {got})")

    def test_the_two_sit_on_opposite_sides_of_the_threshold(self):
        """表の言う「はっきり分かれます」が本当であること (しきい値 0.82 をまたぐ)."""
        import re

        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            m = re.search(r"const TILE_RATIO_MAX = ([\d.]+);", fh.read())
        self.assertTrue(m, "しきい値が読み取れない")
        cut = float(m.group(1))
        self.assertLess(self.measure("FONT.BIN"), cut, "フォントが絵として拾われない")
        self.assertGreater(self.measure("MSG_ENC.BIN"), cut, "文章が絵として拾われてしまう")

    def test_the_same_numbers_in_app_js_agree_with_the_doc(self):
        """画面の説明文にも同じ 2 つが書いてある。2 か所あるので必ず突き合わせる."""
        import re

        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        m = re.search(r"フォントは\s*\*?\s*(\d\.\d\d)、独自文字コードのテキストは (\d\.\d\d)", js)
        self.assertTrue(m, "web/app.js の説明文から 2 つの数字を読めない")
        self.assertEqual(float(m.group(1)), self.doc_row("`FONT.BIN` (1bpp"),
                         "app.js の説明文のフォントの相関が docs/07 と違う")
        self.assertEqual(float(m.group(2)), self.doc_row("`MSG_ENC.BIN` (独自"),
                         "app.js の説明文の文章の相関が docs/07 と違う")


class TestTheNumbersInTheDocAreMeasured(unittest.TestCase):
    """docs/10 が書いている数字を、道具の側から測り直す (#102).

    #101 と同じ型の欠陥を別の面で探した。あちらは**同じ事実が 2 つの文書にある**
    食い違いだったが、こちらは**文書にだけある数字**。docs/10 には
    「練習データなら 18」「形だけで 46/57」「まず疑うマス 12 個」「最低 506 ドット」
    のような、いかにも実測らしい数字が並んでいる。実際どれも当時測った本物だが、
    **測り直す仕掛けがどこにも無い**。定数や生成器を変えた日に、この数字だけが
    もっともらしい顔で残る。社長はそれを目印に「合っているか」を判断するので、
    嘘の目印は道具の不具合より質が悪い。

    ここでは道具から出る値と文書の数字を突き合わせる。ブラウザで測る 46/57 と
    53/57 は tests/e2e/fontdraft.py 側で同じことをする。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "docs", "10-僕夏2の手順.md"), encoding="utf-8") as fh:
            cls.doc = fh.read()
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            cls.ui = fh.read()

    def only(self, pattern: str, where: str = None) -> int:
        """その言い回しがちょうど 1 か所にあることまで確かめてから数字を返す.

        「1 か所だけ」を確かめないと、同じ数字が別の場所にも書いてあるときに
        片方しか直らない (#101 で踏んだのがまさにそれ)。
        """
        import re

        hits = re.findall(pattern, self.doc if where is None else where)
        self.assertEqual(len(hits), 1,
                         f"「{pattern}」に当たる箇所が {len(hits)} 件 (1 件であること)")
        return int(hits[0])

    def test_the_font_width_is_the_two_constants_multiplied(self):
        """最低 506 ドットは 23×22 の答え。列数か刻みを変えたら文書も変わる."""
        need = boku2.FONT_COLS * boku2.FONT_CELL
        import re

        said = re.findall(r"(\d+) ドット(?:要る|に足りません)", self.doc)
        self.assertTrue(said, "docs/10 に最低の幅が書いていない")
        for n in said:
            self.assertEqual(int(n), need,
                             f"docs/10 の最低の幅 {n} が {boku2.FONT_COLS}×{boku2.FONT_CELL}"
                             f" = {need} と違う")

    def test_the_number_of_cells_follows_the_column_count(self):
        """1656 字は 72 行 × 23 列。列数を変えたら書き換える.

        docs/09 の「現在地」にも同じ式が書いてある (こちらは判明した形式の記述で、
        記録欄の履歴ではない)。両方見る。
        """
        want = 72 * boku2.FONT_COLS
        cells = self.only(r"(\d+) 個のマスに")
        self.assertEqual(cells, want,
                         f"docs/10 の {cells} 字が 72 行 × {boku2.FONT_COLS} 列と違う")
        with open(os.path.join(REPO, "docs", "09-調査ログと引き継ぎ.md"),
                  encoding="utf-8") as fh:
            here = fh.read().split("## 最大の発見")[0]   # 現在地だけ。記録欄の履歴は見ない
        self.assertEqual(self.only(r"文字は 72 行 × (\d+) = \d+ 字", here),
                         boku2.FONT_COLS, "docs/09 の現在地の列数が FONT_COLS と違う")
        self.assertEqual(self.only(r"文字は 72 行 × \d+ = (\d+) 字", here),
                         want, f"docs/09 の現在地の字数が {want} と違う")

    def test_the_practice_split_count_is_what_unpack_prints(self):
        """「練習データなら 18」を、実際に切り分けて数え直す."""
        import re
        import subprocess
        import tempfile

        problem = ensure_practice("work/BOKU2SAMPLE/BOKU2.IDX", "make_boku2_sample.py")
        if problem:
            self.skipTest(problem)
        said = self.only(r"練習データなら (\d+)")
        sample = os.path.join(REPO, "work", "BOKU2SAMPLE")
        with tempfile.TemporaryDirectory() as out:
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "boku2.py"), "unpack",
                 os.path.join(sample, "BOKU2.IDX"), os.path.join(sample, "BOKU2.IMG"), out],
                capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        got = re.search(r"(\d+) 個に切り分けました", res.stdout)
        self.assertTrue(got, "unpack が件数を出さなかった:\n" + res.stdout)
        self.assertEqual(int(got.group(1)), said,
                         f"docs/10 は練習データを {said} 個と書いているが、"
                         f"unpack は {got.group(1)} 個と言う")

    def test_the_shaky_cell_count_matches_the_screen(self):
        """「まず疑うマス 12 個」は画面の slice(0, 12) のこと."""
        import re

        said = self.only(r"」が (\d+) 個出ます")
        cut = re.findall(r"\.sort\(\(a, b\) => a\.margin - b\.margin\)\.slice\(0, (\d+)\)", self.ui)
        self.assertEqual(len(cut), 1, f"web/app.js の紛らわしい順の切り出しが {len(cut)} 件")
        self.assertEqual(int(cut[0]), said,
                         f"docs/10 は {said} 個と書いているが、画面が出すのは {cut[0]} 個")


class TestWhatIsConfirmedHasOneAnswer(unittest.TestCase):
    """「実物で何が確かめてあるか」を書く場所を 1 か所に固定する (#101).

    docs/09 は「1951 個に切り分けられた (実物での確認済み)」と書き、docs/10 は
    「この一式は実物のデータで一度も動かしていません」と書いていた。**同じ問いに
    正反対の答えが 2 か所にあった**。どちらも部分的に正しい (切り分けだけは実物を
    通り、その後に作った道具は通っていない) のが余計に悪く、社長は読んだ方を信じる。

    直し方は「両方を正確に書き直す」ではない。2 か所にあれば、次に何かが実物で
    確かめられた日にまた片方だけが古くなる。**docs/09 の表を唯一の置き場にして、
    docs/10 はそこを指すだけ**にした。この検査はその形が崩れていないかを見る。
    """

    SECTION = "## 実物で確かめたこと / まだ確かめていないこと"

    @classmethod
    def setUpClass(cls):
        def read(name):
            with open(os.path.join(REPO, "docs", name), encoding="utf-8") as fh:
                return fh.read()

        cls.log = read("09-調査ログと引き継ぎ.md")
        cls.steps = read("10-僕夏2の手順.md")
        start = cls.log.find(cls.SECTION)
        assert start != -1, f"docs/09 に「{cls.SECTION}」が無い"
        cls.start = start
        end = cls.log.find("\n## ", start + 1)
        # **この節の最初の表だけを見る。** 下に `###` で別の表を足したので
        # (#242 の「実物が届いた日に決まること」)、そこまでで切らないと
        # 関係の無い表の行を「確かめたかどうかが書いていない」と言ってしまう
        sub = cls.log.find("\n### ", start + 1)
        if sub != -1 and (end == -1 or sub < end):
            end = sub
        cls.table = [line for line in cls.log[start:end].splitlines()
                     if line.startswith("|")]

    def test_the_table_marks_exactly_one_thing_as_confirmed(self):
        """確かめた行は 1 行だけ。増えたらこの検査ごと直すこと.

        「ついでに」もう 1 行を確かめた扱いにする書き換えを、素通りさせない。
        実物が届いて本当に増えたなら、ここの数を上げるのが正しい直し方。
        """
        rows = [r for r in self.table if not r.startswith("| ---")][1:]
        self.assertTrue(rows, "表の中身が無い")
        confirmed = [r for r in rows if "**確かめた**" in r]
        self.assertEqual(
            len(confirmed), 1,
            "実物で確かめた行が 1 行ではない:\n" + "\n".join(confirmed))
        self.assertIn("1951", confirmed[0], "確かめた行に根拠の個数が無い")
        self.assertIn("#0 #1", confirmed[0],
                      "当時は名前が付いていなかった但し書きが消えている")
        for row in rows:
            if row is confirmed[0]:
                continue
            self.assertIn("| まだ |", row, f"確かめたかどうかが書いていない行: {row}")

    def test_every_tool_of_the_real_game_is_classified(self):
        """boku2.py に下位コマンドを足したら、この表に載せ忘れない.

        道具が増えるたびに「実物で確かめていない物の一覧」が静かに古くなるのが
        いちばんありがちな腐り方なので、コマンド名は原本から取って突き合わせる。
        """
        import re

        with open(os.path.join(REPO, "tools", "boku2.py"), encoding="utf-8") as fh:
            subcommands = set(re.findall(r'add_parser\("(\w+)"', fh.read()))
        self.assertTrue(subcommands, "boku2.py から下位コマンドを読み取れなかった")
        body = "\n".join(self.table)
        missing = sorted(c for c in subcommands if f"`{c}`" not in body)
        self.assertFalse(
            missing,
            "実物で確かめたかどうかの表に無い boku2.py のコマンド: "
            + ", ".join(missing))

    def test_the_steps_doc_points_here_instead_of_answering_itself(self):
        # assertIn は落ちたとき docs/10 を丸ごと吐く (#96)。自前の文言で出す。
        for needle, why in (("実物で確かめたこと", "docs/09 の表への導線が無い"),
                            ("docs/09", "docs/09 への参照が無い")):
            self.assertTrue(needle in self.steps, f"docs/10: {why}")
        for blanket in ("実物のデータで一度も動かしていません",
                        "実物では一度も動かしていません"):
            self.assertNotIn(
                blanket, self.steps,
                f"docs/10 が独自に言い切っている: {blanket} (docs/09 の表を指すこと)")

    def test_the_answer_is_not_buried_under_the_log(self):
        """記録欄 (1900 行超) の後ろに置かれたら、誰も辿り着かない."""
        self.assertLess(self.start, self.log.find("自走ループの記録欄"),
                        "表が記録欄より後ろにある")


class TestTheSuiteDoesNotDumpWholeFilesOnFailure(unittest.TestCase):
    """検査が落ちたときに、ファイルを丸ごと吐かないこと (#152).

    `assertIn(なにか, doc)` は、落ちると **haystack を丸ごと**メッセージに載せる。
    `doc` が `docs/07` の全文なら、画面は数万字で埋まり、
    **何が足りなかったのかが読めない**。壊して確かめる作業が毎回それで潰れる。

    #129 (docs/10 の全文)・#136 (44KB の文字列集合)・#151 (docs/07 の全文) と
    **3 回踏んだ**。そのたびに踏んだ場所だけ直していたが、#151 で
    「その都度直すだけでは足りない」と書いた。書いたなら仕掛けにする。

    直し方はいつも同じ: `assertTrue(なにか in doc, "自分の言葉")`。
    メッセージを自分で書くので、落ちたときに出るのは 1 行だけになる。

    見つけ方: **`.read()` の結果を受けた変数**を haystack にしている
    `assertIn` / `assertNotIn` を探す。名前で当てにいくと (`doc` など)
    取りこぼすし、短い変数まで巻き込む。**どこから来た値か**で見る。
    """

    #: 見る相手。検査そのものだけでなく、画面の検査も同じ穴を持ちうる
    TARGETS = ["tests/run_tests.py", "tests/e2e"]

    @staticmethod
    def offenders(path: str) -> list:
        """(行, 関数名, haystack の名前) を返す."""
        import ast

        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        out = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            big = set()
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                    continue
                called = node.value.func
                if isinstance(called, ast.Attribute) and called.attr == "read":
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            big.add(target.id)
            if not big:
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("assertIn", "assertNotIn")
                        and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Name)
                        and node.args[1].id in big):
                    out.append((node.lineno, fn.name, node.args[1].id))
        return out

    def files(self) -> list:
        out = []
        for rel in self.TARGETS:
            path = os.path.join(REPO, rel)
            if os.path.isdir(path):
                out += [os.path.join(path, n) for n in sorted(os.listdir(path))
                        if n.endswith(".py")]
            elif os.path.isfile(path):
                out.append(path)
        return out

    def test_no_assertion_uses_a_whole_file_as_its_haystack(self):
        found = []
        for path in self.files():
            for lineno, fn, name in self.offenders(path):
                rel = os.path.relpath(path, REPO)
                found.append(f"{rel}:{lineno} {fn}() が {name} を haystack にしている")
        self.assertEqual(found, [], "落ちるとファイルを丸ごと吐く検査がある。"
                                    "assertTrue(x in y, \"自分の言葉\") に直すこと:\n  "
                                    + "\n  ".join(found))

    def test_the_finder_actually_finds_this_shape(self):
        """見つけ方そのものが働いていること (0 件を見て緑になっていない).

        上の検査は「0 件であること」を見るので、**探し方が壊れても緑**になる。
        わざとその形を書いたファイルを作って、拾えることを確かめる。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "import unittest\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_bad(self):\n"
                    "        with open('x') as fh:\n"
                    "            doc = fh.read()\n"
                    "        self.assertIn('a', doc)\n"
                    "    def test_ok(self):\n"
                    "        with open('x') as fh:\n"
                    "            doc = fh.read()\n"
                    "        self.assertTrue('a' in doc, 'あ')\n")
            got = self.offenders(path)
        self.assertEqual([(g[0], g[1], g[2]) for g in got], [(6, "test_bad", "doc")],
                         f"見つけ方が働いていない: {got}")


class TestTheHeadlessSuiteSaysWhatItSkipped(unittest.TestCase):
    """ヘッドレス側も「何件確かめていないか」を言うこと (#135).

    `tests/e2e/run_all.py` は playwright が無ければ

        skip: playwright がありません (…)

    と 1 行出して**終了コード 0** で終わっていた。件数もどの検査かも言わない。
    #134 で Python 側を直したのと同じ型が、こちらに残っていた。
    しかもこちらは 11 件まるごと (画面の検査が全部) で、割合はもっと悪い。
    """

    def run_without_playwright(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            # playwright を import した瞬間に落ちる偽物を、探索パスの先頭に置く
            with open(os.path.join(tmp, "playwright.py"), "w", encoding="utf-8") as fh:
                fh.write('raise ImportError("playwright は無いことにする")\n')
            env = dict(os.environ, PYTHONPATH=tmp)
            res = subprocess.run(
                [sys.executable, os.path.join(REPO, "tests", "e2e", "run_all.py")],
                capture_output=True, text=True, cwd=REPO, env=env, timeout=120)
        return res

    def test_it_reports_how_many_checks_did_not_run(self):
        res = self.run_without_playwright()
        out = res.stdout + res.stderr
        self.assertNotIn("OK ", out, f"playwright が無いのに検査が走っている:\n{out[:400]}")
        m = re.search(r"飛ばした検査 (\d+) 件", out)
        self.assertTrue(m, f"飛ばした件数を言っていない:\n{out[:400]}")
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "e2e_run_all", os.path.join(REPO, "tests", "e2e", "run_all.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(int(m.group(1)), len(mod.CHECKS),
                         f"報告 {m.group(1)} 件 / 実際にある検査 {len(mod.CHECKS)} 件")
        for name in mod.CHECKS:
            self.assertIn(f"- {name}:", out, f"{name} を飛ばしたと言っていない")

    def test_the_wording_matches_the_python_side(self):
        """2 つの報告で言い方を揃える。読む人が同じものだと分かるように."""
        with open(os.path.join(REPO, "tests", "e2e", "run_all.py"), encoding="utf-8") as fh:
            e2e = fh.read()
        with open(os.path.join(REPO, "tests", "run_tests.py"), encoding="utf-8") as fh:
            py = fh.read()
        phrase = "飛ばした検査 {} 件 (この分は確かめていません)"
        for name, src in (("run_all.py", e2e), ("run_tests.py", py)):
            self.assertTrue(phrase.replace("{}", "{total}") in src
                            or phrase.replace("{}", "{len(CHECKS)}") in src,
                            f"{name} の言い回しが揃っていない")


class TestTheSkipCountIsHonest(unittest.TestCase):
    """飛ばした検査の数が、本当に走らなかった数と合うこと (#134).

    `setUpClass` が飛ぶとクラスの検査は 1 件も走らないのに、unittest は
    skip を **1 件**としか数えない。公開ソースの無い環境で走らせると
    「飛ばした検査 2 件」と出ていたが、実際に走らなかったのは **12 件**だった
    (`Ran 277` が `Ran 266` に減る)。

    しかもその 11 件は `TestAgainstThePublicSource` — 向こうのコードを実際に
    走らせて突き合わせる、**この一式でいちばん強い裏付け**。
    少なく見せると「ほぼ全部確かめた」と読めてしまう。道具に対して
    #123〜#125 で直したのと同じ型が、検査の側に残っていた。
    """

    class Holder:
        def __init__(self, ident):
            self._id = ident

        def id(self):
            return self._id

    def test_a_class_level_skip_counts_every_test_in_the_class(self):
        n = class_test_count("__main__.TestAgainstThePublicSource")
        self.assertGreater(n, 5, f"数え方が壊れている ({n} 件)")
        total, lines = skip_report([(self.Holder("setUpClass (__main__.TestAgainstThePublicSource)"),
                                     "公開ソースが無い")])
        self.assertEqual(total, n, "クラスごと飛んだのに 1 件としか数えていない")
        self.assertIn(f"{n} 件すべて", lines[0], lines[0])

    def test_a_single_skip_still_counts_one(self):
        total, lines = skip_report([(self.Holder("__main__.TestX.test_y"), "理由")])
        self.assertEqual(total, 1, "1 件の skip を数え違えている")
        self.assertIn("TestX.test_y", lines[0], lines[0])

    def test_an_unknown_class_does_not_crash_the_report(self):
        """知らないクラス名でも報告そのものは出ること (数は 1 に倒す)."""
        total, lines = skip_report([(self.Holder("setUpClass (__main__.NoSuchClass)"), "理由")])
        self.assertEqual(total, 1, "知らないクラスで数がおかしくなる")
        self.assertEqual(len(lines), 1, lines)

    def test_the_count_matches_the_tests_that_disappear(self):
        """クラスを飛ばしたときに減る `Ran N` の数と、報告の数が合うこと.

        ここが本題。**実際に走らせて**、減った数と報告の数を突き合わせる。
        """
        import subprocess

        env = dict(os.environ, BOKU2_PUBLIC_SRC="/nonexistent")
        # `-k "A or B"` は unittest では効かず、**0 件走って緑**になる (#108 で踏んだ)。
        # 絞りは 1 つだけにする。下の assertGreater がその見張りも兼ねる
        args = [sys.executable, os.path.join(REPO, "tests", "run_tests.py"),
                "-k", "AgainstThePublicSource"]
        with_src = subprocess.run(args, capture_output=True, text=True, cwd=REPO)
        without = subprocess.run(args, capture_output=True, text=True, cwd=REPO, env=env)
        ran = lambda out: int(re.search(r"^Ran (\d+) tests", out, re.M).group(1))
        a, b = ran(with_src.stdout + with_src.stderr), ran(without.stdout + without.stderr)
        self.assertGreater(a, b, "公開ソースを外しても走る数が減らない (前提が崩れた)")
        said = re.search(r"飛ばした検査 (\d+) 件", without.stdout + without.stderr)
        self.assertTrue(said, "飛ばした件数を報告していない")
        # 消えた分 + 個別に飛んだ分 = 報告の数
        singles = len(re.findall(r"^  - \w+\.\w+:", without.stdout + without.stderr, re.M))
        self.assertEqual(int(said.group(1)), (a - b) + singles,
                         f"報告 {said.group(1)} 件 / 実際に走らなかったのは {(a - b) + singles} 件")


def class_test_count(name: str) -> int:
    """クラス名から、その中の検査の数を返す (分からなければ 1)."""
    cls = getattr(sys.modules[__name__], name.rsplit(".", 1)[-1], None)
    if cls is None or not isinstance(cls, type):
        return 1
    return sum(1 for n in dir(cls) if n.startswith("test"))


def skip_report(skipped) -> tuple[int, list[str]]:
    """飛ばした検査を (本当の件数, 行) にする (#134).

    `setUpClass` が飛ぶと、**そのクラスの検査は 1 件も走らない**のに
    unittest は skip を 1 件としか数えない。公開ソースが無い環境では
    「飛ばした検査 2 件」と出ていたが、実際に走らなかったのは 12 件だった。
    しかもその中身は外部の突き合わせ (#107・#108・#126) — この一式で
    **いちばん強い裏付け**。少なく見せると「ほぼ全部確かめた」と読めてしまう。
    """
    total, lines = 0, []
    for case, why in skipped:
        ident = case.id()
        m = re.match(r"setUpClass \((.+)\)", ident)
        if m:
            n = class_test_count(m.group(1))
            total += n
            lines.append(f"  - {m.group(1).rsplit('.', 1)[-1]} の {n} 件すべて: {why}")
        else:
            total += 1
            lines.append(f"  - {ident.rsplit('.', 2)[-2]}.{ident.rsplit('.', 1)[-1]}: {why}")
    return total, lines


def main() -> int:
    """飛ばした検査を最後にまとめて出す (#82).

    unittest は skip を「OK」の行の括弧に小さく足すだけなので、練習データや node が
    無い環境では**何十件も確かめないまま緑に見える**。何を確かめていないのかは、
    結果と同じくらい大事なので、名前と理由を並べて出す。

    数え方は `skip_report` を見ること。`setUpClass` が飛んだクラスは、
    **中の検査の数だけ**数える (#134)。
    """
    result = unittest.main(verbosity=2, exit=False).result
    if result.skipped:
        total, lines = skip_report(result.skipped)
        print(f"\n飛ばした検査 {total} 件 (この分は確かめていません):")
        print("\n".join(lines))
        print("  練習データが理由なら、先に python3 tools/make_sample.py などを実行してください")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
