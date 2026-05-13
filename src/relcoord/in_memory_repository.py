# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from relcoord.errors import EquivalentVersionExistsError
from relcoord.models import RegisterResult, StoredVersion
from relcoord.repository import ImageVersionRepository
from relcoord.semver import SemanticVersion


class InMemoryImageVersionRepository(ImageVersionRepository):
    def __init__(self) -> None:
        self._versions_by_image: dict[str, dict[str, StoredVersion]] = {}
        self._precedence_index: dict[str, dict[tuple[object, ...], str]] = {}

    async def register(
        self, image: str, semantic_version: SemanticVersion
    ) -> RegisterResult:
        image_versions = self._versions_by_image.setdefault(image, {})
        if semantic_version.original in image_versions:
            return RegisterResult(
                image=image, version=semantic_version.original, created=False
            )

        precedence_index = self._precedence_index.setdefault(image, {})
        precedence_key = semantic_version.precedence_key()
        existing_version = precedence_index.get(precedence_key)
        if existing_version is not None:
            raise EquivalentVersionExistsError(
                image=image,
                existing_version=existing_version,
                requested_version=semantic_version.original,
            )

        image_versions[semantic_version.original] = StoredVersion(
            image=image,
            version=semantic_version.original,
            semantic_version=semantic_version,
        )
        precedence_index[precedence_key] = semantic_version.original
        return RegisterResult(
            image=image, version=semantic_version.original, created=True
        )

    async def latest_for_image(self, image: str) -> str | None:
        image_versions = self._versions_by_image.get(image)
        if not image_versions:
            return None
        latest = max(
            image_versions.values(),
            key=lambda stored: stored.semantic_version,
        )
        return latest.version
