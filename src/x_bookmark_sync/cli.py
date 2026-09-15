"""Fetch, merge, and export X bookmarks without third-party dependencies."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence


ALL_FORMATS = ("json", "csv", "markdown")
CSV_FIELDS = (
    "id",
    "url",
    "text",
    "author_username",
    "author_name",
    "author_id",
    "created_at",
)


class BookmarkSyncError(Exception):
    """An expected, user-actionable sync failure."""


def limit_value(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 100") from error
    if not 1 <= limit <= 100:
        raise argparse.ArgumentTypeError("limit must be from 1 to 100 (xurl's current maximum is 100)")
    return limit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x-bookmark-sync",
        description="Incrementally archive X bookmarks through the official xurl CLI.",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=limit_value,
        default=100,
        help="latest bookmarks to request from xurl (1-100; default: 100)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        metavar="FILE",
        help="read an xurl JSON response from FILE instead of invoking xurl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="export directory (default: current directory)",
    )
    parser.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=ALL_FORMATS,
        help="format to write; repeat as needed (default: all)",
    )
    return parser


def _decode_json(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise BookmarkSyncError(
            f"malformed JSON from {source} (line {error.lineno}, column {error.colno})"
        ) from error


def read_input(path: Path) -> Any:
    try:
        return _decode_json(path.read_text(encoding="utf-8"), str(path))
    except FileNotFoundError as error:
        raise BookmarkSyncError(f"input file not found: {path}") from error
    except (OSError, UnicodeError) as error:
        raise BookmarkSyncError(f"cannot read input file {path}: {error}") from error


def fetch_bookmarks(limit: int) -> Any:
    command = ["xurl", "bookmarks", "-n", str(limit)]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as error:
        raise BookmarkSyncError(
            "xurl was not found on PATH; install the official xurl CLI and try again"
        ) from error
    except OSError as error:
        raise BookmarkSyncError(f"could not start xurl: {error}") from error

    if result.returncode != 0:
        diagnostic = f"{result.stderr}\n{result.stdout}".lower()
        if any(term in diagnostic for term in ("unauthorized", "unauthenticated", "401", "oauth", "token", "login", "authenticate")):
            raise BookmarkSyncError(
                "xurl is not authenticated for bookmark access; complete OAuth 2.0 user authentication and retry"
            )
        raise BookmarkSyncError(
            f"xurl bookmarks failed with exit status {result.returncode}; run 'xurl auth status' to check authentication"
        )
    return _decode_json(result.stdout, "xurl bookmarks")


def _required_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, (str, int)) or str(value) == "":
        raise BookmarkSyncError(f"malformed API output: {context} has no valid {key}")
    return str(value)


def parse_api_response(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise BookmarkSyncError("malformed API output: expected a JSON object")
    if payload.get("errors"):
        errors_text = json.dumps(payload["errors"]).lower()
        if any(term in errors_text for term in ("unauthorized", "authentication", "oauth", "forbidden")):
            raise BookmarkSyncError("xurl returned an authentication error; re-run OAuth 2.0 user authentication")
        raise BookmarkSyncError("X API returned an error response instead of bookmarks")

    data = payload.get("data")
    if not isinstance(data, list):
        raise BookmarkSyncError("malformed API output: 'data' must be a list")
    includes = payload.get("includes", {})
    if not isinstance(includes, dict) or not isinstance(includes.get("users", []), list):
        raise BookmarkSyncError("malformed API output: 'includes.users' must be a list")

    users: dict[str, dict[str, Any]] = {}
    for index, user in enumerate(includes.get("users", [])):
        if not isinstance(user, dict):
            raise BookmarkSyncError(f"malformed API output: user {index} is not an object")
        user_id = _required_string(user, "id", f"user {index}")
        users[user_id] = user

    bookmarks: list[dict[str, str]] = []
    for index, post in enumerate(data):
        if not isinstance(post, dict):
            raise BookmarkSyncError(f"malformed API output: post {index} is not an object")
        post_id = _required_string(post, "id", f"post {index}")
        author_id = _required_string(post, "author_id", f"post {post_id}")
        author = users.get(author_id)
        if author is None:
            raise BookmarkSyncError(
                f"malformed API output: author {author_id} for post {post_id} is missing from includes.users"
            )
        username = _required_string(author, "username", f"author {author_id}")
        bookmarks.append(
            {
                "id": post_id,
                "text": _required_string(post, "text", f"post {post_id}"),
                "author_id": author_id,
                "author_name": _required_string(author, "name", f"author {author_id}"),
                "author_username": username,
                "created_at": str(post.get("created_at", "")),
                "url": f"https://x.com/{username}/status/{post_id}",
            }
        )
    return bookmarks


def _sort_key(bookmark: dict[str, Any]) -> tuple[str, str]:
    return (str(bookmark.get("created_at", "")), str(bookmark["id"]))


def merge_bookmarks(
    existing: Iterable[dict[str, Any]], latest: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source, collection in (("archive", existing), ("latest fetch", latest)):
        for index, bookmark in enumerate(collection):
            if not isinstance(bookmark, dict):
                raise BookmarkSyncError(f"malformed {source}: bookmark {index} is not an object")
            bookmark_id = bookmark.get("id")
            if not isinstance(bookmark_id, (str, int)) or str(bookmark_id) == "":
                raise BookmarkSyncError(f"malformed {source}: bookmark {index} has no valid id")
            normalized = dict(bookmark)
            normalized["id"] = str(bookmark_id)
            merged[str(bookmark_id)] = normalized
    return sorted(merged.values(), key=_sort_key, reverse=True)


def load_archive(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = read_input(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise BookmarkSyncError(f"malformed archive {path}: expected schema_version 1")
    bookmarks = payload.get("bookmarks")
    if not isinstance(bookmarks, list):
        raise BookmarkSyncError(f"malformed archive {path}: 'bookmarks' must be a list")
    return bookmarks


def _atomic_write(path: Path, content: str, newline: str | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline=newline,
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        raise BookmarkSyncError(f"cannot write {path}: {error}") from error


def export_json(path: Path, bookmarks: list[dict[str, Any]]) -> None:
    content = json.dumps(
        {"schema_version": 1, "bookmarks": bookmarks},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    _atomic_write(path, content)


def export_csv(path: Path, bookmarks: list[dict[str, Any]]) -> None:
    buffer = __import__("io").StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for bookmark in bookmarks:
        writer.writerow({field: bookmark.get(field, "") for field in CSV_FIELDS})
    _atomic_write(path, buffer.getvalue(), newline="")


def _markdown_text(text: Any) -> str:
    return str(text).replace("\r\n", "\n").replace("\r", "\n").strip().replace("\n", "  \n")


def export_markdown(path: Path, bookmarks: list[dict[str, Any]]) -> None:
    lines = ["---", "type: x-bookmark-archive", f"bookmark_count: {len(bookmarks)}", "---", "", "# X Bookmarks", ""]
    for bookmark in bookmarks:
        username = str(bookmark.get("author_username", "unknown"))
        name = str(bookmark.get("author_name", username))
        created_at = str(bookmark.get("created_at", ""))
        date_suffix = f" · {created_at}" if created_at else ""
        lines.extend(
            [
                f"## [{name} (@{username})]({bookmark.get('url', '')})",
                "",
                _markdown_text(bookmark.get("text", "")),
                "",
                f"[Open on X]({bookmark.get('url', '')}) · Post ID `{bookmark['id']}`{date_suffix}",
                "",
                "---",
                "",
            ]
        )
    _atomic_write(path, "\n".join(lines))


def run(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[Path]]:
    output_dir = args.output_dir.expanduser()
    archive_path = output_dir / "bookmarks.json"
    raw_payload = read_input(args.input.expanduser()) if args.input else fetch_bookmarks(args.limit)
    latest = parse_api_response(raw_payload)
    merged = merge_bookmarks(load_archive(archive_path), latest)
    formats = tuple(dict.fromkeys(args.formats or ALL_FORMATS))
    paths: list[Path] = []
    exporters = {
        "json": (archive_path, export_json),
        "csv": (output_dir / "bookmarks.csv", export_csv),
        "markdown": (output_dir / "bookmarks.md", export_markdown),
    }
    for output_format in formats:
        path, exporter = exporters[output_format]
        exporter(path, merged)
        paths.append(path)
    return merged, paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bookmarks, paths = run(args)
    except BookmarkSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Archived {len(bookmarks)} bookmarks.")
    for path in paths:
        print(f"Wrote {path.resolve()}")
    return 0
