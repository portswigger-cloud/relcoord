# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_toml(cls, path: str | Path) -> "Settings":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(
            host=data.get("host", cls.host),
            port=data.get("port", cls.port),
        )
