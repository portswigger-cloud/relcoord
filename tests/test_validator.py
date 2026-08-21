# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from relcoord.validator import (
    HttpTreeValidator,
    ValidationError,
    compute_digest,
    read_tree,
    tar_gz,
)

# The digest manifest-validator computes for this tree, from its own
# trees.compute_digest. relcoord has to produce the same bytes or every
# validation is a 400, so the expected value is written out rather than derived.
TREE = {"api.yaml": b"kind: Service\n", "web/deployment.yaml": b"kind: Deployment\n"}
TREE_DIGEST = "sha256:ff1a271a3be808b4f657e31e19a6414f75a3468682da84972136a2e635cefa12"

VERDICT = {
    "passed": False,
    "digest": TREE_DIGEST,
    "cached": False,
    "verdicts": [
        {
            "passed": False,
            "tool": "kics",
            "tool_version": "v2.1.16",
            "ruleset_digest": "sha256:abc",
            "findings": [
                {
                    "rule_id": "RBAC-1",
                    "severity": "high",
                    "file": "api.yaml",
                    "resource": "Service/api",
                    "message": "wildcard rule",
                    "similarity_id": "ff7c",
                    "accepted": None,
                },
                {
                    "rule_id": "RBAC-2",
                    "severity": "low",
                    "file": "api.yaml",
                    "resource": None,
                    "message": "tolerated",
                    "similarity_id": None,
                    "accepted": "that is the mechanism",
                },
            ],
        }
    ],
}


def _sse(*events: tuple[str, str]) -> bytes:
    return "".join(
        f"event: {event}\ndata: {data}\n\n" for event, data in events
    ).encode()


def _validator(handler) -> HttpTreeValidator:
    transport = httpx.MockTransport(handler)
    return HttpTreeValidator(
        url="http://validator", client=httpx.Client(transport=transport)
    )


def test_compute_digest_matches_the_manifest_validator_contract() -> None:
    assert compute_digest(TREE) == TREE_DIGEST


def test_compute_digest_frames_paths_so_a_split_cannot_collide() -> None:
    assert compute_digest({"ab": b"c"}) != compute_digest({"a": b"bc"})


def test_compute_digest_ignores_the_order_files_were_collected_in() -> None:
    assert compute_digest(dict(reversed(list(TREE.items())))) == TREE_DIGEST


def test_read_tree_reads_the_generated_files_by_relative_path(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "api.yaml").write_bytes(b"kind: Service\n")
    (tmp_path / "web" / "deployment.yaml").write_bytes(b"kind: Deployment\n")

    assert read_tree(tmp_path) == TREE


def test_read_tree_leaves_out_the_git_directory_of_a_manifests_checkout(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "abc").write_bytes(b"packed")
    (tmp_path / "api.yaml").write_bytes(b"kind: Service\n")

    assert read_tree(tmp_path) == {"api.yaml": b"kind: Service\n"}


def test_tar_gz_packs_the_tree_the_validator_reads_back() -> None:
    unpacked: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tar_gz(TREE)))) as archive:
        for member in archive:
            content = archive.extractfile(member)
            assert content is not None
            unpacked[member.name] = content.read()

    assert unpacked == TREE


def test_validate_sends_the_tree_the_digest_and_the_named_checks() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=_sse(("result", '{"passed": true, "digest": "sha256:x"}'))
        )

    validation = _validator(handler).validate(TREE, ["structural", "kics"])

    assert validation.passed
    request = requests[0]
    assert request.url.path == "/v1/validate"
    assert request.url.params.get("digest") == TREE_DIGEST
    assert request.url.params.get_list("check") == ["structural", "kics"]
    assert request.headers["content-type"] == "application/gzip"
    assert request.headers["accept"] == "text/event-stream"
    assert gzip.decompress(request.content)


def test_validate_names_no_checks_when_the_output_configured_none() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=_sse(("result", '{"passed": true, "digest": "sha256:x"}'))
        )

    _validator(handler).validate(TREE)

    assert "check=" not in str(requests[0].url)


def test_validate_reports_the_verdict_and_its_findings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(("result", json.dumps(VERDICT))))

    validation = _validator(handler).validate(TREE, ["kics"])

    assert not validation.passed
    assert validation.digest == TREE_DIGEST
    assert [verdict.tool_version for verdict in validation.verdicts] == ["v2.1.16"]
    assert [finding.rule_id for finding in validation.failing_findings] == ["RBAC-1"]
    assert validation.accepted_count == 1


def test_validate_forwards_the_validators_phases_as_progress() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                ("validate", '{"message": "2 files, checks: kics"}'),
                ("running", '{"message": "kics: 2 files"}'),
                ("result", '{"passed": true, "digest": "sha256:x"}'),
            ),
        )

    events: list[tuple[str, str]] = []
    _validator(handler).validate(
        TREE, progress=lambda phase, message: events.append((phase, message))
    )

    assert events == [
        ("validate", "2 files, checks: kics"),
        ("running", "kics: 2 files"),
    ]


def test_validate_raises_when_the_validator_reports_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse(("error", '{"message": "digest mismatch"}'))
        )

    with pytest.raises(ValidationError, match="digest mismatch"):
        _validator(handler).validate(TREE)


def test_validate_raises_when_the_stream_ends_without_a_verdict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(("running", '{"message": "kics"}')))

    with pytest.raises(ValidationError, match="without a verdict"):
        _validator(handler).validate(TREE)


def test_validate_raises_on_an_error_response_and_reports_its_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "unknown check: dev"})

    with pytest.raises(ValidationError, match="answered 400: unknown check: dev"):
        _validator(handler).validate(TREE)


def test_validate_raises_when_the_validator_cannot_be_reached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ValidationError, match="failed to reach manifest-validator"):
        _validator(handler).validate(TREE)


def test_validate_raises_when_a_verdict_has_no_passed_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(("result", '{"digest": "sha256:x"}')))

    with pytest.raises(ValidationError, match="without a passed flag"):
        _validator(handler).validate(TREE)
