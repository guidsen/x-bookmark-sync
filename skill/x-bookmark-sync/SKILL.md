---
name: x-bookmark-sync
description: "Use when archiving X bookmarks through official xurl. Runs safe incremental exports without inspecting credentials."
version: 1.0.0
author: Guido Schmitz
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [x, bookmarks, xurl, archive, obsidian]
---

# X Bookmark Sync

Use the repository's dependency-free CLI to incrementally archive a user's X bookmarks as JSON, CSV, and Obsidian-friendly Markdown.

## Safety rules

1. Never read, inspect, search, copy, or display `~/.xurl` or any xurl credential file.
2. Never request, accept, print, log, or paste a Client ID, Client Secret, OAuth token, callback code, or redirect URL containing a code.
3. The user must register the X app and complete OAuth themselves in a private terminal.
4. Never use `xurl token`, verbose flags, trace flags, or shell debugging around credential commands.
5. Treat every export as private user data. Confirm the output location and do not commit exports.
6. For tests and demonstrations, always use `--input` so xurl is not invoked.

## Prerequisites

- Work from the `x-bookmark-sync` repository root.
- Use Python 3.9 or newer.
- For a live run, require the user to have authenticated the official xurl CLI already. It needs OAuth 2.0 user-context scopes `bookmark.read`, `tweet.read`, and `users.read`.
- If authentication is uncertain, the user can run `xurl auth status` themselves. Do not inspect xurl's storage.

## Run

Prefer an installed command when available:

```bash
x-bookmark-sync --limit 100 --output-dir /absolute/private/archive
```

Otherwise run from the checkout without installing:

```bash
PYTHONPATH=src python3 -m x_bookmark_sync --limit 100 --output-dir /absolute/private/archive
```

The valid limit is 1–100. Default is 100. The default formats are all three. Select formats by repeating the flag:

```bash
x-bookmark-sync --output-dir /absolute/private/archive --format json --format markdown
```

Keep `json` selected to persist the canonical `bookmarks.json` archive for the next merge.

## Offline or test run

```bash
PYTHONPATH=src python3 -m x_bookmark_sync \
  --input tests/fixtures/latest.json \
  --output-dir /tmp/x-bookmark-sync-demo
```

`--input` must be an xurl bookmarks JSON response. It bypasses xurl completely.

## Verify

A successful run exits zero and reports an archive count plus absolute paths. Verify only the expected files exist in the chosen directory:

- `bookmarks.json`
- `bookmarks.csv`
- `bookmarks.md`

For repository changes, run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Do not display bookmark contents unless the user explicitly asks; report counts and paths by default.

## Incremental behavior and limits

The CLI merges by post ID. A newly fetched record replaces the same archived ID, while IDs absent from the latest response are preserved. Output ordering is deterministic.

The official `xurl bookmarks` shortcut currently returns at most the latest 100 per invocation. There is no pagination in this utility. Running it regularly grows the local archive incrementally, but the first run cannot recover bookmarks already outside the latest-100 window. Absence from a fetch does not delete an archived post.

## Errors

- `xurl was not found`: ask the user to install the official xurl CLI and ensure it is on `PATH`.
- Authentication error: ask the user to complete OAuth 2.0 user authentication privately, with the required scopes, then rerun. Do not troubleshoot by opening credential files.
- Malformed API output: retry with a known fixture to distinguish CLI logic from upstream output; never guess missing authors or IDs.
- X 401/403 after OAuth: the user should check scopes, selected app/user, Production/Pay-per-use enrollment, and current X access terms.
