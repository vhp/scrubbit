"""The overwrite-then-delete run over one export.

For comments and self posts there are two passes. Every item is overwritten
first and the new text is read back to confirm it saved; only then is it
deleted. Overwriting first means the original text is gone from Reddit before
the delete, so anything that copies the item later only sees filler. Link posts
can't be edited, so they are only deleted.

A deleted item can never be edited again, so an item whose overwrite failed or
couldn't be confirmed is not deleted: it's reported, and the next run tries the
overwrite again. Some items will never take an edit, such as those in locked or
archived threads; deleting those without overwriting them is the user's call,
made with --delete-if-overwrite-fails.

Reddit sometimes answers 200 without changing anything, so both passes end with
a lookup that reports every item that didn't change.

Before anything changes, comments and posts are looked up in batches of 100.
That gives the live author, score and text, which drive four checks: items
already deleted are skipped, which makes reruns cheap; items already showing
filler aren't edited again; --max-score works even though the export has no
scores; and items the API says you didn't write are skipped, which guards
against an id recovered from the wrong link.
"""

from __future__ import annotations

import csv
import logging
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from scrubbit.export import KINDS, Kind, Row, find_export_file, parse_date, read_rows, subreddit_of

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from scrubbit.reddit import RedditClient
    from scrubbit.report import Report

log = logging.getLogger(__name__)

_LOREM_TEXT: Final = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et "
    "dolore magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea "
    "commodo consequat duis aute irure in reprehenderit voluptate velit esse cillum fugiat nulla pariatur "
    "excepteur sint occaecat cupidatat non proident sunt culpa qui officia deserunt mollit anim id est laborum"
)
LOREM_WORDS: Final = tuple(_LOREM_TEXT.split())
_LOREM_SET: Final = frozenset(LOREM_WORDS)
FILLER_WORDS: Final = (15, 40)
UNKNOWN_SUBREDDIT: Final = "?"


@dataclass(frozen=True)
class Options:
    """What to remove and how. The CLI builds this from flags and environment variables.

    Attributes:
        export_dir: The unzipped data export.
        username: Your username, used to skip items you didn't write.
        kinds: Thing types to process, in order, as keys of ``KINDS``.
        dry_run: Log what would happen and change nothing.
        edit_only: Overwrite comments and self posts but delete and unsave nothing.
        delete_if_overwrite_fails: Delete items even when their overwrite
            failed or couldn't be confirmed, leaving the original text as the
            last version. Off by default.
        before: Only touch items created before this.
        after: Only touch items created after this.
        max_score: Keep items scoring higher than this.
        replacement: Fixed overwrite text, or None for random filler per item.
        skip_subreddits: Lowercase subreddit names to leave alone.
        only_subreddits: If not empty, lowercase subreddit names to limit the run to.
        skip_ids: Bare lowercase ids of comments and posts to leave alone.
    """

    export_dir: Path
    username: str
    kinds: tuple[str, ...]
    dry_run: bool = False
    edit_only: bool = False
    delete_if_overwrite_fails: bool = False
    before: datetime | None = None
    after: datetime | None = None
    max_score: int | None = None
    replacement: str | None = None
    skip_subreddits: frozenset[str] = frozenset()
    only_subreddits: frozenset[str] = frozenset()
    skip_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Target:
    """A comment or post that passed every filter and will be changed."""

    fullname: str
    subreddit: str
    overwrite: bool


def make_filler(rng: random.Random) -> str:
    """Return a sentence of random lorem ipsum words, different on every call."""
    words = rng.choices(LOREM_WORDS, k=rng.randint(*FILLER_WORDS))
    return " ".join(words).capitalize() + "."


def looks_overwritten(text: str, replacement: str | None) -> bool:
    """Tell whether ``text`` is already the overwrite text, so a rerun can skip the edit.

    With a fixed replacement that means an exact match. Random filler differs per
    item, so it is recognized by being made only of lorem ipsum words.
    """
    if replacement is not None:
        return text.strip() == replacement.strip()
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and set(words) <= _LOREM_SET


class Shredder:
    """Runs scrubbit over an export: filters items, then overwrites, deletes or unsaves them.

    Args:
        client: A Reddit client; it logs in on first use if it hasn't already.
        options: What to remove and how.
        report: Collects every item's outcome.
        rng: Source of randomness for filler text. Tests pass a seeded one.
    """

    def __init__(
        self, client: RedditClient, options: Options, report: Report, rng: random.Random | None = None
    ) -> None:
        """Prepare a run; nothing is read or sent until ``run``."""
        self._client = client
        self._options = options
        self._report = report
        self._rng = rng or random.Random()  # noqa: S311  filler only needs to vary, not be secret

    def run(self) -> None:
        """Process every requested thing type in order."""
        for name in self._options.kinds:
            self.shred(KINDS[name])

    def shred(self, kind: Kind) -> None:
        """Process one thing type from its export file."""
        path = find_export_file(self._options.export_dir, kind)
        if path is None:
            return
        try:
            rows = read_rows(path, kind)
        except (OSError, UnicodeError, csv.Error) as error:
            self._report.fail(kind.name, path.name, "", "read export", f"{type(error).__name__}: {error}")
            return
        log.info("%s: %d rows in %s", kind.name, len(rows), path.name)
        if rows and not any(row.key for row in rows):
            self._report.fail(
                kind.name, path.name, "", "read export", f"no {kind.key}s found in any row; see the warnings above"
            )
            return
        rows = [row for row in rows if row.key]
        if kind.prefix is None:
            self._unfriend_all(kind, rows)
        elif kind.saved:
            self._unsave_all(kind, rows)
        else:
            self._overwrite_and_delete(kind, rows)

    def _unfriend_all(self, kind: Kind, rows: list[Row]) -> None:
        for row in rows:
            if self._options.edit_only:
                self._report.skip(kind.name, "--edit-only")
            elif self._options.dry_run:
                log.info("would unfriend %s", row.key)
                self._report.ok(kind.name, "would unfriend")
            elif error := self._client.unfriend(row.key):
                self._report.fail(kind.name, row.key, "", "unfriend", error)
            else:
                self._report.ok(kind.name, "unfriended")

    def _unsave_all(self, kind: Kind, rows: list[Row]) -> None:
        items = {f"{kind.prefix}_{row.key}": row for row in rows}
        # The saved files have no dates, scores or subreddits, so the filters need live data.
        lookup = self._client.lookup(list(items))
        for fullname, row in items.items():
            info = lookup.found.get(fullname, {})
            subreddit = str(info.get("subreddit") or subreddit_of(row) or UNKNOWN_SUBREDDIT)
            if self._lookup_failed(kind, lookup.failed, fullname, row, subreddit):
                continue
            reason = self._filter_reason(row.key, subreddit) or self._date_reason(row, info) or self._score_reason(info)
            if reason:
                log.debug("skip %s in r/%s: %s", fullname, subreddit, reason)
                self._report.skip(kind.name, reason)
            elif self._options.edit_only:
                self._report.skip(kind.name, "--edit-only")
            elif self._options.dry_run:
                log.info("would unsave %s in r/%s", fullname, subreddit)
                self._report.ok(kind.name, "would unsave")
            elif error := self._client.unsave(fullname):
                self._report.fail(kind.name, fullname, subreddit, "unsave", error)
            else:
                self._report.ok(kind.name, "unsaved")

    def _overwrite_and_delete(self, kind: Kind, rows: list[Row]) -> None:
        targets = self._select(kind, rows)
        if self._options.dry_run:
            self._log_plan(kind, targets)
            return
        failed = self._overwrite(kind, targets)
        if self._options.edit_only:
            return
        if failed and not self._options.delete_if_overwrite_fails:
            log.warning(
                "%s: not deleting %d items that couldn't be overwritten (listed above as FAILED overwrite or "
                "verify). Rerun to try again, or add --delete-if-overwrite-fails to delete them without "
                "overwriting them first.",
                kind.name,
                len(failed),
            )
            targets = [target for target in targets if target.fullname not in failed]
        log.info("%s: now deleting %d", kind.name, len(targets))
        self._delete(kind, targets)

    def _select(self, kind: Kind, rows: list[Row]) -> list[Target]:
        """Look items up and return the ones every filter lets through."""
        items = {f"{kind.prefix}_{row.key}": row for row in rows}
        lookup = self._client.lookup(list(items))
        targets: list[Target] = []
        for fullname, row in items.items():
            info = lookup.found.get(fullname, {})
            subreddit = str(info.get("subreddit") or subreddit_of(row) or UNKNOWN_SUBREDDIT)
            if self._lookup_failed(kind, lookup.failed, fullname, row, subreddit):
                continue
            if reason := self._skip_reason(row, info, subreddit):
                log.debug("skip %s in r/%s: %s", fullname, subreddit, reason)
                self._report.skip(kind.name, reason)
                continue
            editable = _can_overwrite(kind, row, info)
            if not editable and self._options.edit_only:
                self._report.skip(kind.name, "link post, can't be overwritten")
                continue
            already = editable and looks_overwritten(_text_of(info), self._options.replacement)
            if already:
                self._report.ok(kind.name, "already overwritten")
            targets.append(Target(fullname, subreddit, overwrite=editable and not already))
        return targets

    def _lookup_failed(self, kind: Kind, failed: frozenset[str], fullname: str, row: Row, subreddit: str) -> bool:
        """Report an item that couldn't be looked up, unless a filter that needs no live data skips it anyway.

        Without live data the author, score, date and link-or-self checks can't
        run, so acting on the item could go against the user's filters, or
        delete a self post as if it were a link post. It's left for a rerun.
        """
        if fullname not in failed or self._filter_reason(row.key, subreddit):
            return False
        self._report.fail(
            kind.name,
            fullname,
            subreddit,
            "look up",
            "couldn't look it up, so its filters and checks can't be applied; left unchanged for a rerun",
        )
        return True

    def _filter_reason(self, item_id: str, subreddit: str) -> str | None:
        """Apply the filters that work without live data."""
        options = self._options
        if item_id in options.skip_ids:
            return "in --skip-ids"
        if options.skip_subreddits and subreddit == UNKNOWN_SUBREDDIT:
            return "subreddit unknown"
        if subreddit.lower() in options.skip_subreddits:
            return "in --skip-subreddits"
        if options.only_subreddits and subreddit.lower() not in options.only_subreddits:
            return "not in --only-subreddits"
        return None

    def _skip_reason(self, row: Row, info: dict[str, Any], subreddit: str) -> str | None:
        """Return why to leave a comment or post alone, or None to process it."""
        return (
            self._filter_reason(row.key, subreddit)
            or self._author_reason(info)
            or self._date_reason(row, info)
            or self._score_reason(info)
        )

    def _author_reason(self, info: dict[str, Any]) -> str | None:
        author = info.get("author")
        if author == "[deleted]":
            return "already deleted"
        if isinstance(author, str) and author.lower() != self._options.username.lower():
            return "not yours"
        return None

    def _date_reason(self, row: Row, info: dict[str, Any]) -> str | None:
        before, after = self._options.before, self._options.after
        if not before and not after:
            return None
        created = parse_date(row.get("date")) or parse_date(str(info.get("created_utc") or ""))
        if created is None:
            return "date unknown"
        if before and created >= before:
            return "not before --before"
        if after and created <= after:
            return "not after --after"
        return None

    def _score_reason(self, info: dict[str, Any]) -> str | None:
        max_score = self._options.max_score
        if max_score is None:
            return None
        score = info.get("score")
        if not isinstance(score, int):
            return "score unknown"
        return "score above --max-score" if score > max_score else None

    def _log_plan(self, kind: Kind, targets: list[Target]) -> None:
        for target in targets:
            steps = (["overwrite"] if target.overwrite else []) + ([] if self._options.edit_only else ["delete"])
            if steps:
                plan = " then ".join(steps)
                log.info("would %s %s in r/%s", plan, target.fullname, target.subreddit)
                self._report.ok(kind.name, f"would {plan}")

    def _overwrite(self, kind: Kind, targets: list[Target]) -> set[str]:
        """Overwrite every target that can be, then read each one back to confirm.

        Returns:
            Fullnames whose overwrite failed or couldn't be confirmed.
        """
        failed: set[str] = set()
        sent: dict[str, str] = {}
        subreddits = {target.fullname: target.subreddit for target in targets}

        def fail(fullname: str, action: str, reason: str, *, permanent: bool = False) -> None:
            failed.add(fullname)
            self._report.fail(
                kind.name, fullname, subreddits[fullname], action, reason + self._failure_note(permanent=permanent)
            )

        for target in targets:
            if not target.overwrite:
                continue
            text = self._options.replacement or make_filler(self._rng)
            if error := self._client.edit_text(target.fullname, text):
                fail(target.fullname, "overwrite", error.reason, permanent=error.permanent)
            else:
                sent[target.fullname] = text
        lookup = self._client.lookup(list(sent))
        for fullname, text in sent.items():
            info = lookup.found.get(fullname)
            if fullname in lookup.failed:
                fail(fullname, "verify", "couldn't read it back to confirm the overwrite")
            elif info is None:
                self._report.ok(kind.name, "overwritten (not visible to the API)")
            elif _text_of(info).strip() == text.strip():
                self._report.ok(kind.name, "overwritten, verified")
            else:
                fail(fullname, "overwrite", "edit returned success but the old text is still there")
        return failed

    def _failure_note(self, *, permanent: bool) -> str:
        """Say what happens next to an item whose overwrite failed."""
        if self._options.edit_only:
            return ""
        if self._options.delete_if_overwrite_fails:
            return "; deleting it anyway (--delete-if-overwrite-fails)"
        if permanent:
            return (
                "; not deleted. Reddit won't accept an edit for it, so add --delete-if-overwrite-fails "
                "to delete it anyway"
            )
        return "; not deleted, so a rerun can try again"

    def _delete(self, kind: Kind, targets: list[Target]) -> None:
        """Delete every target, then look each one up to confirm it's gone."""
        deleted = []
        for target in targets:
            if error := self._client.delete(target.fullname):
                self._report.fail(kind.name, target.fullname, target.subreddit, "delete", error)
            else:
                deleted.append(target)
        lookup = self._client.lookup([target.fullname for target in deleted])
        for target in deleted:
            info = lookup.found.get(target.fullname)
            if target.fullname in lookup.failed:
                self._report.fail(
                    kind.name,
                    target.fullname,
                    target.subreddit,
                    "verify",
                    "deleted, but couldn't read it back to confirm; a rerun will check again",
                )
            elif info is None:
                self._report.ok(kind.name, "deleted (not visible to the API)")
            elif info.get("author") == "[deleted]":
                self._report.ok(kind.name, "deleted, verified")
            else:
                self._report.fail(
                    kind.name,
                    target.fullname,
                    target.subreddit,
                    "delete",
                    "delete returned success but the item is still there",
                )


def _text_of(info: dict[str, Any]) -> str:
    """Return the live text: ``body`` for comments, ``selftext`` for posts."""
    value = info.get("body") if "body" in info else info.get("selftext")
    return str(value or "")


def _can_overwrite(kind: Kind, row: Row, info: dict[str, Any]) -> bool:
    """Comments and self posts can be edited; link posts can't."""
    if kind.prefix == "t1":
        return True
    if "is_self" in info:
        return bool(info["is_self"])
    # The API can't see it, so go by the export, which only has a body for self posts.
    return bool(row.get("body"))
