"""Command-line entry point: flags, SCRUBBIT_* environment variables and scrubbit.env.

Every flag can also be set as an environment variable named ``SCRUBBIT_`` plus the
flag in upper case with underscores, like ``SCRUBBIT_CLIENT_ID``. Those can live in
a ``scrubbit.env`` file in the current directory; real environment variables win
over the file, and flags win over both.

Exit codes: 0 when everything worked, 1 when some items failed (listed in
scrubbit-failures.csv) or an unexpected error stopped the run, 2 for bad
arguments or a refused login, 130 when interrupted. A run that stops partway
still logs its summary and writes the failures file.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final

from scrubbit import __version__
from scrubbit.export import KINDS, bare_id, parse_date
from scrubbit.reddit import Credentials, LoginError, RedditClient
from scrubbit.report import Report
from scrubbit.shredder import Options, Shredder

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger("scrubbit")

ENV_PREFIX: Final = "SCRUBBIT_"
ENV_FILE: Final = Path("scrubbit.env")
FAILURES_FILE: Final = Path("scrubbit-failures.csv")
DEFAULT_KINDS: Final = "posts,comments"
EXIT_FAILURES: Final = 1
EXIT_USAGE: Final = 2
EXIT_INTERRUPTED: Final = 130
_RELATIVE: Final = re.compile(r"-\s*(\d+)\s*(second|minute|hour|day|week)s?")
_FLAGS: Final = ("EDIT_ONLY", "DELETE_IF_OVERWRITE_FAILS", "DRY_RUN", "VERBOSE")
_TRUE: Final = frozenset({"1", "true", "yes", "y", "on"})
_FALSE: Final = frozenset({"", "0", "false", "no", "n", "off"})
_SUBREDDIT: Final = re.compile(r"[a-z0-9_]+|u_[a-z0-9_-]+")
_ID: Final = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Settings:
    """Everything parsed from the command line and environment."""

    credentials: Credentials
    options: Options
    verbose: bool


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load ``KEY=value`` lines into the environment without overriding what's already set.

    Blank lines and lines starting with ``#`` are ignored, and matching quotes
    around a value are removed.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), _unquote(value.strip()))


def parse_when(value: str) -> datetime:
    """Parse ``--before``/``--after``: a past ISO timestamp or a negative duration like ``-30 days``.

    Raises:
        argparse.ArgumentTypeError: The value is unrecognized or in the future.
    """
    now = datetime.now(timezone.utc)
    match = _RELATIVE.fullmatch(value.strip())
    when = now - timedelta(**{f"{match[2]}s": int(match[1])}) if match else parse_date(value)
    if when is None or when > now:
        raise argparse.ArgumentTypeError(f"{value!r}: use a past ISO timestamp or a negative duration like '-30 days'")
    return when


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser, with defaults taken from SCRUBBIT_* environment variables."""
    parser = argparse.ArgumentParser(
        prog="scrubbit",
        description="Overwrite and delete your Reddit history using your Reddit data export.",
        epilog="Every option can also be set as SCRUBBIT_<OPTION> in the environment or in ./scrubbit.env.",
    )
    add = parser.add_argument
    add("--version", action="version", version=f"%(prog)s {__version__}")
    add("-u", "--username", default=_env("USERNAME"), help="your Reddit username")
    add(
        "-p",
        "--password",
        default=_env("PASSWORD"),
        help="your Reddit password; with 2FA, password:123456 (the code must still be valid if the "
        "token expires mid-run)",
    )
    add("--client-id", default=_env("CLIENT_ID"), help="from a 'script' app at https://www.reddit.com/prefs/apps")
    add("--client-secret", default=_env("CLIENT_SECRET"), help="the same app's secret")
    add("--export-dir", type=Path, default=_env("EXPORT_DIR"), help="the unzipped data export folder")
    add(
        "--thing-types",
        default=_env("THING_TYPES", DEFAULT_KINDS),
        help=f"comma-separated, from {', '.join(KINDS)} (default: {DEFAULT_KINDS})",
    )
    add(
        "--before",
        type=parse_when,
        default=_env("BEFORE"),
        help="only items created before this, e.g. 2024-01-01 or '-30 days'",
    )
    add("--after", type=parse_when, default=_env("AFTER"), help="only items created after this")
    add("--max-score", type=int, default=_env("MAX_SCORE"), help="keep items scoring higher than this")
    add(
        "-r",
        "--replacement-comment",
        default=_env("REPLACEMENT_COMMENT"),
        help="fixed text to overwrite with; by default each item gets different random lorem ipsum",
    )
    add("--skip-subreddits", default=_env("SKIP_SUBREDDITS", ""), help="comma-separated subreddits to keep")
    add("--only-subreddits", default=_env("ONLY_SUBREDDITS", ""), help="comma-separated subreddits to limit to")
    add("--skip-ids", default=_env("SKIP_IDS", ""), help="comma-separated comment or post ids to keep")
    add("--user-agent", default=_env("USER_AGENT", f"scrubbit/{__version__}"), help="User-Agent for API requests")
    add(
        "--edit-only",
        action="store_true",
        default=_env_flag("EDIT_ONLY"),
        help="overwrite comments and self posts, delete and unsave nothing",
    )
    add(
        "--delete-if-overwrite-fails",
        action="store_true",
        default=_env_flag("DELETE_IF_OVERWRITE_FAILS"),
        help="delete items even when they couldn't be overwritten, leaving the original text as the last version",
    )
    add("--dry-run", action="store_true", default=_env_flag("DRY_RUN"), help="log what would happen, change nothing")
    add("-v", "--verbose", action="store_true", default=_env_flag("VERBOSE"), help="also log why each item was skipped")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> Settings:
    """Parse and validate arguments, exiting with a usage message on bad input."""
    load_env_file()
    parser = build_parser()
    # An unreadable flag must stop the run: reading it as false would turn off --dry-run.
    if bad := [f"{ENV_PREFIX}{name}={_env(name)!r}" for name in _FLAGS if _flag(_env(name)) is None]:
        parser.error(f"{', '.join(bad)}: use true or false (scrubbit.env doesn't allow comments after a value)")
    args = parser.parse_args(argv)

    required = ("username", "password", "client_id", "client_secret", "export_dir")
    if missing := [name for name in required if not getattr(args, name)]:
        parser.error("missing " + ", ".join(f"--{name.replace('_', '-')}" for name in missing))
    if not args.export_dir.is_dir():
        parser.error(f"--export-dir {args.export_dir} is not a directory (unzip the export first)")
    if args.replacement_comment is not None and not args.replacement_comment.strip():
        parser.error("--replacement-comment can't be blank; Reddit rejects empty edits")
    if args.skip_subreddits and args.only_subreddits:
        parser.error("--skip-subreddits and --only-subreddits can't be used together")
    kinds = tuple(_split(args.thing_types))
    if unknown := [kind for kind in kinds if kind not in KINDS]:
        parser.error(f"unknown thing types {unknown}; choose from {', '.join(KINDS)}")
    try:
        skip_subreddits = _subreddits(args.skip_subreddits, "--skip-subreddits")
        only_subreddits = _subreddits(args.only_subreddits, "--only-subreddits")
        skip_ids = _ids(args.skip_ids)
    except ValueError as error:
        parser.error(str(error))

    return Settings(
        credentials=Credentials(
            username=args.username,
            password=args.password,
            client_id=args.client_id,
            client_secret=args.client_secret,
            user_agent=args.user_agent,
        ),
        options=Options(
            export_dir=args.export_dir,
            username=args.username,
            kinds=kinds,
            dry_run=args.dry_run,
            edit_only=args.edit_only,
            delete_if_overwrite_fails=args.delete_if_overwrite_fails,
            before=args.before,
            after=args.after,
            max_score=args.max_score,
            replacement=args.replacement_comment,
            skip_subreddits=skip_subreddits,
            only_subreddits=only_subreddits,
            skip_ids=skip_ids,
        ),
        verbose=args.verbose,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run scrubbit and return the process exit code."""
    settings = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if settings.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    report = Report()
    client = RedditClient(settings.credentials)
    try:
        client.login()
    except LoginError as error:
        log.error("%s", error)
        return EXIT_USAGE
    try:
        Shredder(client, settings.options, report).run()
    except LoginError as error:
        log.error("%s\nstopping; summary so far:", error)
        _finish(report)
        return EXIT_USAGE
    except KeyboardInterrupt:
        log.warning("interrupted; summary so far:")
        _finish(report)
        return EXIT_INTERRUPTED
    except Exception:
        log.error("stopped by an unexpected error (details below); summary so far:")
        _finish(report)
        raise
    return _finish(report)


def _finish(report: Report) -> int:
    report.log_summary()
    if not report.failures:
        return 0
    report.write_failures(FAILURES_FILE)
    log.error(
        "%d failures written to %s; fix the cause and rerun, finished items are skipped",
        len(report.failures),
        FAILURES_FILE.resolve(),
    )
    return EXIT_FAILURES


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _flag(value: str | None) -> bool | None:
    """Read a boolean setting; None means it's neither clearly true nor clearly false."""
    text = (value or "").strip().lower()
    if text in _TRUE:
        return True
    return False if text in _FALSE else None


def _env_flag(name: str) -> bool:
    return _flag(_env(name)) is True


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _subreddits(value: str, flag: str) -> frozenset[str]:
    """Normalize subreddit names the way the API reports them: r/Pics is pics, u/Name is u_name.

    Raises:
        ValueError: A value isn't a subreddit or profile name.
    """
    names = {}
    for item in _split(value):
        name = re.sub(r"^/?r/", "", item.lower())
        names[item] = re.sub(r"^/?u(?:ser)?/", "u_", name)
    if bad := [item for item, name in names.items() if not _SUBREDDIT.fullmatch(name)]:
        raise ValueError(f"{flag} has values that aren't subreddit names: {bad}")
    return frozenset(names.values())


def _ids(value: str) -> frozenset[str]:
    """Normalize --skip-ids to bare lowercase ids.

    Raises:
        ValueError: A value isn't a comment or post id.
    """
    ids = {item: bare_id(item) for item in _split(value)}
    if bad := [item for item, bare in ids.items() if not _ID.fullmatch(bare)]:
        raise ValueError(f"--skip-ids has values that aren't comment or post ids: {bad}")
    return frozenset(ids.values())


def _unquote(value: str) -> str:
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "'\""  # noqa: PLR2004
    return value[1:-1] if quoted else value
