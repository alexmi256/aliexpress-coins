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
    AppriseSink,
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


def test_apprise_posts_one_plain_text_message_to_the_keyed_endpoint() -> None:
    opener = _Opener()
    sink = AppriseSink('https://apprise.example/', key='coins/nightly', tag='ops', opener=opener)

    sink.send(MESSAGE)

    request = opener.requests[0]
    assert request.full_url == 'https://apprise.example/notify/coins%2Fnightly'
    assert request.get_method() == 'POST'
    assert json.loads(request.data) == {
        'title': MESSAGE.title,
        'body': MESSAGE.body,
        'type': 'failure',
        'format': 'text',
        'tag': 'ops',
    }
    assert request.get_header('Content-type') == 'application/json; charset=utf-8'
    assert opener.timeouts == [10.0]


def test_apprise_without_a_key_posts_to_the_stateless_notify_endpoint() -> None:
    opener = _Opener()
    sink = AppriseSink('https://apprise.example', opener=opener)

    sink.send(Notification(title='done', body='ok', run_id='run', status='completed'))

    assert sink.endpoint == 'https://apprise.example/notify'
    assert json.loads(opener.requests[0].data)['type'] == 'success'


@pytest.mark.parametrize(
    ('url', 'key', 'expected'),
    [
        ('https://apprise.example/notify', None, 'https://apprise.example/notify'),
        ('https://apprise.example/notify', 'coins', 'https://apprise.example/notify/coins'),
        ('https://apprise.example/notify/coins', None, 'https://apprise.example/notify/coins'),
        ('https://apprise.example/notify/coins', 'stale', 'https://apprise.example/notify/coins'),
        ('https://apprise.example/notify?x=1', 'coins', 'https://apprise.example/notify/coins?x=1'),
        ('https://apprise.example?x=1', 'coins', 'https://apprise.example/notify/coins?x=1'),
        (
            'https://apprise.example/base/?token=1#status',
            'coins',
            'https://apprise.example/base/notify/coins?token=1#status',
        ),
    ],
)
def test_apprise_endpoint_keeps_an_explicit_path_and_query_intact(url: str, key: str | None, expected: str) -> None:
    assert AppriseSink(url, key=key).endpoint == expected


@pytest.mark.parametrize(
    ('status', 'expected'),
    [('completed', 'success'), ('budget', 'success'), ('stopped', 'warning'), ('failed', 'failure'), ('other', 'info')],
)
def test_apprise_maps_the_run_status_to_its_message_type(status: str, expected: str) -> None:
    opener = _Opener()
    AppriseSink('https://apprise.example', opener=opener).send(
        Notification(title='run', body='body', run_id='run', status=status),
    )

    assert json.loads(opener.requests[0].data)['type'] == expected


def test_an_apprise_delivery_failure_keeps_the_class_and_status_not_the_url() -> None:
    secret = 'do-not-record-this'
    error = HTTPError(
        f'https://apprise.example/notify/{secret}',
        502,
        f'Bad Gateway {secret}',
        HTTPMessage(),
        None,
    )

    with pytest.raises(NotificationError, match=r'apprise delivery failed: HTTPError \(HTTP 502\)') as raised:
        AppriseSink('https://apprise.example', opener=_Opener(error=error)).send(MESSAGE)

    assert secret not in str(raised.value)


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


def test_build_notifier_uses_apprise_with_its_key_and_tag() -> None:
    config = replace(
        RunConfig.from_environment({}),
        apprise_url='https://apprise.example',
        apprise_key='coins',
        apprise_tag='ops',
    )

    notifier = build_notifier(config)

    assert isinstance(notifier, AppriseSink)
    assert notifier.endpoint == 'https://apprise.example/notify/coins'
    assert notifier.tag == 'ops'


@pytest.mark.parametrize(
    ('apprise', 'webhook', 'topic'),
    [
        (None, None, None),
        ('', '', ''),
        ('https://apprise.example', None, None),
        (None, 'https://hooks.example/coins', None),
        (None, None, 'coins'),
        ('https://apprise.example', 'https://hooks.example/coins', 'coins'),
    ],
)
def test_build_notifier_only_builds_what_is_configured(
    apprise: str | None,
    webhook: str | None,
    topic: str | None,
) -> None:
    config = replace(
        RunConfig.from_environment({}),
        apprise_url=apprise,
        webhook_url=webhook,
        ntfy_topic=topic,
    )
    notifier = build_notifier(config)
    configured = sum(bool(value) for value in (apprise, webhook, topic))
    if configured == 0:
        assert notifier is None
    elif configured == 1:
        assert notifier is not None
        assert not isinstance(notifier, CompositeSink)
    else:
        assert isinstance(notifier, CompositeSink)


@pytest.mark.parametrize(
    ('factory', 'message'),
    [
        (lambda: AppriseSink(' '), 'Apprise URL'),
        (lambda: AppriseSink('https://apprise.example', key=' '), 'Apprise key'),
        (lambda: AppriseSink('https://apprise.example', timeout=0), 'timeout must be positive'),
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
