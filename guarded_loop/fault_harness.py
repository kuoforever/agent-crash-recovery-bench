from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]
NonEmptyStr = Annotated[StrictStr, Field(min_length=1)]
Classification = Literal[
    "duplicate_recovered",
    "redelivery_observed_final_unresolved",
    "redelivery_not_observed_within_capture_budget",
    "invalid_no_fault",
    "cleanup_authority_unknown",
    "fault_outcome_uncertain",
    "fault_injected_observation_failed",
]
FaultStatus = Literal["not_applied", "applied", "unknown"]
ReleaseStatus = Literal["not_requested", "created", "not_created", "unknown"]


class StrictEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcessIdentity(StrictEvidenceModel):
    pid: PositiveInt
    start_marker: StrictStr = Field(min_length=1)


class ExecutorIdentity(StrictEvidenceModel):
    executor_id: StrictStr = Field(min_length=1)
    start_marker: StrictStr = Field(min_length=1)
    restart_count: NonNegativeInt


class TaskState(StrictEvidenceModel):
    run_id: StrictStr = Field(min_length=1)
    task_id: StrictStr = Field(min_length=1)
    delivery_tag: StrictStr = Field(min_length=1)
    worker_pid: PositiveInt
    active: StrictBool
    acknowledged: StrictBool
    redelivered: StrictBool


class BrokerState(StrictEvidenceModel):
    queue_depth: NonNegativeInt
    unacked_count: NonNegativeInt
    unacked_index_count: NonNegativeInt
    task_id: NonEmptyStr | None = None
    delivery_tag: NonEmptyStr | None = None
    redelivered: StrictBool | None = None


class Observation(StrictEvidenceModel):
    captured_at_utc: StrictStr = Field(min_length=1)
    executor: ExecutorIdentity
    parent: ProcessIdentity
    children: tuple[ProcessIdentity, ...]
    task: TaskState | None
    broker: BrokerState
    effect_attempts: tuple[PositiveInt, ...]
    release_present: StrictBool
    final_status: StrictStr | None = None


class HarnessSpec(StrictEvidenceModel):
    run_id: StrictStr = Field(min_length=1)
    task_id: StrictStr = Field(min_length=1)
    recovery_observation_limit: PositiveInt = 3
    final_observation_limit: PositiveInt = 2
    successful_final_status: StrictStr = "succeeded"


class FaultReceipt(StrictEvidenceModel):
    action: Literal["child_loss"]
    target: ProcessIdentity
    applied: StrictBool
    executor_affected: StrictBool
    controller_affected: StrictBool


class ReleaseRequest(StrictEvidenceModel):
    run_id: StrictStr = Field(min_length=1)
    task_id: StrictStr = Field(min_length=1)
    delivery_tag: StrictStr = Field(min_length=1)
    reason: StrictStr = Field(min_length=1)


class ReleaseReceipt(StrictEvidenceModel):
    request: ReleaseRequest
    created: StrictBool


class HarnessAdapter(Protocol):
    def capture(self, label: str) -> Observation: ...

    def inject_child_loss(self, target: ProcessIdentity) -> FaultReceipt: ...

    def release(self, request: ReleaseRequest) -> ReleaseReceipt: ...


class HarnessResult(StrictEvidenceModel):
    schema_version: Literal[1] = 1
    execution_mode: Literal["offline_replay", "external_adapter"]
    classification: Classification
    fault_status: FaultStatus
    release_status: ReleaseStatus
    target: ProcessIdentity | None
    gate_failures: tuple[StrictStr, ...]
    records_archived: NonNegativeInt


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite transcript record: {path.name}")
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class TranscriptWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        if root.exists():
            raise ValueError(f"transcript directory must not already exist: {root}")
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir()
        self.count = 0

    def archive_model(self, name: str, value: BaseModel) -> None:
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError(f"unsafe transcript filename: {name!r}")
        _atomic_write_json(self.root / name, value.model_dump(mode="json"))
        self.count += 1


def _same_runtime(left: Observation, right: Observation) -> bool:
    return left.executor == right.executor and left.parent == right.parent


def _one_child(observation: Observation) -> ProcessIdentity | None:
    return observation.children[0] if len(observation.children) == 1 else None


def _preflight_failures(observation: Observation) -> list[str]:
    failures: list[str] = []
    if _one_child(observation) is None:
        failures.append("preflight_requires_one_child")
    if observation.task is not None:
        failures.append("preflight_task_must_be_absent")
    broker = observation.broker
    if (broker.queue_depth, broker.unacked_count, broker.unacked_index_count) != (0, 0, 0):
        failures.append("preflight_broker_must_be_clear")
    if any((broker.task_id, broker.delivery_tag, broker.redelivered)):
        failures.append("preflight_broker_identity_must_be_absent")
    if observation.effect_attempts:
        failures.append("preflight_effects_must_be_empty")
    if observation.release_present:
        failures.append("preflight_release_must_be_absent")
    return failures


def _blocked_failures(
    spec: HarnessSpec, baseline: Observation, observation: Observation
) -> list[str]:
    failures: list[str] = []
    child = _one_child(observation)
    if not _same_runtime(baseline, observation):
        failures.append("executor_or_parent_changed")
    if child is None or child != _one_child(baseline):
        failures.append("unique_child_identity_changed")
    task = observation.task
    if task is None:
        failures.append("exact_task_missing")
    else:
        if (task.run_id, task.task_id) != (spec.run_id, spec.task_id):
            failures.append("exact_task_identity_mismatch")
        if not task.active or task.acknowledged or task.redelivered:
            failures.append("initial_task_delivery_state_mismatch")
        if child is None or task.worker_pid != child.pid:
            failures.append("task_not_bound_to_unique_child")
    broker = observation.broker
    if (broker.queue_depth, broker.unacked_count, broker.unacked_index_count) != (0, 1, 1):
        failures.append("initial_broker_counts_mismatch")
    if task is not None and (broker.task_id, broker.delivery_tag) != (
        task.task_id,
        task.delivery_tag,
    ):
        failures.append("initial_broker_delivery_identity_mismatch")
    if broker.redelivered not in (None, False):
        failures.append("initial_broker_delivery_already_redelivered")
    if observation.effect_attempts != (1,):
        failures.append("attempt_1_not_exclusively_archived")
    if observation.release_present:
        failures.append("release_present_before_fault")
    return failures


def _immediate_failures(blocked: Observation, immediate: Observation) -> list[str]:
    failures: list[str] = []
    if not _same_runtime(blocked, immediate):
        failures.append("runtime_changed_during_immediate_recapture")
    if immediate.children != blocked.children:
        failures.append("child_tuple_changed_during_immediate_recapture")
    if immediate.task != blocked.task:
        failures.append("task_changed_during_immediate_recapture")
    if immediate.broker != blocked.broker:
        failures.append("broker_changed_during_immediate_recapture")
    if immediate.effect_attempts != (1,) or immediate.release_present:
        failures.append("effect_or_release_changed_during_immediate_recapture")
    return failures


def _recovery_failures(
    baseline: Observation,
    blocked: Observation,
    target: ProcessIdentity,
    observation: Observation,
) -> list[str]:
    failures: list[str] = []
    replacement = _one_child(observation)
    if not _same_runtime(baseline, observation):
        failures.append("executor_or_parent_not_continuous")
    if replacement is None or replacement == target or target in observation.children:
        failures.append("replacement_child_not_uniquely_observed")
    task = observation.task
    initial_task = blocked.task
    if task is None or initial_task is None:
        failures.append("redelivered_task_missing")
    else:
        if (task.run_id, task.task_id, task.delivery_tag) != (
            initial_task.run_id,
            initial_task.task_id,
            initial_task.delivery_tag,
        ):
            failures.append("redelivered_task_identity_mismatch")
        if not task.active or task.acknowledged or not task.redelivered:
            failures.append("redelivered_task_state_mismatch")
        if replacement is None or task.worker_pid != replacement.pid:
            failures.append("redelivered_task_not_bound_to_replacement")
    broker = observation.broker
    if initial_task is not None and (broker.task_id, broker.delivery_tag) != (
        initial_task.task_id,
        initial_task.delivery_tag,
    ):
        failures.append("redelivered_broker_identity_mismatch")
    if (broker.queue_depth, broker.unacked_count, broker.unacked_index_count) != (0, 1, 1):
        failures.append("redelivered_broker_counts_mismatch")
    if broker.redelivered is not True:
        failures.append("broker_redelivery_not_explicit")
    if observation.effect_attempts != (1, 2):
        failures.append("attempt_2_not_archived")
    if observation.release_present:
        failures.append("release_present_before_recovery_gate")
    return failures


def _final_failures(
    spec: HarnessSpec, baseline: Observation, observation: Observation
) -> list[str]:
    failures: list[str] = []
    if not _same_runtime(baseline, observation):
        failures.append("runtime_not_continuous_at_final")
    if observation.task is not None:
        failures.append("task_still_active_at_final")
    broker = observation.broker
    if (broker.queue_depth, broker.unacked_count, broker.unacked_index_count) != (0, 0, 0):
        failures.append("broker_not_clear_at_final")
    if any((broker.task_id, broker.delivery_tag, broker.redelivered)):
        failures.append("broker_identity_not_clear_at_final")
    if observation.effect_attempts != (1, 2):
        failures.append("final_effect_attempts_mismatch")
    if not observation.release_present:
        failures.append("release_not_present_at_final")
    if observation.final_status != spec.successful_final_status:
        failures.append("final_status_mismatch")
    return failures


def _capture_budget_cleanup_failures(
    spec: HarnessSpec,
    baseline: Observation,
    blocked: Observation,
    target: ProcessIdentity,
    observation: Observation,
) -> list[str]:
    failures: list[str] = []
    initial_task = blocked.task
    if initial_task is None:
        return ["cleanup_initial_binding_missing"]
    if not _same_runtime(baseline, observation):
        failures.append("cleanup_runtime_identity_changed")
    child = _one_child(observation)
    if child is None or child == target or target in observation.children:
        failures.append("cleanup_replacement_child_not_current")
    task = observation.task
    if task is None:
        failures.append("cleanup_exact_active_task_missing")
    else:
        if (task.run_id, task.task_id, task.delivery_tag) != (
            spec.run_id,
            spec.task_id,
            initial_task.delivery_tag,
        ):
            failures.append("cleanup_task_binding_changed")
        if not task.active or task.acknowledged or task.redelivered:
            failures.append("cleanup_task_state_not_initial_unacked")
        if child is None or task.worker_pid != child.pid:
            failures.append("cleanup_task_not_bound_to_current_child")
    broker = observation.broker
    if (broker.task_id, broker.delivery_tag) != (spec.task_id, initial_task.delivery_tag):
        failures.append("cleanup_broker_binding_changed")
    if (broker.queue_depth, broker.unacked_count, broker.unacked_index_count) != (0, 1, 1):
        failures.append("cleanup_broker_counts_not_exact")
    if broker.redelivered not in (None, False):
        failures.append("cleanup_broker_already_redelivered")
    if observation.effect_attempts != (1,):
        failures.append("cleanup_effect_attempts_not_exact")
    if observation.release_present:
        failures.append("cleanup_release_unexpectedly_present")
    return failures


class PreforkChildLossHarness:
    """Fail-closed orchestration core with no platform-specific command adapter.

    Captures are archived before any fault or release action. A real adapter must
    be supplied out of tree; the checked-in replay adapter below never touches a
    process, container, broker, or service.
    """

    def __init__(
        self,
        *,
        spec: HarnessSpec,
        adapter: HarnessAdapter,
        transcript: TranscriptWriter,
        execution_mode: Literal["offline_replay", "external_adapter"],
    ) -> None:
        self.spec = spec
        self.adapter = adapter
        self.transcript = transcript
        self.execution_mode = execution_mode

    def _finish(
        self,
        classification: Classification,
        *,
        fault_status: FaultStatus,
        release_status: ReleaseStatus,
        target: ProcessIdentity | None,
        failures: Sequence[str] = (),
    ) -> HarnessResult:
        result = HarnessResult(
            execution_mode=self.execution_mode,
            classification=classification,
            fault_status=fault_status,
            release_status=release_status,
            target=target,
            gate_failures=tuple(failures),
            records_archived=self.transcript.count + 1,
        )
        self.transcript.archive_model("result.json", result)
        return result

    def _release(
        self, observation: Observation, reason: str
    ) -> tuple[ReleaseStatus, ReleaseReceipt | None, str | None]:
        task = observation.task
        if task is None or (task.run_id, task.task_id) != (self.spec.run_id, self.spec.task_id):
            return "unknown", None, "release_binding_unavailable"
        request = ReleaseRequest(
            run_id=self.spec.run_id,
            task_id=self.spec.task_id,
            delivery_tag=task.delivery_tag,
            reason=reason,
        )
        try:
            receipt = self.adapter.release(request)
        except Exception as exc:
            return "unknown", None, f"release_adapter_failed:{type(exc).__name__}"
        if receipt.request != request:
            return "unknown", receipt, "release_receipt_binding_mismatch"
        return ("created" if receipt.created else "not_created"), receipt, None

    def _invalid_gate_result(
        self,
        observation: Observation,
        *,
        target: ProcessIdentity | None,
        failures: Sequence[str],
    ) -> HarnessResult:
        cleanup_needed = bool(observation.effect_attempts) and not observation.release_present
        classification: Classification = (
            "cleanup_authority_unknown" if cleanup_needed else "invalid_no_fault"
        )
        rendered_failures = list(failures)
        if cleanup_needed:
            rendered_failures.append("cleanup_not_attempted_without_valid_identity_gate")
        return self._finish(
            classification,
            fault_status="not_applied",
            release_status="not_requested",
            target=target,
            failures=rendered_failures,
        )

    def run(self) -> HarnessResult:
        self.transcript.archive_model("spec.json", self.spec)
        try:
            preflight = self.adapter.capture("preflight")
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            return self._finish(
                "invalid_no_fault",
                fault_status="not_applied",
                release_status="not_requested",
                target=None,
                failures=(f"preflight_capture_failed:{type(exc).__name__}",),
            )
        self.transcript.archive_model("preflight.json", preflight)
        failures = _preflight_failures(preflight)
        if failures:
            return self._finish(
                "invalid_no_fault",
                fault_status="not_applied",
                release_status="not_requested",
                target=None,
                failures=failures,
            )

        try:
            blocked = self.adapter.capture("blocked")
        except Exception as exc:
            return self._finish(
                "cleanup_authority_unknown",
                fault_status="not_applied",
                release_status="not_requested",
                target=None,
                failures=(
                    f"blocked_capture_failed:{type(exc).__name__}",
                    "cleanup_not_attempted_without_valid_identity_gate",
                ),
            )
        self.transcript.archive_model("blocked.json", blocked)
        failures = _blocked_failures(self.spec, preflight, blocked)
        if failures:
            return self._invalid_gate_result(
                blocked,
                target=_one_child(blocked),
                failures=failures,
            )

        try:
            immediate = self.adapter.capture("pre-fault")
        except Exception as exc:
            return self._invalid_gate_result(
                blocked,
                target=_one_child(blocked),
                failures=(f"pre_fault_capture_failed:{type(exc).__name__}",),
            )
        self.transcript.archive_model("pre-fault.json", immediate)
        failures = _immediate_failures(blocked, immediate)
        target = _one_child(immediate)
        if failures or target is None:
            return self._invalid_gate_result(
                immediate,
                target=target,
                failures=failures or ("pre_fault_target_missing",),
            )

        try:
            receipt = self.adapter.inject_child_loss(target)
        except Exception as exc:
            return self._finish(
                "fault_outcome_uncertain",
                fault_status="unknown",
                release_status="not_requested",
                target=target,
                failures=(f"fault_adapter_failed:{type(exc).__name__}",),
            )
        self.transcript.archive_model("fault-receipt.json", receipt)
        if (
            not receipt.applied
            and receipt.target == target
            and not receipt.executor_affected
            and not receipt.controller_affected
        ):
            release_status, release_receipt, release_failure = self._release(
                blocked, "cleanup_after_confirmed_fault_not_applied"
            )
            if release_receipt is not None:
                self.transcript.archive_model("release-receipt.json", release_receipt)
            receipt_failures = ["fault_adapter_reported_not_applied"]
            if release_failure:
                receipt_failures.append(release_failure)
            return self._finish(
                "cleanup_authority_unknown" if release_failure else "invalid_no_fault",
                fault_status="not_applied",
                release_status=release_status,
                target=target,
                failures=receipt_failures,
            )
        if receipt.target != target or receipt.executor_affected or receipt.controller_affected:
            return self._finish(
                "fault_outcome_uncertain",
                fault_status="applied" if receipt.applied else "unknown",
                release_status="not_requested",
                target=target,
                failures=("fault_receipt_not_exact_child_only",),
            )

        latest_failures: list[str] = ["recovery_gate_not_observed"]
        for sequence in range(self.spec.recovery_observation_limit):
            try:
                observation = self.adapter.capture(f"post-fault-{sequence:04d}")
            except Exception as exc:
                return self._finish(
                    "fault_injected_observation_failed",
                    fault_status="applied",
                    release_status="not_requested",
                    target=target,
                    failures=(f"post_fault_capture_failed:{type(exc).__name__}",),
                )
            self.transcript.archive_model(f"post-fault-{sequence:04d}.json", observation)
            latest_failures = _recovery_failures(preflight, blocked, target, observation)
            if not latest_failures:
                release_status, release_receipt, release_failure = self._release(
                    observation, "release_after_archived_recovery_gate"
                )
                if release_receipt is not None:
                    self.transcript.archive_model("release-receipt.json", release_receipt)
                if release_failure:
                    return self._finish(
                        "cleanup_authority_unknown",
                        fault_status="applied",
                        release_status=release_status,
                        target=target,
                        failures=(release_failure,),
                    )
                if release_status != "created":
                    return self._finish(
                        "redelivery_observed_final_unresolved",
                        fault_status="applied",
                        release_status=release_status,
                        target=target,
                        failures=("release_not_created_after_recovery_gate",),
                    )
                for final_sequence in range(self.spec.final_observation_limit):
                    try:
                        final = self.adapter.capture(f"final-{final_sequence:04d}")
                    except Exception as exc:
                        return self._finish(
                            "redelivery_observed_final_unresolved",
                            fault_status="applied",
                            release_status="created",
                            target=target,
                            failures=(f"final_capture_failed:{type(exc).__name__}",),
                        )
                    self.transcript.archive_model(f"final-{final_sequence:04d}.json", final)
                    final_failures = _final_failures(self.spec, preflight, final)
                    if not final_failures:
                        return self._finish(
                            "duplicate_recovered",
                            fault_status="applied",
                            release_status="created",
                            target=target,
                        )
                    latest_failures = final_failures
                return self._finish(
                    "redelivery_observed_final_unresolved",
                    fault_status="applied",
                    release_status="created",
                    target=target,
                    failures=latest_failures,
                )

        cleanup_failures = _capture_budget_cleanup_failures(
            self.spec, preflight, blocked, target, observation
        )
        if cleanup_failures:
            return self._finish(
                "cleanup_authority_unknown",
                fault_status="applied",
                release_status="not_requested",
                target=target,
                failures=(*latest_failures, *cleanup_failures),
            )
        release_status, release_receipt, release_failure = self._release(
            observation, "cleanup_after_capture_budget_exhausted"
        )
        if release_receipt is not None:
            self.transcript.archive_model("release-receipt.json", release_receipt)
        if release_failure:
            return self._finish(
                "cleanup_authority_unknown",
                fault_status="applied",
                release_status=release_status,
                target=target,
                failures=(*latest_failures, release_failure),
            )
        return self._finish(
            "redelivery_not_observed_within_capture_budget",
            fault_status="applied",
            release_status=release_status,
            target=target,
            failures=latest_failures,
        )


class ReplayScenario(StrictEvidenceModel):
    schema_version: Literal[1]
    mode: Literal["offline_replay"]
    spec: HarnessSpec
    captures: tuple[Observation, ...]


class ReplayAdapter:
    def __init__(self, captures: Sequence[Observation]) -> None:
        self._captures = list(captures)
        self._position = 0

    def capture(self, label: str) -> Observation:
        del label
        if self._position >= len(self._captures):
            raise RuntimeError("offline replay exhausted its captures")
        observation = self._captures[self._position]
        self._position += 1
        return observation

    def inject_child_loss(self, target: ProcessIdentity) -> FaultReceipt:
        return FaultReceipt(
            action="child_loss",
            target=target,
            applied=True,
            executor_affected=False,
            controller_affected=False,
        )

    def release(self, request: ReleaseRequest) -> ReleaseReceipt:
        return ReleaseReceipt(request=request, created=True)


def _load_scenario(path: Path) -> ReplayScenario:
    value = json.loads(path.read_text(encoding="utf-8"))
    return ReplayScenario.model_validate(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the generic child-loss orchestration state machine offline."
    )
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        scenario = _load_scenario(args.scenario)
        transcript = TranscriptWriter(args.out)
        result = PreforkChildLossHarness(
            spec=scenario.spec,
            adapter=ReplayAdapter(scenario.captures),
            transcript=transcript,
            execution_mode="offline_replay",
        ).run()
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        print(f"[offline_replay_invalid] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True, ensure_ascii=True))
    return (
        0
        if result.classification
        in {"duplicate_recovered", "redelivery_not_observed_within_capture_budget"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
