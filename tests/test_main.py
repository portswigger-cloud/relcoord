# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
import logging

from relcoord.main import configure_logging


def test_configure_logging_sets_info_level() -> None:
    configure_logging()

    assert logging.getLogger().getEffectiveLevel() == logging.INFO
