from __future__ import annotations

from dataclasses import dataclass
import re
from functools import total_ordering


SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True)
class PrereleaseIdentifier:
    raw: str

    @property
    def is_numeric(self) -> bool:
        return self.raw.isdigit()

    def sort_key(self) -> tuple[int, int | str]:
        if self.is_numeric:
            return (0, int(self.raw))
        return (1, self.raw)


@total_ordering
@dataclass(frozen=True)
class SemanticVersion:
    original: str
    major: int
    minor: int
    patch: int
    prerelease: tuple[PrereleaseIdentifier, ...]
    build: tuple[str, ...]

    @classmethod
    def parse(cls, value: str) -> "SemanticVersion":
        match = SEMVER_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("version must be valid Semantic Versioning 2.0.0")

        prerelease_value = match.group(4)
        build_value = match.group(5)
        prerelease = tuple(
            PrereleaseIdentifier(raw=identifier)
            for identifier in prerelease_value.split(".")
        ) if prerelease_value else ()
        build = tuple(build_value.split(".")) if build_value else ()
        return cls(
            original=value,
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
            prerelease=prerelease,
            build=build,
        )

    def precedence_key(self) -> tuple[object, ...]:
        prerelease_key: tuple[object, ...]
        if not self.prerelease:
            prerelease_key = (1,)
        else:
            prerelease_key = (0,) + tuple(
                identifier.sort_key() for identifier in self.prerelease
            )
        return (self.major, self.minor, self.patch, prerelease_key)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented

        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (
                other.major,
                other.minor,
                other.patch,
            )

        return self._compare_prerelease(other) < 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.precedence_key() == other.precedence_key()

    def _compare_prerelease(self, other: "SemanticVersion") -> int:
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1

        for left, right in zip(self.prerelease, other.prerelease):
            if left.raw == right.raw:
                continue
            if left.is_numeric and right.is_numeric:
                return -1 if int(left.raw) < int(right.raw) else 1
            if left.is_numeric != right.is_numeric:
                return -1 if left.is_numeric else 1
            return -1 if left.raw < right.raw else 1

        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1
