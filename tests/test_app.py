from starlette.testclient import TestClient

from relcoord.app import create_app


def test_healthz() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_and_resolve_latest_version() -> None:
    client = TestClient(create_app())

    created = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3"},
    )
    latest = client.post(
        "/v1/images/latest",
        json={"images": ["registry.example.com/team/api", "registry.example.com/team/worker"]},
    )

    assert created.status_code == 201
    assert created.json() == {
        "image": "registry.example.com/team/api",
        "version": "1.2.3",
        "created": True,
    }
    assert latest.status_code == 200
    assert latest.json() == {
        "versions": {
            "registry.example.com/team/api": "1.2.3",
            "registry.example.com/team/worker": None,
        }
    }


def test_reject_invalid_version() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "not-semver"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_version",
        "message": "version must be valid Semantic Versioning 2.0.0",
    }


def test_reject_build_metadata_only_variant() -> None:
    client = TestClient(create_app())

    first = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3+build1"},
    )
    second = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3+build2"},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"] == "conflicting_version"
