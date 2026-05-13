# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from relcoord.semver import SemanticVersion


def test_release_sorts_after_prerelease() -> None:
    assert SemanticVersion.parse("1.2.0-rc.1") < SemanticVersion.parse("1.2.0")


def test_build_metadata_does_not_change_precedence() -> None:
    assert SemanticVersion.parse("1.2.0+build5") == SemanticVersion.parse(
        "1.2.0+build7"
    )


def test_numeric_identifiers_sort_before_alphanumeric() -> None:
    assert SemanticVersion.parse("1.2.0-1") < SemanticVersion.parse("1.2.0-alpha")
