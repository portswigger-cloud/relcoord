# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Keep raw AWS and Crossplane resources to the system repository.

A Crossplane AWS resource reaches AWS directly: an IAM role, policy or KMS key
is an identity or a key, a bucket or queue policy grants access as surely as
IAM does, and a ``ProviderConfig`` says which AWS identity Crossplane acts as.
Crossplane's own definitions go further still: a Composition or Function
decides what a composite becomes, and an EnvironmentConfig feeds it. The only
repository trusted to declare any of these is the system repository. Every
other repository asks for what it needs through a composite resource such as a
``KMSPermission``, whose Composition decides which raw resources it becomes.

This is a check on what a config repository generates, made here because
relcoord is the one place that knows which repository a manifest came from:
one Argo CD identity applies every repository's manifests, so the cluster
cannot tell them apart. It is not admission control. It does not see what a
cluster admin applies with kubectl, nor what a Composition composes, which is
how a ``KMSPermission`` legitimately ends up as raw resources.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from relcoord.git import github_repo_from_url
from relcoord.validator import read_tree

#: API groups only the system repository may generate resources in, together
#: with every group under them (``iam.aws.m.upbound.io`` is under
#: ``aws.m.upbound.io``), in any version and of any kind. The AWS provider's
#: groups are one per service plus the provider's own configuration: the
#: ``.m.`` ones are Crossplane v2's namespaced managed resources, the others the
#: cluster-scoped ones of Crossplane v1, which manifest-builder cannot always
#: tell are cluster-scoped and so would write into a namespace directory rather
#: than refuse. The Crossplane groups define and run what composites become:
#: XRDs, Compositions and EnvironmentConfigs, packages such as Providers and
#: Functions, and Operations.
SYSTEM_ONLY_API_GROUPS = frozenset(
    {
        "aws.m.upbound.io",
        "aws.upbound.io",
        "apiextensions.crossplane.io",
        "pkg.crossplane.io",
        "ops.crossplane.io",
    }
)

#: The issuer whose ``repository`` claim names a GitHub repository. A token from
#: any other issuer says nothing about which repository it came from.
GITHUB_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"

YAML_LOADER: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: What a refused change can do instead, closing every refusal.
REMEDY = (
    "Ask for AWS access through a composite resource such as a KMSPermission "
    "instead, or have the platform team add these resources to system. Removing "
    "these resources is always allowed."
)


@dataclass(frozen=True)
class RawResource:
    """A generated resource only the system repository may deploy."""

    file: str
    """The manifest it is in, relative to the output's directory."""
    api_version: str = ""
    kind: str = ""
    namespace: str | None = None
    name: str = ""
    unreadable: str | None = None
    """Why the file could not be read, in which case it is refused unread."""

    def describe(self) -> str:
        if self.unreadable is not None:
            return f"a file that is not readable as YAML ({self.unreadable})"
        where = f"{self.namespace}/{self.name}" if self.namespace else self.name
        return f"{self.kind} {where} ({self.api_version})"


def verified_repository(principal: object) -> str | None:
    """The repository a verified GitHub Actions token was issued to.

    None for a request that was not authenticated and for a token from any other
    issuer: neither says which repository it came from.
    """
    claims = getattr(principal, "claims", None)
    if not isinstance(claims, dict) or claims.get("iss") != GITHUB_ACTIONS_ISSUER:
        return None
    repository = claims.get("repository")
    return repository if isinstance(repository, str) and repository else None


def may_generate_raw_iam_kms(
    *,
    system: bool,
    source_repository: str | None,
    config_repo: str,
    system_repository: str | None,
) -> bool:
    """Whether a generation may emit resources in :data:`SYSTEM_ONLY_API_GROUPS`.

    Only a system-mode generation of the configured system repository, asked
    for by that repository, may. System mode alone is not enough: it is a
    property of the role, and which repository the role was granted to is the
    token's to say, not the request's. ``source_repository`` is that verified
    repository. It is not enough either, because what is generated is
    ``config_repo``, which the caller chooses: the system repository's own
    token asking for another repository to be generated as system gets no
    exemption for it. A deployment without authentication has no verified
    repository, so nothing it generates is exempt.
    """
    if not system or source_repository is None or system_repository is None:
        return False
    canonical = _owner_and_name(system_repository)
    if canonical is None:
        return False
    # GitHub names are case-insensitive; the claim carries GitHub's own casing.
    return source_repository.casefold() == canonical and (
        _owner_and_name(config_repo) == canonical
    )


def _owner_and_name(url: str) -> str | None:
    repo = github_repo_from_url(url)
    return None if repo is None else f"{repo.owner}/{repo.name}".casefold()


def generated_tree(output_dir: Path, namespace: str | None) -> dict[str, bytes]:
    """Read what one generation published into an output, and nothing else.

    In namespace mode that is the namespace's directory, which manifest-builder
    clears, writes and commits as a whole, so it is exactly this repository's
    manifests and none of another owner's. A system-mode generation owns the
    roots it records in ``owners/system.toml``. Paths are relative to
    ``output_dir``.
    """
    roots = {namespace} if namespace is not None else _system_roots(output_dir)
    return {
        f"{root}/{path}": content
        for root in sorted(roots)
        for path, content in read_tree(output_dir / root).items()
    }


def find_raw_resources(files: Mapping[str, bytes]) -> tuple[RawResource, ...]:
    """Find the resources in ``files`` only the system repository may deploy.

    Every document of a multi-document file is read, and the items of a list
    kind as well, since either is what a cluster would be asked to apply. A file
    that cannot be read is refused rather than passed: what it holds is unknown.
    """
    found: list[RawResource] = []
    for path in sorted(files):
        try:
            documents = list(yaml.load_all(files[path], Loader=YAML_LOADER))
        except yaml.YAMLError as exc:
            found.append(RawResource(file=path, unreadable=_one_line(str(exc))))
            continue
        for document in _objects(documents):
            api_version = document.get("apiVersion")
            if not isinstance(api_version, str) or not _system_only(api_version):
                continue
            metadata = document.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            namespace = metadata.get("namespace")
            found.append(
                RawResource(
                    file=path,
                    api_version=api_version,
                    kind=str(document.get("kind", "")),
                    namespace=namespace if isinstance(namespace, str) else None,
                    name=str(metadata.get("name", "")),
                )
            )
    return tuple(found)


def refusal_message(
    output: str, resources: Sequence[RawResource], system_repository: str | None
) -> str:
    """Say what was refused in one output, and what to do instead."""
    listed = "; ".join(
        f"{resource.describe()} in {resource.file}" for resource in resources
    )
    return (
        f"{output}: only {system_name(system_repository)} may deploy raw AWS or "
        f"Crossplane resources (in {', '.join(sorted(SYSTEM_ONLY_API_GROUPS))} or "
        f"a group under them), and this change generates {listed}. {REMEDY}"
    )


def system_name(system_repository: str | None) -> str:
    repo = github_repo_from_url(system_repository) if system_repository else None
    if repo is None:
        return "the system repository"
    return f"{repo.owner}/{repo.name}"


def _system_only(api_version: str) -> bool:
    group, _, _version = api_version.strip().rpartition("/")
    group = group.lower()
    return any(
        group == system_only or group.endswith(f".{system_only}")
        for system_only in SYSTEM_ONLY_API_GROUPS
    )


def _objects(
    documents: Sequence[object], seen: set[int] | None = None
) -> Iterator[dict]:
    # YAML anchors can make a list contain itself; each object is visited once.
    seen = set() if seen is None else seen
    for document in documents:
        if not isinstance(document, dict) or id(document) in seen:
            continue
        seen.add(id(document))
        yield document
        items = document.get("items")
        if isinstance(items, list):
            yield from _objects(items, seen)


def _system_roots(output_dir: Path) -> set[str]:
    owner_file = output_dir / "owners" / "system.toml"
    if not owner_file.is_file():
        return set()
    owned = tomllib.loads(owner_file.read_text()).get("owned", [])
    roots = [owned] if isinstance(owned, str) else owned
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise ValueError(f"'owned' must be a string or list of strings in {owner_file}")
    # manifest-builder refuses these as roots too; reading one would read the
    # whole output, other owners' directories included.
    return {r for r in roots if r and "/" not in r and r not in {".", "..", "owners"}}


def _one_line(text: str) -> str:
    return " ".join(text.split())
