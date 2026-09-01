from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from guarded_loop import evidence_tooling

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT = Path("evidence/dify-prefork-child-loss-report.json")
RAW = Path("evidence/dify-prefork-child-loss-raw.json")
MANIFEST = Path("evidence/dify-prefork-child-loss-manifest.json")
TRACKED_SOURCE = Path("guarded_loop/dify_sink.py")


def copy_public_bundle(root: Path, *, include_tracked_source: bool = True) -> None:
    for relative in (REPORT, RAW, MANIFEST):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    if include_tracked_source:
        destination = root / TRACKED_SOURCE
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / TRACKED_SOURCE, destination)


def test_public_clone_reports_local_sources_unavailable(tmp_path: Path) -> None:
    copy_public_bundle(tmp_path)

    result = evidence_tooling.verify_bundle(root=tmp_path, report_path=REPORT)

    assert result["status"] == "partial"
    assert result["complete_source_verification"] is False
    assert result["artifact_summary"] == {
        "declared": 34,
        "verified": 1,
        "unavailable": 33,
        "failed": 0,
    }
    local_items = [item for item in result["artifacts"] if item["availability"] == "local_only"]
    assert len(local_items) == 33
    assert {item["status"] for item in local_items} == {"unavailable"}
    assert result["limitations"]


def test_partial_cli_requires_explicit_allow_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copy_public_bundle(tmp_path)
    base_args = ["verify", "--root", str(tmp_path), "--report", REPORT.as_posix()]

    assert evidence_tooling.main(base_args) == 3
    capsys.readouterr()
    assert (
        evidence_tooling.main(base_args + ["--allow-unavailable", "--expect-unavailable", "33"])
        == 0
    )
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["status"] == "partial"
    assert rendered["complete_source_verification"] is False


def test_missing_tracked_source_is_failure_not_unavailable(tmp_path: Path) -> None:
    copy_public_bundle(tmp_path, include_tracked_source=False)

    result = evidence_tooling.verify_bundle(root=tmp_path, report_path=REPORT)

    assert result["status"] == "failed"
    assert result["artifact_summary"]["unavailable"] == 33
    tracked = [item for item in result["artifacts"] if item["availability"] == "tracked"]
    assert len(tracked) == 1
    assert tracked[0]["status"] == "missing"


def test_tampered_raw_breaks_report_linkage(tmp_path: Path) -> None:
    copy_public_bundle(tmp_path)
    raw_path = tmp_path / RAW
    value = json.loads(raw_path.read_text(encoding="utf-8"))
    value["valid_attempt"]["classification"] = "tampered"
    raw_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    result = evidence_tooling.verify_bundle(root=tmp_path, report_path=REPORT)

    assert result["status"] == "failed"
    failed_checks = {item["name"] for item in result["checks"] if item["status"] == "fail"}
    assert {"report_raw_sha256", "classification_linkage"} <= failed_checks


def test_manifest_path_traversal_is_rejected_without_escape(tmp_path: Path) -> None:
    copy_public_bundle(tmp_path)
    manifest_path = tmp_path / MANIFEST
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["artifacts"][1]["path"] = "../outside.json"
    manifest_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    result = evidence_tooling.verify_bundle(root=tmp_path, report_path=REPORT)

    assert result["status"] == "failed"
    unsafe = [item for item in result["artifacts"] if item["status"] == "unsafe_path"]
    assert len(unsafe) == 1
    assert unsafe[0]["path"] == "../outside.json"


def test_schema_rejects_missing_critical_report_field(tmp_path: Path) -> None:
    copy_public_bundle(tmp_path)
    report_path = tmp_path / REPORT
    value = json.loads(report_path.read_text(encoding="utf-8"))
    del value["classification"]
    report_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    result = evidence_tooling.verify_bundle(root=tmp_path, report_path=REPORT)

    report_check = next(item for item in result["checks"] if item["name"] == "report_schema")
    assert report_check["status"] == "fail"
    assert "classification" in report_check["detail"]


def test_overlapping_local_only_prefixes_are_rejected(tmp_path: Path) -> None:
    copy_public_bundle(tmp_path)
    manifest_path = tmp_path / MANIFEST
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["availability_policy"]["local_only_prefixes"].append("_dify_bench/published-20260828/")
    manifest_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    result = evidence_tooling.verify_bundle(root=tmp_path, report_path=REPORT)

    assert result["status"] == "failed"
    overlap_check = next(
        item for item in result["checks"] if item["name"] == "availability_policy_non_overlapping"
    )
    assert overlap_check["status"] == "fail"
    assert any(
        item["status"] == "invalid" and "multiple local-only prefixes" in item["detail"]
        for item in result["artifacts"]
    )


def test_sanitizer_redacts_sensitive_fields_assignments_and_local_paths(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.json"
    destination = tmp_path / "safe.json"
    payload = {
        "authorization": "Bearer " + "fixture-credential-value",
        "api_key": "sk-" + "fixturecredentialvalue",
        "password": 123456,
        "access_token": False,
        "refresh_token": None,
        "local_path": "C:\\Users\\fixture-user\\capture.json",
        "windows_argument": "--output=C:\\Users\\fixture-user\\capture.json",
        "posix_argument": "path:/home/fixture-user/capture.json",
        "url": "https://example.invalid/callback?token=" + "fixture-token-value",
        "credential_url": "https://example.invalid/callback?session_token=abc",
        "short_bearer": "Bearer abc",
        "sha256": "a" * 64,
    }
    source.write_text(json.dumps(payload), encoding="utf-8")

    sanitized = evidence_tooling.sanitize_file(source, destination)

    assert sanitized["authorization"] == evidence_tooling.REDACTED
    assert sanitized["api_key"] == evidence_tooling.REDACTED
    assert sanitized["password"] == evidence_tooling.REDACTED
    assert sanitized["access_token"] == evidence_tooling.REDACTED
    assert sanitized["refresh_token"] is None
    assert sanitized["local_path"] == evidence_tooling.REDACTED_LOCAL_PATH
    assert sanitized["windows_argument"] == evidence_tooling.REDACTED_LOCAL_PATH
    assert sanitized["posix_argument"] == evidence_tooling.REDACTED_LOCAL_PATH
    assert sanitized["url"].endswith("token=[REDACTED]")
    assert sanitized["credential_url"].endswith("session_token=[REDACTED]")
    assert sanitized["short_bearer"] == "Bearer [REDACTED]"
    assert sanitized["sha256"] == "a" * 64
    assert evidence_tooling.audit_json(sanitized) == []


def test_audit_flags_non_string_sensitive_values() -> None:
    findings = evidence_tooling.audit_json(
        {"password": 123456, "access_token": False, "refresh_token": None}
    )

    assert {item["path"] for item in findings} == {"$.password", "$.access_token"}
    assert {item["kind"] for item in findings} == {"sensitive_field"}


def test_audit_rejects_redaction_marker_smuggling_and_secret_key() -> None:
    findings = evidence_tooling.audit_json(
        {
            "password": "real-value [REDACTED]",
            "secret": "plain-value",
            "note": "Bearer fixture-credential-value [REDACTED]",
            "mixed": "token=[REDACTED]&password=fixture-secret-value",
            "safe": {"password": evidence_tooling.REDACTED},
            "safe_assignment": "token=[REDACTED]",
        }
    )

    assert {(item["path"], item["kind"]) for item in findings} == {
        ("$.password", "sensitive_field"),
        ("$.secret", "sensitive_field"),
        ("$.note", "bearer_credential"),
        ("$.mixed", "credential_assignment"),
    }

    sanitized = evidence_tooling.sanitize_json(
        {"secret": "plain-value", "credential": 1234, "session_token": False}
    )
    assert sanitized == {
        "secret": evidence_tooling.REDACTED,
        "credential": evidence_tooling.REDACTED,
        "session_token": evidence_tooling.REDACTED,
    }
    assert evidence_tooling.audit_json(sanitized) == []


def test_sanitizer_refuses_in_place_and_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "output.json"
    source.write_text("{}", encoding="utf-8")
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(evidence_tooling.EvidenceError, match="in-place"):
        evidence_tooling.sanitize_file(source, source)
    with pytest.raises(evidence_tooling.EvidenceError, match="refusing to overwrite"):
        evidence_tooling.sanitize_file(source, output)
    assert output.read_text(encoding="utf-8") == "keep"


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"same": 1, "same": 2}', encoding="utf-8")

    with pytest.raises(evidence_tooling.EvidenceError, match="duplicate JSON key"):
        evidence_tooling.load_json_object(duplicate)
