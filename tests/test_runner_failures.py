"""The failure taxonomy is the contract: every class has a retry and a sentence (#32)."""

from __future__ import annotations

import pytest

from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.perception.errors import SnapshotError, StateTimeout
from aliexpress_coins.runner.errors import (
    AppLoggedOut,
    AppNotInstalled,
    DeviceLocked,
    DeviceSleeping,
    DeviceUnavailable,
    RunInterrupted,
)
from aliexpress_coins.runner.failures import (
    FAILURE_TABLE,
    FailureCode,
    FailureStage,
    RetryScope,
    classify_exception,
    failure,
    retry_delay,
    task_failure,
)


@pytest.mark.parametrize('code', list(FailureCode))
def test_every_class_declares_a_stage_a_retry_and_a_human_sentence(code: FailureCode) -> None:
    spec = FAILURE_TABLE[code]

    assert spec.code is code
    assert isinstance(spec.stage, FailureStage)
    assert isinstance(spec.retry, RetryScope)
    assert spec.sentence
    assert spec.sentence[0].isupper()
    if spec.retry is RetryScope.NONE:
        assert spec.retries == 0
    else:
        assert spec.retries == 1


def test_the_table_is_the_only_source_of_the_report_sentence() -> None:
    named = failure(FailureCode.APP_LOGGED_OUT, detail='sign-in prompt')

    assert named.code is FailureCode.APP_LOGGED_OUT
    assert named.stage is FailureStage.ACCOUNT
    assert named.retry is RetryScope.NONE
    assert named.abort_run is True
    assert named.retryable is False
    assert named.describe().startswith('app_logged_out: ')
    assert 'sign-in prompt' in named.describe()
    assert named.as_dict() == {
        'code': 'app_logged_out',
        'stage': 'account',
        'retry': 'none',
        'retries': 0,
        'retryable': False,
        'abort_run': True,
        'sentence': FAILURE_TABLE[FailureCode.APP_LOGGED_OUT].sentence,
        'detail': 'sign-in prompt',
    }


def test_the_retry_table_spells_out_the_three_behaviours() -> None:
    stage = {code for code, spec in FAILURE_TABLE.items() if spec.retry is RetryScope.STAGE}
    task = {code for code, spec in FAILURE_TABLE.items() if spec.retry is RetryScope.TASK}
    abort = {code for code, spec in FAILURE_TABLE.items() if spec.abort_run}

    assert stage == {
        FailureCode.DEVICE_OFFLINE,
        FailureCode.DEVICE_SLEEPING,
        FailureCode.NAVIGATION_FAILED,
    }
    assert task == {FailureCode.TASK_UNVERIFIED}
    assert abort == {
        FailureCode.DEVICE_OFFLINE,
        FailureCode.DEVICE_SLEEPING,
        FailureCode.DEVICE_LOCKED,
        FailureCode.APP_NOT_INSTALLED,
        FailureCode.APP_LOGGED_OUT,
        FailureCode.POPUP_UNCLOSABLE,
        FailureCode.INTERNAL_ERROR,
    }
    assert FAILURE_TABLE[FailureCode.NAVIGATION_FAILED].abort_run is False
    assert FAILURE_TABLE[FailureCode.BUDGET_EXHAUSTED].retry is RetryScope.NONE
    assert FAILURE_TABLE[FailureCode.BUDGET_EXHAUSTED].abort_run is False


def test_the_backoff_is_exponential() -> None:
    assert retry_delay(0) == 1.0
    assert retry_delay(1) == 2.0
    assert retry_delay(2) == 4.0
    with pytest.raises(ValueError, match='negative'):
        retry_delay(-1)


@pytest.mark.parametrize(
    ('exc', 'code'),
    [
        (DeviceUnavailable('gone'), FailureCode.DEVICE_OFFLINE),
        (DeviceSleeping('asleep'), FailureCode.DEVICE_SLEEPING),
        (DeviceLocked('locked'), FailureCode.DEVICE_LOCKED),
        (AppNotInstalled('absent'), FailureCode.APP_NOT_INSTALLED),
        (AppLoggedOut('signed out'), FailureCode.APP_LOGGED_OUT),
        (StateTimeout('route'), FailureCode.NAVIGATION_FAILED),
        (PopupUnclosable('popup'), FailureCode.POPUP_UNCLOSABLE),
        (SnapshotError('screencap'), FailureCode.DEVICE_OFFLINE),
        (RuntimeError('bug'), FailureCode.INTERNAL_ERROR),
    ],
)
def test_every_layer_error_is_translated_once(exc: Exception, code: FailureCode) -> None:
    named = classify_exception(exc)

    assert named is not None
    assert named.code is code
    assert type(exc).__name__ in named.detail


def test_a_stop_is_never_rewritten_as_a_failure() -> None:
    assert classify_exception(RunInterrupted('SIGTERM')) is None


def test_a_task_generic_error_defaults_to_unverified_and_transport_keeps_its_class() -> None:
    generic = classify_exception(RuntimeError('executor bug'), default=FailureCode.TASK_UNVERIFIED)
    transport = task_failure(SnapshotError('screencap failed'))

    assert generic is not None
    assert generic.code is FailureCode.TASK_UNVERIFIED
    assert transport.code is FailureCode.DEVICE_OFFLINE
    assert task_failure('no evidence').code is FailureCode.TASK_UNVERIFIED
    with pytest.raises(RunInterrupted):
        task_failure(RunInterrupted('stop'))
