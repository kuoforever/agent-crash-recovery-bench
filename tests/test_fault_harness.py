from __future__ import annotations

import json
from pathlib import Path

import pytest

from guarded_loop import fault_harness

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECOVERED_FIXTURE = PROJECT_ROOT / "guarded_loop" / "fixtures" / "recovered.json"


class SpyReplayAdapter(fault_harness.ReplayAdapter):
    def __init__(self, captures: tuple[fault_harness.Observation, ...]) -> None:
        super().__init__(captures)
        self.injected = False
        self.release_requests: list[fault_harness.ReleaseRequest] = []

    def inject_child_loss(
        self, target: fault_harness.ProcessIdentity
    ) -> fault_harness.FaultReceipt:
        self.injected = True
        return super().inject_child_loss(target)

    def release(self, request: fault_harness.ReleaseRequest) -> fault_harness.ReleaseReceipt:
        self.release_requests.append(request)
        return super().release(request)


class RaisingFaultAdapter(SpyReplayAdapter):
    def inject_child_loss(
        self, target: fault_harness.ProcessIdentity
    ) -> fault_harness.FaultReceipt:
        del target
        raise RuntimeError("fixture fault outcome is unknown")


class MismatchedReleaseAdapter(SpyReplayAdapter):
    def release(self, request: fault_harness.ReleaseRequest) -> fault_harness.ReleaseReceipt:
        self.release_requests.append(request)
        wrong_request = request.model_copy(update={"task_id": "wrong-task"})
        return fault_harness.ReleaseReceipt(request=wrong_request, created=True)


def load_fixture() -> fault_harness.ReplayScenario:
    return fault_harness.ReplayScenario.model_validate_json(
        RECOVERED_FIXTURE.read_text(encoding="utf-8")
    )


def capture_budget_value() -> dict[str, object]:
    value = load_fixture().model_dump(mode="json")
    value["spec"]["recovery_observation_limit"] = 1
    observation = value["captures"][3]
    observation["task"]["redelivered"] = False
    observation["broker"]["redelivered"] = False
    observation["effect_attempts"] = [1]
    return value


def test_offline_replay_archives_gate_before_fault_and_release(tmp_path: Path) -> None:
    scenario = load_fixture()
    adapter = SpyReplayAdapter(scenario.captures)
    transcript = fault_harness.TranscriptWriter(tmp_path / "transcript")

    result = fault_harness.PreforkChildLossHarness(
        spec=scenario.spec,
        adapter=adapter,
        transcript=transcript,
        execution_mode="offline_replay",
    ).run()

    assert result.classification == "duplicate_recovered"
    assert result.fault_status == "applied"
    assert result.release_status == "created"
    assert adapter.injected is True
    assert [request.reason for request in adapter.release_requests] == [
        "release_after_archived_recovery_gate"
    ]
    assert adapter.release_requests[0].run_id == scenario.spec.run_id
    assert adapter.release_requests[0].task_id == scenario.spec.task_id
    assert adapter.release_requests[0].delivery_tag == "delivery-fixture-001"
    names = {path.name for path in transcript.root.iterdir()}
    assert {
        "preflight.json",
        "blocked.json",
        "pre-fault.json",
        "fault-receipt.json",
        "post-fault-0000.json",
        "release-receipt.json",
        "final-0000.json",
        "result.json",
    } <= names
    assert result.records_archived == len(names)


def test_changed_child_tuple_fails_closed_without_fault(tmp_path: Path) -> None:
    value = load_fixture().model_dump(mode="json")
    value["captures"][2]["children"][0]["pid"] = 999
    value["captures"][2]["task"]["worker_pid"] = 999
    scenario = fault_harness.ReplayScenario.model_validate(value)
    adapter = SpyReplayAdapter(scenario.captures)

    result = fault_harness.PreforkChildLossHarness(
        spec=scenario.spec,
        adapter=adapter,
        transcript=fault_harness.TranscriptWriter(tmp_path / "transcript"),
        execution_mode="offline_replay",
    ).run()

    assert result.classification == "cleanup_authority_unknown"
    assert result.fault_status == "not_applied"
    assert result.release_status == "not_requested"
    assert adapter.injected is False
    assert adapter.release_requests == []
    assert "child_tuple_changed_during_immediate_recapture" in result.gate_failures
    assert "cleanup_not_attempted_without_valid_identity_gate" in result.gate_failures


def test_recovery_requires_explicit_task_and_broker_redelivery(tmp_path: Path) -> None:
    value = capture_budget_value()
    scenario = fault_harness.ReplayScenario.model_validate(value)
    adapter = SpyReplayAdapter(scenario.captures)

    result = fault_harness.PreforkChildLossHarness(
        spec=scenario.spec,
        adapter=adapter,
        transcript=fault_harness.TranscriptWriter(tmp_path / "transcript"),
        execution_mode="offline_replay",
    ).run()

    assert result.classification == "redelivery_not_observed_within_capture_budget"
    assert result.fault_status == "applied"
    assert result.release_status == "created"
    assert [request.reason for request in adapter.release_requests] == [
        "cleanup_after_capture_budget_exhausted"
    ]
    assert "broker_redelivery_not_explicit" in result.gate_failures


@pytest.mark.parametrize(
    ("case", "expected_failure"),
    [
        ("stale_task", "cleanup_task_binding_changed"),
        ("cleared_task_and_broker", "cleanup_exact_active_task_missing"),
        ("broker_counts", "cleanup_broker_counts_not_exact"),
        ("second_effect", "cleanup_effect_attempts_not_exact"),
        ("release_present", "cleanup_release_unexpectedly_present"),
    ],
)
def test_capture_budget_cleanup_requires_fresh_exact_authority(
    tmp_path: Path, case: str, expected_failure: str
) -> None:
    value = capture_budget_value()
    observation = value["captures"][3]
    if case == "stale_task":
        observation["task"]["task_id"] = "stale-task"
    elif case == "cleared_task_and_broker":
        observation["task"] = None
        observation["broker"] = {
            "queue_depth": 0,
            "unacked_count": 0,
            "unacked_index_count": 0,
            "task_id": None,
            "delivery_tag": None,
            "redelivered": None,
        }
    elif case == "broker_counts":
        observation["broker"]["unacked_count"] = 0
    elif case == "second_effect":
        observation["effect_attempts"] = [1, 2]
    elif case == "release_present":
        observation["release_present"] = True
    else:  # pragma: no cover - parameter table is exhaustive
        raise AssertionError(case)
    scenario = fault_harness.ReplayScenario.model_validate(value)
    adapter = SpyReplayAdapter(scenario.captures)

    result = fault_harness.PreforkChildLossHarness(
        spec=scenario.spec,
        adapter=adapter,
        transcript=fault_harness.TranscriptWriter(tmp_path / "transcript"),
        execution_mode="offline_replay",
    ).run()

    assert result.classification == "cleanup_authority_unknown"
    assert result.fault_status == "applied"
    assert result.release_status == "not_requested"
    assert adapter.release_requests == []
    assert expected_failure in result.gate_failures


def test_broker_rejects_empty_non_null_identity() -> None:
    with pytest.raises(ValueError):
        fault_harness.BrokerState(
            queue_depth=0,
            unacked_count=1,
            unacked_index_count=1,
            task_id="",
            delivery_tag="",
            redelivered=False,
        )


def test_fault_adapter_exception_is_unknown_not_not_applied(tmp_path: Path) -> None:
    scenario = load_fixture()
    adapter = RaisingFaultAdapter(scenario.captures)

    result = fault_harness.PreforkChildLossHarness(
        spec=scenario.spec,
        adapter=adapter,
        transcript=fault_harness.TranscriptWriter(tmp_path / "transcript"),
        execution_mode="offline_replay",
    ).run()

    assert result.classification == "fault_outcome_uncertain"
    assert result.fault_status == "unknown"
    assert result.release_status == "not_requested"
    assert adapter.release_requests == []


def test_mismatched_release_receipt_is_authority_unknown(tmp_path: Path) -> None:
    scenario = load_fixture()
    adapter = MismatchedReleaseAdapter(scenario.captures)

    result = fault_harness.PreforkChildLossHarness(
        spec=scenario.spec,
        adapter=adapter,
        transcript=fault_harness.TranscriptWriter(tmp_path / "transcript"),
        execution_mode="offline_replay",
    ).run()

    assert result.classification == "cleanup_authority_unknown"
    assert result.fault_status == "applied"
    assert result.release_status == "unknown"
    assert "release_receipt_binding_mismatch" in result.gate_failures


def test_transcript_writer_refuses_nonempty_directory(tmp_path: Path) -> None:
    output = tmp_path / "occupied"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="must not already exist"):
        fault_harness.TranscriptWriter(output)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_replay_cli_uses_only_fixture_observations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "cli-transcript"
    assert fault_harness.main(["--scenario", str(RECOVERED_FIXTURE), "--out", str(output)]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["execution_mode"] == "offline_replay"
    assert rendered["classification"] == "duplicate_recovered"
    assert json.loads((output / "result.json").read_text(encoding="utf-8")) == rendered
