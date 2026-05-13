# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("RELCOORD_VERSION_SERVICE_HOST", cls.host),
            port=int(os.getenv("RELCOORD_VERSION_SERVICE_PORT", str(cls.port))),
        )
