#!/usr/bin/env python3
"""Read-only SQLCipher4 / SQLite Quote recovery core.

Adapted from the locally verified recover_wechat_quotes.py fallback for the
GreenBubbles empty-compress_content projection bug. No key acquisition code.
Only exact preselected identities may be queried; plaintext databases stay in RAM.
"""
import ctypes
import ctypes.util
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import xml.etree.ElementTree as ET


PAGE = 4096
RESERVE = 80
MAX_BLOB = 4 * 1024 * 1024
SQLITE_MAGIC = b"SQLite format 3\x00"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class SafeError(Exception):
    """Error codes only; never include private source values in diagnostics."""


def signature(path):
    try:
        s = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(s.st_mode):
        raise SafeError("SOURCE_NOT_REGULAR")
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def read_file(path, maximum):
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise SafeError("SOURCE_NOT_REGULAR")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as f:
        current = os.fstat(f.fileno())
        if (not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
            raise SafeError("SOURCE_CHANGED_DURING_OPEN")
        if current.st_size > maximum:
            raise SafeError("SOURCE_SIZE_LIMIT")
        data = f.read(maximum + 1)
    if len(data) > maximum:
        raise SafeError("SOURCE_SIZE_LIMIT")
    return data


def stable_capture(db_path, maximum):
    wal_path = Path(str(db_path) + "-wal")
    journal_path = Path(str(db_path) + "-journal")
    # A rollback journal cannot safely be replayed by this WAL-only utility.
    for _ in range(3):
        before = tuple(signature(p) for p in (db_path, wal_path, journal_path))
        if before[0] is None:
            raise SafeError("SOURCE_DB_MISSING")
        if before[2] and before[2][2] > 0:
            raise SafeError("ROLLBACK_JOURNAL_PRESENT")
        db = read_file(db_path, maximum)
        wal = read_file(wal_path, maximum) if before[1] else b""
        after = tuple(signature(p) for p in (db_path, wal_path, journal_path))
        if before == after:
            return db, wal
    raise SafeError("SOURCE_CHANGED_RETRY_LATER")


def checksum(data, state=(0, 0), endian="<"):
    if len(data) % 8:
        raise SafeError("WAL_CHECKSUM_ALIGNMENT")
    s1, s2 = state
    for x, y in struct.iter_unpack(endian + "II", data):
        s1 = (s1 + x + s2) & 0xffffffff
        s2 = (s2 + y + s1) & 0xffffffff
    return s1, s2


def committed_wal_pages(wal):
    """Follow SQLite WAL checksum/salt chain; expose only last committed prefix."""
    if not wal:
        return {}, None, {"walFrames": 0, "walCommittedFrames": 0}
    if len(wal) < 32:
        raise SafeError("WAL_HEADER_INCOMPLETE")
    magic, version, size = struct.unpack(">III", wal[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != PAGE:
        raise SafeError("WAL_HEADER_UNSUPPORTED")
    endian = "<" if magic == 0x377f0682 else ">"
    running = checksum(wal[:24], endian=endian)
    if running != struct.unpack(">II", wal[24:32]):
        raise SafeError("WAL_HEADER_CHECKSUM_FAILED")
    salt = wal[16:24]
    frames = []
    last_commit = 0
    database_pages = None
    tail_ignored = False
    for offset in range(32, len(wal), 24 + PAGE):
        frame = wal[offset:offset + 24 + PAGE]
        if len(frame) != 24 + PAGE or frame[8:16] != salt:
            tail_ignored = True
            break
        page_number, commit_size = struct.unpack(">II", frame[:8])
        if not page_number:
            tail_ignored = True
            break
        candidate = checksum(frame[:8] + frame[24:], running, endian)
        if candidate != struct.unpack(">II", frame[16:24]):
            tail_ignored = True
            break
        running = candidate
        frames.append((page_number, frame[24:]))
        if commit_size:
            last_commit = len(frames)
            database_pages = commit_size
    pages = dict(frames[:last_commit])
    return pages, database_pages, {
        "walFrames": len(frames), "walCommittedFrames": last_commit,
        "walUncommittedFrames": len(frames) - last_commit,
        "walTrailingBytesIgnored": tail_ignored,
    }


def derive_keys(secret, salt):
    enc = hashlib.pbkdf2_hmac("sha512", secret, salt, 256000, 32)
    mac = hashlib.pbkdf2_hmac("sha512", enc, bytes(x ^ 0x3a for x in salt), 2, 32)
    return enc, mac


def decrypt_page(page, page_number, enc, mac):
    if len(page) != PAGE:
        raise SafeError("PAGE_SIZE_INVALID")
    offset = 16 if page_number == 1 else 0
    expected = hmac.new(mac, page[offset:PAGE - 64] + struct.pack("<I", page_number), "sha512").digest()
    if not hmac.compare_digest(expected, page[PAGE - 64:]):
        raise SafeError("PAGE_HMAC_FAILED")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise SafeError("CRYPTOGRAPHY_REQUIRED") from None
    iv = page[PAGE - RESERVE:PAGE - 64]
    decryptor = Cipher(algorithms.AES(enc), modes.CBC(iv)).decryptor()
    body = decryptor.update(page[offset:PAGE - RESERVE]) + decryptor.finalize()
    return (SQLITE_MAGIC if page_number == 1 else b"") + body + bytes(RESERVE)


def decrypt_capture(db, wal, secret, maximum):
    if len(db) < PAGE or len(db) % PAGE:
        raise SafeError("DB_SIZE_INVALID")
    if db[:16] == SQLITE_MAGIC:
        raise SafeError("EXPECTED_ENCRYPTED_DB")
    enc, mac = derive_keys(secret, db[:16])
    # Validate key against source page 1 even when WAL supplies newer page 1.
    decrypt_page(db[:PAGE], 1, enc, mac)
    overlays, committed_size, report = committed_wal_pages(wal)
    count = committed_size if committed_size is not None else len(db) // PAGE
    if count < 1 or count * PAGE > maximum:
        raise SafeError("DECRYPTED_SIZE_LIMIT")
    plaintext = bytearray(count * PAGE)
    for n in range(1, count + 1):
        page = overlays.get(n, db[(n - 1) * PAGE:n * PAGE])
        plaintext[(n - 1) * PAGE:n * PAGE] = decrypt_page(page, n, enc, mac)
    if plaintext[16:18] != b"\x10\x00" or plaintext[20] != RESERVE:
        raise SafeError("DECRYPTED_HEADER_PROFILE_MISMATCH")
    # WAL is already materialized. In-memory SQLite must not seek WAL sidecars.
    plaintext[18:20] = b"\x01\x01"
    report["decryptedPageCount"] = count
    report["verifiedPageCount"] = count
    report["sourcePageOneVerified"] = True
    return plaintext, report


class Zstd:
    """Prefer the portable Python wheel; otherwise locate the system shared library."""
    def __init__(self, library=None):
        self.python = None
        if library is None:
            try:
                import zstandard
                self.python = zstandard.ZstdDecompressor()
                return
            except ImportError:
                pass
        candidates = [str(library)] if library else [ctypes.util.find_library("zstd")]
        if not library:
            if sys.platform == "darwin":
                candidates += ["libzstd.dylib", "/opt/homebrew/opt/zstd/lib/libzstd.dylib",
                               "/usr/local/opt/zstd/lib/libzstd.dylib"]
            elif os.name == "nt":
                candidates += ["libzstd.dll", "zstd.dll"]
            else:
                candidates += ["libzstd.so.1", "libzstd.so"]
        self.lib = None
        for candidate in dict.fromkeys(c for c in candidates if c):
            try:
                lib = ctypes.CDLL(candidate)
                lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                               ctypes.c_void_p, ctypes.c_size_t]
                lib.ZSTD_decompress.restype = ctypes.c_size_t
                lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
                lib.ZSTD_isError.restype = ctypes.c_uint
                self.lib = lib
                break
            except (OSError, AttributeError):
                continue
        if self.lib is None:
            raise SafeError("ZSTANDARD_OR_LIBZSTD_REQUIRED")

    def decompress(self, value):
        if len(value) > MAX_BLOB:
            raise SafeError("CONTENT_SIZE_LIMIT")
        if self.python is not None:
            try:
                # Streaming prevents a forged content-size header allocating unbounded RAM.
                import io
                with self.python.stream_reader(io.BytesIO(value), read_across_frames=False) as reader:
                    result = reader.read(MAX_BLOB + 1)
                if len(result) > MAX_BLOB:
                    raise SafeError("CONTENT_SIZE_LIMIT")
                return result
            except SafeError:
                raise
            except Exception:
                raise SafeError("ZSTD_DECODE_FAILED") from None
        destination = ctypes.create_string_buffer(MAX_BLOB)
        source = ctypes.create_string_buffer(value)
        size = self.lib.ZSTD_decompress(destination, MAX_BLOB, source, len(value))
        if self.lib.ZSTD_isError(size):
            raise SafeError("ZSTD_DECODE_FAILED")
        return destination.raw[:size]


def materialize_decrypted(db, wal, maximum):
    """Merge committed WAL into an already-decrypted 4096-byte-page SQLite DB."""
    if (len(db) < PAGE or len(db) % PAGE or db[:16] != SQLITE_MAGIC
            or db[16:18] != b"\x10\x00"):
        raise SafeError("DECRYPTED_SQLITE_PROFILE_UNSUPPORTED")
    overlays, committed_size, report = committed_wal_pages(wal)
    count = committed_size if committed_size is not None else len(db) // PAGE
    if count < 1 or count * PAGE > maximum:
        raise SafeError("DECRYPTED_SIZE_LIMIT")
    plaintext = bytearray(count * PAGE)
    for number in range(1, count + 1):
        page = overlays.get(number, db[(number - 1) * PAGE:number * PAGE])
        if len(page) != PAGE:
            raise SafeError("PAGE_SIZE_INVALID")
        plaintext[(number - 1) * PAGE:number * PAGE] = page
    if plaintext[:16] != SQLITE_MAGIC or plaintext[16:18] != b"\x10\x00":
        raise SafeError("DECRYPTED_SQLITE_PROFILE_UNSUPPORTED")
    plaintext[18:20] = b"\x01\x01"
    report.update({"materializedPageCount": count, "verifiedPageCount": 0,
                   "sourcePageOneVerified": False})
    return plaintext, report


def quote_fields(payload):
    if len(payload) > MAX_BLOB:
        raise SafeError("CONTENT_SIZE_LIMIT")
    try:
        text = payload.decode("utf-8")
        start = text.find("<")
        if start < 0 or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError()
        root = ET.fromstring(text[start:])
        app = root if root.tag == "appmsg" else root.find(".//appmsg")
        if app is None:
            raise ValueError()
        def field(parent, name):
            node = parent.find(name) if parent is not None else None
            return "".join(node.itertext()) if node is not None else None
        refer = app.find("refermsg")
        return {
            "raw_xml": text,
            "reply_text": field(app, "title"),
            "refer_content": field(refer, "content"),
            "refer_sender": field(refer, "displayname"),
            "refer_type": field(refer, "type"),
            "refer_server_id": field(refer, "svrid"),
        }
    except (ValueError, UnicodeError, ET.ParseError):
        raise SafeError("QUOTE_XML_INVALID") from None


def recover_rows(plaintext, conversation, since, until, limit, selection, zstd_library=None):
    if selection is None or not 0 < len(selection) <= limit:
        raise SafeError("EXACT_SELECTED_QUOTES_REQUIRED")
    if any(not since <= identity["createTime"] <= until for _, _, identity in selection):
        raise SafeError("SELECTION_OUTSIDE_TIME_WINDOW")
    if not hasattr(sqlite3.Connection, "deserialize"):
        raise SafeError("SQLITE_DESERIALIZE_REQUIRED_USE_PYTHON_3_11_PLUS")
    table = "Msg_" + hashlib.md5(conversation.encode()).hexdigest()
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(plaintext)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA trusted_schema=OFF")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            raise SafeError("CONVERSATION_TABLE_NOT_IN_SHARD")
        columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
        required = {"create_time", "local_type", "message_content", "server_id", "sort_seq"}
        if not required <= columns:
            raise SafeError("MESSAGE_SCHEMA_UNSUPPORTED")
        opt = lambda n: f'"{n}"' if n in columns else "NULL"
        sql_one = f'''SELECT rowid, server_id, sort_seq, create_time, local_type,
          message_content, {opt("compress_content")}, {opt("WCDB_CT_message_content")}
          FROM "{table}" WHERE rowid=? AND server_id=? AND sort_seq=? AND create_time=?
          AND (local_type & 4294967295)=49 AND (local_type >> 32)=57'''
        selected = []
        for number, token, identity in selection:
            row = db.execute(sql_one, (identity["rowId"], identity["serverId"],
                             identity["sortSequence"], identity["createTime"])).fetchone()
            if row is None:
                raise SafeError("SELECTED_QUOTE_NO_LONGER_MATCHES_SOURCE")
            selected.append((number, token, row))
        if len(selected) > limit:
            raise SafeError("QUOTE_ROW_LIMIT_USE_SMALLER_TIME_WINDOW")
        zstd = None
        items = []
        for number, token, row in selected:
            rowid, server_id, sort_seq, timestamp, local_type, content, compressed, marker = row
            def as_bytes(v):
                if v is None:
                    return b""
                return v.encode() if isinstance(v, str) else bytes(v)
            primary, alternate = as_bytes(content), as_bytes(compressed)
            payload = alternate if alternate else primary
            if alternate or payload.startswith(ZSTD_MAGIC) or marker == 4:
                if zstd is None:
                    zstd = Zstd(zstd_library)
                payload = zstd.decompress(payload)
            fields = quote_fields(payload)
            # The readable fields are sufficient; do not persist full raw XML.
            fields.pop("raw_xml", None)
            items.append({"rowid": rowid, "serverId": server_id, "sortSequence": sort_seq,
                          "createdAtUnix": timestamp, "content": {"Quote": fields},
                          "contentDecodeState": "recovered", "contentTruncated": False,
                          "id": token, "originalOrdinal": number})
        return items
    finally:
        db.close()

