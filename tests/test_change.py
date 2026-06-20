# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from dulwich import porcelain
from dulwich.errors import NotGitRepository
import pytest

from relcoord import change
from relcoord.change import (
    ChangeProcessor,
    CredentialError,
    DeployConfigError,
    DeploymentDetectionError,
)
from relcoord.config import OutputSettings
from relcoord.git import GitCredentialError


@dataclass(frozen=True)
class Ref:
    kind: str
    namespace: str | None
    name: str


@dataclass(frozen=True)
class GenerationResult:
    written_paths: set[Path]
    created_or_modified: set[Ref]
    removed: set[Ref]
    deploy_id: str | None


def test_change_processor_checks_out_deploy_config_generates_commit_and_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        calls.append(("checkout", repo, commit, target.name, idcat))
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        calls.append(("clone", repo, target.name, idcat, kwargs))
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        manifests_checkout: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str,
        vars: dict[str, object],
    ) -> set[Path]:
        calls.append(
            (
                "generate",
                deploy_config.name,
                manifests_checkout.name,
                repo_root,
                create_commit,
                image,
                namespace,
                vars,
            )
        )
        return {manifests_checkout / "api.yaml", manifests_checkout / "worker.yaml"}

    def fake_head_commit(repo_path: Path) -> str:
        calls.append(("head", repo_path.name))
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append(("push", repo_path.name, remote, idcat))

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", fake_head_commit)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    with caplog.at_level(logging.INFO, logger="relcoord.change"):
        result = ChangeProcessor("https://github.com/acme/manifests.git").process(
            "https://github.com/acme/config.git",
            "deadbeef",
            "registry.example.com/team/api:1.2.3",
        )

    assert result.repo == "https://github.com/acme/config.git"
    assert result.commit == "deadbeef"
    assert result.deploy_config == tmp_path / "source" / ".deploy"
    assert result.manifests_checkout == tmp_path / "manifests"
    assert result.generated_count == 2
    assert calls == [
        (
            "checkout",
            "https://github.com/acme/config.git",
            "deadbeef",
            "source",
            None,
        ),
        (
            "clone",
            "https://github.com/acme/manifests.git",
            "manifests",
            None,
            {
                "purpose": (
                    "cloning manifests repo https://github.com/acme/manifests.git"
                ),
                "depth": "1",
            },
        ),
        (
            "generate",
            ".deploy",
            "manifests",
            Path("/"),
            True,
            "registry.example.com/team/api:1.2.3",
            "config",
            {},
        ),
        ("head", "manifests"),
        ("push", "manifests", "https://github.com/acme/manifests.git", None),
    ]
    assert (
        "change step 2/7: checking out source repo https://github.com/acme/config.git "
        "at commit deadbeef"
    ) in caplog.text
    assert "change step 5/7: invoking manifest-builder" in caplog.text
    assert (
        "change step 6/7: manifest-builder created manifests commit feedface"
        in caplog.text
    )
    assert (
        "change step 7/7: pushing manifests commit feedface to "
        "https://github.com/acme/manifests.git"
    ) in caplog.text


def test_change_processor_generates_configured_outputs_with_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    outputs = [
        OutputSettings(
            name="example-dev",
            repository="https://github.com/acme/manifests.git",
            directory=Path("example-dev"),
            vars={
                "cluster_name": "example-dev",
                "account_id": 111122223333,
            },
        ),
        OutputSettings(
            name="example-prod",
            repository="https://github.com/acme/manifests.git",
            directory=Path("example-prod"),
            vars={
                "cluster_name": "example-prod",
                "account_id": 444455556666,
            },
        ),
    ]

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        calls.append(("checkout", repo, commit, target.name, idcat))
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        calls.append(("clone", repo, target.name, idcat, kwargs))
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        output_path: Path,
        *,
        repo_root: Path,
        create_commit: bool,
        image: str | None,
        namespace: str,
        vars: dict[str, object],
    ) -> GenerationResult:
        calls.append(
            (
                "generate",
                deploy_config.name,
                output_path.relative_to(tmp_path / "manifests"),
                repo_root,
                create_commit,
                image,
                namespace,
                vars,
            )
        )
        return GenerationResult(
            written_paths={output_path / "api.yaml"},
            created_or_modified=set(),
            removed=set(),
            deploy_id="0123456789abcdef",
        )

    def fake_head_commit(repo_path: Path) -> str:
        calls.append(("head", repo_path.name))
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append(("push", repo_path.name, remote, idcat))

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", fake_head_commit)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    result = ChangeProcessor(outputs=outputs).process(
        "https://github.com/acme/config.git",
        "deadbeef",
        None,
    )

    assert result.generated_count == 2
    assert [output.name for output in result.outputs] == [
        "example-dev",
        "example-prod",
    ]
    assert calls == [
        (
            "checkout",
            "https://github.com/acme/config.git",
            "deadbeef",
            "source",
            None,
        ),
        (
            "clone",
            "https://github.com/acme/manifests.git",
            "manifests",
            None,
            {
                "purpose": (
                    "cloning manifests repo https://github.com/acme/manifests.git"
                ),
                "depth": "1",
            },
        ),
        (
            "generate",
            ".deploy",
            Path("example-dev"),
            Path("/"),
            True,
            None,
            "config",
            {"cluster_name": "example-dev", "account_id": 111122223333},
        ),
        (
            "generate",
            ".deploy",
            Path("example-prod"),
            Path("/"),
            True,
            None,
            "config",
            {"cluster_name": "example-prod", "account_id": 444455556666},
        ),
        ("head", "manifests"),
        ("push", "manifests", "https://github.com/acme/manifests.git", None),
    ]


def test_change_processor_detects_deployment_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    created = {Ref(kind="Deployment", namespace="config", name="api")}
    removed = {Ref(kind="ConfigMap", namespace="config", name="old-api")}

    class Detector:
        def __init__(self) -> None:
            self.called = threading.Event()
            self.kwargs: dict[str, object] | None = None

        def wait_for_success(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.called.set()

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        calls.append(("checkout", repo, commit, target.name, idcat))
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        calls.append(("clone", repo, target.name, idcat, kwargs))
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        calls.append(("generate", args[0].name, args[1].name))
        manifests_checkout = args[1]
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified=created,
            removed=removed,
            deploy_id="0123456789abcdef",
        )

    def fake_head_commit(repo_path: Path) -> str:
        calls.append(("head", repo_path.name))
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append(("push", repo_path.name, remote, idcat))

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", fake_head_commit)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    detector = Detector()
    result = ChangeProcessor(
        "https://github.com/acme/manifests.git",
        detect_deployment=True,
        deployment_detector=detector,
    ).process("https://github.com/acme/config.git", "deadbeef", None)

    assert result.generated_count == 1
    assert result.deploy_id == "0123456789abcdef"
    assert calls[-1] == (
        "push",
        "manifests",
        "https://github.com/acme/manifests.git",
        None,
    )
    assert detector.called.wait(timeout=1)
    assert detector.kwargs == {
        "deploy_id": "0123456789abcdef",
        "created_or_modified": created,
        "removed": removed,
    }


def test_change_processor_requires_deploy_id_for_deployment_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / ".deploy").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(*args, **kwargs) -> GenerationResult:
        manifests_checkout = args[1]
        return GenerationResult(
            written_paths={manifests_checkout / "api.yaml"},
            created_or_modified=set(),
            removed=set(),
            deploy_id=None,
        )

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        calls.append("push")

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    with pytest.raises(DeploymentDetectionError, match="did not return a deploy_id"):
        ChangeProcessor(
            "https://github.com/acme/manifests.git",
            detect_deployment=True,
        ).process("https://github.com/acme/config.git", "deadbeef", None)

    assert calls == []


def test_change_processor_requires_top_level_deploy_directory(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        target.mkdir(parents=True)

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)

    processor = ChangeProcessor("https://github.com/acme/manifests.git")

    try:
        processor.process("https://github.com/acme/config.git", "deadbeef", None)
    except DeployConfigError as exc:
        assert "does not contain a .deploy directory" in str(exc)
    else:
        raise AssertionError("expected DeployConfigError")


def test_change_processor_uses_custom_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy_configs: list[Path] = []

    def fake_checkout_commit(repo: str, commit: str, target: Path, idcat) -> None:
        (target / "deploy" / "system").mkdir(parents=True)

    def fake_clone_repository(repo: str, target: Path, idcat, **kwargs) -> None:
        target.mkdir(parents=True)

    def fake_generate(
        deploy_config: Path,
        manifests_checkout: Path,
        **kwargs,
    ) -> set[Path]:
        deploy_configs.append(deploy_config)
        return set()

    def fake_head_commit(repo_path: Path) -> str:
        return "feedface"

    def fake_push_repository(repo_path: Path, remote: str, idcat) -> None:
        pass

    monkeypatch.setattr(
        change, "tempfile", type("T", (), {"mkdtemp": lambda prefix: str(tmp_path)})
    )
    monkeypatch.setattr(change, "_checkout_commit", fake_checkout_commit)
    monkeypatch.setattr(change, "_clone_repository", fake_clone_repository)
    monkeypatch.setattr(change, "generate", fake_generate)
    monkeypatch.setattr(change, "_head_commit", fake_head_commit)
    monkeypatch.setattr(change, "_push_repository", fake_push_repository)

    processor = ChangeProcessor("https://github.com/acme/manifests.git")
    processor.process(
        "https://github.com/acme/config.git",
        "deadbeef",
        None,
        config_path="deploy/system",
    )

    assert deploy_configs == [tmp_path / "source" / "deploy" / "system"]


def test_dulwich_error_message_falls_back_to_exception_type_when_blank() -> None:
    # NotGitRepository (raised for missing/private/inaccessible repos) has an
    # empty string representation, so the message must surface its type instead.
    message = change._dulwich_error_message("clone", NotGitRepository(), BytesIO())

    assert message == "dulwich clone failed: dulwich.errors.NotGitRepository"


def test_dulwich_error_message_prefers_stderr() -> None:
    errstream = BytesIO(b"fatal: repository not found\n")

    message = change._dulwich_error_message("clone", NotGitRepository(), errstream)

    assert message == "dulwich clone failed: fatal: repository not found"


@pytest.mark.parametrize(
    ("repo", "namespace"),
    [
        ("https://github.com/acme/config.git", "config"),
        ("https://github.com/acme/config", "config"),
        ("acme/config.git", "config"),
    ],
)
def test_namespace_from_repo(repo: str, namespace: str) -> None:
    assert change._namespace_from_repo(repo) == namespace


def test_checkout_commit_materializes_requested_commit(tmp_path: Path) -> None:
    source = tmp_path / "source-repo"
    repo = porcelain.init(source)
    try:
        (source / "README.md").write_text("first\n")
        porcelain.add(repo, b"README.md")
        first_commit = porcelain.commit(
            repo,
            message=b"first",
            author=b"Test <test@example.com>",
            committer=b"Test <test@example.com>",
            sign=False,
        )

        (source / ".deploy").mkdir()
        (source / ".deploy" / "api.yaml").write_text("image: example\n")
        porcelain.add(repo, [b".deploy/api.yaml"])
        porcelain.commit(
            repo,
            message=b"second",
            author=b"Test <test@example.com>",
            committer=b"Test <test@example.com>",
            sign=False,
        )
    finally:
        repo.close()

    checkout = tmp_path / "checkout"
    change._checkout_commit(str(source), first_commit.decode("ascii"), checkout, None)

    assert (checkout / "README.md").read_text() == "first\n"
    assert not (checkout / ".deploy").exists()


def test_credentials_for_wraps_git_credential_error_with_operation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_github_https_credentials(source: str, idcat) -> None:
        raise GitCredentialError("idcat returned HTTP 401: not allowed")

    monkeypatch.setattr(
        change, "github_https_credentials", fake_github_https_credentials
    )

    with pytest.raises(CredentialError) as excinfo:
        change._credentials_for(
            "https://github.com/acme/system.git",
            None,
            "checking out source repo https://github.com/acme/system.git",
        )

    message = str(excinfo.value)
    assert "checking out source repo https://github.com/acme/system.git" in message
    assert "idcat returned HTTP 401: not allowed" in message
    assert isinstance(excinfo.value.__cause__, GitCredentialError)
