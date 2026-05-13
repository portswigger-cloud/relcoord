from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Optional

from relcoord.models import RegisterResult
from relcoord.semver import SemanticVersion


class ImageVersionRepository(ABC):
    @abstractmethod
    async def register(self, image: str, semantic_version: SemanticVersion) -> RegisterResult:
        raise NotImplementedError

    @abstractmethod
    async def latest_for_image(self, image: str) -> Optional[str]:
        raise NotImplementedError

    async def latest_for_images(self, images: Iterable[str]) -> dict[str, Optional[str]]:
        results: dict[str, Optional[str]] = {}
        for image in images:
            if image not in results:
                results[image] = await self.latest_for_image(image)
        return results
