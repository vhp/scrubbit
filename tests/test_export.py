from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scrubbit.export import KINDS, Row, bare_id, find_export_file, key_from_text, parse_date, read_rows, subreddit_of

UTC = timezone.utc


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2017-04-24 18:52:15 UTC", datetime(2017, 4, 24, 18, 52, 15, tzinfo=UTC)),
        ("2019-01-01T00:00:00Z", datetime(2019, 1, 1, tzinfo=UTC)),
        ("2019-01-01", datetime(2019, 1, 1, tzinfo=UTC)),
        ("1493059935", datetime(2017, 4, 24, 18, 52, 15, tzinfo=UTC)),
        ("garbage", None),
        ("", None),
        (None, None),
        ("9" * 30, None),
    ],
)
def test_parse_date(value: str | None, expected: datetime | None) -> None:
    assert parse_date(value) == expected


def test_bare_id() -> None:
    assert bare_id(" t1_AbC ") == "abc"
    assert bare_id("xyz") == "xyz"


@pytest.mark.parametrize(
    ("text", "prefix", "expected"),
    [
        ("https://www.reddit.com/r/x/comments/abc/slug/def/", "t1", "def"),
        ("https://www.reddit.com/r/x/comments/abc/slug/def/", "t3", ""),
        ("/r/x/comments/abc/slug/", "t3", "abc"),
        ("/r/x/comments/abc/slug/", "t1", ""),
        ("see https://redd.it/xyz9", "t3", "xyz9"),
        ("t1_q1w2 and t3_zzz", "t1", "q1w2"),
        ("/r/x/comments/p9/t/mine/ then a pasted t1_zzz", "t1", "mine"),
        ("https://www.reddit.com/user/Some_One/", None, "Some_One"),
        ("nothing here", "t1", ""),
    ],
)
def test_key_from_text(text: str, prefix: str | None, expected: str) -> None:
    assert key_from_text(text, prefix) == expected


@pytest.mark.parametrize(
    ("fields", "text", "expected"),
    [
        ({"subreddit": "AskReddit"}, "", "AskReddit"),
        ({"permalink": "/r/pics/comments/a/b/"}, "", "pics"),
        ({"permalink": "/user/me/comments/a/b/"}, "", "u_me"),
        ({}, "x https://www.reddit.com/r/linux/comments/a/b/ y", "linux"),
        ({}, "talk about /r/linux sometime", ""),
    ],
)
def test_subreddit_of(fields: dict[str, str], text: str, expected: str) -> None:
    assert subreddit_of(Row(line=2, key="a", fields=fields, text=text)) == expected


def test_read_rows_standard_export(export_dir: Path) -> None:
    rows = read_rows(export_dir / "comments.csv", KINDS["comments"])
    assert [row.key for row in rows] == ["c1", "c2"]
    assert rows[0].get("subreddit") == "2012"
    assert rows[0].get("body") == "multi\nline"
    assert rows[1].line == 4  # the multi-line body shifts physical line numbers


def test_read_rows_recovers_ids_when_schema_drifts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    path = tmp_path / "posts.csv"
    path.write_text(
        "Link,Created At,Text\n"
        "https://www.reddit.com/r/pics/comments/p3/title/,2021-05-05T10:00:00Z,hello\n"
        "no link here,2021-05-05,???\n"
        "extra,https://redd.it/p5,2021,x,y\n"
    )
    rows = read_rows(path, KINDS["posts"])
    assert [row.key for row in rows] == ["p3", "", "p5"]
    assert rows[0].get("date") == "2021-05-05T10:00:00Z"
    log = caplog.text
    assert "has no id column" in log
    assert "recovered 2 of 3 ids from links" in log
    assert "1 rows have no recognizable id" in log
    assert "different number of fields" in log
    assert "unrecognized format" in log


def test_read_rows_ignores_parent_when_recovering_ids(tmp_path: Path) -> None:
    path = tmp_path / "comments.csv"
    path.write_text("uid,parent,permalink\nx!,t1_theirs,/r/x/comments/p/s/mine/\n")
    assert read_rows(path, KINDS["comments"])[0].key == "mine"


def test_read_rows_replaces_invalid_id_with_link(tmp_path: Path) -> None:
    path = tmp_path / "comments.csv"
    path.write_text("id,permalink\nnot an id!,/r/x/comments/p/s/realid/\n")
    assert read_rows(path, KINDS["comments"])[0].key == "realid"


def test_find_export_file_exact(export_dir: Path) -> None:
    assert find_export_file(export_dir, KINDS["posts"]) == export_dir / "posts.csv"


def test_find_export_file_falls_back_to_similar_name(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "post_votes.csv").write_text("id\n")
    (tmp_path / "data" / "post_headers.csv").write_text("id\n")
    (tmp_path / "data" / "my_posts_v2.csv").write_text("id\n")
    assert find_export_file(tmp_path, KINDS["posts"]) == tmp_path / "data" / "my_posts_v2.csv"
    assert find_export_file(tmp_path, KINDS["comments"]) is None
