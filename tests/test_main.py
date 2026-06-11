# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import asyncio
import logging
from pathlib import Path

import pytest

from relcoord.change import ChangeProcessor
from relcoord.config import OutputSettings, PersistenceSettings, Settings
from relcoord.dynamodb_store import DynamoDBImageInfoStore
from relcoord.in_memory_store import InMemoryImageInfoStore
from relcoord.main import configure_logging, make_change_processor, make_store


def test_make_change_processor_requires_manifests_repository() -> None:
    with pytest.raises(
        RuntimeError,
        match=r"manifests-repository or at least one \[\[output\]\]",
    ):
        make_change_processor(Settings())


def test_make_change_processor_uses_manifests_repository() -> None:
    processor = make_change_processor(
        Settings(
            manifests_repository="https://github.com/acme/manifests.git",
            detect_deployment=True,
        )
    )

    assert isinstance(processor, ChangeProcessor)
    assert processor.manifests_repository == "https://github.com/acme/manifests.git"
    assert processor.detect_deployment is True


def test_make_change_processor_uses_outputs() -> None:
    processor = make_change_processor(
        Settings(
            outputs=[
                OutputSettings(
                    name="example-dev",
                    repository="https://github.com/acme/manifests.git",
                    directory=Path("example-dev"),
                    vars={"cluster_name": "example-dev"},
                )
            ]
        )
    )

    assert isinstance(processor, ChangeProcessor)
    assert processor.manifests_repository is None
    assert processor.outputs[0].name == "example-dev"


def test_make_store_uses_in_memory_backend() -> None:
    store = asyncio.run(
        make_store(Settings(persistence=PersistenceSettings(backend="in-memory")))
    )

    assert isinstance(store, InMemoryImageInfoStore)


def test_make_store_uses_dynamodb_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = InMemoryImageInfoStore()

    async def connect(config: PersistenceSettings) -> InMemoryImageInfoStore:
        assert config.table_name == "relcoord-image-versions"
        return expected

    monkeypatch.setattr(DynamoDBImageInfoStore, "connect", connect)

    store = asyncio.run(
        make_store(
            Settings(
                persistence=PersistenceSettings(
                    backend="dynamodb",
                    table_name="relcoord-image-versions",
                )
            )
        )
    )

    assert store is expected


def test_configure_logging_uses_configured_log_level() -> None:
    root_logger = logging.getLogger()
    original_level = root_logger.level

    try:
        configure_logging("WARNING")

        assert root_logger.level == logging.WARNING
    finally:
        root_logger.setLevel(original_level)
