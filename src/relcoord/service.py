# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from relcoord.errors import ValidationError
from relcoord.models import RegisterResult
from relcoord.repository import ImageVersionRepository
from relcoord.semver import SemanticVersion


class ImageVersionService:
    def __init__(self, repository: ImageVersionRepository) -> None:
        self._repository = repository

    async def register_version(self, image: str, version: str) -> RegisterResult:
        validated_image = self._validate_image(image)
        semantic_version = self._validate_version(version)
        return await self._repository.register(validated_image, semantic_version)

    async def latest_versions(self, images: list[str]) -> dict[str, str | None]:
        if not isinstance(images, list):
            raise ValidationError(
                error="invalid_images",
                message="images must be an array of non-empty strings",
            )

        validated_images = [self._validate_image(image) for image in images]
        return await self._repository.latest_for_images(validated_images)

    def _validate_image(self, image: str) -> str:
        if not isinstance(image, str) or not image.strip():
            raise ValidationError(
                error="invalid_image",
                message="image must be a non-empty string",
            )
        return image

    def _validate_version(self, version: str) -> SemanticVersion:
        if not isinstance(version, str) or not version.strip():
            raise ValidationError(
                error="invalid_version",
                message="version must be a non-empty string",
            )
        try:
            return SemanticVersion.parse(version)
        except ValueError as exc:
            raise ValidationError(
                error="invalid_version",
                message=str(exc),
            ) from exc
