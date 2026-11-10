from __future__ import annotations

import email.message
import io
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import NoReturn

import pytest

from scrubbit.reddit import LoginError, RedditClient, Response, describe_failure, urllib_transport
from tests.conftest import CREDENTIALS
from tests.fake_reddit import FakeReddit


@pytest.mark.parametrize(
    ("status", "body", "hint"),
    [
        (400, {"error": "invalid_grant"}, "2FA"),
        (401, {"message": "Unauthorized"}, "client id or secret"),
        (0, {"error": "URLError: offline"}, "couldn't reach"),
        (403, {"non_json_body": "<html>"}, "approved API app"),
        (200, {"something": "new"}, "may have changed"),
    ],
)
def test_login_failures_explain_why(status: int, body: dict[str, str], hint: str) -> None:
    def transport(method: str, url: str, data: Mapping[str, str] | None, headers: Mapping[str, str]) -> Response:
        return Response(status, {}, body)

    with pytest.raises(LoginError, match=hint):
        RedditClient(CREDENTIALS, transport=transport, sleep=lambda _: None).login()


def test_login_retries_transient_failures() -> None:
    replies = [Response(503, {}, {}), Response(0, {}, {"error": "reset"}), Response(200, {}, {"access_token": "t"})]
    sent = []

    def transport(method: str, url: str, data: Mapping[str, str] | None, headers: Mapping[str, str]) -> Response:
        sent.append(url)
        return replies.pop(0)

    RedditClient(CREDENTIALS, transport=transport, sleep=lambda _: None).login()
    assert len(sent) == 3


class _BodyTimesOut(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise TimeoutError("timed out")


def test_transport_survives_failure_reading_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: urllib.request.Request, timeout: float) -> NoReturn:
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", email.message.Message(), _BodyTimesOut())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    response = urllib_transport("GET", "https://example.invalid/", None, {})
    assert response.status == 0
    assert "TimeoutError" in response.body["error"]


def test_credentials_repr_hides_secrets() -> None:
    assert "pw" not in repr(CREDENTIALS)
    assert "secret" not in repr(CREDENTIALS)


def test_request_retries_rate_limits_and_server_errors(client: RedditClient, fake: FakeReddit) -> None:
    fake.scripted = [Response(429, {"x-ratelimit-reset": "5"}, {}), Response(503, {}, {})]
    assert client.request("GET", "/api/info.json", params={"id": "t1_x"}).status == 200
    assert sum(1 for _, path, _ in fake.calls if path == "/api/info.json") == 3


def test_request_logs_in_again_when_token_expires(client: RedditClient, fake: FakeReddit) -> None:
    fake.scripted = [Response(401, {}, {})]
    assert client.request("GET", "/api/info.json", params={"id": "t1_x"}).status == 200
    assert sum(1 for _, url, _ in fake.calls if url.endswith("access_token")) == 2


def test_request_gives_up_and_returns_last_failure(client: RedditClient, fake: FakeReddit) -> None:
    fake.scripted = [Response(0, {}, {"error": "offline"})] * 10
    response = client.request("GET", "/api/info.json")
    assert response.status == 0
    assert "network error" in describe_failure(response)


def test_lookup_reports_unexpected_shape(
    client: RedditClient, fake: FakeReddit, caplog: pytest.LogCaptureFixture
) -> None:
    fake.scripted = [Response(200, {}, {"data": {"children": [{"data": {"name": "t1_a"}}, {"oops": 1}]}})]
    lookup = client.lookup(["t1_a", "t1_b"])
    assert lookup.found == {}
    assert lookup.failed == {"t1_a", "t1_b"}
    assert "unexpected shape" in caplog.text


def test_lookup_separates_failed_batches_from_missing_items(client: RedditClient, fake: FakeReddit) -> None:
    fake.things = {"t1_a": {"name": "t1_a"}}
    lookup = client.lookup(["t1_a", "t1_gone"])
    assert set(lookup.found) == {"t1_a"}
    assert lookup.failed == frozenset()
    fake.scripted = [Response(503, {}, {})] * 6
    assert client.lookup(["t1_a"]).failed == {"t1_a"}


def test_lookup_batches_by_100(client: RedditClient, fake: FakeReddit) -> None:
    fake.things = {f"t1_{i}": {"name": f"t1_{i}"} for i in range(250)}
    assert len(client.lookup(list(fake.things)).found) == 250
    assert sum(1 for _, path, _ in fake.calls if path == "/api/info.json") == 3


def test_edit_waits_out_ratelimit_then_succeeds(client: RedditClient, fake: FakeReddit) -> None:
    fake.things = {"t1_a": {"name": "t1_a", "body": "old"}}
    fake.scripted = [Response(200, {}, {"json": {"errors": [["RATELIMIT", "try again", "ratelimit"]]}})]
    assert client.edit_text("t1_a", "new") is None
    assert fake.things["t1_a"]["body"] == "new"


@pytest.mark.parametrize(
    ("responses", "message", "permanent"),
    [
        ([Response(200, {}, {"json": {"errors": [["THREAD_LOCKED", "x", "y"]]}})], "rejected the edit", True),
        ([Response(403, {}, {})], "forbidden", False),
        ([Response(200, {}, {"ok": True})], "unexpected /api/editusertext response", False),
        ([Response(503, {}, {})] * 6, "server error", False),
        ([Response(200, {}, {"json": {"errors": [["RATELIMIT", "x", "y"]]}})] * 3, "still rate limited", False),
    ],
)
def test_edit_failures_explain_why(
    client: RedditClient, fake: FakeReddit, responses: list[Response], message: str, permanent: bool
) -> None:
    fake.scripted = list(responses)
    error = client.edit_text("t1_a", "new")
    assert error is not None
    assert message in error.reason
    assert error.permanent is permanent


def test_delete_reports_error_in_body(client: RedditClient, fake: FakeReddit) -> None:
    fake.scripted = [Response(200, {}, {"error": "NOT_AUTHOR"})]
    error = client.delete("t1_a")
    assert error is not None
    assert "NOT_AUTHOR" in error
