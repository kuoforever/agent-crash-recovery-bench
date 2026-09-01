from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

SCHEMA_FILES = {
    "manifest": "evidence-manifest.schema.json",
    "raw": "dify-prefork-child-loss-raw.schema.json",
    "report": "dify-prefork-child-loss-report.schema.json",
}
SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(?:authorization|proxy[_-]?authorization|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|session[_-]?token|id[_-]?token|token|password|passwd|cookie|"
    r"set[_-]?cookie|secret|secrets|client[_-]?secret|credential|credentials|private[_-]?key)$"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"id[_-]?token|token|password|passwd|secret|client[_-]?secret|credential)="
    r"([^&\s]+)"
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
LOCAL_ABSOLUTE_PATH_RE = re.compile(
    r"""(?i)(?<![A-Za-z0-9._-])(?:[a-z]:[\\/]|\\\\|/(?:home|users|root|tmp|var/tmp|mnt/[a-z])/)"""
)
REDACTED = "[REDACTED]"
REDACTED_LOCAL_PATH = "[REDACTED_LOCAL_PATH]"


class EvidenceError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"top-level JSON value must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise EvidenceError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_schema(kind: Literal["manifest", "raw", "report"]) -> dict[str, Any]:
    resource = files("guarded_loop").joinpath("schemas", SCHEMA_FILES[kind])
    value = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvidenceError(f"schema must be a JSON object: {SCHEMA_FILES[kind]}")
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        raise EvidenceError(f"invalid tracked schema {SCHEMA_FILES[kind]}: {exc.message}") from exc
    return value


def _schema_errors(kind: Literal["manifest", "raw", "report"], value: object) -> list[str]:
    validator = Draft202012Validator(_load_schema(kind), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    rendered: list[str] = []
    for error in errors:
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        rendered.append(f"{location}: {error.message}")
    return rendered


def _has_unsafe_assignment(value: str) -> bool:
    return any(match.group(2) != REDACTED for match in ASSIGNMENT_SECRET_RE.finditer(value))


def _sanitize_string(value: str) -> str:
    if LOCAL_ABSOLUTE_PATH_RE.search(value):
        return REDACTED_LOCAL_PATH
    value = BEARER_RE.sub(f"Bearer {REDACTED}", value)
    value = JWT_RE.sub(REDACTED, value)
    value = OPENAI_KEY_RE.sub(REDACTED, value)
    return ASSIGNMENT_SECRET_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", value)


def sanitize_json(value: Any, *, key: str | None = None) -> Any:
    if key is not None and SENSITIVE_KEY_RE.fullmatch(key):
        return None if value is None else REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_json(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


def audit_json(value: Any, *, path: str = "$") -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY_RE.fullmatch(str(key)) and item is not None and item != REDACTED:
                findings.append({"path": child_path, "kind": "sensitive_field"})
            findings.extend(audit_json(item, path=child_path))
        return findings
    if isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(audit_json(item, path=f"{path}[{index}]"))
        return findings
    if not isinstance(value, str):
        return findings
    if LOCAL_ABSOLUTE_PATH_RE.search(value):
        findings.append({"path": path, "kind": "local_absolute_path"})
    if BEARER_RE.search(value):
        findings.append({"path": path, "kind": "bearer_credential"})
    if JWT_RE.search(value):
        findings.append({"path": path, "kind": "jwt_credential"})
    if OPENAI_KEY_RE.search(value):
        findings.append({"path": path, "kind": "api_credential"})
    if _has_unsafe_assignment(value):
        findings.append({"path": path, "kind": "credential_assignment"})
    return findings


def sanitize_file(source: Path, destination: Path, *, overwrite: bool = False) -> dict[str, Any]:
    if source.resolve() == destination.resolve():
        raise EvidenceError("source and destination must differ; in-place sanitization is disabled")
    sanitized = sanitize_json(load_json_object(source))
    if not isinstance(sanitized, dict):  # pragma: no cover - object loader guarantees this
        raise EvidenceError("sanitized payload is not an object")
    findings = audit_json(sanitized)
    if findings:
        raise EvidenceError(f"sanitizer left {len(findings)} unsafe value(s)")
    _atomic_write_json(destination, sanitized, overwrite=overwrite)
    return sanitized


def _safe_repo_path(root: Path, reference: object, *, field: str) -> tuple[str, Path]:
    if not isinstance(reference, str) or not reference:
        raise EvidenceError(f"{field} must be a non-empty repository-relative path")
    if "\\" in reference or re.match(r"^[A-Za-z]:", reference):
        raise EvidenceError(f"{field} must use a repository-relative POSIX path: {reference!r}")
    pure = PurePosixPath(reference)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise EvidenceError(f"unsafe {field}: {reference!r}")
    normalized = pure.as_posix()
    if normalized != reference:
        raise EvidenceError(f"non-canonical {field}: {reference!r}")
    candidate = (root / Path(*pure.parts)).resolve()
    if not candidate.is_relative_to(root):
        raise EvidenceError(f"{field} escapes verification root: {reference!r}")
    return normalized, candidate


def _contained_input(root: Path, value: Path, *, field: str) -> tuple[str, Path]:
    candidate = value.resolve() if value.is_absolute() else (root / value).resolve()
    if not candidate.is_relative_to(root):
        raise EvidenceError(f"{field} is outside verification root: {value}")
    return candidate.relative_to(root).as_posix(), candidate


def _check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if passed else "fail", "detail": detail}


def verify_bundle(*, root: Path, report_path: Path) -> dict[str, Any]:
    root = root.resolve()
    report_reference, report_file = _contained_input(root, report_path, field="report")
    report = load_json_object(report_file)
    report_schema_errors = _schema_errors("report", report)

    raw_reference, raw_file = _safe_repo_path(
        root, report.get("raw_snapshot"), field="raw_snapshot"
    )
    manifest_reference, manifest_file = _safe_repo_path(
        root, report.get("source_artifact_manifest"), field="source_artifact_manifest"
    )
    raw = load_json_object(raw_file)
    manifest = load_json_object(manifest_file)
    raw_schema_errors = _schema_errors("raw", raw)
    manifest_schema_errors = _schema_errors("manifest", manifest)

    raw_hash = sha256_file(raw_file)
    manifest_hash = sha256_file(manifest_file)
    provenance = raw.get("provenance")
    raw_provenance = provenance if isinstance(provenance, dict) else {}
    raw_valid_attempt = raw.get("valid_attempt")
    raw_attempt = raw_valid_attempt if isinstance(raw_valid_attempt, dict) else {}

    checks = [
        _check(
            "report_schema",
            not report_schema_errors,
            "valid" if not report_schema_errors else "; ".join(report_schema_errors),
        ),
        _check(
            "raw_schema",
            not raw_schema_errors,
            "valid" if not raw_schema_errors else "; ".join(raw_schema_errors),
        ),
        _check(
            "manifest_schema",
            not manifest_schema_errors,
            "valid" if not manifest_schema_errors else "; ".join(manifest_schema_errors),
        ),
        _check(
            "report_raw_sha256",
            report.get("raw_snapshot_sha256") == raw_hash,
            f"declared={report.get('raw_snapshot_sha256')} actual={raw_hash}",
        ),
        _check(
            "report_manifest_sha256",
            report.get("source_artifact_manifest_sha256") == manifest_hash,
            f"declared={report.get('source_artifact_manifest_sha256')} actual={manifest_hash}",
        ),
        _check(
            "raw_manifest_reference",
            raw_provenance.get("source_artifact_manifest") == manifest_reference,
            f"raw={raw_provenance.get('source_artifact_manifest')} report={manifest_reference}",
        ),
        _check(
            "raw_manifest_sha256",
            raw_provenance.get("source_artifact_manifest_sha256") == manifest_hash,
            f"declared={raw_provenance.get('source_artifact_manifest_sha256')} actual={manifest_hash}",
        ),
        _check(
            "classification_linkage",
            report.get("classification") == raw_attempt.get("classification"),
            f"report={report.get('classification')} raw={raw_attempt.get('classification')}",
        ),
    ]

    audit_findings = {
        "report": audit_json(report),
        "raw": audit_json(raw),
        "manifest": audit_json(manifest),
    }
    for kind, findings in audit_findings.items():
        checks.append(
            _check(
                f"{kind}_sanitization",
                not findings,
                "no unsafe values" if not findings else json.dumps(findings, sort_keys=True),
            )
        )

    artifacts_value = manifest.get("artifacts")
    artifacts = artifacts_value if isinstance(artifacts_value, list) else []
    policy_value = manifest.get("availability_policy")
    policy = policy_value if isinstance(policy_value, dict) else {}
    tracked_paths_value = policy.get("tracked_paths")
    local_prefixes_value = policy.get("local_only_prefixes")
    tracked_paths: set[str] = (
        {item for item in tracked_paths_value if isinstance(item, str)}
        if isinstance(tracked_paths_value, list)
        else set()
    )
    local_prefixes: tuple[str, ...] = (
        tuple(item for item in local_prefixes_value if isinstance(item, str))
        if isinstance(local_prefixes_value, list)
        else ()
    )
    overlapping_prefixes = sorted(
        {
            tuple(sorted((left, right)))
            for index, left in enumerate(local_prefixes)
            for right in local_prefixes[index + 1 :]
            if left.startswith(right) or right.startswith(left)
        }
    )
    checks.append(
        _check(
            "availability_policy_non_overlapping",
            not overlapping_prefixes,
            "no overlapping local-only prefixes"
            if not overlapping_prefixes
            else f"overlaps={overlapping_prefixes}",
        )
    )
    declared_count = manifest.get("artifact_count")
    checks.append(
        _check(
            "artifact_count",
            type(declared_count) is int and declared_count == len(artifacts),
            f"declared={declared_count} actual={len(artifacts)}",
        )
    )

    artifact_results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    matched_tracked_paths: set[str] = set()
    matched_local_prefixes: set[str] = set()
    computed_availability = {"tracked": 0, "local_only": 0}
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            artifact_results.append(
                {"index": index, "path": None, "availability": None, "status": "invalid"}
            )
            continue
        reference = item.get("path")
        availability: str | None = None
        try:
            normalized, artifact_file = _safe_repo_path(
                root, reference, field=f"artifacts[{index}].path"
            )
        except EvidenceError as exc:
            artifact_results.append(
                {
                    "index": index,
                    "path": reference,
                    "availability": availability,
                    "status": "unsafe_path",
                    "detail": str(exc),
                }
            )
            continue
        is_tracked = normalized in tracked_paths
        matching_local_prefixes = tuple(
            prefix for prefix in local_prefixes if normalized.startswith(prefix)
        )
        if len(matching_local_prefixes) > 1:
            artifact_results.append(
                {
                    "index": index,
                    "path": normalized,
                    "availability": None,
                    "status": "invalid",
                    "detail": f"path matches multiple local-only prefixes: {matching_local_prefixes}",
                }
            )
            continue
        is_local_only = len(matching_local_prefixes) == 1
        if is_tracked == is_local_only:
            artifact_results.append(
                {
                    "index": index,
                    "path": normalized,
                    "availability": None,
                    "status": "invalid",
                    "detail": "path must match exactly one availability policy",
                }
            )
            continue
        availability = "tracked" if is_tracked else "local_only"
        computed_availability[availability] += 1
        if is_tracked:
            matched_tracked_paths.add(normalized)
        else:
            matched_local_prefixes.update(matching_local_prefixes)
        if normalized in seen_paths:
            artifact_results.append(
                {
                    "index": index,
                    "path": normalized,
                    "availability": availability,
                    "status": "invalid",
                    "detail": "duplicate manifest path",
                }
            )
            continue
        seen_paths.add(normalized)
        if not artifact_file.is_file():
            status = "unavailable" if availability == "local_only" else "missing"
            artifact_results.append(
                {
                    "index": index,
                    "path": normalized,
                    "availability": availability,
                    "status": status,
                    "detail": (
                        "local-only source is not present in this clone"
                        if status == "unavailable"
                        else "required tracked source is missing"
                    ),
                }
            )
            continue
        actual_hash = sha256_file(artifact_file)
        actual_bytes = artifact_file.stat().st_size
        declared_hash = item.get("sha256")
        declared_bytes = item.get("bytes")
        matches = declared_hash == actual_hash and declared_bytes == actual_bytes
        artifact_results.append(
            {
                "index": index,
                "path": normalized,
                "availability": availability,
                "status": "verified" if matches else "mismatch",
                "declared_sha256": declared_hash,
                "actual_sha256": actual_hash,
                "declared_bytes": declared_bytes,
                "actual_bytes": actual_bytes,
            }
        )

    declared_availability = manifest.get("availability_counts")
    checks.append(
        _check(
            "availability_policy_coverage",
            tracked_paths == matched_tracked_paths
            and set(local_prefixes) == matched_local_prefixes,
            f"tracked={sorted(matched_tracked_paths)} local_prefixes={sorted(matched_local_prefixes)}",
        )
    )
    checks.append(
        _check(
            "availability_counts",
            declared_availability == computed_availability,
            f"declared={declared_availability} actual={computed_availability}",
        )
    )
    failed_artifact_statuses = {"invalid", "unsafe_path", "missing", "mismatch"}
    failures = sum(check["status"] == "fail" for check in checks) + sum(
        item["status"] in failed_artifact_statuses for item in artifact_results
    )
    unavailable = sum(item["status"] == "unavailable" for item in artifact_results)
    verified = sum(item["status"] == "verified" for item in artifact_results)
    status = "failed" if failures else "partial" if unavailable else "verified"
    return {
        "schema_version": 1,
        "status": status,
        "complete_source_verification": status == "verified",
        "bundle": {
            "report": report_reference,
            "raw": raw_reference,
            "manifest": manifest_reference,
        },
        "checks": checks,
        "artifact_summary": {
            "declared": len(artifacts),
            "verified": verified,
            "unavailable": unavailable,
            "failed": sum(item["status"] in failed_artifact_statuses for item in artifact_results),
        },
        "artifacts": artifact_results,
        "limitations": (
            [
                "Local-only sources absent from this clone are unavailable, not verified; "
                "the manifest preserves their declared hash and size but cannot substitute for them."
            ]
            if unavailable
            else []
        ),
    }


def _sanitize_command(args: argparse.Namespace) -> int:
    try:
        sanitize_file(args.input, args.output, overwrite=args.overwrite)
    except (EvidenceError, OSError) as exc:
        print(f"[sanitize_failed] {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "sanitized", "output": args.output.as_posix()}, sort_keys=True))
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    try:
        result = verify_bundle(root=args.root, report_path=args.report)
    except (EvidenceError, OSError) as exc:
        print(f"[verification_invalid] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    unavailable = result["artifact_summary"]["unavailable"]
    if args.expect_unavailable is not None and unavailable != args.expect_unavailable:
        print(
            f"[unexpected_unavailable_count] expected={args.expect_unavailable} actual={unavailable}",
            file=sys.stderr,
        )
        return 1
    if result["status"] == "failed":
        return 1
    if result["status"] == "partial" and not args.allow_unavailable:
        return 3
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sanitize and verify tracked evidence bundles.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sanitize_parser = subparsers.add_parser(
        "sanitize", help="Redact secrets and local paths in JSON"
    )
    sanitize_parser.add_argument("--input", type=Path, required=True)
    sanitize_parser.add_argument("--output", type=Path, required=True)
    sanitize_parser.add_argument("--overwrite", action="store_true")
    sanitize_parser.set_defaults(handler=_sanitize_command)

    verify_parser = subparsers.add_parser("verify", help="Validate schema, linkage, and artifacts")
    verify_parser.add_argument("--root", type=Path, default=Path("."))
    verify_parser.add_argument("--report", type=Path, required=True)
    verify_parser.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="Return zero for a partial bundle while retaining status=partial in output",
    )
    verify_parser.add_argument("--expect-unavailable", type=int)
    verify_parser.set_defaults(handler=_verify_command)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
