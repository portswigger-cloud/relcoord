# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Only the system repository may generate raw AWS and Crossplane resources.

The end-to-end tests here run manifest-builder for real, against config and
manifests repositories that are git repositories on disk, so what is checked is
the tree a change would publish and the remote is what a change did publish.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from dulwich import porcelain
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit
from dulwich.refs import HEADREF
from dulwich.refs import Ref as GitRef
from dulwich.repo import Repo

from relcoord import change
from relcoord.auth import ValidatedClaims
from relcoord.change import (
    ChangeProcessor,
    ChangeProgress,
    CommentPostError,
    DiffCommentProcessor,
    RawIamKmsError,
)
from relcoord.config import OutputSettings, RolloutSettings, RolloutStage
from relcoord.github import GithubCommentError, PostedComment
from relcoord.manifest_diff import render_raw_iam_kms_refusal
from relcoord.raw_iam_kms import (
    GITHUB_ACTIONS_ISSUER,
    RawResource,
    find_raw_resources,
    generated_tree,
    may_generate_raw_iam_kms,
    refusal_message,
    verified_repository,
)

SYSTEM_REPOSITORY = "https://github.com/acme/system"
SYSTEM = "acme/system"
TENANT = "acme/idfrog"
MAIN = GitRef(b"refs/heads/main")
AUTHOR = b"Test <test@example.com>"

DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: idfrog
spec:
  replicas: 1
"""

RAW_ROLE = """\
apiVersion: iam.aws.m.upbound.io/v1beta1
kind: Role
metadata:
  name: idfrog-irsa
spec:
  forProvider:
    assumeRolePolicy: "{}"
"""

RAW_ROLE_POLICY = """\
apiVersion: iam.aws.m.upbound.io/v1beta1
kind: RolePolicy
metadata:
  name: idfrog-kms
spec:
  forProvider:
    policy: "{}"
"""

KMS_PERMISSION = """\
apiVersion: portswigger.io/v1alpha1
kind: KMSPermission
metadata:
  name: idfrog-signing
spec:
  serviceAccount: idfrog
"""


def _document(api_version: str, kind: str, name: str = "x") -> str:
    return f"apiVersion: {api_version}\nkind: {kind}\nmetadata:\n  name: {name}\n"


# What the rule reads


@pytest.mark.parametrize(
    ("api_version", "kind"),
    [
        ("iam.aws.m.upbound.io/v1beta1", "Role"),
        ("iam.aws.m.upbound.io/v1beta1", "RolePolicy"),
        ("iam.aws.m.upbound.io/v1beta1", "Policy"),
        ("iam.aws.m.upbound.io/v1beta1", "RolePolicyAttachment"),
        ("iam.aws.m.upbound.io/v1beta2", "Role"),
        ("iam.aws.m.upbound.io/v1", "User"),
        ("kms.aws.m.upbound.io/v1beta1", "Key"),
        ("kms.aws.m.upbound.io/v1beta1", "Alias"),
        ("kms.aws.m.upbound.io/v1beta1", "Grant"),
        # Policies of other services grant access as IAM does.
        ("s3.aws.m.upbound.io/v1beta1", "BucketPolicy"),
        ("sqs.aws.m.upbound.io/v1beta1", "QueuePolicy"),
        ("ecr.aws.m.upbound.io/v1beta1", "RepositoryPolicy"),
        ("s3.aws.m.upbound.io/v1beta1", "Bucket"),
        ("dynamodb.aws.m.upbound.io/v1beta1", "Table"),
        # The provider's configuration says which AWS identity Crossplane uses.
        ("aws.m.upbound.io/v1beta1", "ProviderConfig"),
        ("aws.m.upbound.io/v1beta1", "ClusterProviderConfig"),
        # Crossplane v1's cluster-scoped equivalents.
        ("iam.aws.upbound.io/v1beta1", "Role"),
        ("iam.aws.upbound.io/v1beta1", "RolePolicyAttachment"),
        ("kms.aws.upbound.io/v1beta1", "Key"),
        ("kms.aws.upbound.io/v1beta1", "Alias"),
        ("aws.upbound.io/v1beta1", "ProviderConfig"),
        # What decides and runs what a composite becomes.
        ("apiextensions.crossplane.io/v1", "CompositeResourceDefinition"),
        ("apiextensions.crossplane.io/v1", "Composition"),
        ("apiextensions.crossplane.io/v1beta1", "EnvironmentConfig"),
        ("pkg.crossplane.io/v1", "Provider"),
        ("pkg.crossplane.io/v1", "Function"),
        ("pkg.crossplane.io/v1beta1", "DeploymentRuntimeConfig"),
        ("ops.crossplane.io/v1alpha1", "Operation"),
        # The API server would not know this group; refusing it costs nothing.
        ("IAM.aws.m.upbound.io/v1beta1", "Role"),
        ("PKG.crossplane.io/v1", "Function"),
    ],
)
def test_every_aws_and_crossplane_kind_in_any_version_is_found(
    api_version: str, kind: str
) -> None:
    found = find_raw_resources({"ns/x.yaml": _document(api_version, kind).encode()})

    assert [(r.api_version, r.kind) for r in found] == [(api_version, kind)]


@pytest.mark.parametrize(
    ("api_version", "kind"),
    [
        # Composites are how every other repository asks for AWS.
        ("portswigger.io/v1alpha1", "KMSPermission"),
        ("platform.portswigger.io/v1alpha1", "IrsaRole"),
        ("rbac.authorization.k8s.io/v1", "Role"),
        ("v1", "ConfigMap"),
        ("apps/v1", "Deployment"),
        ("external-secrets.io/v1", "ExternalSecret"),
        ("grafana.integreatly.org/v1beta1", "GrafanaDashboard"),
        # Only the groups themselves and the groups under them, nothing that
        # merely ends with the same letters or starts with the same group.
        ("xaws.m.upbound.io/v1beta1", "Role"),
        ("iam.aws.m.upbound.io.example.com/v1", "Role"),
        ("crossplane.io/v1", "Thing"),
        ("upbound.io/v1", "Thing"),
        # Other Crossplane groups and other providers are outside the rule.
        ("protection.crossplane.io/v1beta1", "Usage"),
        ("postgresql.sql.crossplane.io/v1alpha1", "Role"),
    ],
)
def test_other_api_groups_are_left_alone(api_version: str, kind: str) -> None:
    files = {"ns/x.yaml": _document(api_version, kind).encode()}

    assert find_raw_resources(files) == ()


def test_every_document_of_a_file_and_every_item_of_a_list_is_read() -> None:
    files = {
        "idfrog/all.yaml": (
            DEPLOYMENT
            + "---\n"
            + RAW_ROLE
            + "---\n"
            + "apiVersion: v1\nkind: List\nitems:\n"
            + "- apiVersion: kms.aws.m.upbound.io/v1beta1\n"
            + "  kind: Key\n"
            + "  metadata: {name: signing, namespace: idfrog}\n"
        ).encode()
    }

    found = find_raw_resources(files)

    assert [r.describe() for r in found] == [
        "Role idfrog-irsa (iam.aws.m.upbound.io/v1beta1)",
        "Key idfrog/signing (kms.aws.m.upbound.io/v1beta1)",
    ]
    assert {r.file for r in found} == {"idfrog/all.yaml"}


def test_a_list_that_contains_itself_is_read_once() -> None:
    text = (
        "&list\napiVersion: v1\nkind: List\nitems:\n- *list\n"
        "- {apiVersion: kms.aws.m.upbound.io/v1beta1, kind: Key, metadata: {name: k}}\n"
    )

    found = find_raw_resources({"ns/list.yaml": text.encode()})

    assert [r.kind for r in found] == ["Key"]


def test_a_file_that_is_not_yaml_is_refused_rather_than_passed() -> None:
    found = find_raw_resources({"idfrog/broken.yaml": b"kind: [unclosed\n"})

    assert len(found) == 1
    assert found[0].file == "idfrog/broken.yaml"
    assert found[0].unreadable is not None
    assert found[0].describe().startswith("a file that is not readable as YAML")


def test_the_generated_tree_of_a_namespace_is_its_own_directory_only(
    tmp_path: Path,
) -> None:
    (tmp_path / "idfrog").mkdir()
    (tmp_path / "idfrog" / "deployment-idfrog.yaml").write_text(DEPLOYMENT)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "role-legacy.yaml").write_text(RAW_ROLE)
    (tmp_path / "owners").mkdir()
    (tmp_path / "owners" / "idfrog.toml").write_text('owned = "idfrog"\n')

    assert set(generated_tree(tmp_path, "idfrog")) == {"idfrog/deployment-idfrog.yaml"}


def test_the_generated_tree_of_system_is_the_roots_system_owns(
    tmp_path: Path,
) -> None:
    for path in ("cluster/a.yaml", "kube-system/b.yaml", "idfrog/c.yaml"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(DEPLOYMENT)
    (tmp_path / "owners").mkdir()
    (tmp_path / "owners" / "system.toml").write_text(
        'owned = ["cluster", "kube-system", ".", "owners"]\n'
    )
    (tmp_path / "owners" / "idfrog.toml").write_text('owned = "idfrog"\n')

    assert set(generated_tree(tmp_path, None)) == {
        "cluster/a.yaml",
        "kube-system/b.yaml",
    }


# Who may


TENANT_REPOSITORY = "https://github.com/acme/idfrog"


@pytest.mark.parametrize(
    ("system", "source", "config_repo", "system_repository", "allowed"),
    [
        (True, SYSTEM, SYSTEM_REPOSITORY, SYSTEM_REPOSITORY, True),
        (True, "ACME/System", SYSTEM_REPOSITORY, SYSTEM_REPOSITORY, True),
        (True, SYSTEM, SYSTEM_REPOSITORY + ".git", SYSTEM_REPOSITORY, True),
        (True, SYSTEM, SYSTEM_REPOSITORY, SYSTEM_REPOSITORY + ".git", True),
        # System mode is the role's; the repository is the token's.
        (True, TENANT, SYSTEM_REPOSITORY, SYSTEM_REPOSITORY, False),
        (True, None, SYSTEM_REPOSITORY, SYSTEM_REPOSITORY, False),
        # The system repository's token generating another repository as system.
        (True, SYSTEM, TENANT_REPOSITORY, SYSTEM_REPOSITORY, False),
        (True, SYSTEM, "/srv/checkouts/system", SYSTEM_REPOSITORY, False),
        # The system repository's own token, outside system mode.
        (False, SYSTEM, SYSTEM_REPOSITORY, SYSTEM_REPOSITORY, False),
        (False, TENANT, TENANT_REPOSITORY, SYSTEM_REPOSITORY, False),
        # Nothing names the system repository to compare with.
        (True, SYSTEM, SYSTEM_REPOSITORY, None, False),
        (True, SYSTEM, SYSTEM_REPOSITORY, "git@github.com:acme/system.git", False),
        (True, "other/system", SYSTEM_REPOSITORY, SYSTEM_REPOSITORY, False),
    ],
)
def test_only_system_mode_of_the_system_repository_by_it_may(
    system: bool,
    source: str | None,
    config_repo: str,
    system_repository: str | None,
    allowed: bool,
) -> None:
    assert (
        may_generate_raw_iam_kms(
            system=system,
            source_repository=source,
            config_repo=config_repo,
            system_repository=system_repository,
        )
        is allowed
    )


def test_the_verified_repository_is_the_github_tokens_claim() -> None:
    principal = ValidatedClaims(
        role="system-main",
        claims={"iss": GITHUB_ACTIONS_ISSUER, "repository": SYSTEM},
        allow_system=True,
    )

    assert verified_repository(principal) == SYSTEM


@pytest.mark.parametrize(
    "principal",
    [
        None,
        ValidatedClaims(
            role="k8s",
            claims={"iss": "https://kubernetes.default.svc", "repository": SYSTEM},
            allow_system=True,
        ),
        ValidatedClaims(
            role="gh", claims={"iss": GITHUB_ACTIONS_ISSUER}, allow_system=True
        ),
        ValidatedClaims(
            role="gh",
            claims={"iss": GITHUB_ACTIONS_ISSUER, "repository": ["acme/system"]},
        ),
    ],
)
def test_no_verified_repository_without_a_github_repository_claim(
    principal: object,
) -> None:
    assert verified_repository(principal) is None


# What a refusal says


def test_a_refusal_names_each_resource_and_what_to_do_instead() -> None:
    message = refusal_message(
        "fe-dev",
        (
            RawResource(
                file="idfrog/role-idfrog-irsa.yaml",
                api_version="iam.aws.m.upbound.io/v1beta1",
                kind="Role",
                namespace="idfrog",
                name="idfrog-irsa",
            ),
        ),
        SYSTEM_REPOSITORY,
    )

    assert message.startswith(
        "fe-dev: only acme/system may deploy raw AWS or Crossplane"
    )
    assert (
        "Role idfrog/idfrog-irsa (iam.aws.m.upbound.io/v1beta1) in "
        "idfrog/role-idfrog-irsa.yaml" in message
    )
    assert "KMSPermission" in message
    assert "Removing these resources is always allowed." in message


def test_the_comment_tables_refused_resources_without_breaking_its_rows() -> None:
    hostile = RawResource(
        file="idfrog/role-x.yaml",
        api_version="iam.aws.m.upbound.io/v1beta1",
        kind="Role",
        name="a|b\nc",
    )

    body = render_raw_iam_kms_refusal([("fe-dev", (hostile,))], SYSTEM_REPOSITORY)

    assert body.startswith(
        "### Raw AWS and Crossplane resources\n\n**Refused** — only `acme"
    )
    assert "| fe-dev | Role a\\|b c (iam.aws.m.upbound.io/v1beta1) |" in body
    assert body.rstrip().endswith("Removing these resources is always allowed.")
    assert render_raw_iam_kms_refusal([], SYSTEM_REPOSITORY) == ""


def test_the_comment_counts_what_it_leaves_out() -> None:
    many = tuple(
        RawResource(
            file=f"ns/{i}.yaml",
            api_version="kms.aws.m.upbound.io/v1",
            kind="Key",
            name=str(i),
        )
        for i in range(25)
    )

    body = render_raw_iam_kms_refusal([("fe-dev", many)], SYSTEM_REPOSITORY)

    assert body.count("| fe-dev |") == 20
    assert "_5 more in the error relcoord returned._" in body


# End to end, with manifest-builder and git


def _commit_all(path: Path, files: Mapping[str, str], message: bytes) -> bytes:
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    repo = Repo(str(path))
    try:
        porcelain.add(repo, [name.encode() for name in files])
        return porcelain.commit(repo, message=message, author=AUTHOR, committer=AUTHOR)
    finally:
        repo.close()


def _tenant_repo(tmp_path: Path, manifests: Mapping[str, str]) -> tuple[str, str]:
    """A tenant config repository copying ``manifests`` into its namespace.

    The directory's name is the namespace, as a repository's name is.
    """
    path = tmp_path / "idfrog"
    porcelain.init(str(path)).close()
    files = {".deploy/config.toml": '[[copy]]\nsource = "manifests"\n'}
    files.update({f".deploy/manifests/{n}": text for n, text in manifests.items()})
    commit = _commit_all(path, files, b"config")
    return str(path), commit.decode()


def _system_repo(tmp_path: Path, manifests: Mapping[str, str]) -> tuple[str, str]:
    path = tmp_path / "system"
    porcelain.init(str(path)).close()
    files = {
        "config.toml": (
            '[[copy]]\nname = "crossplane"\nnamespace = "crossplane-system"\n'
            'source = "manifests"\n'
        )
    }
    files.update({f"manifests/{n}": text for n, text in manifests.items()})
    commit = _commit_all(path, files, b"config")
    return str(path), commit.decode()


@dataclass
class _Remote:
    """A manifests repository standing in for GitHub."""

    path: Path
    base: bytes = b""

    @classmethod
    def seeded(cls, tmp_path: Path, files: Mapping[str, str]) -> _Remote:
        remote = cls(tmp_path / "manifests.git")
        porcelain.init(str(remote.path), bare=True).close()
        seed = tmp_path / "seed"
        porcelain.init(str(seed)).close()
        _commit_all(seed, {"README.md": "manifests\n", **files}, b"seed")
        seed_repo = Repo(str(seed))
        try:
            (_, branch), _ = seed_repo.refs.follow(HEADREF)
        finally:
            seed_repo.close()
        porcelain.push(
            str(seed),
            str(remote.path),
            branch + b":refs/heads/main",
            errstream=BytesIO(),
        )
        repo = Repo(str(remote.path))
        try:
            repo.refs.set_symbolic_ref(HEADREF, MAIN)
            remote.base = repo.refs[MAIN]
        finally:
            repo.close()
        return remote

    def tip(self) -> bytes:
        repo = Repo(str(self.path))
        try:
            return repo.refs[MAIN]
        finally:
            repo.close()

    def files(self) -> dict[str, str]:
        repo = Repo(str(self.path))
        try:
            commit = repo[repo.refs[MAIN]]
            assert isinstance(commit, Commit)
            contents: dict[str, str] = {}
            for entry in iter_tree_contents(repo.object_store, commit.tree):
                assert entry.path is not None and entry.sha is not None
                blob = repo[entry.sha]
                assert isinstance(blob, Blob)
                contents[entry.path.decode()] = blob.data.decode()
            return contents
        finally:
            repo.close()


def _outputs(remote: _Remote, *names: str) -> list[OutputSettings]:
    return [
        OutputSettings(name=name, repository=str(remote.path), directory=Path(name))
        for name in names
    ]


@pytest.fixture(autouse=True)
def no_system_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenant changes clone the system repository's plugins; these need none."""
    monkeypatch.setattr(change, "_system_plugins", lambda *args, **kwargs: None)


def _serve(monkeypatch: pytest.MonkeyPatch, urls: Mapping[str, str]) -> None:
    """Clone each of ``urls`` from the local repository it maps to.

    The exemption compares ``config_repo`` with the configured GitHub URL, so a
    system change has to be asked for by that URL to be one.
    """
    clone = change._clone_repository

    def serve(source: str, *args: Any, **kwargs: Any) -> None:
        clone(urls.get(source, source), *args, **kwargs)

    monkeypatch.setattr(change, "_clone_repository", serve)
    # A diff rebases the pull request onto its default branch; these are.
    monkeypatch.setattr(
        change,
        "_checkout_rebased_commit",
        lambda source, commit, target, idcat: change._checkout_commit(
            source, commit, target, idcat
        ),
    )


def _events() -> tuple[list[ChangeProgress], Any]:
    events: list[ChangeProgress] = []
    return events, events.append


def test_a_tenant_change_generating_raw_iam_is_refused_and_pushes_nothing(
    tmp_path: Path,
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(
        tmp_path,
        {
            "deployment.yaml": DEPLOYMENT,
            "role.yaml": RAW_ROLE,
            "policy.yaml": RAW_ROLE_POLICY,
        },
    )
    events, progress = _events()

    with pytest.raises(RawIamKmsError) as raised:
        ChangeProcessor(
            system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
        ).process(repo, commit, None, source_repository=TENANT, progress=progress)

    message = str(raised.value)
    assert message.startswith(
        "fe-dev: only acme/system may deploy raw AWS or Crossplane"
    )
    assert "Role idfrog/idfrog-irsa (iam.aws.m.upbound.io/v1beta1)" in message
    assert "in idfrog/role-idfrog-irsa.yaml" in message
    assert "RolePolicy idfrog/idfrog-kms (iam.aws.m.upbound.io/v1beta1)" in message
    assert "KMSPermission" in message
    assert "Nothing generated for fe-dev was pushed." in message
    assert remote.tip() == remote.base
    refused = [event for event in events if event.phase == "raw-iam-kms-refused"]
    assert [(r["kind"], r["name"]) for r in refused[0].detail["resources"]] == [
        ("Role", "idfrog-irsa"),
        ("RolePolicy", "idfrog-kms"),
    ]
    assert not any(event.phase in {"push", "pushed"} for event in events)


def test_a_tenant_change_with_a_kms_permission_is_deployed(tmp_path: Path) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(
        tmp_path, {"deployment.yaml": DEPLOYMENT, "kms.yaml": KMS_PERMISSION}
    )

    ChangeProcessor(
        system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
    ).process(repo, commit, None, source_repository=TENANT)

    files = remote.files()
    assert "fe-dev/idfrog/kmspermission-idfrog-signing.yaml" in files
    assert "fe-dev/idfrog/deployment-idfrog.yaml" in files


def test_a_legacy_cluster_scoped_iam_role_is_refused_from_a_namespace(
    tmp_path: Path,
) -> None:
    # manifest-builder does not know this kind is cluster-scoped, so namespace
    # mode writes it into the namespace directory instead of refusing it.
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(
        tmp_path,
        {"role.yaml": RAW_ROLE.replace("iam.aws.m.upbound.io", "iam.aws.upbound.io")},
    )

    with pytest.raises(
        RawIamKmsError, match=r"Role idfrog/idfrog-irsa \(iam\.aws\.upbound\.io/"
    ):
        ChangeProcessor(
            system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
        ).process(repo, commit, None, source_repository=TENANT)

    assert remote.tip() == remote.base


def test_a_tenant_change_removing_its_raw_iam_is_deployed(tmp_path: Path) -> None:
    remote = _Remote.seeded(
        tmp_path,
        {
            "fe-dev/owners/idfrog.toml": 'owned = "idfrog"\n',
            "fe-dev/idfrog/role-idfrog-irsa.yaml": RAW_ROLE,
            "fe-dev/idfrog/rolepolicy-idfrog-kms.yaml": RAW_ROLE_POLICY,
        },
    )
    repo, commit = _tenant_repo(
        tmp_path, {"deployment.yaml": DEPLOYMENT, "kms.yaml": KMS_PERMISSION}
    )

    result = ChangeProcessor(
        system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
    ).process(repo, commit, None, source_repository=TENANT)

    files = remote.files()
    assert "fe-dev/idfrog/role-idfrog-irsa.yaml" not in files
    assert "fe-dev/idfrog/rolepolicy-idfrog-kms.yaml" not in files
    assert "fe-dev/idfrog/kmspermission-idfrog-signing.yaml" in files
    assert {(ref.kind, ref.name) for ref in result.outputs[0].removed} == {
        ("Role", "idfrog-irsa"),
        ("RolePolicy", "idfrog-kms"),
    }


def test_another_owners_raw_iam_does_not_stop_a_tenant_change(tmp_path: Path) -> None:
    remote = _Remote.seeded(
        tmp_path,
        {
            "fe-dev/owners/legacy.toml": 'owned = "legacy"\n',
            "fe-dev/legacy/role-legacy.yaml": RAW_ROLE,
            "fe-dev/crossplane-system/role-provider.yaml": RAW_ROLE,
            "fe-dev/owners/system.toml": 'owned = ["crossplane-system"]\n',
        },
    )
    repo, commit = _tenant_repo(tmp_path, {"deployment.yaml": DEPLOYMENT})

    ChangeProcessor(
        system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
    ).process(repo, commit, None, source_repository=TENANT)

    files = remote.files()
    assert "fe-dev/idfrog/deployment-idfrog.yaml" in files
    assert files["fe-dev/legacy/role-legacy.yaml"] == RAW_ROLE


def test_a_tenant_naming_the_system_repository_as_config_repo_is_still_refused(
    tmp_path: Path,
) -> None:
    # config_repo is the caller's to choose; only the token says who it is.
    remote = _Remote.seeded(tmp_path, {})
    path, commit = _tenant_repo(tmp_path, {"role.yaml": RAW_ROLE})
    renamed = tmp_path / "system"
    Path(path).rename(renamed)

    with pytest.raises(RawIamKmsError):
        ChangeProcessor(
            system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
        ).process(str(renamed), commit, None, source_repository=TENANT)

    assert remote.tip() == remote.base


def test_system_may_deploy_raw_iam_and_kms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _system_repo(
        tmp_path,
        {
            "role.yaml": RAW_ROLE,
            "key.yaml": _document("kms.aws.m.upbound.io/v1beta1", "Key", "signing"),
        },
    )
    _serve(monkeypatch, {SYSTEM_REPOSITORY: repo})

    ChangeProcessor(
        system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
    ).process(SYSTEM_REPOSITORY, commit, None, system=True, source_repository=SYSTEM)

    files = remote.files()
    assert "fe-dev/crossplane-system/role-idfrog-irsa.yaml" in files
    assert "fe-dev/crossplane-system/key-signing.yaml" in files


@pytest.mark.parametrize("source", [None, TENANT])
def test_system_mode_without_the_system_repositorys_token_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str | None
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _system_repo(tmp_path, {"role.yaml": RAW_ROLE})
    _serve(monkeypatch, {SYSTEM_REPOSITORY: repo})

    with pytest.raises(
        RawIamKmsError,
        match=r"Role crossplane-system/idfrog-irsa .* in crossplane-system/role-idfrog-irsa.yaml",
    ):
        ChangeProcessor(
            system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
        ).process(
            SYSTEM_REPOSITORY, commit, None, system=True, source_repository=source
        )

    assert remote.tip() == remote.base


def test_the_system_token_generating_another_repository_as_system_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The token is the system repository's; what is checked out is not.
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _system_repo(tmp_path, {"role.yaml": RAW_ROLE})
    _serve(monkeypatch, {TENANT_REPOSITORY: repo})

    with pytest.raises(RawIamKmsError, match=r"Role crossplane-system/idfrog-irsa"):
        ChangeProcessor(
            system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
        ).process(
            TENANT_REPOSITORY, commit, None, system=True, source_repository=SYSTEM
        )

    assert remote.tip() == remote.base


class _Detector:
    def __init__(self) -> None:
        self.waits = 0

    def wait_for_success(self, *, created_or_modified: Any, removed: Any) -> None:
        self.waits += 1


@pytest.mark.parametrize("generate_while_waiting", [False, True])
def test_a_rollout_refuses_raw_iam_before_its_first_stage_is_pushed(
    tmp_path: Path, generate_while_waiting: bool
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(
        tmp_path, {"deployment.yaml": DEPLOYMENT, "role.yaml": RAW_ROLE}
    )
    detector = _Detector()
    processor = ChangeProcessor(
        system_repository=SYSTEM_REPOSITORY,
        outputs=_outputs(remote, "fe-dev", "fe-prod"),
        rollouts=(
            RolloutSettings(
                name="linear",
                stages=(
                    RolloutStage(outputs=("fe-dev",)),
                    RolloutStage(outputs=("fe-prod",)),
                ),
            ),
        ),
        detect_deployment=True,
        deployment_detector=detector,
        generate_while_waiting=generate_while_waiting,
    )

    with pytest.raises(RawIamKmsError, match=r"^fe-dev: "):
        processor.process(repo, commit, None, source_repository=TENANT)

    assert remote.tip() == remote.base
    assert detector.waits == 0


@dataclass
class _Commenter:
    posted: list[tuple[str, int, str]] = field(default_factory=list)

    def post_comment(
        self, repo: str, pull_request: int, body: str, *, marker: str | None = None
    ) -> PostedComment:
        self.posted.append((repo, pull_request, body))
        return PostedComment(url="https://github.com/acme/idfrog/pull/7#c1")


def test_a_tenant_diff_comments_the_refusal_and_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(
        tmp_path, {"deployment.yaml": DEPLOYMENT, "role.yaml": RAW_ROLE}
    )
    # A diff rebases the pull request onto its default branch; this one is.
    monkeypatch.setattr(
        change,
        "_checkout_rebased_commit",
        lambda source, commit, target, idcat: change._checkout_commit(
            source, commit, target, idcat
        ),
    )
    commenter = _Commenter()
    events, progress = _events()

    with pytest.raises(RawIamKmsError) as raised:
        DiffCommentProcessor(
            system_repository=SYSTEM_REPOSITORY,
            outputs=_outputs(remote, "fe-dev", "fe-prod"),
            commenter=commenter,
        ).diff(
            repo,
            commit,
            pull_request=7,
            source_repository=TENANT,
            progress=progress,
        )

    [(_, pull_request, body)] = commenter.posted
    assert pull_request == 7
    assert body.index("### Raw AWS and Crossplane resources") < body.index("diff --git")
    assert "| fe-dev | Role idfrog/idfrog-irsa (iam.aws.m.upbound.io/v1beta1) |" in body
    assert "| fe-prod | Role idfrog/idfrog-irsa" in body
    assert "KMSPermission" in body
    assert str(raised.value).startswith("fe-dev: only acme/system may deploy")
    assert "fe-prod: only acme/system may deploy" in str(raised.value)
    assert [e.detail["output"] for e in events if e.phase == "raw-iam-kms-refused"] == [
        "fe-dev",
        "fe-prod",
    ]
    assert remote.tip() == remote.base


def test_a_tenant_diff_without_raw_iam_says_nothing_about_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(
        tmp_path, {"deployment.yaml": DEPLOYMENT, "kms.yaml": KMS_PERMISSION}
    )
    monkeypatch.setattr(
        change,
        "_checkout_rebased_commit",
        lambda source, commit, target, idcat: change._checkout_commit(
            source, commit, target, idcat
        ),
    )

    result = DiffCommentProcessor(
        system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
    ).diff(repo, commit, source_repository=TENANT)

    assert "Raw AWS" not in result.comment.body
    assert "kmspermission-idfrog-signing.yaml" in result.comment.body


def test_a_system_diff_with_raw_iam_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _system_repo(tmp_path, {"role.yaml": RAW_ROLE})
    _serve(monkeypatch, {SYSTEM_REPOSITORY: repo})

    result = DiffCommentProcessor(
        system_repository=SYSTEM_REPOSITORY, outputs=_outputs(remote, "fe-dev")
    ).diff(SYSTEM_REPOSITORY, commit, system=True, source_repository=SYSTEM)

    assert "Raw AWS" not in result.comment.body
    assert "role-idfrog-irsa.yaml" in result.comment.body


def test_a_system_token_diff_of_another_repository_as_system_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _system_repo(tmp_path, {"role.yaml": RAW_ROLE})
    _serve(monkeypatch, {TENANT_REPOSITORY: repo})
    commenter = _Commenter()

    with pytest.raises(RawIamKmsError, match=r"Role crossplane-system/idfrog-irsa"):
        DiffCommentProcessor(
            system_repository=SYSTEM_REPOSITORY,
            outputs=_outputs(remote, "fe-dev"),
            commenter=commenter,
        ).diff(
            TENANT_REPOSITORY,
            commit,
            system=True,
            pull_request=7,
            source_repository=SYSTEM,
        )

    [(_, _, body)] = commenter.posted
    assert "### Raw AWS and Crossplane resources" in body


@dataclass
class _FailingCommenter:
    def post_comment(
        self, repo: str, pull_request: int, body: str, *, marker: str | None = None
    ) -> PostedComment:
        raise GithubCommentError("GitHub returned HTTP 403")


def test_a_refused_diff_whose_comment_fails_still_says_what_was_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(tmp_path, {"role.yaml": RAW_ROLE})
    _serve(monkeypatch, {})

    with pytest.raises(RawIamKmsError) as raised:
        DiffCommentProcessor(
            system_repository=SYSTEM_REPOSITORY,
            outputs=_outputs(remote, "fe-dev"),
            commenter=_FailingCommenter(),
        ).diff(repo, commit, pull_request=7, source_repository=TENANT)

    message = str(raised.value)
    assert "Role idfrog/idfrog-irsa (iam.aws.m.upbound.io/v1beta1)" in message
    assert "KMSPermission" in message
    assert message.endswith(
        "The pull request comment saying so could not be posted: "
        "GitHub returned HTTP 403"
    )


def test_a_diff_whose_comment_fails_without_a_refusal_reports_the_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Remote.seeded(tmp_path, {})
    repo, commit = _tenant_repo(tmp_path, {"deployment.yaml": DEPLOYMENT})
    _serve(monkeypatch, {})

    with pytest.raises(CommentPostError, match="HTTP 403"):
        DiffCommentProcessor(
            system_repository=SYSTEM_REPOSITORY,
            outputs=_outputs(remote, "fe-dev"),
            commenter=_FailingCommenter(),
        ).diff(repo, commit, pull_request=7, source_repository=TENANT)
