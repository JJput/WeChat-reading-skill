#!/usr/bin/env python3
"""Recover only empty Quote replies selected by an existing single-group export.

Usage: python recover_quotes.py RUN_DIRECTORY --key-file EXISTING_KEY_FILE
Older summaries without sourceRoot also need --source DATABASE_ROOT.
For an already decrypted source use --decrypted instead of --key-file.

Original pages/summary/transcript are preserved. No key extraction, app changes,
plaintext database files, or unselected message export. Output remains private.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

from quote_recovery_core import (SafeError, decrypt_capture, materialize_decrypted,
                                 read_file, recover_rows, stable_capture)

MAX_SELECTED = 50_000
MAX_WINDOW = 14 * 86400
MAX_BATCH = 500
OUTPUTS = ("recovered-quotes.json", "transcript-complete.txt", "recovery-summary.json")
LOCK_NAME = ".quote-recovery.lock"


def private_directory(path):
    meta = os.lstat(path)
    if not stat.S_ISDIR(meta.st_mode):
        raise SafeError("OUTPUT_DIRECTORY_NOT_REAL_DIRECTORY")
    if os.name != "nt" and (meta.st_uid != os.geteuid() or meta.st_mode & 0o077):
        raise SafeError("OUTPUT_DIRECTORY_MUST_BE_OWNER_ONLY")


def private_write(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def acquire_lock(directory):
    private_directory(directory)
    lock = directory / LOCK_NAME
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise SafeError("RECOVERY_ALREADY_RUNNING_OR_STALE_LOCK") from None
    token = uuid.uuid4().hex
    try:
        private_write(lock / "owner.json", json_bytes({"token": token, "pid": os.getpid()}))
    except BaseException:
        lock.rmdir()
        raise
    return token


def checked_lock(directory, token):
    lock = directory / LOCK_NAME
    private_directory(lock)
    owner = load_json(lock / "owner.json", 4096)
    if not token or not isinstance(owner, dict) or owner.get("token") != token:
        raise SafeError("RECOVERY_LOCK_OWNERSHIP_MISMATCH")
    return lock


def release_lock(directory, token, success):
    lock = checked_lock(directory, token)
    for name in OUTPUTS:
        stage, target = lock / name, directory / name
        if not success and os.path.lexists(stage) and os.path.lexists(target):
            a, b = os.lstat(stage), os.lstat(target)
            if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                target.unlink()
        if os.path.lexists(stage):
            stage.unlink()
    (lock / "owner.json").unlink()
    lock.rmdir()


def load_json(path, maximum):
    try:
        return json.loads(read_file(path, maximum))
    except (ValueError, UnicodeError):
        raise SafeError("INPUT_JSON_INVALID") from None


def empty_quote(item):
    content = item.get("content")
    if not isinstance(content, dict) or "Quote" not in content:
        return False
    quote = content["Quote"]
    return (not isinstance(quote, dict) or not isinstance(quote.get("reply_text"), str)
            or not quote["reply_text"].strip())


def exact_identity(item, conversation, source_identity):
    token = item["id"]
    try:
        if len(token) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError()
        identity = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if not isinstance(identity, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise SafeError("MESSAGE_IDENTITY_INVALID") from None
    if identity.get("version") != 1 or identity.get("kind") != "message.identity":
        raise SafeError("MESSAGE_IDENTITY_UNSUPPORTED")
    digest = hashlib.sha256(b"greenbubbles-conversation-v1\x00" + conversation.encode()).hexdigest()
    if identity.get("conversationDigest") != digest:
        raise SafeError("IDENTITY_CONVERSATION_MISMATCH")
    if identity.get("sourceIdentity") != source_identity:
        raise SafeError("IDENTITY_SOURCE_MISMATCH")
    for name in ("shardId", "rowId", "serverId", "sortSequence", "createTime"):
        if type(identity.get(name)) is not int:
            raise SafeError("IDENTITY_NUMERIC_FIELD_INVALID")
    if not 0 <= identity["shardId"] <= 0xffffffff or identity["rowId"] <= 0:
        raise SafeError("IDENTITY_NUMERIC_FIELD_INVALID")
    if (identity["createTime"] != item["createdAtUnix"]
            or identity["sortSequence"] != item["sortSequence"]
            or ("serverId" in item and identity["serverId"] != item["serverId"])):
        raise SafeError("IDENTITY_MESSAGE_FIELDS_MISMATCH")
    return identity


def load_selection(directory):
    private_directory(directory)
    private_directory(directory / "pages")
    summary = load_json(directory / "summary.json", 2 * 1024 * 1024)
    if (not isinstance(summary, dict) or summary.get("schema") != "wechat.single-chat-read.v1"
            or summary.get("ok") is not True):
        raise SafeError("SUCCESSFUL_SINGLE_GROUP_SUMMARY_REQUIRED")
    conversation, source_identity = summary.get("conversationId"), summary.get("sourceIdentity")
    if not isinstance(conversation, str) or not conversation.endswith("@chatroom"):
        raise SafeError("EXACT_GROUP_SCOPE_REQUIRED")
    if not isinstance(source_identity, str) or not source_identity:
        raise SafeError("SOURCE_IDENTITY_REQUIRED")
    since, until = summary.get("sinceUnix"), summary.get("untilUnix")
    if type(until) is not int or (since is not None and type(since) is not int):
        raise SafeError("SUMMARY_TIME_BOUNDS_INVALID")
    paths = sorted((directory / "pages").glob("page-*.json"))
    if not paths or len(paths) > 100 or len(paths) != summary.get("pageCount"):
        raise SafeError("PAGE_COUNT_MISMATCH")
    items, seen, cursors = [], set(), set()
    source_mode = None
    for number, path in enumerate(paths, 1):
        if path.name != f"page-{number:03d}.json":
            raise SafeError("PAGE_SEQUENCE_INVALID")
        page = load_json(path, 16 * 1024 * 1024)
        if (not isinstance(page, dict) or page.get("schema") != "greenbubbles.query.v1"
                or page.get("formatVersion") != 1 or page.get("ok") is not True
                or page.get("operation") != "messages.list"):
            raise SafeError("PAGE_ENVELOPE_INVALID")
        source = page.get("source", {})
        if not isinstance(source, dict) or source.get("identity") != source_identity:
            raise SafeError("PAGE_SOURCE_MISMATCH")
        mode = source.get("mode")
        if source_mode is None:
            source_mode = mode
        elif mode != source_mode:
            raise SafeError("PAGE_SOURCE_MODE_MISMATCH")
        rows, pagination = page.get("items"), page.get("page")
        if (not isinstance(rows, list) or not isinstance(pagination, dict)
                or pagination.get("returned") != len(rows)
                or type(pagination.get("hasMore")) is not bool):
            raise SafeError("PAGE_SHAPE_INVALID")
        cursor = pagination.get("nextCursor")
        if pagination["hasMore"]:
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise SafeError("PAGE_CURSOR_INVALID")
            cursors.add(cursor)
        elif number != len(paths) or cursor not in (None, ""):
            raise SafeError("PAGE_TERMINATION_INVALID")
        for item in rows:
            if not isinstance(item, dict) or item.get("conversationId") != conversation:
                raise SafeError("PAGE_CONVERSATION_MISMATCH")
            token, timestamp, sequence = item.get("id"), item.get("createdAtUnix"), item.get("sortSequence")
            if (not isinstance(token, str) or not token or type(timestamp) is not int
                    or type(sequence) is not int or token in seen):
                raise SafeError("PAGE_MESSAGE_IDENTITY_INVALID_OR_DUPLICATE")
            if timestamp > until or (since is not None and timestamp < since):
                raise SafeError("PAGE_MESSAGE_OUTSIDE_TIME_WINDOW")
            seen.add(token)
            items.append(item)
        if len(items) > MAX_SELECTED:
            raise SafeError("MESSAGE_COUNT_LIMIT")
    if len(items) != summary.get("messages"):
        raise SafeError("SUMMARY_MESSAGE_COUNT_MISMATCH")
    items.sort(key=lambda item: (item["createdAtUnix"], item["sortSequence"]))
    selections = {}
    for ordinal, item in enumerate(items, 1):
        if empty_quote(item):
            identity = exact_identity(item, conversation, source_identity)
            selections.setdefault(identity["shardId"], []).append((ordinal, item["id"], identity))
    return summary, items, selections, source_mode


def shard_paths(root, shard_ids):
    # Numeric IDs share one namespace: business shards start at 1,000,000.
    for path in (root, root / "message"):
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise SafeError("SOURCE_ROOT_OR_MESSAGE_DIRECTORY_INVALID")
    inventory = {}
    with os.scandir(root / "message") as entries:
        for count, entry in enumerate(entries, 1):
            if count > 100_000:
                raise SafeError("SOURCE_INVENTORY_LIMIT")
            match = re.fullmatch(r"(message_|biz_message_)([0-9]+)\.db", entry.name)
            if not match:
                continue
            number = int(match[2]) + (1_000_000 if match[1] == "biz_message_" else 0)
            if number in inventory or number > 0xffffffff:
                raise SafeError("SOURCE_SHARD_ID_AMBIGUOUS")
            inventory[number] = root / "message" / entry.name
    if not set(shard_ids) <= inventory.keys():
        raise SafeError("SELECTED_SHARD_MISSING")
    return {number: inventory[number] for number in shard_ids}


def verify_source_identity(root, source_identity, mode):
    # GreenBubbles 0.10 source identity binds canonical path + device/inode and
    # access mode. A moved/copy source needs a fresh reader export, not an ID bypass.
    if mode not in ("liveEncrypted", "snapshotEncrypted", "decrypted"):
        raise SafeError("SOURCE_IDENTITY_MODE_UNSUPPORTED")
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise SafeError("SOURCE_ROOT_NOT_REAL_DIRECTORY")
    canonical = root.resolve(strict=True)
    meta = canonical.stat()
    digest = hashlib.sha256(b"greenbubbles-live-source-v1\x00" + mode.encode() + b"\x00"
                            + os.fsencode(canonical) + meta.st_dev.to_bytes(8, "little")
                            + meta.st_ino.to_bytes(8, "little")).hexdigest()[:32]
    if source_identity != "sha256:" + digest:
        raise SafeError("CURRENT_SOURCE_IDENTITY_MISMATCH_REREAD_SOURCE")


def batches(selection):
    current = []
    for row in selection:
        if current and (len(current) >= MAX_BATCH
                        or row[2]["createTime"] - current[0][2]["createTime"] >= MAX_WINDOW):
            yield current
            current = []
        current.append(row)
    if current:
        yield current


def stdin_secret():
    value = sys.stdin.buffer.read(4097)
    if len(value) == 32:
        return value
    stripped = value.strip()
    if len(stripped) == 64:
        try:
            result = bytes.fromhex(stripped.decode("ascii"))
            if len(result) == 32:
                return result
        except (ValueError, UnicodeError):
            pass
    raise SafeError("INVALID_STDIN_ACCOUNT_PASSPHRASE_FORMAT")


def perform(args):
    directory = args.directory
    stage = checked_lock(directory, args._lock_token)
    summary, items, selections, source_mode = load_selection(directory)
    for name in OUTPUTS:
        if os.path.lexists(directory / name):
            raise SafeError("RECOVERY_OUTPUT_ALREADY_EXISTS")
    source_value = str(args.source) if args.source else summary.get("sourceRoot")
    if not isinstance(source_value, str) or not source_value:
        raise SafeError("SOURCE_REQUIRED_FOR_LEGACY_SUMMARY")
    root = Path(source_value).expanduser()
    access_mode = summary.get("accessMode")
    expected_decrypted = source_mode == "decrypted" or access_mode == "decrypted"
    expected_encrypted = (source_mode in ("liveEncrypted", "snapshotEncrypted")
                          or access_mode == "passphrase-stdin")
    if (args.decrypted and expected_encrypted) or (not args.decrypted and expected_decrypted):
        raise SafeError("SOURCE_ACCESS_MODE_MISMATCH")
    # The source path is user-supplied/export metadata. Identity, group digest and
    # rowId/serverId/time/sort are independently checked before any merge.
    secret = None if args.decrypted or not selections else stdin_secret()
    maximum = args.max_source_mib * 1024 * 1024
    recovered, reports = [], []
    if selections:
        verify_source_identity(root, summary["sourceIdentity"], source_mode)
    paths = shard_paths(root, selections) if selections else {}
    for shard_id, selection in sorted(selections.items()):
        encrypted, wal = stable_capture(paths[shard_id], maximum)
        if args.decrypted:
            plaintext, report = materialize_decrypted(encrypted, wal, maximum)
        else:
            plaintext, report = decrypt_capture(encrypted, wal, secret, maximum)
        del encrypted, wal
        windows = []
        try:
            for batch in batches(selection):
                since, until = batch[0][2]["createTime"], batch[-1][2]["createTime"]
                rows = recover_rows(plaintext, summary["conversationId"], since, until,
                                    MAX_BATCH, batch, args.zstd_library)
                for row in rows:
                    row["shardId"] = shard_id
                recovered.extend(rows)
                windows.append({"sinceUnix": since, "untilUnix": until, "selected": len(batch)})
        finally:
            plaintext[:] = bytes(len(plaintext))
        reports.append({"shardId": shard_id, "selected": len(selection), "windows": windows, **report})
    expected = {token: identity for selection in selections.values() for _, token, identity in selection}
    by_id = {}
    for row in recovered:
        token = row["id"]
        identity = expected.get(token)
        if (token in by_id or identity is None or row["shardId"] != identity["shardId"]
                or row["rowid"] != identity["rowId"] or row["serverId"] != identity["serverId"]
                or row["sortSequence"] != identity["sortSequence"]
                or row["createdAtUnix"] != identity["createTime"]):
            raise SafeError("RECOVERY_IDENTITY_COVERAGE_MISMATCH")
        by_id[token] = row
    if by_id.keys() != expected.keys():
        raise SafeError("RECOVERY_IDENTITY_COVERAGE_MISMATCH")
    merged = copy.deepcopy(items)
    for item in merged:
        row = by_id.get(item["id"])
        if row:
            for key in ("content", "contentDecodeState", "contentTruncated"):
                item[key] = row[key]
    from read_chat import transcript
    text, after, total_quotes = transcript(merged, timezone=ZoneInfo(summary.get("timezone", "Asia/Shanghai")))
    before = sum(empty_quote(item) for item in items)
    report = {
        "schema": "wechat.quote-recovery.v2", "ok": True,
        "messages": len(items), "quotes": total_quotes, "selectedQuotes": len(expected),
        "recoveredQuotes": len(recovered), "initialEmptyQuotes": before, "emptyQuotes": after,
        "allSelectedIdentitiesMatched": True, "allSelectedRepliesReadable": after == 0,
        "shards": reports, "accessMode": "decrypted" if args.decrypted else "passphrase-stdin",
        "originalPagesPreserved": True, "originalSummaryPreserved": True,
        "isSample": summary.get("isSample"), "paginationExhausted": summary.get("paginationExhausted"),
        "sourceCoverageComplete": summary.get("sourceCoverageComplete"),
        "recordCoverageComplete": summary.get("recordCoverageComplete"),
        "scope": "Only empty Quote identities already selected in this single-group export.",
        "notice": "Quote recovery does not expand export coverage or decode other media. All output remains private.",
        "memoryNotice": "Involved shards and committed WAL are materialized in RAM; only selected rows are exported.",
    }
    recovered.sort(key=lambda row: row["originalOrdinal"])
    private_write(stage / OUTPUTS[0], json_bytes({"schema": "wechat.recovered-quotes.v2", "items": recovered}))
    private_write(stage / OUTPUTS[1], text.encode())
    private_write(stage / OUTPUTS[2], json_bytes(report))
    # Stage all files first. Hard links publish without replacing an existing file;
    # the final summary is the commit marker. Parent removes this run's links on
    # failure/timeout, using inode equality so unrelated files are never removed.
    for name in OUTPUTS:
        os.link(stage / name, directory / name)
    print(json.dumps({"ok": True, "selectedQuotes": len(expected), "recoveredQuotes": len(recovered),
                      "initialEmptyQuotes": before, "emptyQuotes": after, "shards": len(reports),
                      "outputDirectory": str(directory)}, ensure_ascii=False))
    return 0


def run(args):
    if not 1 <= args.max_source_mib <= 2048 or not 1 <= args.timeout <= 3600:
        raise SafeError("RESOURCE_BOUND_INVALID")
    if args._worker:
        return perform(args)
    key = None
    lock_token, success = acquire_lock(args.directory), False
    try:
        if args.key_file:
            meta = os.lstat(args.key_file)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                    or (os.name != "nt" and (meta.st_uid != os.geteuid() or meta.st_mode & 0o077))):
                raise SafeError("KEY_FILE_MUST_BE_PRIVATE_REGULAR_FILE")
            fd = os.open(args.key_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_BINARY", 0))
            key = os.fdopen(fd, "rb")
            opened = os.fstat(key.fileno())
            if (opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino):
                raise SafeError("KEY_FILE_CHANGED_DURING_OPEN")
        command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:],
                   "--_worker", "--_lock-token", lock_token]
        try:
            result = subprocess.run(command, stdin=key if key else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=args.timeout, check=False, close_fds=True)
        except subprocess.TimeoutExpired:
            raise SafeError("RECOVERY_TIMEOUT_CHECK_ACCESS_AND_OUTPUT_DIRECTORY") from None
        except OSError:
            raise SafeError("RECOVERY_WORKER_START_FAILED") from None
        # Only the fixed JSON diagnostics/summary from our worker are released.
        stream = result.stdout if result.returncode == 0 else result.stderr
        try:
            response = json.loads(stream)
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise ValueError()
            if result.returncode and (not isinstance(response.get("errorCode"), str)
                                      or not re.fullmatch(r"[A-Z0-9_]+", response["errorCode"])):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise SafeError("RECOVERY_WORKER_FAILED") from None
        print(json.dumps(response, ensure_ascii=False), file=sys.stdout if result.returncode == 0 else sys.stderr)
        success = result.returncode == 0
        return 0 if result.returncode == 0 else 2
    finally:
        if key is not None:
            key.close()
        release_lock(args.directory, lock_token, success)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path, help="Existing reader output with summary.json and pages/")
    parser.add_argument("--source", type=Path, help="Database root; defaults to summary sourceRoot (required for old summaries)")
    access = parser.add_mutually_exclusive_group(required=True)
    access.add_argument("--key-file", type=Path, help="Existing owner-only account passphrase file; sent only as worker stdin")
    access.add_argument("--decrypted", action="store_true", help="Source already contains plaintext SQLite; do not use a key")
    parser.add_argument("--zstd-library", type=Path, help="Optional trusted libzstd shared library path")
    parser.add_argument("--max-source-mib", type=int, default=512, help="Per source DB/WAL and plaintext size limit, 1..2048 MiB")
    parser.add_argument("--timeout", type=int, default=300, help="Entire worker timeout, 1..3600 seconds (default: 300)")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_lock-token", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return run(args)
    except SafeError as exc:
        print(json.dumps({"ok": False, "errorCode": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"ok": False, "errorCode": "PRIVATE_RECOVERY_FAILED"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
