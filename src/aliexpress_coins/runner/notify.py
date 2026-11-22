"""Best-effort run notifications (issue #31).

The transport is deliberately small: a generic JSON webhook and an ntfy topic,
both configured from the environment. Delivery is never allowed to change the
run's outcome. The engine catches :class:`NotificationError`, records it in the
report and the structured event log, and exits on the run's own status; Apprise
(#40) replaces this transport without changing that contract.

Tests inject the opener (the callable that performs the HTTP request), so the
suite never opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.errors import NotificationError

__all__ = [
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
    except NotificationError:
        raise
    except Exception as exc:
        raise NotificationError(f'{name} delivery failed: {type(exc).__name__}: {exc}') from exc


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
        failures: list[str] = []
        for sink in self.sinks:
            try:
                sink.send(notification)
            except NotificationError as exc:
                failures.append(str(exc))
        if failures:
            raise NotificationError('; '.join(failures))


def build_notifier(config: RunConfig) -> NotificationSink | None:
    """Build the configured sinks, or ``None`` when notifications are off."""
    sinks: list[NotificationSink] = []
    if config.webhook_url:
        sinks.append(WebhookSink(config.webhook_url))
    if config.ntfy_topic:
        sinks.append(NtfySink(config.ntfy_topic, server=config.ntfy_server))
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)
