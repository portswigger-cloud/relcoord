# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _read_secret_file(path: Path) -> str:
    return path.read_text().rstrip("\r\n")


@dataclass(frozen=True)
class IdmouseSettings:
    url: str
    token_path: Path

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "IdmouseSettings":
        token_path = data.get("token_path", data.get("bearer_token_file"))
        if not isinstance(data.get("url"), str) or not data["url"].strip():
            raise ValueError("persistence.idmouse.url must be a non-empty string")
        if not isinstance(token_path, str) or not token_path.strip():
            raise ValueError(
                "persistence.idmouse.token_path must be a non-empty string"
            )
        return cls(url=data["url"], token_path=Path(token_path))

    def bearer_token(self) -> str:
        return _read_secret_file(self.token_path)


@dataclass(frozen=True)
class PersistenceSettings:
    uri: str
    idmouse: IdmouseSettings
    namespace: str = "default"
    database: str = "relcoord"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PersistenceSettings":
        if not isinstance(data.get("uri"), str) or not data["uri"].strip():
            raise ValueError("persistence.uri must be a non-empty string")

        idmouse_data = data.get("idmouse")
        if idmouse_data is None:
            raise ValueError("persistence.idmouse must be configured")
        if not isinstance(idmouse_data, dict):
            raise ValueError("persistence.idmouse must be a table")

        return cls(
            uri=data["uri"],
            namespace=_string_or_default(data, "namespace", cls.namespace),
            database=_string_or_default(data, "database", cls.database),
            idmouse=IdmouseSettings.from_mapping(idmouse_data),
        )


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    persistence: PersistenceSettings | None = None

    @classmethod
    def from_toml(cls, path: str | Path) -> "Settings":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        persistence = data.get("persistence")
        if persistence is not None and not isinstance(persistence, dict):
            raise ValueError("persistence must be a table")
        return cls(
            host=data.get("host", cls.host),
            port=data.get("port", cls.port),
            persistence=(
                PersistenceSettings.from_mapping(persistence) if persistence else None
            ),
        )


def _string_or_default(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"persistence.{key} must be a non-empty string")
    return value
