"""Shared fixtures: a fake Reddit, a client wired to it, and a standard-layout export."""

from __future__ import annotations

from pathlib import Path

import pytest

from scrubbit.reddit import Credentials, RedditClient
from tests.fake_reddit import FakeReddit

CREDENTIALS = Credentials("me", "pw", "client", "secret", "scrubbit-tests")


@pytest.fixture
def fake() -> FakeReddit:
    return FakeReddit()


@pytest.fixture
def client(fake: FakeReddit) -> RedditClient:
    return RedditClient(CREDENTIALS, transport=fake, sleep=lambda _: None, clock=lambda: 0.0)


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    """A small export laid out the way Reddit currently ships it."""
    root = tmp_path / "export"
    root.mkdir()
    (root / "comments.csv").write_text(
        "﻿id,permalink,date,ip,subreddit,gildings,link,parent,body,media\n"
        'c1,/r/2012/comments/p9/t/c1/,2017-04-24 18:52:15 UTC,,2012,,l,p,"multi\nline",\n'
        "c2,/r/tech/comments/p9/t/c2/,2018-01-01 00:00:00 UTC,,tech,0,l,p,hi,\n",
        encoding="utf-8",
    )
    (root / "posts.csv").write_text(
        "id,permalink,date,ip,subreddit,gildings,title,url,body\n"
        "p1,/r/pics/comments/p1/t/,2020-01-01 00:00:00 UTC,,pics,,t,,self text\n"
        "p2,/r/pics/comments/p2/t/,2020-01-01 00:00:00 UTC,,pics,,t,https://example.com,\n",
        encoding="utf-8",
    )
    (root / "saved_posts.csv").write_text("id,permalink\ns1,https://www.reddit.com/r/foo/comments/s1/x/\n")
    (root / "friends.csv").write_text("username,note\nsomefriend,\n")
    return root
