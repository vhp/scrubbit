"""A small Reddit OAuth client built for long, unattended runs.

It uses only the standard library. Every request is paced by Reddit's rate-limit
headers, retried on network errors, 429 and 5xx, and re-authenticated once when
the hour-long access token expires, since a large export takes longer than that.

Nothing here raises on an HTTP failure. Methods that change something return
None on success or a sentence saying why it failed, and ``describe_failure``
turns any response into such a sentence. Response bodies are never assumed to
have a particular shape, because Reddit changes them without versioning; an
unexpected shape is reported as exactly that, with the body attached.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

log = logging.getLogger(__name__)

API_BASE: Final = "https://oauth.reddit.com"
TOKEN_URL: Final = "https://www.reddit.com/api/v1/access_token"  # noqa: S105  a URL, not a secret
MIN_GAP_SECONDS: Final = 1.0  # OAuth clients get 100 requests per minute
MAX_ATTEMPTS: Final = 6
INFO_BATCH_SIZE: Final = 100  # /api/info accepts at most 100 ids per call
EDIT_ATTEMPTS: Final = 3
EDIT_RATELIMIT_WAIT_SECONDS: Final = 60.0
REQUEST_TIMEOUT_SECONDS: Final = 30.0

_STATUS_HINTS: Final = {
    0: "network error",
    HTTPStatus.UNAUTHORIZED: "token rejected",
    HTTPStatus.FORBIDDEN: "forbidden: private or banned subreddit, locked or archived thread, or not your item",
    HTTPStatus.NOT_FOUND: "not found: the item is gone, or Reddit moved this endpoint",
    HTTPStatus.TOO_MANY_REQUESTS: "still rate limited after retries",
}


@dataclass(frozen=True)
class Response:
    """An HTTP response, or a stand-in for one that never arrived.

    Attributes:
        status: The HTTP status, or 0 when the request failed before any
            response (DNS, TLS, timeout, connection reset).
        headers: Response headers with lowercase names.
        body: The parsed JSON body. A body that isn't JSON becomes
            ``{"non_json_body": <first 300 characters>}`` and a network failure
            becomes ``{"error": <description>}``.
    """

    status: int
    headers: Mapping[str, str]
    body: Any

    @property
    def retryable(self) -> bool:
        """Whether the failure is likely transient: no response, 429, or a server error."""
        return self.status in (0, HTTPStatus.TOO_MANY_REQUESTS) or self.status >= HTTPStatus.INTERNAL_SERVER_ERROR


class Transport(Protocol):
    """Sends one HTTP request. ``urllib_transport`` in production, a fake in tests."""

    def __call__(self, method: str, url: str, data: Mapping[str, str] | None, headers: Mapping[str, str]) -> Response:
        """Send the request and return the response; never raise."""
        ...


def urllib_transport(method: str, url: str, data: Mapping[str, str] | None, headers: Mapping[str, str]) -> Response:
    """Send a form-encoded request with urllib. Network errors become status 0."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:  # noqa: S310
            status, raw_headers, raw = reply.status, reply.headers, reply.read()
    except urllib.error.HTTPError as error:
        status, raw_headers = error.code, error.headers
        try:
            raw = error.read()
        except (http.client.HTTPException, OSError) as read_error:
            return Response(0, {}, {"error": f"HTTP {status}, then {type(read_error).__name__}: {read_error}"})
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        return Response(0, {}, {"error": f"{type(error).__name__}: {error}"})
    headers_lower = {name.lower(): value for name, value in raw_headers.items()}
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {"non_json_body": raw[:300].decode(errors="replace")}
    return Response(status, headers_lower, parsed)


@dataclass(frozen=True)
class Credentials:
    """What Reddit's password grant needs, from a "script" app at reddit.com/prefs/apps."""

    username: str
    password: str
    client_id: str
    client_secret: str
    user_agent: str

    def __repr__(self) -> str:
        """Keep the password and secret out of logs and tracebacks."""
        return f"Credentials(username={self.username!r}, client_id={self.client_id!r})"


class LoginError(Exception):
    """Reddit refused the login. The message gives the most likely reason."""


@dataclass(frozen=True)
class EditError:
    """Why an edit failed.

    Attributes:
        reason: A sentence saying why, for the log and the failures file.
        permanent: Reddit refused this edit itself, as for a locked or archived
            thread, so retrying won't help. False when the failure may be
            transient or its cause is unknown.
    """

    reason: str
    permanent: bool = False


@dataclass(frozen=True)
class Lookup:
    """What ``RedditClient.lookup`` learned.

    Attributes:
        found: Fullname to the API's data, for every item the API returned.
        failed: Fullnames in batches whose request failed or came back in an
            unexpected shape. Nothing is known about these, which is different
            from the API answering without them.
    """

    found: dict[str, dict[str, Any]]
    failed: frozenset[str]


def summarize(value: object, limit: int = 300) -> str:
    """Render a value as compact JSON, cut to ``limit`` characters, for log messages."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else f"{text[:limit]}..."


def describe_failure(response: Response) -> str:
    """Explain a failed response in a sentence, with the body for detail."""
    if response.status in _STATUS_HINTS:
        reason = _STATUS_HINTS[response.status]
    elif response.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        reason = f"Reddit server error (HTTP {response.status})"
    else:
        reason = f"HTTP {response.status}"
    return f"{reason}: {summarize(response.body)}"


class RedditClient:
    """Authenticated access to the handful of Reddit endpoints scrubbit needs.

    Args:
        credentials: Account and app credentials for the password grant.
        transport: Sends HTTP requests. Tests pass a fake.
        sleep: Waits between requests. Tests pass a no-op.
        clock: A monotonic clock used for pacing.
    """

    def __init__(
        self,
        credentials: Credentials,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a client. Nothing is sent until ``login`` or the first request."""
        self._credentials = credentials
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._token = ""
        self._next_request_at = 0.0

    def login(self) -> None:
        """Get a fresh access token, retrying network errors, 429 and 5xx.

        Raises:
            LoginError: Reddit refused, or kept failing, with the most likely
                reason in the message.
        """
        creds = self._credentials
        basic = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
        for attempt in range(MAX_ATTEMPTS):
            response = self._transport(
                "POST",
                TOKEN_URL,
                {"grant_type": "password", "username": creds.username, "password": creds.password},
                {"User-Agent": creds.user_agent, "Authorization": f"Basic {basic}"},
            )
            body = response.body if isinstance(response.body, dict) else {}
            token = body.get("access_token")
            if isinstance(token, str) and token:
                self._token = token
                return
            if not response.retryable or attempt == MAX_ATTEMPTS - 1:
                raise LoginError(
                    f"login failed (HTTP {response.status}): {_login_hint(response, body)}\n"
                    f"response: {summarize(response.body)}"
                )
            wait = 2.0**attempt
            log.warning("login: %s; retrying in %.0fs", describe_failure(response), wait)
            self._sleep(wait)

    def request(
        self,
        method: str,
        path: str,
        data: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Response:
        """Call an OAuth endpoint, pacing, retrying and re-authenticating as needed.

        Returns:
            The final response, which may still be a failure after every retry.

        Raises:
            LoginError: The token expired and logging in again failed.
        """
        if not self._token:
            self.login()
        url = API_BASE + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
        relogged = False
        response = Response(0, {}, {"error": "no attempt made"})
        for attempt in range(MAX_ATTEMPTS):
            self._sleep(max(0.0, self._next_request_at - self._clock()))
            headers = {"User-Agent": self._credentials.user_agent, "Authorization": f"bearer {self._token}"}
            response = self._transport(method, url, data, headers)
            reset = self._pace(response)
            if response.status == HTTPStatus.UNAUTHORIZED and not relogged:
                log.info("access token expired, logging in again")
                relogged = True
                self.login()
                continue
            if response.retryable:
                wait = max(reset, 2.0**attempt)
                log.warning("%s %s: %s; retrying in %.0fs", method, path, describe_failure(response), wait)
                self._sleep(wait)
                continue
            return response
        return response

    def lookup(self, fullnames: Sequence[str]) -> Lookup:
        """Fetch live data for comments and posts, 100 per request.

        Items the API can't see, like those in private subreddits, are absent
        from ``found``. Items in a batch that failed are in ``failed`` instead,
        and the failure is logged.
        """
        found: dict[str, dict[str, Any]] = {}
        failed: set[str] = set()
        for start in range(0, len(fullnames), INFO_BATCH_SIZE):
            batch = fullnames[start : start + INFO_BATCH_SIZE]
            response = self.request("GET", "/api/info.json", params={"id": ",".join(batch), "raw_json": "1"})
            if response.status != HTTPStatus.OK:
                log.error("/api/info failed for %d items: %s", len(batch), describe_failure(response))
                failed.update(batch)
                continue
            try:
                children = {child["data"]["name"]: child["data"] for child in response.body["data"]["children"]}
            except (KeyError, TypeError) as error:
                log.error(
                    "/api/info returned an unexpected shape (missing %s); Reddit may have changed it: %s",
                    error,
                    summarize(response.body),
                )
                failed.update(batch)
                continue
            found.update(children)
        if failed:
            log.warning(
                "couldn't look up %d of %d items; filters and checks that need live data won't work for them",
                len(failed),
                len(fullnames),
            )
        return Lookup(found, frozenset(failed))

    def edit_text(self, fullname: str, text: str) -> EditError | None:
        """Replace a comment's or self post's text.

        Returns:
            None on success, else why it failed. An error Reddit puts in the
            body (other than a rate limit) counts as permanent. Anything else,
            including a 403, might work on a later run: a 403 could just as
            well come from an app or token problem that affects every edit.
        """
        for _ in range(EDIT_ATTEMPTS):
            response = self.request(
                "POST", "/api/editusertext", {"thing_id": fullname, "text": text, "api_type": "json"}
            )
            if response.status != HTTPStatus.OK:
                return EditError(describe_failure(response))
            errors = _json_errors(response.body)
            if errors is None:
                return EditError(
                    f"unexpected /api/editusertext response; Reddit may have changed it: {summarize(response.body)}"
                )
            if not errors:
                return None
            if _is_ratelimit(errors):
                log.warning("edit rate limited, waiting %.0fs: %s", EDIT_RATELIMIT_WAIT_SECONDS, summarize(errors))
                self._sleep(EDIT_RATELIMIT_WAIT_SECONDS)
                continue
            return EditError(f"Reddit rejected the edit: {summarize(errors)}", permanent=True)
        return EditError(f"still rate limited after {EDIT_ATTEMPTS} tries")

    def delete(self, fullname: str) -> str | None:
        """Delete a comment or post. Returns None on success, else why it failed."""
        return self._action("/api/del", {"id": fullname})

    def unsave(self, fullname: str) -> str | None:
        """Remove a comment or post from your saved list. Returns None on success, else why it failed."""
        return self._action("/api/unsave", {"id": fullname})

    def unfriend(self, username: str) -> str | None:
        """Remove someone from your friends. Returns None on success, else why it failed."""
        response = self.request("DELETE", f"/api/v1/me/friends/{urllib.parse.quote(username)}")
        return None if response.status in (HTTPStatus.OK, HTTPStatus.NO_CONTENT) else describe_failure(response)

    def _action(self, path: str, data: Mapping[str, str]) -> str | None:
        response = self.request("POST", path, data)
        if response.status != HTTPStatus.OK:
            return describe_failure(response)
        body = response.body
        if isinstance(body, dict) and (body.get("error") or _json_errors(body)):
            return f"Reddit returned an error: {summarize(body)}"
        return None

    def _pace(self, response: Response) -> float:
        """Schedule the next request from the rate-limit headers; return seconds until the window resets."""
        try:
            remaining = float(response.headers.get("x-ratelimit-remaining", "1"))
            reset = float(response.headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            remaining, reset = 1.0, 0.0
        self._next_request_at = self._clock() + (reset + 1 if remaining < 1 else MIN_GAP_SECONDS)
        return reset


def _login_hint(response: Response, body: Mapping[str, Any]) -> str:
    if response.status == 0:
        return "couldn't reach reddit.com"
    if response.status == HTTPStatus.UNAUTHORIZED:
        return "client id or secret is wrong; check the 'script' app at https://www.reddit.com/prefs/apps"
    if body.get("error") == "invalid_grant":
        return (
            "username or password is wrong, or 2FA is on and the code is missing or expired "
            "(pass the password as password:123456)"
        )
    if response.status in (HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS) or "non_json_body" in body:
        return "Reddit refused the login; it may require an approved API app or a different --user-agent"
    return "unexpected response from the token endpoint; Reddit may have changed it"


def _json_errors(body: object) -> list[Any] | None:
    """Return the ``json.errors`` list from an ``api_type=json`` response, or None if it isn't there."""
    if not isinstance(body, dict):
        return None
    inner = body.get("json")
    if not isinstance(inner, dict):
        return None
    errors = inner.get("errors")
    return errors if isinstance(errors, list) else None


def _is_ratelimit(errors: list[Any]) -> bool:
    return any(isinstance(error, list) and error[:1] == ["RATELIMIT"] for error in errors)
