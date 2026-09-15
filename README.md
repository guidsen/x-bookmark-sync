# x-bookmark-sync

A small, dependency-free Python CLI that uses the official [`xurl`](https://github.com/xdevplatform/xurl) command to keep a local archive of your X bookmarks. Each run merges the latest fetched posts into the existing archive by post ID, so older entries remain available, then writes JSON, CSV, and Obsidian-friendly Markdown.

> This is an independent utility, not an X Corp. product. API access, pricing, and portal screens can change.

## Table of contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Install](#install)
- [Beginner setup: X Developer Portal and OAuth 2.0](#beginner-setup-x-developer-portal-and-oauth-20)
- [Usage](#usage)
- [Limitations](#limitations)
- [Scheduling](#scheduling)
- [Install with your coding agent](#install-with-your-coding-agent)
- [Privacy and security](#privacy-and-security)
- [Development](#development)
- [License](#license)

## What it does

- Runs `xurl bookmarks -n 100` by default.
- Maps each post's `author_id` through `includes.users`.
- Builds canonical links such as `https://x.com/username/status/123`.
- Replaces an archived record when the same post ID is fetched again.
- Preserves archived IDs that are not in the newest response.
- Produces deterministic output ordered by creation time and post ID, newest first.
- Reads fixture JSON with `--input`, without running `xurl`—useful for tests and offline exports.
- Lists X bookmark folders and archives a specific folder by name or ID.

It does not read or inspect xurl's credential files, and it never asks xurl to print a token.

## Requirements

- Python 3.9 or newer
- The official `xurl` CLI for live syncs
- An X Developer account/app with OAuth 2.0 user-context bookmark access

No Python runtime dependencies are required.

## Install

From a checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
x-bookmark-sync --help
```

You can also run directly without installing:

```bash
PYTHONPATH=src python3 -m x_bookmark_sync --help
```

## Beginner setup: X Developer Portal and OAuth 2.0

X changes its developer portal wording and navigation periodically. The labels below describe the concepts to look for; a button or section may have a slightly different name in the current portal. Consult the [X Developer documentation](https://docs.x.com/) if the screen differs.

### 1. Create or open a project and app

1. Sign in to the [X Developer Portal](https://developer.x.com/en/portal/dashboard) with the X account whose bookmarks you want to archive.
2. Create a developer project and an app, or open an existing project/app you control.
3. If X asks for a use-case description, explain that this is a private, read-only personal bookmark archive.
4. Complete any account verification or developer-policy prompts.

X may require the account/project to be enrolled in **Production** access and **Pay-per-use** before the bookmarks endpoint is available. Plans, credits, prices, and enrollment screens can change. Review the current terms and spending controls in the portal before making API calls; this project cannot grant API access or bypass plan restrictions.

### 2. Enable OAuth 2.0 user authentication

Open the app's user-authentication settings and enable **OAuth 2.0**. If the portal asks for an app type, choose the option intended for a **Web App, Automated App, or Bot** rather than a Native App. The exact wording may differ.

Enter these values:

- **Callback / redirect URI:** `http://localhost:8080/callback`
- **Website URL:** use a real URL you control, such as your personal site or this project's eventual public repository URL. If the portal accepts a placeholder during development, use a valid HTTPS URL such as `https://example.com` and replace it before production use. Do not invent ownership of someone else's site.
- **Permissions/scopes:** read access with `bookmark.read`, `tweet.read`, and `users.read`

Bookmarks are private user data, so app-only authentication is insufficient. The OAuth grant used by xurl must include all three scopes. If you change scopes after authenticating, authenticate again so the new grant takes effect.

Save the settings. The callback URI in the portal and the URI supplied to xurl must match exactly, including `http`, port `8080`, and `/callback`.

### 3. Locate the Client ID and Client Secret

Find the app's OAuth 2.0 client credentials in the portal's keys/credentials area. X may show the **Client Secret** only once or require you to regenerate it.

**Treat both values as credentials. Never commit them, put them in this repository, paste them into an issue/chat/prompt, include them in screenshots, or share terminal history.** Run the registration command yourself in a private local terminal. This project never needs to receive those values.

### 4. Register the app with xurl locally

Install the [official xurl CLI](https://github.com/xdevplatform/xurl), then run this yourself, replacing the placeholders and choosing a local name such as `bookmark-sync`:

```bash
xurl auth apps add bookmark-sync \
  --client-id YOUR_CLIENT_ID \
  --client-secret YOUR_CLIENT_SECRET \
  --redirect-uri http://localhost:8080/callback
```

Because inline secrets can remain in shell history or process listings, use a private machine and follow xurl's latest credential-entry guidance. Clear sensitive history if appropriate for your shell. Never put the completed command in a script or commit it.

### 5. Complete OAuth locally

On a machine with a browser:

```bash
xurl auth oauth2 --app bookmark-sync
xurl auth default bookmark-sync
xurl whoami
```

The first command opens X's authorization page. Confirm the account and requested read scopes. `xurl whoami` verifies that user-context authentication works. You can inspect non-secret auth status with `xurl auth status`; do not use commands that print access tokens.

For a headless or remote server, use xurl's headless flow:

```bash
xurl auth oauth2 --app bookmark-sync --headless
xurl auth default bookmark-sync
xurl whoami
```

xurl prints an authorization URL. Open it on a trusted device, authorize the app, then paste the resulting redirect URL or authorization code back into the private terminal as xurl instructs. Do not paste that URL or code into chat, logs, issues, or this repository. If policy permits, authenticating and running the sync on your own workstation is simpler than placing credentials on a shared server.

### 6. Test bookmark access

```bash
xurl bookmarks -n 1
```

A JSON response confirms the endpoint is accessible. A 401/403 can mean an incomplete OAuth grant, wrong default app/user, missing scopes, or unavailable product/plan access. Recheck the portal setup and re-run OAuth after changing scopes.

## Usage

Export the latest 100 bookmarks into `./archive`:

```bash
x-bookmark-sync --output-dir ./archive
```

Choose a smaller fetch size (valid range: 1–100):

```bash
x-bookmark-sync --limit 25 --output-dir ./archive
```

Select output formats by repeating `--format`:

```bash
x-bookmark-sync --output-dir ./archive --format json --format markdown
```

Use saved xurl JSON instead of making a network request:

```bash
x-bookmark-sync --input tests/fixtures/latest.json --output-dir ./archive
```

Default files are:

- `bookmarks.json` — canonical versioned archive used for the next merge
- `bookmarks.csv` — spreadsheet-friendly rows
- `bookmarks.md` — one Obsidian-friendly note with YAML front matter and post links

The program reads `OUTPUT_DIR/bookmarks.json` before every export when it exists. Keep `json` among selected formats if you customize `--format`; otherwise the latest merge is exported for that run but is not persisted back to the canonical archive. Writes are atomic, but normal backups are still recommended.

### Bookmark folders

List the folders available to the authenticated X account:

```bash
x-bookmark-sync --list-folders
```

Archive one folder using its name or ID:

```bash
x-bookmark-sync --folder "AI Research" --output-dir ./archive
x-bookmark-sync --folder-id 1234567890123456789 --output-dir ./archive
```

Folder names are matched case-insensitively. Numeric values passed to `--folder` are treated as IDs. Folder archives are kept separate from the main archive under:

```text
OUTPUT_DIR/folders/FOLDER-SLUG-FOLDER-ID/bookmarks.{json,csv,md}
```

The X folder endpoint returns Post IDs only. The CLI therefore makes a second API request to retrieve the complete Posts and author information. These requests may incur X API usage charges.

## Limitations

The `xurl bookmarks` shortcut currently supports only `-n 1` through `-n 100` and does not expose pagination through this utility. A single invocation can therefore see only the latest 100 bookmarks. The local archive grows incrementally across runs: entries captured previously remain even after they fall outside that window. The first run cannot recover older bookmarks that are already beyond the latest 100.

X's documented folder endpoint can return a `next_token`, but its current request specification does not document a matching pagination parameter. If that happens, the CLI exports the available page and prints a warning instead of claiming the folder archive is complete.

Deleted or unbookmarked posts are not removed automatically, because absence from the latest window does not prove removal. Posts unavailable to the API cannot be archived. API behavior, access tiers, and billing are controlled by X.

## Scheduling

A daily cron job on Linux/macOS can keep the incremental archive current:

```cron
15 7 * * * /absolute/path/to/.venv/bin/x-bookmark-sync --output-dir /absolute/path/to/private-x-archive >>/absolute/path/to/x-bookmark-sync.log 2>&1
```

Use absolute paths, keep the archive and log private, and first run the command interactively under the same OS account to verify xurl authentication. Avoid overly frequent schedules; API calls may incur usage charges.

## Install with your coding agent

If your coding agent can access GitHub and run terminal commands, send it this message:

> Install and configure <https://github.com/guidsen/x-bookmark-sync> for me. Follow its README, run the tests, and verify a fixture-based export. Never ask me to paste X credentials or tokens into chat, and never inspect `~/.xurl`. Stop when private X Developer Portal or OAuth input is required and tell me exactly what I need to complete myself.

The agent should be able to clone the repository, install the CLI, test it, and guide you through setup. Creating the X app and entering its credentials remain manual security steps.

### Hermes Agent

The repository includes `skill/x-bookmark-sync/SKILL.md`. To make it available to Hermes, copy the `x-bookmark-sync` skill directory into the active profile's skills directory (normally `$HERMES_HOME/skills/`, or `~/.hermes/skills/` when `HERMES_HOME` is unset), then start a new Hermes session.

You can also ask Hermes directly from this repository:

> Run x-bookmark-sync with an input fixture and export into a temporary directory. Do not access xurl credentials.

For a live run, authenticate xurl yourself first. Then ask:

> Run x-bookmark-sync with limit 100 and output to `/absolute/private/archive`; report only the count and output paths. Never inspect xurl credential files or print tokens.

Hermes should run the CLI, not handle Client IDs, Client Secrets, callback codes, or tokens.

## Privacy and security

- `bookmarks.json`, CSV, and Markdown can contain private bookmarks, names, post text, and URLs. Store them as sensitive personal data.
- Default root-level exports are gitignored, but files written elsewhere are your responsibility. Check `git status` before every commit.
- Never commit or paste Client IDs, Client Secrets, OAuth tokens, callback URLs containing codes, or xurl credential storage.
- The CLI launches `xurl bookmarks` without verbose/trace/token flags, captures its output, and reports sanitized authentication errors rather than relaying xurl stderr.
- The CLI never reads `~/.xurl`. xurl itself manages its own authentication when invoked.
- `--input` reads exactly the file you provide. Review untrusted fixtures before processing them.

## Development

Run the dependency-free test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Run a fixture-based end-to-end export:

```bash
rm -rf /tmp/x-bookmark-sync-demo
PYTHONPATH=src python3 -m x_bookmark_sync \
  --input tests/fixtures/latest.json \
  --output-dir /tmp/x-bookmark-sync-demo
```

## License

MIT. See [LICENSE](LICENSE).
