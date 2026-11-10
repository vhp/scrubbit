"""Tally what happened to every item and keep the failures for a rerun.

Every item ends up as exactly one outcome line in the summary: done, skipped
with a reason, or failed. Failures are also written to a CSV so they can be
looked at after a long run; rerunning scrubbit retries them, since everything that
finished is detected and skipped.
"""

from __future__ import annotations

import csv
import dataclasses
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Failure:
    """One thing that went wrong, as written to the failures file.

    Attributes:
        kind: The thing type, like ``comments``.
        item: The fullname, username or file that failed.
        subreddit: Where the item lives, or empty if not applicable.
        action: What was being attempted, like ``overwrite`` or ``delete``.
        detail: Why it failed, specific enough to act on.
    """

    kind: str
    item: str
    subreddit: str
    action: str
    detail: str


class Report:
    """Counts outcomes per thing type and records failures."""

    def __init__(self) -> None:
        """Start an empty report."""
        self.counts: Counter[tuple[str, str]] = Counter()
        self.failures: list[Failure] = []

    def ok(self, kind: str, outcome: str) -> None:
        """Count an item that ended well, like ``deleted, verified``."""
        self.counts[(kind, outcome)] += 1

    def skip(self, kind: str, reason: str) -> None:
        """Count an item left alone on purpose, with the reason."""
        self.counts[(kind, f"skipped: {reason}")] += 1

    def fail(self, kind: str, item: str, subreddit: str, action: str, detail: str) -> None:
        """Count, log and keep a failure."""
        self.counts[(kind, f"FAILED {action}")] += 1
        self.failures.append(Failure(kind, item, subreddit, action, detail))
        where = f" in r/{subreddit}" if subreddit else ""
        log.error("%s %s%s failed: %s", action, item, where, detail)

    def log_summary(self) -> None:
        """Log one line per thing type and outcome, with its count."""
        for (kind, outcome), count in sorted(self.counts.items()):
            log.info("%-15s %-40s %d", kind, outcome, count)

    def write_failures(self, path: Path) -> None:
        """Write the failures to a CSV, replacing any earlier file."""
        fieldnames = [field.name for field in dataclasses.fields(Failure)]
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dataclasses.asdict(failure) for failure in self.failures)
