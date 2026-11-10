from __future__ import annotations

import dataclasses
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scrubbit.reddit import RedditClient, Response
from scrubbit.report import Report
from scrubbit.shredder import Options, Shredder, looks_overwritten, make_filler
from tests.fake_reddit import FakeReddit


def live_things() -> dict[str, dict[str, Any]]:
    return {
        "t1_c1": {"name": "t1_c1", "author": "me", "subreddit": "2012", "score": 5, "body": "orig"},
        "t1_c2": {"name": "t1_c2", "author": "me", "subreddit": "tech", "score": 50, "body": "orig"},
        "t3_p1": {
            "name": "t3_p1",
            "author": "me",
            "subreddit": "pics",
            "score": 2,
            "is_self": True,
            "selftext": "orig",
        },
        "t3_p2": {"name": "t3_p2", "author": "me", "subreddit": "pics", "score": 2, "is_self": False, "selftext": ""},
    }


def run(client: RedditClient, export_dir: Path, **overrides: Any) -> Report:
    options = Options(export_dir=export_dir, username="me", kinds=("comments", "posts"))
    report = Report()
    Shredder(client, dataclasses.replace(options, **overrides), report, rng=random.Random(1)).run()
    return report


def outcomes(report: Report) -> dict[str, int]:
    return {f"{kind}: {outcome}": count for (kind, outcome), count in report.counts.items()}


def test_dry_run_changes_nothing(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    report = run(client, export_dir, dry_run=True, kinds=("comments", "posts", "saved-posts", "friends"))
    assert fake.mutations() == []
    assert outcomes(report) == {
        "comments: would overwrite then delete": 2,
        "posts: would overwrite then delete": 1,
        "posts: would delete": 1,
        "saved-posts: would unsave": 1,
        "friends: would unfriend": 1,
    }


def test_overwrites_everything_before_deleting(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    report = run(client, export_dir)
    assert fake.mutations() == [
        ("/api/editusertext", "t1_c1"),
        ("/api/editusertext", "t1_c2"),
        ("/api/del", "t1_c1"),
        ("/api/del", "t1_c2"),
        ("/api/editusertext", "t3_p1"),  # p2 is a link post, so it's only deleted
        ("/api/del", "t3_p1"),
        ("/api/del", "t3_p2"),
    ]
    assert not report.failures
    assert outcomes(report) == {
        "comments: overwritten, verified": 2,
        "comments: deleted, verified": 2,
        "posts: overwritten, verified": 1,
        "posts: deleted, verified": 2,
    }


def test_silent_failures_are_caught_by_verification(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    fake.locked = {"t1_c1"}
    fake.ignore_edits = {"t3_p1"}
    fake.ignore_deletes = {"t1_c2"}
    report = run(client, export_dir)
    assert {(f.item, f.action) for f in report.failures} == {
        ("t1_c1", "overwrite"),
        ("t3_p1", "overwrite"),
        ("t1_c2", "delete"),
    }
    details = {f.item: f.detail for f in report.failures}
    assert "THREAD_LOCKED" in details["t1_c1"]
    assert "still there" in details["t1_c2"]
    # Neither item that couldn't be overwritten is deleted, even c1, which Reddit will never let you edit.
    deleted = {target for path, target in fake.mutations() if path == "/api/del"}
    assert deleted == {"t1_c2", "t3_p2"}
    assert "--delete-if-overwrite-fails" in details["t1_c1"]


def test_delete_if_overwrite_fails_deletes_anyway(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    fake.locked = {"t1_c1"}
    report = run(client, export_dir, kinds=("comments",), delete_if_overwrite_fails=True)
    assert ("/api/del", "t1_c1") in fake.mutations()
    assert "deleting it anyway" in report.failures[0].detail


def test_transient_overwrite_failure_keeps_item_for_rerun(
    client: RedditClient, fake: FakeReddit, export_dir: Path
) -> None:
    fake.things = live_things()
    fake.failing_edits = {"t1_c1"}
    report = run(client, export_dir, kinds=("comments",))
    assert ("/api/del", "t1_c1") not in fake.mutations()
    assert fake.things["t1_c1"]["author"] == "me"
    assert "rerun can try again" in report.failures[0].detail

    fake.failing_edits = set()
    fake.calls.clear()
    run(client, export_dir, kinds=("comments",))
    assert fake.mutations() == [("/api/editusertext", "t1_c1"), ("/api/del", "t1_c1")]


def test_failed_read_back_is_a_failure_not_a_success(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    fake.info_down_after_changes = True
    report = run(client, export_dir, kinds=("posts",))
    # p1's overwrite couldn't be confirmed, so it isn't deleted; p2 is a link post and is.
    assert fake.mutations() == [("/api/editusertext", "t3_p1"), ("/api/del", "t3_p2")]
    assert {(f.item, f.action) for f in report.failures} == {("t3_p1", "verify"), ("t3_p2", "verify")}
    assert not any("not visible" in outcome for _, outcome in report.counts)


@pytest.mark.parametrize("kinds", [("posts",), ("saved-posts",)])
def test_failed_lookup_changes_nothing_and_is_a_failure(
    client: RedditClient, fake: FakeReddit, export_dir: Path, kinds: tuple[str, ...]
) -> None:
    fake.things = live_things()
    fake.scripted = [Response(404, {}, {})]
    report = run(client, export_dir, kinds=kinds, skip_ids=frozenset({"p2"}))
    assert fake.mutations() == []
    expected = {("t3_p1", "look up")} if kinds == ("posts",) else {("t3_s1", "look up")}
    assert {(f.item, f.action) for f in report.failures} == expected


def test_rerun_skips_finished_work(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    fake.ignore_deletes = {"t1_c2"}
    run(client, export_dir)
    fake.calls.clear()
    report = run(client, export_dir)
    # c2 kept its filler but survived the delete: no second edit, just another delete.
    assert fake.mutations() == [("/api/del", "t1_c2")]
    assert outcomes(report)["comments: already overwritten"] == 1


def test_items_by_someone_else_are_skipped(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    fake.things["t1_c1"]["author"] = "someone_else"
    report = run(client, export_dir, kinds=("comments",))
    assert ("/api/del", "t1_c1") not in fake.mutations()
    assert outcomes(report)["comments: skipped: not yours"] == 1


def test_max_score_uses_live_scores(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    del fake.things["t1_c1"]  # not visible to the API, so its score is unknown
    report = run(client, export_dir, kinds=("comments",), max_score=10)
    assert fake.mutations() == []
    assert outcomes(report) == {"comments: skipped: score unknown": 1, "comments: skipped: score above --max-score": 1}


def test_date_and_subreddit_filters(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    report = run(client, export_dir, kinds=("comments",), before=datetime(2018, 1, 1, tzinfo=timezone.utc))
    assert {target for _, target in fake.mutations()} == {"t1_c1"}
    assert outcomes(report)["comments: skipped: not before --before"] == 1

    fake.calls.clear()
    report = run(client, export_dir, kinds=("comments",), only_subreddits=frozenset({"tech"}))
    assert {target for _, target in fake.mutations()} == {"t1_c2"}


def test_edit_only_deletes_nothing(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    report = run(client, export_dir, edit_only=True, kinds=("comments", "posts", "saved-posts"))
    assert {path for path, _ in fake.mutations()} == {"/api/editusertext"}
    assert outcomes(report)["posts: skipped: link post, can't be overwritten"] == 1


def test_unsave_failure_is_reported(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.forbidden_unsaves = {"t3_s1"}
    report = run(client, export_dir, kinds=("saved-posts",))
    assert [(f.item, f.subreddit, f.action) for f in report.failures] == [("t3_s1", "foo", "unsave")]


def test_saved_items_obey_date_and_score_filters(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = {"t3_s1": {"name": "t3_s1", "author": "other", "created_utc": 1700000000.0, "score": 900}}
    report = run(client, export_dir, kinds=("saved-posts",), before=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert fake.mutations() == []
    assert outcomes(report) == {"saved-posts: skipped: not before --before": 1}

    report = run(client, export_dir, kinds=("saved-posts",), max_score=100)
    assert outcomes(report) == {"saved-posts: skipped: score above --max-score": 1}


def test_skip_subreddits_keeps_items_whose_subreddit_is_unknown(client: RedditClient, tmp_path: Path) -> None:
    (tmp_path / "comments.csv").write_text("id,body\nzz,hello\n")
    report = run(client, tmp_path, kinds=("comments",), skip_subreddits=frozenset({"pics"}))
    assert outcomes(report) == {"comments: skipped: subreddit unknown": 1}


def test_export_without_ids_is_a_failure(client: RedditClient, tmp_path: Path) -> None:
    (tmp_path / "comments.csv").write_text("a,b\n1,2\n")
    report = run(client, tmp_path, kinds=("comments",))
    assert [(f.item, f.action) for f in report.failures] == [("comments.csv", "read export")]


def test_unreadable_export_is_a_failure(client: RedditClient, tmp_path: Path) -> None:
    (tmp_path / "comments.csv").write_bytes(b"id,body\nc1,caf\xe9\n")
    report = run(client, tmp_path, kinds=("comments",))
    assert [(f.item, f.action) for f in report.failures] == [("comments.csv", "read export")]
    assert "UnicodeDecodeError" in report.failures[0].detail


def test_fixed_replacement_is_used(client: RedditClient, fake: FakeReddit, export_dir: Path) -> None:
    fake.things = live_things()
    run(client, export_dir, kinds=("comments",), replacement="gone")
    assert fake.things["t1_c1"]["body"] == "gone"


def test_filler_varies_and_is_recognized() -> None:
    rng = random.Random(7)
    fillers = {make_filler(rng) for _ in range(5)}
    assert len(fillers) == 5
    assert all(looks_overwritten(text, None) for text in fillers)
    assert not looks_overwritten("my actual opinion", None)
    assert not looks_overwritten("", None)


@pytest.mark.parametrize(("text", "expected"), [(" gone ", True), ("Lorem ipsum.", False)])
def test_fixed_replacement_matching(text: str, expected: bool) -> None:
    assert looks_overwritten(text, "gone") is expected
