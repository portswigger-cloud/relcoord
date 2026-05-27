# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from dataclasses import dataclass

import httpx
import pytest

from relcoord.kubernetes import (
    DEPLOY_ID_ANNOTATION,
    DeploymentDetectionError,
    KubernetesDeploymentDetector,
)


@dataclass(frozen=True)
class Ref:
    kind: str
    namespace: str | None
    name: str


def test_deployment_detector_accepts_annotated_changes_and_absent_removed_objects() -> (
    None
):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/v1":
            return httpx.Response(
                200,
                json={
                    "resources": [
                        {
                            "name": "namespaces",
                            "kind": "Namespace",
                            "namespaced": False,
                            "verbs": ["get"],
                        },
                        {
                            "name": "configmaps",
                            "kind": "ConfigMap",
                            "namespaced": True,
                            "verbs": ["get"],
                        },
                    ]
                },
            )
        if request.url.path == "/apis":
            return httpx.Response(
                200,
                json={
                    "groups": [
                        {
                            "name": "apps",
                            "versions": [{"version": "v1"}],
                        }
                    ]
                },
            )
        if request.url.path == "/apis/apps/v1":
            return httpx.Response(
                200,
                json={
                    "resources": [
                        {
                            "name": "deployments",
                            "kind": "Deployment",
                            "namespaced": True,
                            "verbs": ["get", "list"],
                        }
                    ]
                },
            )
        if request.url.path == "/apis/apps/v1/namespaces/default/deployments/api":
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "annotations": {DEPLOY_ID_ANNOTATION: "0123456789abcdef"}
                    }
                },
            )
        if request.url.path == "/api/v1/namespaces/production":
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "annotations": {DEPLOY_ID_ANNOTATION: "0123456789abcdef"}
                    }
                },
            )
        if request.url.path == "/api/v1/namespaces/default/configmaps/old-api":
            return httpx.Response(404, json={"reason": "NotFound"})
        return httpx.Response(500, json={"unexpected": request.url.path})

    detector = KubernetesDeploymentDetector(
        api_url="https://kubernetes.example.test",
        timeout_seconds=0.1,
        interval_seconds=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    detector.wait_for_success(
        deploy_id="0123456789abcdef",
        created_or_modified={
            Ref("Deployment", "default", "api"),
            Ref("Namespace", None, "production"),
        },
        removed={Ref("ConfigMap", "default", "old-api")},
    )

    assert "/apis/apps/v1/namespaces/default/deployments/api" in requests
    assert "/api/v1/namespaces/production" in requests
    assert "/api/v1/namespaces/default/configmaps/old-api" in requests


def test_deployment_detector_times_out_while_annotation_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1":
            return httpx.Response(200, json={"resources": []})
        if request.url.path == "/apis":
            return httpx.Response(
                200,
                json={"groups": [{"name": "apps", "versions": [{"version": "v1"}]}]},
            )
        if request.url.path == "/apis/apps/v1":
            return httpx.Response(
                200,
                json={
                    "resources": [
                        {
                            "name": "deployments",
                            "kind": "Deployment",
                            "namespaced": True,
                            "verbs": ["get"],
                        }
                    ]
                },
            )
        if request.url.path == "/apis/apps/v1/namespaces/default/deployments/api":
            return httpx.Response(200, json={"metadata": {"annotations": {}}})
        return httpx.Response(500, json={"unexpected": request.url.path})

    detector = KubernetesDeploymentDetector(
        api_url="https://kubernetes.example.test",
        timeout_seconds=0,
        interval_seconds=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(DeploymentDetectionError, match="expected '0123456789abcdef'"):
        detector.wait_for_success(
            deploy_id="0123456789abcdef",
            created_or_modified={Ref("Deployment", "default", "api")},
            removed=set(),
        )
