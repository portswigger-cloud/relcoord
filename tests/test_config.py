# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from pathlib import Path

import pytest

from relcoord.config import Settings


def test_settings_parse_surrealdb_idmouse_config(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    token_file = tmp_path / "idmouse-token"
    token_file.write_text("local-bearer-token\n")
    config.write_text(
        f"""
        host = "127.0.0.1"
        port = 9000

        [persistence]
        uri = "ws://surrealdb:8000/"
        namespace = "default"
        database = "relcoord"

        [persistence.idmouse]
        url = "http://idmouse:9000/token"
        token_path = "{token_file}"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.persistence is not None
    assert settings.persistence.uri == "ws://surrealdb:8000/"
    assert settings.persistence.namespace == "default"
    assert settings.persistence.database == "relcoord"
    assert settings.persistence.idmouse is not None
    assert settings.persistence.idmouse.url == "http://idmouse:9000/token"
    assert settings.persistence.idmouse.bearer_token() == "local-bearer-token"


def test_settings_accepts_idmouse_bearer_token_file_alias(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    token_file = tmp_path / "idmouse-token"
    token_file.write_text("local-bearer-token\n")
    config.write_text(
        f"""
        [persistence]
        uri = "ws://surrealdb:8000/"

        [persistence.idmouse]
        url = "http://idmouse:9000/token"
        bearer_token_file = "{token_file}"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.persistence is not None
    assert settings.persistence.idmouse is not None
    assert settings.persistence.idmouse.token_path == token_file


def test_settings_parses_role_entries(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[role]]
        name = "kubernetes-default"
        audience = "relcoord"
        issuer = "https://kubernetes.default.svc"
        jwks-uri = "https://kubernetes.default.svc/openid/v1/jwks"

        [role.claims]
        sub = "system:serviceaccount:default:default"

        [[role]]
        name = "buildkite"
        audience = "relcoord"
        issuer = "https://agent.buildkite.com"
        jwks-uri = "https://agent.buildkite.com/.well-known/jwks"
        algorithms = ["RS256", "ES256"]
        """
    )

    settings = Settings.from_toml(config)

    assert len(settings.roles) == 2
    assert settings.roles[0].name == "kubernetes-default"
    assert settings.roles[0].jwks_uri == "https://kubernetes.default.svc/openid/v1/jwks"
    assert settings.roles[0].claims == {"sub": "system:serviceaccount:default:default"}
    assert settings.roles[1].algorithms == ("RS256", "ES256")


def test_settings_rejects_duplicate_role_names(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[role]]
        name = "x"
        audience = "relcoord"
        issuer = "https://issuer"
        jwks-uri = "https://issuer/.well-known/jwks.json"

        [[role]]
        name = "x"
        audience = "relcoord"
        issuer = "https://issuer"
        jwks-uri = "https://issuer/.well-known/jwks.json"
        """
    )

    with pytest.raises(ValueError, match="duplicate role 'x'"):
        Settings.from_toml(config)


def test_settings_rejects_persistence_without_idmouse(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [persistence]
        uri = "ws://surrealdb:8000/"
        """
    )

    with pytest.raises(ValueError, match="persistence.idmouse must be configured"):
        Settings.from_toml(config)
