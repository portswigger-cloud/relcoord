# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from relcoord.auth import KUBERNETES_CA_CERT_PATH, KUBERNETES_TOKEN_PATH

logger = logging.getLogger(__name__)

KUBERNETES_API_URL = "https://kubernetes.default.svc"
DEPLOY_ID_ANNOTATION = "noa.re/deploy-id"


class KubernetesObjectRef(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def namespace(self) -> str | None: ...
    @property
    def name(self) -> str: ...


class DeploymentDetectionError(Exception):
    pass


class KubernetesObjectNotFound(Exception):
    pass


@dataclass(frozen=True)
class KubernetesResource:
    path_prefix: str
    name: str
    namespaced: bool

    def object_path(self, ref: KubernetesObjectRef) -> str:
        if self.namespaced:
            if ref.namespace is None:
                raise DeploymentDetectionError(
                    f"{ref.kind}/{ref.name} must have a namespace"
                )
            return (
                f"{self.path_prefix}/namespaces/{ref.namespace}/{self.name}/{ref.name}"
            )
        return f"{self.path_prefix}/{self.name}/{ref.name}"


class KubernetesDeploymentDetector:
    def __init__(
        self,
        *,
        api_url: str = KUBERNETES_API_URL,
        timeout_seconds: float = 300,
        interval_seconds: float = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._interval_seconds = interval_seconds
        self._client = client or httpx.Client(**_kubernetes_request_options())
        self._owns_client = client is None
        self._resources_by_kind: dict[str, list[KubernetesResource]] | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def wait_for_success(
        self,
        *,
        deploy_id: str,
        created_or_modified: set[KubernetesObjectRef],
        removed: set[KubernetesObjectRef],
    ) -> None:
        deadline = time.monotonic() + self._timeout_seconds
        pending: list[str] = []
        while True:
            pending = self._pending(deploy_id, created_or_modified, removed)
            if not pending:
                return
            if time.monotonic() >= deadline:
                waiting_for = "; ".join(pending)
                raise DeploymentDetectionError(
                    "deployment detection timed out after "
                    f"{self._timeout_seconds:g}s waiting for {waiting_for}"
                )
            logger.info(
                "deployment detection waiting for %d Kubernetes object(s): %s",
                len(pending),
                "; ".join(pending),
            )
            time.sleep(self._interval_seconds)

    def _pending(
        self,
        deploy_id: str,
        created_or_modified: set[KubernetesObjectRef],
        removed: set[KubernetesObjectRef],
    ) -> list[str]:
        pending = []
        for ref in sorted(created_or_modified, key=_object_ref_sort_key):
            observed = self._matching_deploy_id(ref, deploy_id)
            if observed is True:
                continue
            if observed is None:
                pending.append(f"{_format_ref(ref)} has not appeared")
            else:
                pending.append(
                    f"{_format_ref(ref)} has deploy-id {observed!r}, expected {deploy_id!r}"
                )

        for ref in sorted(removed, key=_object_ref_sort_key):
            if self._exists(ref):
                pending.append(f"{_format_ref(ref)} still exists")
        return pending

    def _matching_deploy_id(
        self, ref: KubernetesObjectRef, deploy_id: str
    ) -> bool | str | None:
        observed: str | None = None
        found = False
        for resource in self._resources(ref):
            try:
                obj = self._get(resource.object_path(ref))
            except KubernetesObjectNotFound:
                continue
            found = True
            annotations = obj.get("metadata", {}).get("annotations", {})
            value = annotations.get(DEPLOY_ID_ANNOTATION)
            if value == deploy_id:
                return True
            observed = value
        if not found:
            return None
        return observed if observed is not None else "<missing>"

    def _exists(self, ref: KubernetesObjectRef) -> bool:
        for resource in self._resources(ref):
            try:
                self._get(resource.object_path(ref))
            except KubernetesObjectNotFound:
                continue
            return True
        return False

    def _resources(self, ref: KubernetesObjectRef) -> list[KubernetesResource]:
        resources = self._matching_resources(ref)
        if resources:
            return resources
        self._resources_by_kind = self._discover_resources()
        return self._matching_resources(ref)

    def _matching_resources(self, ref: KubernetesObjectRef) -> list[KubernetesResource]:
        return [
            resource
            for resource in self._resources_by_kind_cached().get(ref.kind, [])
            if resource.namespaced == (ref.namespace is not None)
        ]

    def _resources_by_kind_cached(self) -> dict[str, list[KubernetesResource]]:
        if self._resources_by_kind is None:
            self._resources_by_kind = self._discover_resources()
        return self._resources_by_kind

    def _discover_resources(self) -> dict[str, list[KubernetesResource]]:
        resources: dict[str, list[KubernetesResource]] = {}
        core = self._get("/api/v1")
        _add_resources(resources, "/api/v1", core)

        apis = self._get("/apis")
        for group in apis.get("groups", []):
            group_name = group.get("name")
            for version in group.get("versions", []):
                version_name = version.get("version")
                if not isinstance(group_name, str) or not isinstance(version_name, str):
                    continue
                path_prefix = f"/apis/{group_name}/{version_name}"
                _add_resources(resources, path_prefix, self._get(path_prefix))
        return resources

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self._client.get(f"{self._api_url}{path}")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise KubernetesObjectNotFound() from exc
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} failed with status "
                f"{exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} failed: {exc}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise DeploymentDetectionError(
                f"Kubernetes API GET {path} did not return a JSON object"
            )
        return data


def _kubernetes_request_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    if KUBERNETES_CA_CERT_PATH.exists():
        options["verify"] = KUBERNETES_CA_CERT_PATH
    if KUBERNETES_TOKEN_PATH.exists():
        token = KUBERNETES_TOKEN_PATH.read_text().strip()
        options["headers"] = {"Authorization": f"Bearer {token}"}
    return options


def _add_resources(
    resources: dict[str, list[KubernetesResource]],
    path_prefix: str,
    resource_list: dict[str, Any],
) -> None:
    for resource in resource_list.get("resources", []):
        name = resource.get("name")
        kind = resource.get("kind")
        namespaced = resource.get("namespaced")
        verbs = resource.get("verbs", [])
        if (
            not isinstance(name, str)
            or "/" in name
            or not isinstance(kind, str)
            or not isinstance(namespaced, bool)
            or "get" not in verbs
        ):
            continue
        resources.setdefault(kind, []).append(
            KubernetesResource(
                path_prefix=path_prefix,
                name=name,
                namespaced=namespaced,
            )
        )


def _format_ref(ref: KubernetesObjectRef) -> str:
    if ref.namespace is None:
        return f"{ref.kind}/{ref.name}"
    return f"{ref.kind}/{ref.namespace}/{ref.name}"


def _object_ref_sort_key(ref: KubernetesObjectRef) -> tuple[str, str, str]:
    return (ref.kind, ref.namespace or "", ref.name)
