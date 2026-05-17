# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio
import logging

import click
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

from relcoord.app import create_app
from relcoord.config import Settings
from relcoord.in_memory_repository import InMemoryImageVersionRepository
from relcoord.repository import ImageVersionRepository
from relcoord.surreal_repository import SurrealImageVersionRepository

DEFAULT_CONFIG_PATH = "relcoord.toml"
LOG_FORMAT = "[%(asctime)s] [%(process)d] [%(levelname)s] %(name)s: %(message)s"


async def run(config_path: str) -> None:
    settings = Settings.from_toml(config_path)
    config = HypercornConfig()
    config.bind = [f"{settings.host}:{settings.port}"]
    repository = await make_repository(settings)
    try:
        # This has been raised upstream: https://github.com/pgjones/hypercorn/issues/353
        # noinspection PyTypeChecker
        await serve(create_app(repository), config)  # ty: ignore[invalid-argument-type]
    finally:
        close = getattr(repository, "close", None)
        if close is not None:
            await close()


async def make_repository(settings: Settings) -> ImageVersionRepository:
    if settings.persistence is None:
        return InMemoryImageVersionRepository()
    return await SurrealImageVersionRepository.connect(settings.persistence)


@click.command()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to the TOML configuration file.",
)
def main(config_path: str) -> None:
    configure_logging()
    asyncio.run(run(config_path))


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format=LOG_FORMAT,
            datefmt="%Y-%m-%d %H:%M:%S %z",
        )


if __name__ == "__main__":
    main()
