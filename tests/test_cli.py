import contextlib
import csv
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from x_bookmark_sync.cli import (
    BookmarkSyncError,
    build_parser,
    fetch_bookmark_folders,
    fetch_bookmarks,
    fetch_folder_post_ids,
    folder_slug,
    hydrate_posts,
    main,
    merge_bookmarks,
    parse_api_response,
    resolve_folder,
)


FIXTURES = Path(__file__).parent / "fixtures"


class ParserTests(unittest.TestCase):
    def test_limit_defaults_to_100_and_accepts_bounds(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args([]).limit, 100)
        self.assertEqual(parser.parse_args(["--limit", "1"]).limit, 1)
        self.assertEqual(parser.parse_args(["--limit", "100"]).limit, 100)

    def test_limit_rejects_values_outside_xurl_range(self):
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--limit", "0"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--limit", "101"])

    def test_input_and_formats_parse(self):
        args = build_parser().parse_args(
            ["--input", "sample.json", "--output-dir", "out", "--format", "csv", "--format", "markdown"]
        )
        self.assertEqual(args.input, Path("sample.json"))
        self.assertEqual(args.formats, ["csv", "markdown"])

    def test_folder_options_are_mutually_exclusive(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["--folder", "AI"]).folder, "AI")
        self.assertEqual(parser.parse_args(["--folder-id", "123"]).folder_id, "123")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--folder", "AI", "--list-folders"])


class DataTests(unittest.TestCase):
    def test_author_mapping_and_canonical_url(self):
        payload = json.loads((FIXTURES / "latest.json").read_text(encoding="utf-8"))
        bookmarks = parse_api_response(payload)
        ada = next(item for item in bookmarks if item["author_id"] == "11")
        self.assertEqual(ada["author_name"], "Ada Example")
        self.assertEqual(ada["author_username"], "ada_example")
        self.assertEqual(
            ada["url"],
            "https://x.com/ada_example/status/1890000000000000001",
        )

    def test_merge_replaces_fetched_ids_and_preserves_old_ids(self):
        existing = json.loads((FIXTURES / "existing.json").read_text(encoding="utf-8"))["bookmarks"]
        latest = parse_api_response(json.loads((FIXTURES / "latest.json").read_text(encoding="utf-8")))
        merged = merge_bookmarks(existing, latest)
        self.assertEqual([item["id"] for item in merged], [
            "1890000000000000002", "1890000000000000001", "1880000000000000000"
        ])
        self.assertEqual(merged[1]["text"], "A useful post about Python & APIs.")

    def test_malformed_payloads_have_useful_errors(self):
        for payload in ([], {}, {"data": "wrong"}, {"errors": [{"title": "Unauthorized"}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(BookmarkSyncError):
                    parse_api_response(payload)

    def test_post_without_mapped_author_is_rejected(self):
        payload = {"data": [{"id": "1", "text": "x", "author_id": "missing"}], "includes": {"users": []}}
        with self.assertRaisesRegex(BookmarkSyncError, "author"):
            parse_api_response(payload)


class XurlTests(unittest.TestCase):
    @mock.patch("x_bookmark_sync.cli.subprocess.run")
    def test_invokes_official_xurl_bookmarks_command(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"data": [], "includes": {"users": []}}', stderr="")
        payload = fetch_bookmarks(37)
        self.assertEqual(payload["data"], [])
        self.assertEqual(run.call_args.args[0], ["xurl", "bookmarks", "-n", "37"])

    @mock.patch("x_bookmark_sync.cli.subprocess.run", side_effect=FileNotFoundError)
    def test_absent_xurl_is_explained(self, _run):
        with self.assertRaisesRegex(BookmarkSyncError, "not found"):
            fetch_bookmarks(100)

    @mock.patch("x_bookmark_sync.cli.subprocess.run")
    def test_auth_failure_does_not_echo_stderr(self, run):
        secret = "VERY_SECRET_TOKEN"
        run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr=f"401 Unauthorized bearer {secret}")
        with self.assertRaises(BookmarkSyncError) as caught:
            fetch_bookmarks(100)
        self.assertIn("authenticated", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))


class FolderTests(unittest.TestCase):
    @mock.patch("x_bookmark_sync.cli._run_xurl")
    def test_lists_all_folder_pages_and_sorts_names(self, run_xurl):
        run_xurl.side_effect = [
            {"data": [{"id": "2", "name": "Work"}], "meta": {"next_token": "NEXT"}},
            {"data": [{"id": "1", "name": "AI"}], "meta": {}},
        ]
        folders = fetch_bookmark_folders("42")
        self.assertEqual(folders, [{"id": "1", "name": "AI"}, {"id": "2", "name": "Work"}])
        self.assertIn("pagination_token=NEXT", run_xurl.call_args_list[1].args[0][1])

    def test_resolves_folder_by_case_insensitive_name_or_id(self):
        folders = [{"id": "123", "name": "AI Research"}]
        self.assertEqual(resolve_folder(folders, "ai research")["id"], "123")
        self.assertEqual(resolve_folder(folders, "123")["name"], "AI Research")
        self.assertEqual(folder_slug(folders[0]), "ai-research-123")

    @mock.patch("x_bookmark_sync.cli._run_xurl")
    def test_folder_post_ids_warn_when_x_reports_an_unpageable_next_token(self, run_xurl):
        run_xurl.return_value = {"data": [{"id": "10"}], "meta": {"next_token": "MORE"}}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(fetch_folder_post_ids("42", "123"), ["10"])
        self.assertIn("documented folder endpoint does not accept", stderr.getvalue())

    @mock.patch("x_bookmark_sync.cli._run_xurl")
    def test_hydrates_folder_post_ids_with_authors(self, run_xurl):
        run_xurl.return_value = {
            "data": [{"id": "10", "text": "Folder post", "author_id": "7", "created_at": "2026-01-01T00:00:00Z"}],
            "includes": {"users": [{"id": "7", "name": "Ada", "username": "ada"}]},
        }
        payload = hydrate_posts(["10"])
        bookmarks = parse_api_response(payload)
        self.assertEqual(bookmarks[0]["url"], "https://x.com/ada/status/10")
        request_url = run_xurl.call_args.args[0][1]
        self.assertIn("%2C", request_url)
        self.assertIn("expansions=author_id", request_url)

    @mock.patch("x_bookmark_sync.cli.fetch_folder_bookmarks")
    @mock.patch("x_bookmark_sync.cli.fetch_bookmark_folders")
    @mock.patch("x_bookmark_sync.cli.fetch_current_user_id", return_value="42")
    def test_folder_export_uses_isolated_folder_directory(self, _user, folders, folder_bookmarks):
        folders.return_value = [{"id": "123", "name": "AI Research"}]
        folder_bookmarks.return_value = json.loads((FIXTURES / "latest.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            result = main(["--folder", "AI Research", "--output-dir", temporary])
            self.assertEqual(result, 0)
            folder_dir = Path(temporary) / "folders" / "ai-research-123"
            self.assertEqual({path.name for path in folder_dir.iterdir()}, {"bookmarks.json", "bookmarks.csv", "bookmarks.md"})


class EndToEndTests(unittest.TestCase):
    def test_fixture_export_is_deterministic_and_preserves_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "bookmarks.json").write_text(
                (FIXTURES / "existing.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            arguments = ["--input", str(FIXTURES / "latest.json"), "--output-dir", str(output)]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(arguments), 0)
            first = {path.name: path.read_bytes() for path in output.iterdir()}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments), 0)
            second = {path.name: path.read_bytes() for path in output.iterdir()}
            self.assertEqual(first, second)
            self.assertEqual(set(first), {"bookmarks.json", "bookmarks.csv", "bookmarks.md"})
            archive = json.loads(first["bookmarks.json"])
            self.assertEqual(len(archive["bookmarks"]), 3)
            rows = list(csv.DictReader(io.StringIO(first["bookmarks.csv"].decode())))
            self.assertEqual(len(rows), 3)
            markdown = first["bookmarks.md"].decode()
            self.assertIn("# X Bookmarks", markdown)
            self.assertIn("https://x.com/ada_example/status/1890000000000000001", markdown)

    def test_missing_input_returns_nonzero_without_traceback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["--input", "/definitely/missing.json"])
        self.assertEqual(result, 1)
        self.assertIn("error:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
