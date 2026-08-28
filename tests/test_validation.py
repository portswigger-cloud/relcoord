# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""How a validated tree gates a change and reports on a diff."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from relcoord import change
from relcoord.change import (
    ChangeProcessor,
    ChangeProgress,
    DiffCommentProcessor,
    ManifestValidationError,
)
from relcoord.config import OutputSettings, RolloutSettings, RolloutStage
from relcoord.github import PostedComment
from relcoord.manifest_diff import ManifestDiff
from relcoord.validator import (
    Finding,
    Validation,
    ValidationError,
    ValidationProgress,
    Verdict,
    compute_digest,
)

# Validation on a change is commented out for now, so the tests covering that
# gate are skipped rather than deleted; see ChangeProcessor.process.
on_change_disabled = pytest.mark.skip(reason="validation on a change is disabled")

CONFIG_REPO = "https://github.com/acme/config.git"
MANIFESTS_REPO = "https://github.com/acme/manifests.git"

DEV = OutputSettings(
    name="acme-dev",
    repository=MANIFESTS_REPO,
    directory=Path("acme-dev"),
    checks=("structural", "kics-dev"),
)
PROD = OutputSettings(
    name="acme-prod",
    repository=MANIFESTS_REPO,
    directory=Path("acme-prod"),
    checks=("structural", "kics-prod"),
)

FINDING = Finding(
    rule_id="RBAC-1",
    severity="high",
    message="wildcard rule",
    file="acme-prod/rbac.yaml",
)
FAILED = Validation(
    passed=False,
    digest="sha256:bad",
    verdicts=(Verdict(passed=False, tool="kics", findings=(FINDING,)),),
)
PASSED = Validation(
    passed=True,
    digest="sha256:good",
    verdicts=(Verdict(passed=True, tool="kics"),),
)


@dataclass(frozen=True)
class Ref:
    kind: str = "Service"
    namespace: str | None = "acme"
    name: str = "api"
    api_version: str = "v1"


@dataclass(frozen=True)
class GenerationResult:
    written_paths: set[Path]
    created_or_modified: set[object]
    removed: set[object]
    deploy_id: str | None = None


class Validator:
    """Answers with a verdict per output, and records what it was asked."""

    def __init__(
        self,
        verdicts: Mapping[str, Validation | Exception] | None = None,
        default: Validation | Exception = PASSED,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, bytes]]] = []
        self._verdicts = dict(verdicts or {})
        self._default = default

    def validate(
        self,
        files: Mapping[str, bytes],
        checks: Sequence[str] = (),
        *,
        progress: ValidationProgress = lambda phase, message: None,
    ) -> Validation:
        self.calls.append((tuple(checks), dict(files)))
        progress("running", f"kics: {len(files)} files")
        answer = self._verdicts.get(_output_of(files), self._default)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _output_of(files: Mapping[str, bytes]) -> str:
    """Name the output a tree came from, which its manifests record."""
    for content in files.values():
        return content.decode().strip()
    return ""


class Commenter:
    def __init__(self) -> None:
        self.bodies: list[str] = []

    def post_comment(
        self, repo: str, pull_request: int, body: str, *, marker: str | None = None
    ) -> PostedComment:
        self.bodies.append(body)
        return PostedComment(url="https://github.com/acme/config/pull/7#c1")


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    pushed: list[str] | None = None,
) -> None:
    """Replace the git and manifest-builder work, writing real manifests.

    Generation has to leave files on disk here, because what gets validated is
    the output directory as it stands rather than what generate() reported.
    """

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True, exist_ok=True)

    def fake_generate(
        deploy_config: Path,
        output_path: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str | None,
        vars: dict[str, object] | None = None,
        target: str | None = None,
        plugins: object | None = None,
    ) -> GenerationResult:
        manifest = output_path / "api.yaml"
        manifest.write_text(f"{output_path.name}\n")
        return GenerationResult(
            written_paths={manifest},
            created_or_modified={Ref()},
            removed=set(),
            deploy_id="deploy-1",
        )

    def fake_push(repo_path: Path, remote: str, idcat) -> None:
        if pushed is not None:
            pushed.append(remote)

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    # A diff checks the source out rebased onto its default branch; a change does
    # not, and both paths are exercised here.
    monkeypatch.setattr(change, "_checkout_rebased_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", lambda repo_path: "feedface")
    monkeypatch.setattr(change, "_push_repository", fake_push)
    monkeypatch.setattr(
        change, "_manifests_diff", lambda checkout, base: ManifestDiff(stat="", diff="")
    )


@on_change_disabled
def test_a_change_validates_each_output_with_its_own_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed: list[str] = []
    _fake_git(monkeypatch, tmp_path, pushed=pushed)
    validator = Validator()

    ChangeProcessor(outputs=[DEV, PROD], validator=validator).process(
        CONFIG_REPO, "deadbeef", None
    )

    assert [checks for checks, _ in validator.calls] == [
        ("structural", "kics-dev"),
        ("structural", "kics-prod"),
    ]
    assert [tree for _, tree in validator.calls] == [
        {"api.yaml": b"acme-dev\n"},
        {"api.yaml": b"acme-prod\n"},
    ]
    assert pushed == [MANIFESTS_REPO]


@on_change_disabled
def test_a_change_does_not_push_an_output_that_failed_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed: list[str] = []
    _fake_git(monkeypatch, tmp_path, pushed=pushed)
    validator = Validator({"acme-prod": FAILED})

    with pytest.raises(ManifestValidationError) as raised:
        ChangeProcessor(outputs=[DEV, PROD], validator=validator).process(
            CONFIG_REPO, "deadbeef", None
        )

    assert pushed == []
    assert "acme-prod" in str(raised.value)
    assert "high RBAC-1 in acme-prod/rbac.yaml" in str(raised.value)


@on_change_disabled
def test_a_change_does_not_push_when_no_verdict_could_be_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed: list[str] = []
    _fake_git(monkeypatch, tmp_path, pushed=pushed)
    validator = Validator(default=ValidationError("connection refused"))

    with pytest.raises(ManifestValidationError, match="no verdict"):
        ChangeProcessor(outputs=[DEV], validator=validator).process(
            CONFIG_REPO, "deadbeef", None
        )

    assert pushed == []


@on_change_disabled
def test_a_rollout_stage_that_fails_validation_never_reaches_the_next_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed: list[str] = []
    _fake_git(monkeypatch, tmp_path, pushed=pushed)
    validator = Validator({"acme-dev": FAILED})
    rollout = RolloutSettings(
        name="linear",
        stages=(
            RolloutStage(outputs=("acme-dev",)),
            RolloutStage(outputs=("acme-prod",)),
        ),
    )

    with pytest.raises(ManifestValidationError, match="acme-dev"):
        ChangeProcessor(
            outputs=[DEV, PROD],
            rollouts=[rollout],
            validator=validator,
            detect_deployment=True,
            deployment_detector=type(
                "Detector", (), {"wait_for_success": lambda self, **kwargs: None}
            )(),
        ).process(CONFIG_REPO, "deadbeef", None)

    assert [checks for checks, _ in validator.calls] == [("structural", "kics-dev")]
    assert pushed == []


def test_a_change_pushes_unvalidated_when_no_validator_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed: list[str] = []
    _fake_git(monkeypatch, tmp_path, pushed=pushed)

    ChangeProcessor(outputs=[DEV]).process(CONFIG_REPO, "deadbeef", None)

    assert pushed == [MANIFESTS_REPO]


@on_change_disabled
def test_a_change_reports_the_validation_it_ran_as_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    events: list[ChangeProgress] = []

    ChangeProcessor(outputs=[DEV], validator=Validator()).process(
        CONFIG_REPO, "deadbeef", None, progress=events.append
    )

    phases = [event.phase for event in events]
    assert phases.index("validate") > phases.index("generated")
    assert phases.index("validated") < phases.index("push")
    by_phase = {event.phase: event for event in events}
    assert by_phase["validate"].message == (
        "validating acme-dev: 1 manifests, structural, kics-dev"
    )
    assert by_phase["running"].message == "acme-dev: kics: 1 files"
    assert by_phase["validated"].message == "acme-dev: passed"
    assert by_phase["validated"].detail["digest"] == "sha256:good"


@on_change_disabled
def test_a_change_validates_the_tree_the_validator_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree is the output directory, not the files the commit touched."""
    _fake_git(monkeypatch, tmp_path)
    validator = Validator()

    ChangeProcessor(outputs=[DEV], validator=validator).process(
        CONFIG_REPO, "deadbeef", None
    )

    _checks, tree = validator.calls[0]
    assert compute_digest(tree).startswith("sha256:")


def test_a_diff_reports_a_failed_verdict_and_still_comments_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    monkeypatch.setattr(
        change,
        "_manifests_diff",
        lambda checkout, base: ManifestDiff(stat="api.yaml | 1 +\n", diff="+api"),
    )
    commenter = Commenter()

    result = DiffCommentProcessor(
        outputs=[DEV, PROD],
        commenter=commenter,
        validator=Validator({"acme-prod": FAILED}),
    ).diff(CONFIG_REPO, "deadbeef", pull_request=7)

    assert [entry.output for entry in result.validations] == ["acme-dev", "acme-prod"]
    assert [entry.failed for entry in result.validations] == [False, True]
    body = commenter.bodies[0]
    assert "### Manifest validation" in body
    assert "- `acme-dev` — passed (kics)" in body
    assert "- `acme-prod` — **failed** (kics): 1 finding" in body
    assert "| acme-prod | high | RBAC-1 | acme-prod/rbac.yaml | wildcard rule |" in body
    assert "api.yaml | 1 +" in body


def test_a_diff_still_reports_the_diff_when_the_validator_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    monkeypatch.setattr(
        change,
        "_manifests_diff",
        lambda checkout, base: ManifestDiff(stat="api.yaml | 1 +\n", diff="+api"),
    )
    commenter = Commenter()

    result = DiffCommentProcessor(
        outputs=[DEV],
        commenter=commenter,
        validator=Validator(default=ValidationError("connection refused")),
    ).diff(CONFIG_REPO, "deadbeef", pull_request=7)

    assert result.diffs[0].manifest_diff.diff == "+api"
    assert result.validations[0].error == "connection refused"
    assert "no verdict: connection refused" in commenter.bodies[0]
    assert "api.yaml | 1 +" in commenter.bodies[0]


def test_a_diff_without_a_validator_says_nothing_about_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    commenter = Commenter()

    result = DiffCommentProcessor(outputs=[DEV], commenter=commenter).diff(
        CONFIG_REPO, "deadbeef", pull_request=7
    )

    assert result.validations == ()
    assert "Manifest validation" not in commenter.bodies[0]


ADVISORY = Validation(
    passed=True,
    digest="sha256:advisory",
    verdicts=(
        Verdict(passed=True, tool="structural"),
        Verdict(
            passed=True,
            tool="kics",
            advisory=True,
            findings=(
                Finding(
                    rule_id="RBAC Wildcard In Rule",
                    severity="high",
                    message="wildcard rule",
                    file="acme-dev/rbac.yaml",
                ),
            ),
        ),
    ),
)


@on_change_disabled
def test_an_advisory_finding_does_not_stop_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator passed the tree, so relcoord pushes it and says what it saw."""
    pushed: list[str] = []
    _fake_git(monkeypatch, tmp_path, pushed=pushed)
    events: list[ChangeProgress] = []

    ChangeProcessor(outputs=[DEV], validator=Validator(default=ADVISORY)).process(
        CONFIG_REPO, "deadbeef", None, progress=events.append
    )

    assert pushed == [MANIFESTS_REPO]
    by_phase = {event.phase: event for event in events}
    assert by_phase["validated"].message == "acme-dev: passed, 1 advisory"


def test_a_diff_shows_advisory_findings_apart_from_the_ones_that_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(monkeypatch, tmp_path)
    commenter = Commenter()

    result = DiffCommentProcessor(
        outputs=[DEV], commenter=commenter, validator=Validator(default=ADVISORY)
    ).diff(CONFIG_REPO, "deadbeef", pull_request=7)

    assert [entry.failed for entry in result.validations] == [False]
    body = commenter.bodies[0]
    assert "- `acme-dev` — passed (structural, kics), 1 advisory" in body
    assert "#### Advisory findings" in body
    assert "These failed no verdict and blocked nothing." in body
    assert (
        "| acme-dev | high | RBAC Wildcard In Rule | acme-dev/rbac.yaml "
        "| wildcard rule |"
    ) in body
    assert "**failed**" not in body
