# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio

import pytest

from relcoord.errors import EquivalentVersionExistsError
from relcoord.in_memory_repository import InMemoryImageVersionRepository
from relcoord.service import ImageVersionService


def test_register_version_is_idempotent() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    created = asyncio.run(
        service.register_version("registry.example.com/team/api", "1.2.3")
    )
    duplicate = asyncio.run(
        service.register_version("registry.example.com/team/api", "1.2.3")
    )

    assert created.created is True
    assert duplicate.created is False


def test_register_rejects_build_metadata_only_variant() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    asyncio.run(
        service.register_version("registry.example.com/team/api", "1.2.3+build1")
    )

    with pytest.raises(EquivalentVersionExistsError):
        asyncio.run(
            service.register_version("registry.example.com/team/api", "1.2.3+build2")
        )


def test_latest_versions_returns_highest_precedence() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    asyncio.run(service.register_version("registry.example.com/team/api", "1.2.3-rc.1"))
    asyncio.run(service.register_version("registry.example.com/team/api", "1.2.3"))

    versions = asyncio.run(service.latest_versions(["registry.example.com/team/api"]))

    assert versions == {"registry.example.com/team/api": "1.2.3"}


def test_latest_versions_allows_empty_image_lists() -> None:
    service = ImageVersionService(InMemoryImageVersionRepository())

    assert asyncio.run(service.latest_versions([])) == {}
