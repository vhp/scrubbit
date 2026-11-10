"""An in-memory Reddit used as a Transport, so tests never touch the network."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

from scrubbit.reddit import API_BASE, TOKEN_URL, Response


class FakeReddit:
    """Holds things by fullname and answers the endpoints scrubbit calls.

    Attributes:
        things: Live data by fullname, changed by edits and deletes.
        calls: Every request as (method, path, data), in order.
        locked: Fullnames whose edits Reddit rejects.
        ignore_edits: Fullnames whose edits "succeed" without changing anything.
        ignore_deletes: Fullnames whose deletes "succeed" without changing anything.
        forbidden_unsaves: Fullnames whose unsave returns 403.
        failing_edits: Fullnames whose edits get a 503 every time.
        info_down_after_changes: Once anything has changed, /api/info answers 503.
        scripted: Responses to return, in order, before normal handling resumes.
    """

    def __init__(self, things: dict[str, dict[str, Any]] | None = None) -> None:
        self.things = things or {}
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []
        self.locked: set[str] = set()
        self.ignore_edits: set[str] = set()
        self.ignore_deletes: set[str] = set()
        self.forbidden_unsaves: set[str] = set()
        self.failing_edits: set[str] = set()
        self.info_down_after_changes = False
        self.scripted: list[Response] = []
        self.token_body: Any = {"access_token": "token"}

    def __call__(self, method: str, url: str, data: Mapping[str, str] | None, headers: Mapping[str, str]) -> Response:
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        self.calls.append((method, path if url.startswith(API_BASE) else url, dict(data) if data else None))
        if url == TOKEN_URL:
            return Response(200, {}, self.token_body)
        if self.scripted:
            return self.scripted.pop(0)
        if path == "/api/info.json":
            if self.info_down_after_changes and self.mutations():
                return Response(503, {}, {})
            ids = urllib.parse.parse_qs(parts.query)["id"][0].split(",")
            children = [{"kind": i[:2], "data": dict(self.things[i])} for i in ids if i in self.things]
            return Response(200, {"x-ratelimit-remaining": "99"}, {"data": {"children": children}})
        assert data is not None or method == "DELETE"
        return self._mutate(method, path, data or {})

    def mutations(self) -> list[tuple[str, str]]:
        """Return (path, target) for every call that changes something."""
        return [
            (path, (data or {}).get("thing_id") or (data or {}).get("id") or path.rsplit("/", 1)[-1])
            for method, path, data in self.calls
            if method in {"POST", "DELETE"} and not path.startswith("https://")
        ]

    def _mutate(self, method: str, path: str, data: Mapping[str, str]) -> Response:
        if path == "/api/editusertext":
            name = data["thing_id"]
            if name in self.failing_edits:
                return Response(503, {}, {})
            if name in self.locked:
                return Response(200, {}, {"json": {"errors": [["THREAD_LOCKED", "locked", "parent"]]}})
            if name in self.things and name not in self.ignore_edits:
                self.things[name]["body" if name.startswith("t1") else "selftext"] = data["text"]
            return Response(200, {}, {"json": {"errors": [], "data": {}}})
        if path == "/api/del":
            name = data["id"]
            if name in self.things and name not in self.ignore_deletes:
                self.things[name]["author"] = "[deleted]"
            return Response(200, {}, {})
        if path == "/api/unsave":
            if data["id"] in self.forbidden_unsaves:
                return Response(403, {}, {"message": "Forbidden", "error": 403})
            return Response(200, {}, {})
        if method == "DELETE" and path.startswith("/api/v1/me/friends/"):
            return Response(204, {}, {})
        return Response(404, {}, {"message": "Not Found"})
