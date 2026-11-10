from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scrubbit import cli
from scrubbit.reddit import Credentials, RedditClient, Response
from tests.fake_reddit import FakeReddit

BASE = ["-u", "me", "-p", "pw", "--client-id", "id", "--client-secret", "s"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test in an empty directory with no SCRUBBIT_* variables set."""
    for name in list(os.environ):
        if name.startswith(cli.ENV_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_parses_flags_into_options(export_dir: Path) -> None:
    settings = cli.parse_args(
        [
            *BASE,
            "--export-dir",
            str(export_dir),
            "--thing-types",
            "comments, saved-posts",
            "--skip-subreddits",
            "r/Pics,Tech,u/Some-One",
            "--skip-ids",
            "t1_AB",
            "--dry-run",
        ]
    )
    options = settings.options
    assert not options.delete_if_overwrite_fails
    assert options.kinds == ("comments", "saved-posts")
    assert options.skip_subreddits == {"pics", "tech", "u_some-one"}
    assert options.skip_ids == {"ab"}
    assert options.dry_run
    assert options.replacement is None


def test_reads_environment_and_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_dir: Path) -> None:
    Path("scrubbit.env").write_text(
        f"# comment\nSCRUBBIT_USERNAME=fromfile\nSCRUBBIT_PASSWORD='pw'\n"
        f"SCRUBBIT_CLIENT_ID=id\nSCRUBBIT_CLIENT_SECRET=s\nSCRUBBIT_EXPORT_DIR={export_dir}\n"
    )
    monkeypatch.setenv("SCRUBBIT_USERNAME", "fromenv")
    monkeypatch.setenv("SCRUBBIT_DRY_RUN", "true")
    settings = cli.parse_args([])
    assert settings.credentials.username == "fromenv"
    assert settings.credentials.password == "pw"
    assert settings.options.dry_run


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--export-dir", "/nonexistent"], "not a directory"),
        (["--thing-types", "upvotes"], "unknown thing types"),
        (["-r", "  "], "can't be blank"),
        (["--skip-subreddits", "a", "--only-subreddits", "b"], "can't be used together"),
        (["--skip-subreddits", "pics # keep my photos"], "aren't subreddit names"),
        (["--skip-ids", "abc # keep"], "aren't comment or post ids"),
    ],
)
def test_rejects_bad_arguments(
    export_dir: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.parse_args([*BASE, "--export-dir", str(export_dir), *extra])
    assert exit_info.value.code == cli.EXIT_USAGE
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("value", ["true # rehearse first", "sure", "2"])
def test_unreadable_flag_stops_instead_of_meaning_false(
    monkeypatch: pytest.MonkeyPatch, export_dir: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    monkeypatch.setenv("SCRUBBIT_DRY_RUN", value)
    with pytest.raises(SystemExit) as exit_info:
        cli.parse_args([*BASE, "--export-dir", str(export_dir)])
    assert exit_info.value.code == cli.EXIT_USAGE
    assert "SCRUBBIT_DRY_RUN" in capsys.readouterr().err


def test_login_failure_mid_run_still_writes_failures(
    monkeypatch: pytest.MonkeyPatch, export_dir: Path, fake: FakeReddit
) -> None:
    fake.things = {name: {"name": name, "author": "me", "body": "orig"} for name in ("t1_c1", "t1_c2")}
    fake.locked = {"t1_c1"}

    def transport(method: str, url: str, data: Mapping[str, str] | None, headers: Mapping[str, str]) -> Response:
        response = fake(method, url, data, headers)
        if url.endswith("/api/editusertext"):
            fake.token_body = {"error": "invalid_grant"}
            fake.scripted = [Response(401, {}, {})]
        return response

    def make_client(credentials: Credentials) -> RedditClient:
        return RedditClient(credentials, transport=transport, sleep=lambda _: None, clock=lambda: 0.0)

    monkeypatch.setattr(cli, "RedditClient", make_client)
    assert cli.main([*BASE, "--export-dir", str(export_dir), "--thing-types", "comments"]) == cli.EXIT_USAGE
    assert "t1_c1" in cli.FAILURES_FILE.read_text()


def test_missing_credentials_are_named(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args([])
    assert "--username, --password, --client-id, --client-secret, --export-dir" in capsys.readouterr().err


def test_parse_when() -> None:
    now = datetime.now(timezone.utc)
    assert abs(cli.parse_when("-30 days") - (now - timedelta(days=30))) < timedelta(seconds=5)
    assert cli.parse_when("2020-01-01") == datetime(2020, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_when("30 days")
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_when("3000-01-01")
