#!/usr/bin/env python3
"""ツール一式の自己テスト.

    python3 tests/run_tests.py

外部ライブラリは使いません (Pillow が入っていればフォント生成も試します)。
"""

from __future__ import annotations

import codecs
import json
import os
import struct
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
import boku2  # noqa: F401  (TestBoku2Cli で使う。読み込めることも確認)


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
        """置換文字 (U+FFFD) が生で入っていると公開先に弾かれる。エスケープで書く (#25 で踏んだ)."""
        import glob
        files = glob.glob(os.path.join(self.webdir, "*")) + glob.glob(os.path.join(REPO, "docs", "*.md"))
        for path in files:
            if os.path.isdir(path):
                continue
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("\ufffd", text, f"{os.path.relpath(path, REPO)} に生の置換文字があります")
        self.assertNotIn("\ufffd", self._build(), "組み立てた HTML に置換文字があります")


class TestArchiveFixture(unittest.TestCase):
    """索引ファイル + データ本体の練習用ペアが正しく作られること."""

    @classmethod
    def setUpClass(cls):
        cls.idx = os.path.join(REPO, "work", "PACK.IDX")
        cls.img = os.path.join(REPO, "work", "PACK.IMG")
        if not (os.path.exists(cls.idx) and os.path.exists(cls.img)):
            raise unittest.SkipTest("work/PACK.IDX がありません (make_archive.py を実行)")

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
        if not os.path.exists(os.path.join(REPO, "work", "PACK.IDX")):
            self.skipTest("work/PACK.IDX がありません")
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
        if not os.path.exists(path):
            self.skipTest("work/BOOT.ELF がありません (python3 tools/make_elf.py)")
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
        if not os.path.exists(path):
            self.skipTest("work/BOOT.ELF がありません")
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
            self.assertNotIn(b"\r\n", raw)
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
            n = boku2.unpack(idx_path, img_path, out)
            self.assertEqual(n, 11)
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
            self.assertIn("[入れ物] 文言の入れ物: あり diary.bin, fish_on_mem.bin", res.stdout)
            self.assertIn("見つからない on_mem_event.bin, saveload.bin", res.stdout)
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
            self.assertIn("確認事項 2 件", res.stdout)
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
        arrows = re.findall(r'say\(f?"→ ([^"{]+)', src)
        self.assertGreaterEqual(len(arrows), 8, arrows)
        keys = set()
        for head in arrows:
            key = re.split(r"[。、(:]", head)[0].strip()[:14]      # 行頭の言い回しで照合
            keys.add(key)
            self.assertIn(key, doc, f"docs/10 に説明が無い → の行: {head}")
        # ブラウザの要約の → も、同じ言い回しで、CLI に無いものを増やしていないこと (#64)
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
        js_arrows = re.findall(r'lines\.push\([`"]→ ([^`"$]+)', app)
        self.assertGreaterEqual(len(js_arrows), 6, js_arrows)
        for head in js_arrows:
            key = re.split(r"[。、(:]", head)[0].strip()[:14]
            self.assertIn(key, keys, f"ブラウザだけにある → の行 (CLI と docs/10 に合わせる): {head}")
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

    def test_manual_mentions_the_screen_features(self):
        """画面にある主要な物 (要約の行、目盛りの色、ページ送り、入れ物の入れ子…) が、
        説明書 docs/07 にも書いてあること。画面だけ増えて説明書が古くなるのを防ぐ (#61)."""
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            manual = fh.read()
        with open(os.path.join(REPO, "web", "app.js"), encoding="utf-8") as fh:
            app = fh.read()
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
            self.assertIn(on_screen, app, f"画面側の文言が変わった: {on_screen}")
            self.assertIn(in_manual, manual, f"docs/07 に無い: {in_manual}")

    def test_every_tab_is_documented(self):
        import re
        with open(os.path.join(REPO, "web", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        tabs = re.findall(r'role="tab" data-tab="\w+"[^>]*>([^<]+)<', html)
        self.assertGreaterEqual(len(tabs), 10)
        with open(os.path.join(REPO, "docs", "07-構造探査台.md"), encoding="utf-8") as fh:
            doc = fh.read()
        undocumented = [t for t in tabs if t.replace(" ", "") not in doc.replace(" ", "")]
        self.assertEqual(undocumented, [])

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

    def test_font_table_progress_line(self):
        import io
        import boku2
        import make_boku2_sample
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "S")
            make_boku2_sample.build_sample(folder)
            out = io.StringIO()
            boku2.check(folder, out=out)
            self.assertRegex(out.getvalue(), r"\[文字表\] font\.txt: \d+ 字 / 上の \.msg で使われている番号 \d+ 種のうち文字表に無い 0 種。この範囲は全部読める")
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


class TestDamageDrill(unittest.TestCase):
    """診断の読み方の練習 (make_boku2_sample.py --break …) が、意図した → の行を出すこと."""

    EXPECT = {
        "idx": "DFI でないので",
        "name": "名前が付かないファイルが多い",
        "msg": "読めない .msg の例: system/system.msg",
        "font": "TIM2 として読めません",
        "map": "入れ物として読めないファイルの例",
    }

    def test_each_damage_kind_is_diagnosed(self):
        import io
        import boku2
        import make_boku2_sample
        for kind, want in self.EXPECT.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                folder = os.path.join(tmp, "S")
                make_boku2_sample.build_sample(folder)
                note = make_boku2_sample.damage(folder, kind)
                self.assertTrue(note)
                out = io.StringIO()
                rc = boku2.check(folder, out=out)
                self.assertEqual(rc, 1, f"{kind}: 問題なしになった\n{out.getvalue()}")
                self.assertIn(want, out.getvalue(), kind)
                self.assertIn("→", out.getvalue(), kind)
                # どの壊れ方でも締めの行まで出ること。途中で止める壊れ方 (idx) だけ
                # 締めが無く、道具が落ちたのか診た結果なのか分からなかった (#73)
                self.assertIn("== 結果:", out.getvalue(), f"{kind}: 締めの行が無い\n{out.getvalue()}")
                self.assertIn("この出力ごと報告してください", out.getvalue(), kind)

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


class TestDisassemblerInBrowser(unittest.TestCase):
    """ブラウザ側の逆アセンブラも同じ答えと突き合わせる."""

    def test_browser_decoder_matches(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        script = os.path.join(REPO, "tests", "test_disasm.mjs")
        if not os.path.exists(os.path.join(REPO, "work", "BOOT.ELF")):
            self.skipTest("work/BOOT.ELF がありません")
        res = subprocess.run([node, script], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("OK", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
