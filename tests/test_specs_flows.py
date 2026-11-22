"""Flow state machines: parsing, completeness and fixture integrity (issue #10).

`specs/flows.yaml` is the contract the quiz (#25), surprise-item (#26) and
notification (#28) executors are written from, so the tests here are about the
document being implementable: every flow has an entry, transitions, a popup
list, an exit and timings; every state names OCR labels and a reviewed
full-frame fixture; and every transition points at a state that exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / 'specs' / 'flows.yaml'
FIXTURES = ROOT / 'tests' / 'data' / 'flows'

FLOW_KEYS = ('surprise_items', 'coins_quiz', 'notifications')
FLOW_FIELDS = ('key', 'title', 'entry', 'states', 'transitions', 'popups', 'exit', 'timings')
STATE_FIELDS = ('key', 'identify', 'control', 'fixture')
FRAME_SIZE = (1200, 2000)


@pytest.fixture(scope='module')
def spec() -> dict[str, Any]:
    loaded = yaml.safe_load(SPEC_PATH.read_text(encoding='utf-8'))
    assert isinstance(loaded, dict)
    return loaded


def _flows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    flows = spec['flows']
    assert isinstance(flows, list)
    return flows


def _progress_fixtures(flow: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for state in flow['states']:
        paths.extend(str(value) for value in state.get('progress_fixtures', {}).values())
    return paths


def test_spec_parses_with_the_reference_geometry(spec: dict[str, Any]) -> None:
    assert spec['version'] == 1
    reference = spec['reference']
    assert (reference['width'], reference['height']) == FRAME_SIZE
    assert reference['device'] == 'T3_US'
    assert reference['captured'], 'a flow spec without a capture date cannot be audited'


def test_the_three_measured_flows_are_present(spec: dict[str, Any]) -> None:
    assert tuple(flow['key'] for flow in _flows(spec)) == FLOW_KEYS


def test_every_flow_has_the_fields_an_executor_needs(spec: dict[str, Any]) -> None:
    for flow in _flows(spec):
        missing = [field for field in FLOW_FIELDS if field not in flow]
        assert not missing, f'{flow.get("key")} is missing {missing}'
        assert flow['states'], f'{flow["key"]} has no states'
        assert flow['transitions'], f'{flow["key"]} has no transitions'
        assert flow['entry'], f'{flow["key"]} has no entry'
        assert flow['exit'], f'{flow["key"]} has no exit'
        assert flow['timings'], f'{flow["key"]} has no timings'


def test_every_state_names_labels_and_a_control(spec: dict[str, Any]) -> None:
    for flow in _flows(spec):
        for state in flow['states']:
            missing = [field for field in STATE_FIELDS if field not in state]
            assert not missing, f'{flow["key"]}/{state.get("key")} is missing {missing}'
            assert state['identify'], f'{flow["key"]}/{state["key"]} has no identifying labels'
            assert all(isinstance(label, str) and label.strip() for label in state['identify'])
            assert state['control'], f'{flow["key"]}/{state["key"]} has no control'


def test_every_state_fixture_is_a_reviewed_full_frame(spec: dict[str, Any]) -> None:
    """A captured state must have a frame; an uncaptured one must say why.

    Passing `fixture: null` is only allowed for a state that is explicitly not
    captured (`unobserved`/`prior`), and it must carry the reason. That keeps
    the acceptance rule honest: a state the spec asks an executor to implement
    is proved by a reviewed full frame, and a gap is a documented gap rather
    than a silent one.
    """
    for flow in _flows(spec):
        for state in flow['states']:
            path = state.get('fixture')
            if path is None:
                assert state.get('fixture_absent_reason'), f'{flow["key"]}/{state["key"]} has no fixture and no reason'
                assert state.get('status') != 'reproduced', (
                    f'{flow["key"]}/{state["key"]} is reproduced but has no fixture'
                )
                continue
            fixture = ROOT / path
            assert fixture.is_file(), f'{path} is referenced but absent'
            with Image.open(fixture) as image:
                assert image.size == FRAME_SIZE, f'{path} is {image.size}, not a full frame'
        for path in _progress_fixtures(flow):
            fixture = ROOT / path
            assert fixture.is_file(), f'{path} is referenced but absent'
            with Image.open(fixture) as image:
                assert image.size == FRAME_SIZE, f'{path} is {image.size}, not a full frame'


def test_transitions_reference_states_that_exist(spec: dict[str, Any]) -> None:
    for flow in _flows(spec):
        states = {state['key'] for state in flow['states']}
        for transition in flow['transitions']:
            assert transition['from'] in states, f'{flow["key"]}: unknown from {transition["from"]}'
            assert transition['to'] in states, f'{flow["key"]}: unknown to {transition["to"]}'
            assert transition['action'], f'{flow["key"]}: a transition with no action'
            assert transition['verify'], f'{flow["key"]}: a transition with no verification'


def test_surprise_counter_rule_records_the_evidence_that_proves_it(spec: dict[str, Any]) -> None:
    flow = next(flow for flow in _flows(spec) if flow['key'] == 'surprise_items')
    evidence = flow['counter_rule']['evidence']
    counters = [step['counter_after_back'] for step in evidence]
    assert counters == ['1/3', '1/3', '1/3', '2/3', '3/3']
    assert len(evidence) == 5
