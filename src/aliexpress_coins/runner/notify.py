"""Best-effort run notifications (issues #31 and #40).

The primary transport is the Apprise HTTP API: the operator runs an Apprise
server with their own targets, and this project sends it one plain-text message
per run. A generic JSON webhook and an ntfy topic remain available for an
operator who already configured them; Apprise is the documented default because
it does not make this project own every notification backend.

Delivery is never allowed to change the run's outcome. The engine catches
:class:`NotificationError`, records it in the report and the structured event
log, and exits on the run's own status.

Tests inject the opener (the callable that performs the HTTP request), so the
suite never opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.errors import NotificationError

__all__ = [
    'AppriseSink',
    'CompositeSink',
    'Notification',
    'NotificationSink',
    'NtfySink',
    'WebhookSink',
    'build_notifier',
]

DEFAULT_NOTIFICATION_TIMEOUT = 10.0
"""A notification may not hold a finished run open for long."""

Opener = Callable[..., Any]
"""The HTTP seam: ``urllib.request.urlopen`` in production, a fake in tests."""


@dataclass(frozen=True, slots=True)
class Notification:
    """The single message every sink delivers."""

    title: str
    body: str
    run_id: str
    status: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'tags', tuple(self.tags))

    def as_dict(self) -> dict[str, object]:
        """Render the message for a generic webhook payload."""
        return {
            'title': self.title,
            'body': self.body,
            'run_id': self.run_id,
            'status': self.status,
            'tags': list(self.tags),
        }


class NotificationSink(Protocol):
    """The injectable delivery seam."""

    @property
    def name(self) -> str:
        """A short sink name for the report and logs."""
        ...

    def send(self, notification: Notification) -> None:
        """Deliver one message or raise :class:`NotificationError`."""
        ...


def _post(opener: Opener, request: Request, timeout: float, name: str) -> None:
    try:
        with opener(request, timeout=timeout) as response:
            response.read()
    except Exception as exc:
        # A webhook URL may carry userinfo, so neither the URL nor the raw
        # exception message is safe to record. Keep only the class and, when
        # the transport exposes one, the HTTP status.
        status = getattr(exc, 'code', None)
        suffix = f' (HTTP {status})' if isinstance(status, int) else ''
        raise NotificationError(
            f'{name} delivery failed: {type(exc).__name__}{suffix}',
            status=status if isinstance(status, int) else None,
        ) from exc


_APPRISE_TYPES: dict[str, str] = {
    'completed': 'success',
    'budget': 'success',
    'stopped': 'warning',
    'failed': 'failure',
}
"""Run status -> Apprise message type (its own severity vocabulary)."""


def _apprise_endpoint(url: str, key: str | None) -> str:
    """Build ``<base>/notify[/<key>]`` without duplicating an explicit endpoint.

    ``AECOINS_APPRISE_URL`` is normally the server base. Accepting a full
    ``/notify`` (or ``/notify/<key>``) URL as well means an operator who copied
    an endpoint from the Apprise UI is not silently given a doubled path.
    """
    base = url.rstrip('/')
    path = urlsplit(base).path.rstrip('/')
    if path.endswith('/notify'):
        return base if key is None else f'{base}/{quote(key, safe="")}'
    if '/notify/' in path:
        return base
    return f'{base}/notify' if key is None else f'{base}/notify/{quote(key, safe="")}'


@dataclass(frozen=True, slots=True)
class AppriseSink:
    """POST one plain-text message to an Apprise API server.

    The operator supplies the server base URL and, when their Apprise instance
    uses a saved configuration, its key and optional tag. Apprise owns the fan
    out to ntfy, mail, Discord or anything else, so this class only speaks the
    one documented HTTP endpoint.
    """

    url: str
    key: str | None = None
    tag: str | None = None
    timeout: float = DEFAULT_NOTIFICATION_TIMEOUT
    opener: Opener = urlopen

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError('an Apprise URL is required')
        if self.timeout <= 0:
            raise ValueError(f'the notification timeout must be positive, got {self.timeout!r}')
        if self.key is not None and not self.key.strip():
            raise ValueError('an Apprise key must not be blank')

    @property
    def name(self) -> str:
        return 'apprise'

    @property
    def endpoint(self) -> str:
        """The notify endpoint this sink posts to (never logged)."""
        return _apprise_endpoint(self.url, self.key)

    def send(self, notification: Notification) -> None:
        payload: dict[str, str] = {
            'title': notification.title,
            'body': notification.body,
            'type': _APPRISE_TYPES.get(notification.status, 'info'),
            'format': 'text',
        }
        if self.tag:
            payload['tag'] = self.tag
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST',
        )
        _post(self.opener, request, self.timeout, self.name)


@dataclass(frozen=True, slots=True)
class WebhookSink:
    """POST a JSON document to a user-supplied URL."""

    url: str
    timeout: float = DEFAULT_NOTIFICATION_TIMEOUT
    opener: Opener = urlopen

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError('a webhook URL is required')
        if self.timeout <= 0:
            raise ValueError(f'the notification timeout must be positive, got {self.timeout!r}')

    @property
    def name(self) -> str:
        return 'webhook'

    def send(self, notification: Notification) -> None:
        payload = json.dumps(notification.as_dict(), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        request = Request(
            self.url,
            data=payload,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST',
        )
        _post(self.opener, request, self.timeout, self.name)


@dataclass(frozen=True, slots=True)
class NtfySink:
    """POST the message body to ``<server>/<topic>`` (the ntfy HTTP API)."""

    topic: str
    server: str = 'https://ntfy.sh'
    timeout: float = DEFAULT_NOTIFICATION_TIMEOUT
    opener: Opener = urlopen

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError('an ntfy topic is required')
        if not self.server.strip():
            raise ValueError('an ntfy server is required')
        if self.timeout <= 0:
            raise ValueError(f'the notification timeout must be positive, got {self.timeout!r}')

    @property
    def name(self) -> str:
        return 'ntfy'

    def send(self, notification: Notification) -> None:
        url = f'{self.server.rstrip("/")}/{quote(self.topic, safe="")}'
        headers = {
            'Content-Type': 'text/plain; charset=utf-8',
            'Title': notification.title,
            'Priority': 'high' if notification.status == 'failed' else 'default',
        }
        if notification.tags:
            headers['Tags'] = ','.join(notification.tags)
        request = Request(url, data=notification.body.encode('utf-8'), headers=headers, method='POST')
        _post(self.opener, request, self.timeout, self.name)


@dataclass(frozen=True, slots=True)
class CompositeSink:
    """Try every configured sink, then report all their failures together."""

    sinks: Sequence[NotificationSink] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'sinks', tuple(self.sinks))
        if not self.sinks:
            raise ValueError('a composite sink needs at least one sink')

    @property
    def name(self) -> str:
        return '+'.join(sink.name for sink in self.sinks)

    def send(self, notification: Notification) -> None:
        failures: list[NotificationError] = []
        for sink in self.sinks:
            try:
                sink.send(notification)
            except NotificationError as exc:
                failures.append(exc)
        if failures:
            status = next((failure.status for failure in failures if failure.status is not None), None)
            raise NotificationError('; '.join(str(failure) for failure in failures), status=status)


def build_notifier(config: RunConfig) -> NotificationSink | None:
    """Build the configured sinks, or ``None`` when notifications are off.

    Apprise is built first because it is the documented transport. The older
    webhook/ntfy settings still work so an existing deployment does not lose
    notifications on upgrade; configure only one transport to receive one
    message per run.
    """
    sinks: list[NotificationSink] = []
    if config.apprise_url:
        sinks.append(AppriseSink(config.apprise_url, key=config.apprise_key, tag=config.apprise_tag))
    if config.webhook_url:
        sinks.append(WebhookSink(config.webhook_url))
    if config.ntfy_topic:
        sinks.append(NtfySink(config.ntfy_topic, server=config.ntfy_server))
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)
