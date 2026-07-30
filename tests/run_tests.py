#!/usr/bin/env python3
"""ツール一式の自己テスト.

    python3 tests/run_tests.py

外部ライブラリは使いません (Pillow が入っていればフォント生成も試します)。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.join(REPO, "answers"))

import scrp
import make_sample
import proofread
import relative_search


SOURCE = os.path.join(REPO, "data", "script_source.tsv")


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
        planted = {rid for rid, *_ in plant_errors.PLANTED}
        self.assertEqual(planted - flagged, set(), "見逃している行があります")
        self.assertEqual(flagged - planted, set(), "仕込んでいない行を誤検出しています")


if __name__ == "__main__":
    unittest.main(verbosity=2)
