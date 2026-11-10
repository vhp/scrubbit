"""Read Reddit's data export without trusting its layout.

Reddit publishes no schema or version number for the export, and the layout has
changed without notice: columns were added, renamed and reordered. So this
module assumes as little as it can:

* A file missing from its usual name is looked for under similar names anywhere
  in the export.
* Columns are matched by header name with a few aliases each (``FIELDS``), so
  their order doesn't matter and any of them may be missing.
* When the id or username column is missing, or holds something that isn't an
  id, the value is recovered from any Reddit link in the row.

Each fallback logs a warning naming the file and column, so a format change
shows up in the log instead of as a crash. Anything the export can't provide
(dates, subreddits, scores) is filled in later from the live API.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Kind:
    """A kind of thing scrubbit removes, and how to find it in the export.

    Attributes:
        name: The name used on the command line, like ``saved-posts``.
        filename: The file's usual name in the export.
        prefix: Reddit's fullname prefix, ``t1`` for comments and ``t3`` for
            posts, or None for friends, which are addressed by username.
        key: The field that identifies a row: ``id``, or ``username`` for friends.
        wanted: Optional fields worth a warning when the file lacks them.
        name_has: Words a substitute file's name must contain.
        name_lacks: Words a substitute file's name must not contain.
    """

    name: str
    filename: str
    prefix: str | None
    key: str
    wanted: tuple[str, ...]
    name_has: tuple[str, ...]
    name_lacks: tuple[str, ...] = ()

    @property
    def saved(self) -> bool:
        """Whether this kind lists saved items, which are unsaved rather than deleted."""
        return self.name.startswith("saved")


# Vote, header and message files also have "post" or "comment" in their names.
_NOT_AUTHORED: Final = ("saved", "vote", "header", "hidden", "message", "chat")
_AUTHORED_FIELDS: Final = ("permalink", "date", "subreddit")

KINDS: Final[dict[str, Kind]] = {
    kind.name: kind
    for kind in (
        Kind("comments", "comments.csv", "t1", "id", _AUTHORED_FIELDS, ("comment",), _NOT_AUTHORED),
        Kind("posts", "posts.csv", "t3", "id", _AUTHORED_FIELDS, ("post",), _NOT_AUTHORED),
        Kind("saved-comments", "saved_comments.csv", "t1", "id", ("permalink",), ("saved", "comment")),
        Kind("saved-posts", "saved_posts.csv", "t3", "id", ("permalink",), ("saved", "post")),
        Kind("friends", "friends.csv", None, "username", (), ("friend",)),
    )
}

FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "id": ("id", "fullname", "thing_id", "comment_id", "post_id"),
    "username": ("username", "user", "name", "friend"),
    "permalink": ("permalink", "url_path"),
    "date": ("date", "created_utc", "created", "created_at", "timestamp"),
    "subreddit": ("subreddit", "subreddit_name", "community"),
    "body": ("body", "selftext", "text", "content"),
    "parent": ("parent", "parent_id"),
}
"""Header names each field has been seen under or plausibly could be, in priority order.

``parent`` holds the fullname of the comment or post a comment replied to, so
it is left out when recovering a row's own id from its links.
"""

# /comments/<post id>/<slug>/<comment id>; only comment links have the last part.
_PERMALINK: Final = re.compile(r"/comments/([a-z0-9]+)(?:/[^/\s\"',]*/([a-z0-9]+))?", re.IGNORECASE)
_SHORTLINK: Final = re.compile(r"redd\.it/([a-z0-9]+)", re.IGNORECASE)
_USER_LINK: Final = re.compile(r"/u(?:ser)?/([A-Za-z0-9_-]{3,20})\b")
_SUBREDDIT_IN_PERMALINK: Final = re.compile(r"/(r|u|user)/([^/\s]+)")
_SUBREDDIT_IN_TEXT: Final = re.compile(r"/(r|u|user)/([A-Za-z0-9_]+)/comments/")
_ID: Final = re.compile(r"[a-z0-9]+")
_USERNAME: Final = re.compile(r"[A-Za-z0-9_-]+")
_MAX_FIELD_BYTES: Final = 2**31 - 1  # long comment bodies exceed csv's 128 KiB default


@dataclass(frozen=True)
class Row:
    """One row of an export file.

    Attributes:
        line: Where the row ends in the file, for log messages.
        key: The bare lowercase id, or the username for friends. Empty when
            neither the key column nor any link in the row gave one.
        fields: Cells keyed by the field names in ``FIELDS``. Fields the file
            has no column for are absent.
        text: Every cell joined with spaces, body last. Searched for links.
    """

    line: int
    key: str
    fields: dict[str, str]
    text: str

    def get(self, field: str) -> str:
        """Return a field's value, or an empty string if the file has no such column."""
        return self.fields.get(field, "")


def parse_date(value: str | None) -> datetime | None:
    """Parse the date formats seen in exports and the API into an aware datetime.

    Accepts ``2017-04-24 18:52:15 UTC``, ISO 8601 with or without an offset, and
    epoch seconds. Naive times are taken as UTC.

    Returns:
        The datetime, or None for an empty or unrecognized value.
    """
    text = (value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        try:
            return datetime.fromtimestamp(float(text), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = re.sub(r"\s*(UTC|GMT|Z)$", "+00:00", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def bare_id(value: str) -> str:
    """Strip a ``t1_``-style prefix and lowercase an id: ``t1_AbC`` becomes ``abc``."""
    return re.sub(r"^t\d_", "", value.strip(), flags=re.IGNORECASE).lower()


def subreddit_of(row: Row) -> str:
    """Return the row's subreddit from its column, else from a link in the row.

    Posts on a user profile come back as ``u_<username>``, which is how the API
    names them too. Returns an empty string when nothing in the row says.
    """
    if subreddit := row.get("subreddit"):
        return subreddit
    match = _SUBREDDIT_IN_PERMALINK.search(row.get("permalink")) or _SUBREDDIT_IN_TEXT.search(row.text)
    if not match:
        return ""
    return match[2] if match[1] == "r" else f"u_{match[2]}"


def key_from_text(text: str, prefix: str | None) -> str:
    """Recover an id or username from Reddit links anywhere in ``text``.

    Args:
        text: The text to search, usually every cell of a row.
        prefix: ``t1`` to find a comment id, ``t3`` for a post id, or None for
            a username.

    Returns:
        The bare lowercase id or the username, or an empty string if no link
        of the right kind is present. When there are several, the earliest in
        the text wins, whatever its form. A link to a comment never yields a
        post id, so a post row can't pick up the parent post of a comment.
    """
    if prefix is None:
        match = _USER_LINK.search(text)
        return match[1] if match else ""
    found: list[tuple[int, str]] = []
    if match := re.search(rf"\b{prefix}_([a-z0-9]+)\b", text, re.IGNORECASE):
        found.append((match.start(), match[1]))
    for match in _PERMALINK.finditer(text):
        if prefix == "t1" and match[2]:
            found.append((match.start(), match[2]))
            break
        if prefix == "t3" and not match[2]:
            found.append((match.start(), match[1]))
            break
    if prefix == "t3" and (match := _SHORTLINK.search(text)):
        found.append((match.start(), match[1]))
    return min(found)[1].lower() if found else ""


def find_export_file(export_dir: Path, kind: Kind) -> Path | None:
    """Locate a kind's CSV, falling back to similarly named files anywhere in the export.

    Returns:
        The file to read, or None if there is nothing plausible. Both the
        fallback and the miss are logged.
    """
    expected = export_dir / kind.filename
    if expected.is_file():
        return expected

    def plausible(path: Path) -> bool:
        stem = path.stem.lower()
        return all(word in stem for word in kind.name_has) and not any(word in stem for word in kind.name_lacks)

    candidates = sorted(
        (path for path in export_dir.rglob("*.csv") if plausible(path)),
        key=lambda path: (len(path.parts), len(path.stem)),
    )
    if not candidates:
        log.warning(
            "%s not found in the export and no similarly named CSV either; skipping %s", kind.filename, kind.name
        )
        return None
    others = [str(path.relative_to(export_dir)) for path in candidates[1:]]
    log.warning(
        "%s not found; using %s instead%s",
        kind.filename,
        candidates[0].relative_to(export_dir),
        f" (other candidates: {others})" if others else "",
    )
    return candidates[0]


def read_rows(path: Path, kind: Kind) -> list[Row]:
    """Read an export CSV into rows, logging every way it differs from what's expected.

    Blank lines are dropped. Rows whose key couldn't be found are kept with an
    empty ``key`` so the caller can tell an unusable file from an empty one.
    """
    csv.field_size_limit(_MAX_FIELD_BYTES)
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.reader(file)
        headers = [_normalize_header(header) for header in next(reader, [])]
        columns = _match_columns(headers)
        _warn_about_columns(path, kind, headers, columns)
        rows: list[Row] = []
        ragged = 0
        for raw_cells in reader:
            cells = [cell.strip() for cell in raw_cells]
            if not any(cells):
                continue
            ragged += len(cells) != len(headers)
            rows.append(_to_row(reader.line_num, cells, columns, kind))
    _warn_about_rows(path, kind, rows, ragged=ragged, key_column=kind.key in columns)
    return rows


def _normalize_header(header: str) -> str:
    return re.sub(r"[\s-]+", "_", header.strip().lower())


def _match_columns(headers: list[str]) -> dict[str, int]:
    """Map each field in FIELDS to the index of the first header that names it."""
    columns: dict[str, int] = {}
    for field, aliases in FIELDS.items():
        index = next((headers.index(alias) for alias in aliases if alias in headers), None)
        if index is not None:
            columns[field] = index
    return columns


def _to_row(line: int, cells: list[str], columns: dict[str, int], kind: Kind) -> Row:
    fields = {field: cells[index] if index < len(cells) else "" for field, index in columns.items()}
    body_index = columns.get("body")
    # Body last, so a link to the item itself wins over a link someone pasted into the text.
    text = " ".join(cell for index, cell in enumerate(cells) if index != body_index)
    text = f"{text} {fields.get('body', '')}"
    parent_index = columns.get("parent")
    own_text = " ".join(cell for index, cell in enumerate(cells) if index not in (body_index, parent_index))
    own_text = f"{fields.get('permalink', '')} {own_text} {fields.get('body', '')}"
    return Row(line=line, key=_row_key(fields.get(kind.key, ""), own_text, kind), fields=fields, text=text)


def _row_key(given: str, text: str, kind: Kind) -> str:
    """Use the key column's value if it looks valid, else recover one from links.

    ``text`` should start with the row's permalink and leave out cells that
    point at other items, so the first link found is the row's own.
    """
    if kind.key == "id":
        candidate = bare_id(given)
        return candidate if _ID.fullmatch(candidate) else key_from_text(text, kind.prefix)
    return given if _USERNAME.fullmatch(given) else key_from_text(text, None)


def _warn_about_columns(path: Path, kind: Kind, headers: list[str], columns: dict[str, int]) -> None:
    log.debug("%s columns: %s", path.name, headers)
    if kind.key not in columns:
        log.warning(
            "%s has no %s column (its columns are %s); recovering %ss from links in any column",
            path.name,
            kind.key,
            headers,
            kind.key,
        )
    if missing := [field for field in kind.wanted if field not in columns]:
        log.warning("%s has no column for %s; using the API and links for that instead", path.name, ", ".join(missing))


def _warn_about_rows(path: Path, kind: Kind, rows: list[Row], *, ragged: int, key_column: bool) -> None:
    if ragged:
        log.warning(
            "%s: %d rows have a different number of fields than the header, so columns may be "
            "shifted; ids are checked and bad dates fall back to the API",
            path.name,
            ragged,
        )
    if not key_column and (recovered := sum(1 for row in rows if row.key)):
        log.info("%s: recovered %d of %d %ss from links", path.name, recovered, len(rows), kind.key)
    if lost := [row for row in rows if not row.key]:
        log.warning(
            "%s: %d rows have no recognizable %s and will be skipped (first ends at line %d: %.120s)",
            path.name,
            len(lost),
            kind.key,
            lost[0].line,
            lost[0].text,
        )
    if bad := [row.get("date") for row in rows if row.get("date") and parse_date(row.get("date")) is None]:
        log.warning(
            "%s: %d dates are in an unrecognized format (e.g. %r); using the API's created_utc instead",
            path.name,
            len(bad),
            bad[0],
        )
