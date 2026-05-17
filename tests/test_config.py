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
