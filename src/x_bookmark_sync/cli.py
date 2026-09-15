"""Fetch, merge, and export X bookmarks without third-party dependencies."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode


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
    folders = parser.add_mutually_exclusive_group()
    folders.add_argument(
        "--folder",
        metavar="NAME_OR_ID",
        help="archive only bookmarks from this folder name or numeric ID",
    )
    folders.add_argument(
        "--folder-id",
        metavar="ID",
        help="archive only bookmarks from this numeric folder ID",
    )
    folders.add_argument(
        "--list-folders",
        action="store_true",
        help="list bookmark folder names and IDs, then exit",
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


def _run_xurl(command: list[str], source: str) -> Any:
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
            f"{source} failed with exit status {result.returncode}; run 'xurl auth status' to check authentication"
        )
    return _decode_json(result.stdout, source)


def fetch_bookmarks(limit: int) -> Any:
    return _run_xurl(["xurl", "bookmarks", "-n", str(limit)], "xurl bookmarks")


def fetch_current_user_id() -> str:
    payload = _run_xurl(["xurl", "whoami"], "xurl whoami")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise BookmarkSyncError("malformed xurl whoami output: expected a data object")
    return _required_string(payload["data"], "id", "authenticated user")


def fetch_bookmark_folders(user_id: str) -> list[dict[str, str]]:
    folders: list[dict[str, str]] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        query: dict[str, str | int] = {"max_results": 100}
        if token:
            query["pagination_token"] = token
        url = f"/2/users/{user_id}/bookmarks/folders?{urlencode(query)}"
        payload = _run_xurl(["xurl", url], "xurl bookmark folders")
        if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
            raise BookmarkSyncError("malformed bookmark folders output: 'data' must be a list")
        if payload.get("errors"):
            raise BookmarkSyncError("X API returned an error while listing bookmark folders")
        for index, folder in enumerate(payload.get("data", [])):
            if not isinstance(folder, dict):
                raise BookmarkSyncError(f"malformed bookmark folders output: folder {index} is not an object")
            folders.append(
                {
                    "id": _required_string(folder, "id", f"folder {index}"),
                    "name": _required_string(folder, "name", f"folder {index}"),
                }
            )
        meta = payload.get("meta", {})
        next_token = meta.get("next_token") if isinstance(meta, dict) else None
        if not next_token:
            break
        token = str(next_token)
        if token in seen_tokens:
            raise BookmarkSyncError("bookmark folder pagination returned a repeated token")
        seen_tokens.add(token)
    return sorted(folders, key=lambda item: (item["name"].casefold(), item["id"]))


def resolve_folder(folders: list[dict[str, str]], value: str, id_only: bool = False) -> dict[str, str]:
    if id_only or value.isdigit():
        matches = [folder for folder in folders if folder["id"] == value]
    else:
        matches = [folder for folder in folders if folder["name"].casefold() == value.casefold()]
    if not matches:
        raise BookmarkSyncError(f"bookmark folder not found: {value}")
    if len(matches) > 1:
        ids = ", ".join(folder["id"] for folder in matches)
        raise BookmarkSyncError(f"multiple bookmark folders are named {value!r}; use --folder-id with one of: {ids}")
    return matches[0]


def fetch_folder_post_ids(user_id: str, folder_id: str) -> list[str]:
    url = f"/2/users/{user_id}/bookmarks/folders/{folder_id}"
    payload = _run_xurl(["xurl", url], "xurl folder bookmarks")
    if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
        raise BookmarkSyncError("malformed folder bookmarks output: 'data' must be a list")
    if payload.get("errors"):
        raise BookmarkSyncError("X API returned an error while fetching folder bookmarks")
    post_ids = []
    for index, post in enumerate(payload.get("data", [])):
        if not isinstance(post, dict):
            raise BookmarkSyncError(f"malformed folder bookmarks output: post {index} is not an object")
        post_ids.append(_required_string(post, "id", f"folder post {index}"))
    meta = payload.get("meta", {})
    if isinstance(meta, dict) and meta.get("next_token"):
        print(
            "warning: X returned more folder bookmarks, but its documented folder endpoint does not accept a pagination token; this export contains the available page only",
            file=sys.stderr,
        )
    return post_ids


def hydrate_posts(post_ids: list[str]) -> dict[str, Any]:
    all_posts: list[Any] = []
    users_by_id: dict[str, Any] = {}
    for start in range(0, len(post_ids), 100):
        chunk = post_ids[start : start + 100]
        query = urlencode(
            {
                "ids": ",".join(chunk),
                "tweet.fields": "created_at,author_id",
                "expansions": "author_id",
                "user.fields": "id,name,username",
            }
        )
        payload = _run_xurl(["xurl", f"/2/tweets?{query}"], "xurl post lookup")
        if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
            raise BookmarkSyncError("malformed post lookup output: 'data' must be a list")
        all_posts.extend(payload.get("data", []))
        includes = payload.get("includes", {})
        if isinstance(includes, dict) and isinstance(includes.get("users", []), list):
            for user in includes["users"]:
                if isinstance(user, dict) and user.get("id") is not None:
                    users_by_id[str(user["id"])] = user
    return {"data": all_posts, "includes": {"users": list(users_by_id.values())}}


def fetch_folder_bookmarks(user_id: str, folder_id: str) -> Any:
    return hydrate_posts(fetch_folder_post_ids(user_id, folder_id))


def folder_slug(folder: dict[str, str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", folder["name"].casefold()).strip("-") or "folder"
    return f"{slug}-{folder['id']}"


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
    if args.input and (args.folder or args.folder_id):
        raise BookmarkSyncError("--input cannot be combined with --folder or --folder-id")
    if args.folder_id and (not args.folder_id.isdigit() or len(args.folder_id) > 19):
        raise BookmarkSyncError("--folder-id must be a numeric X folder ID of at most 19 digits")

    if args.input:
        raw_payload = read_input(args.input.expanduser())
    elif args.folder or args.folder_id:
        user_id = fetch_current_user_id()
        folders = fetch_bookmark_folders(user_id)
        value = args.folder_id or args.folder
        folder = resolve_folder(folders, value, id_only=bool(args.folder_id))
        output_dir = output_dir / "folders" / folder_slug(folder)
        raw_payload = fetch_folder_bookmarks(user_id, folder["id"])
    else:
        raw_payload = fetch_bookmarks(args.limit)

    archive_path = output_dir / "bookmarks.json"
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
        if args.list_folders:
            if args.input:
                raise BookmarkSyncError("--input cannot be combined with --list-folders")
            folders = fetch_bookmark_folders(fetch_current_user_id())
            if not folders:
                print("No bookmark folders found.")
            else:
                print("ID\tNAME")
                for folder in folders:
                    print(f"{folder['id']}\t{folder['name']}")
            return 0
        bookmarks, paths = run(args)
    except BookmarkSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Archived {len(bookmarks)} bookmarks.")
    for path in paths:
        print(f"Wrote {path.resolve()}")
    return 0
