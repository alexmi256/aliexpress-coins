"""Notification sinks: configured, injected and never allowed to raise unnoticed."""

from __future__ import annotations

import json
from dataclasses import replace
from http.client import HTTPMessage
from typing import Any
from urllib.error import HTTPError

import pytest

from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.errors import NotificationError
from aliexpress_coins.runner.notify import (
    CompositeSink,
    Notification,
    NtfySink,
    WebhookSink,
    build_notifier,
)

MESSAGE = Notification(
    title='aliexpress-coins failed 20261122T033000Z',
    body='status: failed\nerror: DeviceError: tablet absent',
    run_id='20261122T033000Z',
    status='failed',
    tags=('coins', 'failed'),
)


class _Response:
    def __init__(self, payload: bytes = b'ok') -> None:
        self.payload = payload
        self.reads = 0

    def read(self) -> bytes:
        self.reads += 1
        return self.payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Opener:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Any, *, timeout: float) -> _Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return _Response()


def test_the_webhook_posts_the_notification_as_json() -> None:
    opener = _Opener()
    sink = WebhookSink('https://hooks.example/coins', opener=opener)

    sink.send(MESSAGE)

    request = opener.requests[0]
    assert request.full_url == 'https://hooks.example/coins'
    assert request.get_method() == 'POST'
    assert json.loads(request.data) == {
        'title': MESSAGE.title,
        'body': MESSAGE.body,
        'run_id': MESSAGE.run_id,
        'status': 'failed',
        'tags': ['coins', 'failed'],
    }
    assert opener.timeouts == [10.0]


def test_ntfy_posts_the_body_with_title_and_priority_headers() -> None:
    opener = _Opener()
    sink = NtfySink('coins/nightly', server='https://ntfy.example/', opener=opener)

    sink.send(MESSAGE)

    request = opener.requests[0]
    assert request.full_url == 'https://ntfy.example/coins%2Fnightly'
    assert request.data == MESSAGE.body.encode('utf-8')
    assert request.get_header('Title') == MESSAGE.title
    assert request.get_header('Priority') == 'high'
    assert request.get_header('Tags') == 'coins,failed'


def test_a_failed_delivery_records_the_class_but_not_the_message_or_url() -> None:
    secret = 'correct-horse'
    opener = _Opener(error=OSError(f'connection refused for https://alice:{secret}@hooks.example/coins'))
    with pytest.raises(NotificationError) as raised:
        WebhookSink(f'https://alice:{secret}@hooks.example/coins', opener=opener).send(MESSAGE)

    assert str(raised.value) == 'webhook delivery failed: OSError'
    assert secret not in str(raised.value)


def test_a_failed_delivery_keeps_the_http_status_when_available() -> None:
    error = HTTPError('https://hooks.example/coins', 503, 'Service Unavailable', HTTPMessage(), None)
    with pytest.raises(NotificationError, match=r'webhook delivery failed: HTTPError \(HTTP 503\)'):
        WebhookSink('https://hooks.example/coins', opener=_Opener(error=error)).send(MESSAGE)


def test_an_already_typed_notification_error_is_also_sanitised() -> None:
    opener = _Opener(error=NotificationError('already typed with https://alice:secret@host'))
    with pytest.raises(NotificationError, match='webhook delivery failed: NotificationError'):
        WebhookSink('https://hooks.example/coins', opener=opener).send(MESSAGE)


def test_ntfy_without_tags_omits_the_header() -> None:
    opener = _Opener()
    message = Notification(title='done', body='ok', run_id='run', status='completed')

    NtfySink('coins', opener=opener).send(message)

    assert opener.requests[0].get_header('Tags') is None
    assert opener.requests[0].get_header('Priority') == 'default'


def test_a_composite_sink_does_not_raise_when_every_sink_succeeds() -> None:
    first = _Opener()
    second = _Opener()
    sink = CompositeSink(
        [
            WebhookSink('https://hooks.example/coins', opener=first),
            NtfySink('coins', opener=second),
        ],
    )

    sink.send(MESSAGE)

    assert len(first.requests) == 1
    assert len(second.requests) == 1


def test_a_composite_sink_tries_every_sink_then_reports_all_failures() -> None:
    first = _Opener(error=OSError('first down'))
    second = _Opener(error=OSError('second down'))
    sink = CompositeSink(
        [
            WebhookSink('https://hooks.example/coins', opener=first),
            NtfySink('coins', opener=second),
        ],
    )

    assert sink.name == 'webhook+ntfy'
    with pytest.raises(NotificationError) as raised:
        sink.send(MESSAGE)

    assert str(raised.value) == 'webhook delivery failed: OSError; ntfy delivery failed: OSError'
    assert len(first.requests) == 1
    assert len(second.requests) == 1


def test_build_notifier_uses_both_configured_sinks() -> None:
    config = replace(
        RunConfig.from_environment({}),
        webhook_url='https://hooks.example/coins',
        ntfy_topic='coins',
        ntfy_server='https://ntfy.example',
    )

    notifier = build_notifier(config)

    assert isinstance(notifier, CompositeSink)
    assert notifier.name == 'webhook+ntfy'


@pytest.mark.parametrize(
    ('webhook', 'topic'),
    [(None, None), ('', ''), (None, 'coins'), ('https://hooks.example/coins', None)],
)
def test_build_notifier_only_builds_what_is_configured(webhook: str | None, topic: str | None) -> None:
    config = replace(RunConfig.from_environment({}), webhook_url=webhook, ntfy_topic=topic)
    notifier = build_notifier(config)
    if webhook and topic:
        assert isinstance(notifier, CompositeSink)
    elif webhook:
        assert isinstance(notifier, WebhookSink)
    elif topic:
        assert isinstance(notifier, NtfySink)
    else:
        assert notifier is None


@pytest.mark.parametrize(
    ('factory', 'message'),
    [
        (lambda: WebhookSink(' '), 'webhook URL'),
        (lambda: WebhookSink('https://example', timeout=0), 'timeout must be positive'),
        (lambda: NtfySink(' '), 'ntfy topic'),
        (lambda: NtfySink('coins', server=' '), 'ntfy server'),
        (lambda: NtfySink('coins', timeout=-1), 'timeout must be positive'),
        (lambda: CompositeSink([]), 'at least one sink'),
    ],
)
def test_sink_configuration_is_validated(factory: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()
