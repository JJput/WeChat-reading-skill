#!/usr/bin/env python3
"""Read one explicitly selected WeChat group with GreenBubbles.

Requires Python 3.9+ on a POSIX system and greenbubbles on PATH. Supply an
existing source plus either an already-held private key file or --decrypted.
Only chats find and messages list are invoked, with explicit source/access
arguments; no default-account discovery or credential acquisition is used.
Keys go directly from the file to child stdin, never into argv or logs.
All output, including the lightly redacted transcript, remains private.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Optional

if sys.version_info < (3, 9):
    raise SystemExit("Python 3.9 or newer is required.")

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 128 * 1024 * 1024


class ReadError(Exception):
    """Fixed diagnostic codes only; no private values or original exceptions."""


def get_timezone(name: str):
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ReadError("TIMEZONE_UNAVAILABLE_USE_AN_INSTALLED_IANA_ZONE") from None


def time_bound(value: Optional[str], *, until: bool = False, timezone=None) -> Optional[int]:
    if value is None:
        return None
    timezone = timezone or get_timezone(DEFAULT_TIMEZONE)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date = dt.date.fromisoformat(value)
            clock = dt.time(23, 59, 59) if until else dt.time()
            stamp = dt.datetime.combine(date, clock, timezone)
        else:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone)
        seconds = stamp.timestamp()
        return math.floor(seconds) if until else math.ceil(seconds)
    except (ValueError, OverflowError):
        raise ReadError("INVALID_TIME_USE_ISO_OR_YYYY_MM_DD") from None


def iso_time(value: Optional[int], timezone=None) -> Optional[str]:
    timezone = timezone or get_timezone(DEFAULT_TIMEZONE)
    return dt.datetime.fromtimestamp(value, timezone).isoformat() if value is not None else None


def private_output_parent(selected: Optional[str]) -> Path:
    path = (Path(selected).expanduser() if selected else
            Path.home() / ".wechat-chat-reader" / "reads").absolute()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    meta = path.lstat()
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid()
            or stat.S_IMODE(meta.st_mode) & 0o077):
        raise ReadError("OUTPUT_PARENT_NOT_PRIVATE_OWNED_DIRECTORY")
    return path


def private_write(path: Path, value: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(value)
        out.flush()
        os.fsync(out.fileno())


def private_json(path: Path, value: object) -> None:
    private_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def cli_json(arguments: list[str], timeout: int, cli: str,
             key_file: Optional[Path]) -> tuple[bytes, object]:
    # Source is always positional in arguments. Explicit access bypasses CLI
    # profiles and implicit-account discovery. Do not read credentials here.
    key_input = None
    if key_file is not None:
        try:
            fd = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            key_input = os.fdopen(fd, "rb")
        except OSError:
            raise ReadError("KEY_FILE_UNAVAILABLE_OR_UNSAFE") from None
        meta = os.fstat(key_input.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid()
                or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_nlink != 1):
            key_input.close()
            raise ReadError("KEY_FILE_NOT_PRIVATE_REGULAR_FILE")
    try:
        # Spool stdout privately rather than buffering an unbounded response.
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run(
                [cli, *arguments, "--passphrase-stdin" if key_input else "--decrypted"],
                stdin=key_input if key_input else subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False, close_fds=True,
            )
            output.seek(0)
            raw = output.read(MAX_RESPONSE_BYTES + 1)
    except subprocess.TimeoutExpired:
        raise ReadError("CLI_TIMEOUT") from None
    except OSError:
        raise ReadError("CLI_START_FAILED") from None
    finally:
        if key_input is not None:
            key_input.close()
    if result.returncode:
        raise ReadError("CLI_FAILED")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReadError("CLI_RESPONSE_TOO_LARGE")
    try:
        return raw, json.loads(raw)
    except (ValueError, UnicodeError):
        raise ReadError("CLI_INVALID_JSON") from None


def resolve_conversation(selector: str, database_root: str, timeout: int,
                         cli: str, key_file: Optional[Path]) -> str:
    if selector.startswith("wxid_") and not selector.endswith("@chatroom"):
        raise ReadError("GROUP_SELECTOR_REQUIRES_CHATROOM")
    if selector.endswith("@chatroom"):
        return selector
    # Resolve metadata first so an empty message page still has an exact scope.
    # Never print candidate names/IDs. Ambiguous names require a narrower selector.
    _, candidates = cli_json([
        "chats", "find", selector, database_root,
        "--limit", "500",
    ], timeout, cli, key_file)
    if not isinstance(candidates, list):
        raise ReadError("CHAT_LOOKUP_RESPONSE_INVALID")
    if any(not isinstance(c, dict) or not isinstance(c.get("id"), str)
           or not c["id"] for c in candidates):
        raise ReadError("CHAT_LOOKUP_RESPONSE_INVALID")
    groups = [c for c in candidates if c["id"].endswith("@chatroom")]
    needle = selector.casefold()
    exact = [c for c in groups if any(isinstance(c.get(k), str) and c[k].casefold() == needle
             for k in ("id", "displayName", "remark", "nickname", "alias"))]
    choices = exact if exact else groups
    identities = {c["id"] for c in choices}
    if not identities:
        raise ReadError("CONVERSATION_NOT_FOUND")
    if len(identities) != 1:
        raise ReadError("CONVERSATION_NAME_AMBIGUOUS_USE_EXACT_ID")
    conversation = next(iter(identities))
    if conversation.startswith("-") or any(ord(c) < 32 for c in conversation):
        raise ReadError("CHAT_LOOKUP_RESPONSE_INVALID")
    return conversation


def validate_page(response, conversation, since, until, limit, source_identity, seen):
    if (not isinstance(response, dict) or response.get("schema") != "greenbubbles.query.v1"
            or response.get("formatVersion") != 1 or response.get("ok") is not True
            or response.get("operation") != "messages.list"):
        raise ReadError("UNEXPECTED_RESPONSE_ENVELOPE")
    source = response.get("source")
    identity = source.get("identity") if isinstance(source, dict) else None
    if not isinstance(identity, str) or not identity:
        raise ReadError("SOURCE_IDENTITY_MISSING")
    if source_identity is not None and identity != source_identity:
        raise ReadError("SOURCE_IDENTITY_CHANGED")
    items, page = response.get("items"), response.get("page")
    if not isinstance(items, list) or not isinstance(page, dict):
        raise ReadError("PAGE_SHAPE_INVALID")
    if (len(items) > limit or type(page.get("returned")) is not int
            or page["returned"] != len(items) or type(page.get("hasMore")) is not bool):
        raise ReadError("PAGE_COUNT_INVALID")
    next_cursor = page.get("nextCursor")
    if page["hasMore"] and (not isinstance(next_cursor, str) or not next_cursor):
        raise ReadError("NEXT_CURSOR_MISSING")
    if not page["hasMore"] and next_cursor not in (None, ""):
        raise ReadError("TERMINAL_PAGE_HAS_CURSOR")
    local_ids = set()
    for item in items:
        if not isinstance(item, dict) or item.get("conversationId") != conversation:
            raise ReadError("CONVERSATION_SCOPE_MISMATCH")
        message_id, created, sequence = item.get("id"), item.get("createdAtUnix"), item.get("sortSequence")
        if (not isinstance(message_id, str) or not message_id
                or type(created) is not int or type(sequence) is not int):
            raise ReadError("MESSAGE_IDENTITY_INVALID")
        if created > until or (since is not None and created < since):
            raise ReadError("MESSAGE_OUTSIDE_TIME_WINDOW")
        if message_id in seen or message_id in local_ids:
            raise ReadError("DUPLICATE_MESSAGE_ID")
        local_ids.add(message_id)
    warnings = response.get("warnings", [])
    if not isinstance(warnings, list):
        raise ReadError("WARNINGS_SHAPE_INVALID")
    return identity, local_ids, next_cursor


def message_text(item):
    content = item.get("content")
    if isinstance(content, dict):
        if isinstance(content.get("Text"), str):
            return content["Text"], False, False
        if "Quote" in content:
            quote = content["Quote"] if isinstance(content["Quote"], dict) else {}
            reply, reference = quote.get("reply_text"), quote.get("refer_content")
            empty = not isinstance(reply, str) or not reply.strip()
            text = "[引用回复正文未解析]" if empty else reply
            if isinstance(reference, str) and reference.strip():
                # Keep interpretation separate; do not guess embedded attachment XML.
                reference = ("[引用非纯文本内容]" if is_embedded_xml(reference)
                             else reference)
                text += " 〔引用他人：" + reference + "〕"
            elif not empty:
                text += " 〔被引用正文未解析或不可用〕"
            return text, empty, True
        return "[非文本消息：" + "/".join(map(str, content.keys())) + "]", False, False
    return "[非文本消息]", False, False


def is_embedded_xml(value: str) -> bool:
    # Some restored quotes prefix XML with a sender ID. Do not expose embedded
    # media access attributes by assuming XML always begins at character zero.
    return (value.lstrip().startswith("<") or "<?xml" in value
            or re.search(r"<(?:msg|img|appmsg|voicemsg|videomsg|emoji)\b", value, re.I) is not None)


def transcript(items, timezone=None):
    timezone = timezone or get_timezone(DEFAULT_TIMEZONE)
    rows, empty_quotes, quotes = [], 0, 0
    for number, item in enumerate(sorted(items, key=lambda m: (m["createdAtUnix"], m["sortSequence"])), 1):
        text, empty, quote = message_text(item)
        empty_quotes += int(empty)
        quotes += int(quote)
        if is_embedded_xml(text):
            text = "[结构化非文本内容未展开]"
        text = re.sub(r"https?://\S+", "[链接]", text)
        text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", text)
        text = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "[证件号]", text)
        text = re.sub(r"wxid_[A-Za-z0-9_]+", "[微信ID]", text)
        text = re.sub(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]", text)
        text = text.replace("\r", " ").replace("\n", " / ")
        sender = "群友" + hashlib.sha256(str(item.get("sender", "")).encode()).hexdigest()[:6]
        created = dt.datetime.fromtimestamp(item["createdAtUnix"], timezone)
        rows.append(f"[{number:05d}] {created:%Y-%m-%d %H:%M:%S} {sender}: {text}")
    heading = "私有分析文本：已做基础信息替换，仍可能含可识别细节；请勿直接公开。\n"
    return heading + "\n".join(rows) + "\n", empty_quotes, quotes


def run(args):
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ReadError("POSIX_PRIVATE_FILE_SUPPORT_REQUIRED")
    timezone = get_timezone(args.timezone)
    since = time_bound(args.since, timezone=timezone)
    started = dt.datetime.now(timezone)
    until = (time_bound(args.until, until=True, timezone=timezone) if args.until
             else int(started.timestamp()))
    if args.all and since is None:
        raise ReadError("ALL_REQUIRES_SINCE")
    if since is not None and since > until:
        raise ReadError("SINCE_AFTER_UNTIL")
    if not 1 <= args.limit <= 500 or not 1 <= args.timeout <= 300:
        raise ReadError("LIMIT_OR_TIMEOUT_OUT_OF_RANGE")
    selector = args.conversation
    if (not args.source.strip() or any(ord(c) < 32 for c in args.source)
            or not isinstance(selector, str) or not selector.strip()
            or selector != selector.strip() or any(ord(c) < 32 for c in selector)
            or selector.startswith("-")):
        raise ReadError("SOURCE_OR_SELECTOR_INVALID")
    database_root = str(Path(args.source).expanduser().absolute())
    if args.key_file is not None and (not args.key_file.strip()
            or any(ord(c) < 32 for c in args.key_file)):
        raise ReadError("KEY_FILE_PATH_INVALID")
    key_file = Path(args.key_file).expanduser().absolute() if args.key_file is not None else None
    cli = shutil.which(os.path.expanduser(args.cli))
    if cli is None:
        raise ReadError("GREENBUBBLES_NOT_ON_PATH")
    # Do not stat the WeChat source path: a macOS privacy prompt can block it.
    # The child process accesses the source under the bounded timeout instead.
    parent = private_output_parent(args.output_parent)
    directory = Path(tempfile.mkdtemp(prefix=started.strftime("read-%Y%m%d-%H%M%S-"), dir=parent))
    os.chmod(directory, 0o700)
    pages = directory / "pages"
    pages.mkdir(mode=0o700)
    items, seen, cursors, warning_codes = [], set(), set(), collections.Counter()
    warning_count, page_count = 0, 0
    conversation, source_identity, cursor, error = None, None, None, None
    reached_end, coverage_complete, has_more = False, True, None
    try:
        conversation = resolve_conversation(selector, database_root, args.timeout, cli, key_file)
        for number in range(1, MAX_PAGES + 1 if args.all else 2):
            command = ["messages", "list", database_root, "--conversation", conversation,
                       "--until", str(until), "--limit", str(args.limit), "--json"]
            if since is not None:
                command += ["--since", str(since)]
            if cursor is not None:
                command += ["--cursor", cursor]
            raw, response = cli_json(command, args.timeout, cli, key_file)
            identity, ids, next_cursor = validate_page(response, conversation, since, until, args.limit, source_identity, seen)
            has_more = response["page"]["hasMore"]
            if has_more and next_cursor in cursors:
                raise ReadError("REPEATED_PAGINATION_CURSOR")
            private_write(pages / f"page-{number:03d}.json", raw)
            page_count += 1
            source_identity = identity
            seen.update(ids)
            items.extend(response["items"])
            warnings = response.get("warnings", [])
            warning_count += len(warnings)
            for warning in warnings:
                code = warning.get("code") if isinstance(warning, dict) else None
                warning_codes[code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else "unspecified"] += 1
            consistency = response.get("consistency", {})
            coverage_complete = coverage_complete and isinstance(consistency, dict) and consistency.get("coverageComplete") is True
            if not has_more:
                reached_end = True
                break
            if not args.all:
                break
            cursors.add(next_cursor)
            cursor = next_cursor
        if args.all and not reached_end:
            raise ReadError("MAX_100_PAGES_REACHED")
    except ReadError as exc:
        error = str(exc)
    except Exception:
        error = "PRIVATE_READ_FAILED"
    text, empty_quotes, quote_count = transcript(items, timezone)
    private_write(directory / "transcript.txt", text.encode())
    exhausted = bool(args.all and reached_end and error is None)
    summary = {
        "schema": "wechat.single-chat-read.v1", "ok": error is None, "errorCode": error,
        "mode": "all" if args.all else "sample", "isSample": not args.all,
        "startedAt": started.isoformat(), "finishedAt": dt.datetime.now(timezone).isoformat(),
        "timezone": args.timezone, "since": iso_time(since, timezone), "until": iso_time(until, timezone),
        "sinceUnix": since, "untilUnix": until, "conversationId": conversation,
        "sourceRoot": database_root, "accessMode": "passphrase-stdin" if key_file else "decrypted",
        "cliPath": str(Path(cli).absolute()),
        "sourceIdentity": source_identity, "pageSize": args.limit, "pageCount": page_count,
        "messages": len(items), "oldestMessageAt": iso_time(min((m["createdAtUnix"] for m in items), default=None), timezone),
        "newestMessageAt": iso_time(max((m["createdAtUnix"] for m in items), default=None), timezone),
        "paginationExhausted": exhausted, "sourceHasMore": has_more,
        "sourceCoverageComplete": coverage_complete if page_count else False,
        "warningCount": warning_count, "warningCodes": dict(warning_codes),
        "quotes": quote_count, "emptyQuotes": empty_quotes,
        "recordCoverageComplete": exhausted and coverage_complete and warning_count == 0,
        "contentNotice": "Pagination coverage does not imply complete decoding; inspect emptyQuotes and non-text placeholders.",
        "scope": "One explicitly selected group; local records, not server history.",
        "transcriptPrivacy": "Private; basic redaction is not complete anonymization.",
        "sampleNotice": "Sample only; do not claim a full export." if not args.all else None,
    }
    private_json(directory / "summary.json", summary)
    print(json.dumps({"ok": error is None, "errorCode": error, "mode": summary["mode"],
                      "messages": len(items), "pages": page_count, "emptyQuotes": empty_quotes,
                      "paginationExhausted": exhausted, "outputDirectory": str(directory),
                      "summary": str(directory / "summary.json"),
                      "transcript": str(directory / "transcript.txt")}, ensure_ascii=False))
    return 0 if error is None else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conversation", required=True, help="One unambiguous group name or exact @chatroom ID; contacts are excluded")
    parser.add_argument("--source", required=True, help="Existing source directory; never auto-discovered")
    parser.add_argument("--cli", default="greenbubbles", help="Already installed CLI executable name/path (default: greenbubbles on PATH)")
    access = parser.add_mutually_exclusive_group(required=True)
    access.add_argument("--key-file", help="Already-held owner-only regular file, passed directly to CLI stdin")
    access.add_argument("--decrypted", action="store_true", help="The selected source already contains plaintext SQLite databases")
    parser.add_argument("--output-parent", help="Private owned directory; default: ~/.wechat-chat-reader/reads")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Installed IANA timezone (default: Asia/Shanghai)")
    parser.add_argument("--since", help="Inclusive ISO time or YYYY-MM-DD; no offset uses --timezone")
    parser.add_argument("--until", help="Inclusive ISO time; date means 23:59:59 in --timezone; default: fixed start time")
    parser.add_argument("--limit", type=int, default=50, help="Messages per page, 1..500 (default: 50)")
    parser.add_argument("--all", action="store_true", help="Follow at most 100 pages; requires --since; otherwise one sample page")
    parser.add_argument("--timeout", type=int, default=60, help="Per CLI invocation timeout in seconds, 1..300 (default: 60)")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return run(args)
    except ReadError as exc:
        print(json.dumps({"ok": False, "errorCode": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"ok": False, "errorCode": "PRIVATE_OPERATION_FAILED"}), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
