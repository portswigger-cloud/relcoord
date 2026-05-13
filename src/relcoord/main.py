# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import asyncio

from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

from relcoord.app import create_app
from relcoord.config import Settings


async def run() -> None:
    settings = Settings.from_env()
    config = HypercornConfig()
    config.bind = [f"{settings.host}:{settings.port}"]
    await serve(create_app(), config)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
