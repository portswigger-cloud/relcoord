# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from relcoord.semver import SemanticVersion


@dataclass(frozen=True)
class StoredVersion:
    image: str
    version: str
    semantic_version: SemanticVersion


@dataclass(frozen=True)
class RegisterResult:
    image: str
    version: str
    created: bool


@dataclass(frozen=True)
class LatestVersionResult:
    image: str
    version: Optional[str]
