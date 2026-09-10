"""SCRP アーカイブと文字コードの共通処理.

このモジュールが扱うのは、家庭用ゲームのスクリプトデータでよく見る形の
「ポインタテーブル + 可変長テキストブロック」という構造です。

    +--------------------------------+ 0x00
    | "SCRP"                         |  マジック (4 バイト)
    | version   (u32 LE)             |  0x04
    | count     (u32 LE)             |  0x08  メッセージ数
    | encoding  (u32 LE)             |  0x0C  0=Shift-JIS, 1=独自テーブル
    +--------------------------------+ 0x10
    | pointer[0]   (u32 LE)          |  各メッセージの先頭オフセット
    | pointer[1]                     |  (ファイル先頭からの絶対値)
    | ...                            |
    +--------------------------------+
    | メッセージ本体 (0xFF 終端)      |
    | ...                            |
    +--------------------------------+

u32 はすべてリトルエンディアンです。PS2 の CPU (MIPS R5900) がリトル
エンディアンなので、実機のデータもこの並びになっているのが普通です。
バイナリエディタで 0x2C 0x01 0x00 0x00 と見えたら 0x0000012C = 300 と
読む、という部分を体で覚えるのがここでの目的です。
"""

from __future__ import annotations

import codecs
import io
import re
import struct
import unicodedata


def open_text(path: str) -> io.StringIO:
    """利用者が作った文字ファイルを、保存形式を問わず読む.

    メモ帳・Excel は保存の仕方で中身が変わる:
      UTF-8 (BOM 無し / 有り)     … メモ帳の既定、Excel の「CSV UTF-8」
      UTF-16 (BOM 有り)           … Excel の「Unicode テキスト (*.txt)」(タブ区切り)
      cp932 (Shift-JIS)           … メモ帳の ANSI、Excel の「CSV (コンマ区切り)」
    どれで保存されても同じに読めるよう、BOM を見て、無ければ UTF-8 → cp932 の順に試す。
    改行 (CRLF / CR) は LF に揃える。読んだ結果を文字列の入れ物にして返すので、
    `with open_text(p) as fh:` とファイルと同じに使える."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp932", errors="replace")
    return io.StringIO(text.replace("\r\n", "\n").replace("\r", "\n"))

MAGIC = b"SCRP"
VERSION = 1
HEADER_SIZE = 0x10

ENC_SJIS = 0
ENC_CUSTOM = 1
ENCODING_NAMES = {ENC_SJIS: "sjis", ENC_CUSTOM: "custom"}
ENCODING_IDS = {v: k for k, v in ENCODING_NAMES.items()}

#: メッセージ終端バイト
END = 0xFF

#: 制御コード: バイト値 -> (タグ名, 引数バイト数)
#:
#: 0xF0 以降に置いてあるのは偶然ではありません。Shift-JIS の 2 バイト文字は
#: 先頭バイトが 0x81-0x9F / 0xE0-0xEF なので、そこを避けないと「制御コードか
#: 漢字の先頭バイトか区別できない」データになってしまいます。実際のゲームでも
#: 制御コードは未使用のバイト範囲に押し込まれています。
CONTROL_CODES = {
    0xF0: ("BR", 0),  # 改行
    0xF1: ("NAME", 1),  # 話者名テーブルの参照
    0xF2: ("WAIT", 0),  # ボタン入力待ち
    0xF3: ("COLOR", 1),  # 文字色の変更
    0xF4: ("VAR", 1),  # 変数の差し込み (プレイヤー名など)
    0xF5: ("CLEAR", 0),  # メッセージウィンドウのクリア
}
TAG_BYTES = {name: value for value, (name, _) in CONTROL_CODES.items()}
TAG_ARGC = {name: argc for _, (name, argc) in CONTROL_CODES.items()}

#: TSV 上では '<' がタグの開始文字なので、文字としての '<' はこのタグで書く
LITERAL_TAGS = {"LT": "<"}

TAG_RE = re.compile(r"<([A-Z]+)(?::([0-9A-Fa-f]{2}))?>")


class ScrpError(Exception):
    """フォーマット違反・エンコード不能など、この題材固有のエラー."""


# ---------------------------------------------------------------------------
# 文字コード
# ---------------------------------------------------------------------------


class Codec:
    """1 文字単位で読み書きできる文字コードの共通インタフェース."""

    name = "?"

    def decode_char(self, data: bytes, i: int) -> tuple[str, int]:
        """data[i] から 1 文字読み、(文字, 消費バイト数) を返す."""
        raise NotImplementedError

    def encode_char(self, ch: str) -> bytes:
        """1 文字をバイト列にする. 表現できない場合は ScrpError."""
        raise NotImplementedError

    def charset(self) -> set[str] | None:
        """使える文字の集合. 制限がない (あるいは不明な) 場合は None."""
        return None


class SjisCodec(Codec):
    """Shift-JIS (CP932). バイナリエディタでそのまま日本語が見えるパターン."""

    name = "sjis"

    def __init__(self, charset: set[str] | None = None):
        self._charset = charset

    @staticmethod
    def is_lead_byte(b: int) -> bool:
        return 0x81 <= b <= 0x9F or 0xE0 <= b <= 0xEF

    def decode_char(self, data: bytes, i: int) -> tuple[str, int]:
        size = 2 if self.is_lead_byte(data[i]) else 1
        chunk = data[i : i + size]
        if len(chunk) != size:
            raise ScrpError(f"0x{i:X}: 2 バイト文字の途中でデータが終わっています")
        try:
            return chunk.decode("cp932"), size
        except UnicodeDecodeError as exc:
            raise ScrpError(f"0x{i:X}: Shift-JIS として解釈できない {chunk.hex(' ')}") from exc

    def encode_char(self, ch: str) -> bytes:
        try:
            raw = ch.encode("cp932")
        except UnicodeEncodeError as exc:
            raise ScrpError(f"Shift-JIS にない文字: {ch!r}") from exc
        # 1 バイト文字が 0xF0 以降だと制御コードと区別できなくなる。
        # 2 バイト文字の 2 バイト目 (トレイルバイト) は先頭バイトで長さが
        # 決まるので衝突しない。
        if len(raw) == 1 and raw[0] >= 0xF0:
            raise ScrpError(f"制御コード領域と衝突する文字: {ch!r} ({raw.hex(' ')})")
        return raw

    def charset(self) -> set[str] | None:
        return self._charset


class TableCodec(Codec):
    """独自文字コード. .tbl (「16 進=文字」の行) で対応表を与える."""

    name = "custom"

    def __init__(self, mapping: dict[bytes, str]):
        self.by_bytes = dict(mapping)
        self.by_char: dict[str, bytes] = {}
        for raw, ch in self.by_bytes.items():
            # 同じ文字に複数コードが割り当たっている場合は短い方を採用する
            if ch not in self.by_char or len(raw) < len(self.by_char[ch]):
                self.by_char[ch] = raw
        self.max_len = max((len(k) for k in self.by_bytes), default=1)

    def decode_char(self, data: bytes, i: int) -> tuple[str, int]:
        for size in range(self.max_len, 0, -1):
            chunk = data[i : i + size]
            if len(chunk) == size and chunk in self.by_bytes:
                return self.by_bytes[chunk], size
        raise ScrpError(f"0x{i:X}: 文字テーブルにないコード 0x{data[i]:02X}")

    def encode_char(self, ch: str) -> bytes:
        try:
            return self.by_char[ch]
        except KeyError as exc:
            raise ScrpError(f"文字テーブル (フォント) にない文字: {ch!r}") from exc

    def charset(self) -> set[str]:
        return set(self.by_char)


def load_table(path: str) -> TableCodec:
    """.tbl ファイルを読み込む.

    1 行 1 エントリで ``01=あ`` / ``E000=一`` のように書く、ROM ハック界で
    昔から使われている素朴な形式です。'#' 以降はコメント。
    """
    mapping: dict[bytes, str] = {}
    with open_text(path) as fh:                       # メモ帳/Excel のどの保存形式でも読む
        for lineno, line in enumerate(fh, 1):
            line = line.split("#", 1)[0].rstrip("\n")
            if not line.strip():
                continue
            code, sep, ch = line.partition("=")
            if not sep or not ch:
                raise ScrpError(f"{path}:{lineno}: 「16進=文字」の形式ではありません: {line!r}")
            code = code.strip()
            if len(code) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", code):
                raise ScrpError(f"{path}:{lineno}: 16 進コードが不正です: {code!r}")
            mapping[bytes.fromhex(code)] = ch
    if not mapping:
        raise ScrpError(f"{path}: エントリが 1 件もありません")
    return TableCodec(mapping)


def save_table(path: str, mapping: dict[bytes, str], header: str = "") -> None:
    lines = []
    if header:
        lines += [f"# {line}" for line in header.strip().splitlines()]
        lines.append("")
    for raw in sorted(mapping, key=lambda r: (len(r), r)):
        lines.append(f"{raw.hex().upper()}={mapping[raw]}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def make_codec(encoding_id: int, table_path: str | None = None,
               charset: set[str] | None = None) -> Codec:
    if encoding_id == ENC_SJIS:
        return SjisCodec(charset)
    if encoding_id == ENC_CUSTOM:
        if not table_path:
            raise ScrpError("独自文字コードのファイルです。--table で .tbl を指定してください")
        return load_table(table_path)
    raise ScrpError(f"未知の encoding id: {encoding_id}")


# ---------------------------------------------------------------------------
# メッセージ 1 件のエンコード / デコード
# ---------------------------------------------------------------------------


def decode_message(data: bytes, offset: int, codec: Codec) -> tuple[str, int]:
    """offset から 0xFF までを読み、(タグ入りテキスト, バイト数) を返す."""
    out: list[str] = []
    i = offset
    while True:
        if i >= len(data):
            raise ScrpError(f"0x{offset:X}: 終端 0xFF が見つかりません")
        b = data[i]
        if b == END:
            return "".join(out), i - offset + 1
        if b in CONTROL_CODES:
            tag, argc = CONTROL_CODES[b]
            args = data[i + 1 : i + 1 + argc]
            if len(args) != argc:
                raise ScrpError(f"0x{i:X}: 制御コード <{tag}> の引数が足りません")
            out.append(f"<{tag}>" if argc == 0 else f"<{tag}:{args.hex().upper()}>")
            i += 1 + argc
            continue
        ch, size = codec.decode_char(data, i)
        out.append("<LT>" if ch == "<" else ch)
        i += size


def encode_message(text: str, codec: Codec) -> bytes:
    """タグ入りテキストを 0xFF 終端のバイト列にする."""
    out = bytearray()
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "<":
            m = TAG_RE.match(text, i)
            if not m:
                raise ScrpError(f"閉じていない、または不正なタグ: {text[i:i + 12]!r}")
            tag, arg = m.group(1), m.group(2)
            if tag in LITERAL_TAGS:
                if arg is not None:
                    raise ScrpError(f"<{tag}> は引数を取りません")
                out += codec.encode_char(LITERAL_TAGS[tag])
            elif tag in TAG_BYTES:
                argc = TAG_ARGC[tag]
                if argc and arg is None:
                    raise ScrpError(f"<{tag}> には 2 桁の 16 進引数が必要です")
                if not argc and arg is not None:
                    raise ScrpError(f"<{tag}> は引数を取りません")
                out.append(TAG_BYTES[tag])
                if argc:
                    out.append(int(arg, 16))
            else:
                raise ScrpError(f"未知のタグ: <{tag}>")
            i = m.end()
            continue
        if ch in "\r\n\t":
            raise ScrpError("生の改行・タブは使えません。改行は <BR> で表します")
        out += codec.encode_char(ch)
        i += 1
    out.append(END)
    return bytes(out)


def iter_tags(text: str):
    """テキスト中のタグを (タグ名, 引数 or None) で順に返す."""
    for m in TAG_RE.finditer(text):
        yield m.group(1), m.group(2)


def strip_tags(text: str) -> str:
    """タグを取り除いて、表示される文字だけにする (<LT> は '<' に戻す)."""

    def repl(m: re.Match) -> str:
        return LITERAL_TAGS.get(m.group(1), "")

    return TAG_RE.sub(repl, text)


def display_width(text: str) -> float:
    """全角を 1.0、半角を 0.5 として数えた表示幅. タグは幅 0 とみなす."""
    total = 0.0
    for ch in strip_tags(text):
        total += 1.0 if unicodedata.east_asian_width(ch) in "FWA" else 0.5
    return total


def split_lines(text: str) -> list[str]:
    """<BR> / <CLEAR> でメッセージを行に割る (<CLEAR> はページ送り扱い)."""
    normalized = text.replace("<CLEAR>", "<BR>")
    return normalized.split("<BR>")


# ---------------------------------------------------------------------------
# アーカイブの読み書き
# ---------------------------------------------------------------------------


class Archive:
    """SCRP ファイル 1 つ分."""

    def __init__(self, encoding_id: int, pointers: list[int], data: bytes, version: int = VERSION):
        self.encoding_id = encoding_id
        self.pointers = pointers
        self.data = data
        self.version = version

    @property
    def count(self) -> int:
        return len(self.pointers)

    def raw_block(self, index: int) -> bytes:
        """index 番のメッセージの生バイト列 (終端 0xFF を含む)."""
        start = self.pointers[index]
        end = self.data.index(bytes([END]), start) + 1
        return self.data[start:end]

    def decode_all(self, codec: Codec) -> list[tuple[int, int, str]]:
        """[(オフセット, バイト数, テキスト), ...] を返す."""
        rows = []
        for ptr in self.pointers:
            text, size = decode_message(self.data, ptr, codec)
            rows.append((ptr, size, text))
        return rows

    def survey_unreadable(self, codec: Codec) -> tuple[list[int], list[int], dict[int, int]]:
        """表で読める id / 読めない id / 表に無いバイトの数え上げ を返す (#84).

        最初の 1 バイトで止めて「0xD5: 文字テーブルにないコード 0xE0」とだけ言うと、
        あと何が足りないのかが分からず、1 バイト直しては打ち直す、を繰り返すことに
        なる。課題 3 の表を育てる工程は、残りの量が見えないと進め方が決められない。
        """
        ok: list[int] = []
        bad: list[int] = []
        missing: dict[int, int] = {}
        for index in range(self.count):
            try:
                block = self.raw_block(index)
            except ValueError:
                bad.append(index)
                continue
            clean, i = True, 0
            while i < len(block):
                b = block[i]
                if b == END:
                    break
                if b in CONTROL_CODES:
                    i += 1 + CONTROL_CODES[b][1]
                    continue
                try:
                    _ch, size = codec.decode_char(block, i)
                except ScrpError:
                    missing[b] = missing.get(b, 0) + 1
                    clean = False
                    i += 1
                    continue
                i += size
            (ok if clean else bad).append(index)
        return ok, bad, missing


def guess_other_format(data: bytes) -> str:
    """SCRP でないバイト列が、この一式で扱う別の形式に見えるかを言う (#76).

    docs/01〜03 の練習は SCRP 形式で進むので、素人はその流れのまま実物のファイルを
    渡してしまう。「SCRP ではありません」だけだと、**次に何をすればいいか**が分からない。
    自信のあるときだけ、形式の名前と使うべき道具を添える。

    boku2.py は scrp.py を読み込む側なので、ここから boku2 を呼ぶことはできない
    (循環する)。判定はこの中だけで完結させる。
    """
    if len(data) < 16:
        return ""
    if data[:4] == b"DFI\0":
        return ("僕の夏休み 2 の索引 (DFI) に見えます。"
                "python3 tools/boku2.py unpack BOKU2.IDX BOKU2.IMG OUT/ を使ってください")
    if data[:4] == b"TMS\0" or data[0x80:0x84] == b"TIM2" or data[:4] == b"TIM2":
        return ("PS2 の画像 (TIM2) に見えます。構造探査台の「既知の形式」タブで開くか、"
                "文字表を作るなら docs/10 の手順 3 へ")

    def u32(p: int) -> int:
        return struct.unpack_from("<I", data, p)[0]

    # 僕の夏休み 2 の .msg: u32 件数 + 位置表 (8 または 4 バイト刻み)。
    # 1 件目の位置が表の直後で、位置が減らずファイル内に収まっていれば、それ
    n = u32(0)
    if 1 <= n <= 20000:
        for stride in (8, 4):
            tab = 4 + n * stride
            if tab > len(data) or tab + 2 > len(data):
                continue
            offs = [u32(4 + i * stride) for i in range(n)]
            live = [o for o in offs if o]
            if not live or live[0] != tab:
                continue
            if all(a <= b for a, b in zip(live, live[1:])) and live[-1] < len(data):
                return ("僕の夏休み 2 の会話ファイル (.msg) に見えます。"
                        "python3 tools/boku2.py text ファイル -f font.txt -o all.tsv を使ってください "
                        "(画面なら構造探査台の「.msg として読む」)")
    # 入れ物: u32 項目数 + (位置, 長さ) の並び
    if 1 <= n <= 64:
        for rec in (8, 12):
            head = 4 + n * rec
            if head > len(data):
                continue
            ok = True
            for i in range(n):
                off, size = u32(4 + i * rec), u32(8 + i * rec)
                if off == 0 and size == 0:
                    continue
                if not (head <= off <= len(data) and off + size <= len(data)):
                    ok = False
                    break
            if ok:
                return ("部品をまとめた入れ物に見えます (MAP のファイルなど)。"
                        "python3 tools/boku2.py maps フォルダ -o OUT/maps で切り分けてください")
    return ""


def read_archive(path: str) -> Archive:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        hint = guess_other_format(data)
        raise ScrpError(f"{path}: SCRP ファイルではありません (先頭 4 バイト = {data[:4]!r})"
                        + (f"\n  {hint}" if hint else ""))
    version, count, encoding_id = struct.unpack_from("<3I", data, 4)
    if version != VERSION:
        raise ScrpError(f"{path}: 未対応のバージョン {version}")
    table_end = HEADER_SIZE + count * 4
    if len(data) < table_end:
        raise ScrpError(f"{path}: ポインタテーブルが途中で終わっています")
    pointers = list(struct.unpack_from(f"<{count}I", data, HEADER_SIZE))
    for i, ptr in enumerate(pointers):
        if not table_end <= ptr < len(data):
            raise ScrpError(f"{path}: pointer[{i}] = 0x{ptr:X} がファイル外を指しています")
    return Archive(encoding_id, pointers, data, version)


def build_archive(encoding_id: int, blobs: list[bytes], pool_duplicates: bool = False) -> bytes:
    """バイト列のリストからファイル全体を組み立てる.

    pool_duplicates=True にすると、まったく同じ本文を 1 か所にまとめて複数の
    ポインタから共有します。容量が足りないときに実際に使われる手です。
    """
    count = len(blobs)
    body_base = HEADER_SIZE + count * 4
    body = bytearray()
    pointers: list[int] = []
    seen: dict[bytes, int] = {}
    for blob in blobs:
        if pool_duplicates and blob in seen:
            pointers.append(seen[blob])
            continue
        offset = body_base + len(body)
        if pool_duplicates:
            seen[blob] = offset
        pointers.append(offset)
        body += blob
    header = MAGIC + struct.pack("<3I", VERSION, count, encoding_id)
    return bytes(header + struct.pack(f"<{count}I", *pointers) + body)


# ---------------------------------------------------------------------------
# 抽出結果の TSV
# ---------------------------------------------------------------------------

TSV_COLUMNS = ["id", "offset", "size", "original", "translation"]


def write_tsv(path: str, rows: list[dict]) -> None:
    # BOM 付き UTF-8: Excel で開いたとき日本語が化けない (BOM 無しだと cp932 と誤認する)
    with open(path, "w", encoding="utf-8-sig", newline="\n") as fh:
        fh.write("\t".join(TSV_COLUMNS) + "\n")
        for row in rows:
            fh.write("\t".join(str(row.get(c, "")) for c in TSV_COLUMNS) + "\n")


def read_tsv(path: str) -> list[dict]:
    rows = []
    with open_text(path) as fh:                       # Excel から保存し直した TSV (UTF-16 / BOM 付き) も読む
        header = fh.readline().rstrip("\n").split("\t")
        missing = {"id", "original"} - set(header)
        if missing:
            raise ScrpError(f"{path}: 必須列がありません: {', '.join(sorted(missing))}")
        for lineno, line in enumerate(fh, 2):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            values = line.split("\t")
            if len(values) != len(header):
                raise ScrpError(
                    f"{path}:{lineno}: 列数が {len(values)} で、見出しの {len(header)} と違います"
                )
            row = dict(zip(header, values))
            row["_lineno"] = lineno
            rows.append(row)
    return rows


def final_text(row: dict) -> str:
    """訳文 (translation) があればそれを、なければ原文を返す."""
    text = row.get("translation", "")
    return text if text.strip() else row.get("original", "")


# 練習データの作り方。docs/01〜03 はこれらを作ってある前提で書かれているので、
# 無いときは「どのコマンドで作るか」まで言う (#79)
MAKERS = {
    "SCRIPT.BIN": "python3 tools/make_sample.py",
    "MSG_ENC.BIN": "python3 tools/make_sample.py",
    "FONT.BIN": "python3 tools/make_sample.py",
    "SCRIPT.tsv": "python3 tools/make_sample.py && python3 tools/dump_text.py work/SCRIPT.BIN -o work/SCRIPT.tsv",
    "RINFOLT.iso": "python3 tools/make_iso.py",
    "PACK.IDX": "python3 tools/make_archive.py",
    "PACK.IMG": "python3 tools/make_archive.py",
    "FONT.TMS": "python3 tools/make_tim2.py",
    "FONT.TM2": "python3 tools/make_tim2.py",
    "BOOT.ELF": "python3 tools/make_elf.py",
    "viewer.html": "python3 tools/make_viewer.py",
    "BOKU2SAMPLE": "python3 tools/make_boku2_sample.py",
    "BOKU2.IDX": "python3 tools/make_boku2_sample.py",
    "BOKU2.IMG": "python3 tools/make_boku2_sample.py",
}


#: 標準では入っていない部品 -> (pip の名前, 何に使うか). ここに無い名前は書き間違い扱い
OPTIONAL_MODULES = {
    "PIL": ("Pillow", "画像を扱う機能で使います"),
}


def optional_module_error(module: str, instead: str = "") -> "ScrpError":
    """入っていない追加部品を、入れ方と代わりの手つきで伝える (#85)."""
    package, why = OPTIONAL_MODULES.get(module, (module, "この機能で使います"))
    text = (f"{package} が入っていません ({why})。\n"
            f"  入れる:  python3 -m pip install {package}")
    if instead:
        text += f"\n  入れずに済ませる:  {instead}"
    return ScrpError(text)


def missing_file_help(path: str) -> str:
    """無いファイルが練習データなら、それを作るコマンドを返す.

    work/ の中でも、この一式が作らない名前がある (docs/06 の `work/qa_fixed.tsv` は
    読む人が自分で用意するファイル)。そこに「make_sample.py を実行してください」と
    出すと、実行しても何も変わらないので、**言われたとおりにして直らない**という
    いちばん困る状態になる (#83)。作れる名前のときだけ、作り方を言う。
    """
    import os

    name = os.path.basename(path)
    cmd = MAKERS.get(name)
    if cmd:
        return f"練習データはまだ作られていません。先にこれを実行してください:\n  {cmd}"
    if os.sep + "work" + os.sep in os.sep + path or path.startswith("work" + os.sep):
        made = "、".join(sorted(MAKERS))
        return (f"work/ は生成物と自分の作業ファイルの置き場です。{name} を作る道具は"
                "この一式にはありません。\n"
                "  自分で用意するファイル (直した TSV など) なら、名前と場所を確かめてください。\n"
                f"  この一式が work/ に作る名前: {made}")
    return ""


def cli_main(main) -> None:
    """各ツールの共通の入口. エラー表示とパイプ切断の処理をまとめる."""
    import sys

    try:
        sys.exit(main())
    except ScrpError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        # 素の例外を出すと、素人は「道具が壊れた」と読む。docs/01 の最初のコマンドで
        # 必ず踏むので、無いファイル名と作り方まで言う (#79)
        path = exc.filename or ""
        if not path:
            # ファイル名を持たない FileNotFoundError もある (「一致するファイルが
            # ありません」など、伝えたい言葉が本体側にある場合)。空の名前を出さない
            print(f"エラー: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"エラー: ファイルがありません: {path}", file=sys.stderr)
        hint = missing_file_help(path)
        if hint:
            print("  " + hint.replace("\n", "\n  "), file=sys.stderr)
        sys.exit(1)
    except IsADirectoryError as exc:
        print(f"エラー: フォルダが指定されています (ファイルを指定してください): {exc.filename}",
              file=sys.stderr)
        sys.exit(1)
    except ModuleNotFoundError as exc:
        # 追加で入れる部品が要る機能がある (課題 4 の PNG など)。素の ImportError を
        # 出すと、素人は「道具が壊れた」と読む。入れ方と、入れずに済ませる道を言う (#85)
        top = (exc.name or "").split(".")[0]
        if top not in OPTIONAL_MODULES:
            raise                                  # こちらの書き間違いなので隠さない
        package, why = OPTIONAL_MODULES[top]
        print(f"エラー: {package} が入っていません ({why})。", file=sys.stderr)
        print(f"  入れる:  python3 -m pip install {package}", file=sys.stderr)
        sys.exit(1)
    except PermissionError as exc:
        print(f"エラー: 読み書きの権限がありません: {exc.filename}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # head などに渡したときに出る。終了処理でも同じ例外が出るので黙らせる
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
