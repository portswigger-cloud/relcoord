# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)
ALLOWED_ALGORITHMS: frozenset[str] = frozenset(
    {
        "HS256",
        "HS384",
        "HS512",
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "EdDSA",
    }
)


class AuthError(Exception):
    """Raised when a bearer token cannot be validated against any role."""


@dataclass(frozen=True)
class RoleConfig:
    name: str
    audience: str
    issuer: str
    jwks_uri: str
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    claims: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RoleConfig":
        name = _required_string(data, "name", context="role")
        audience = _required_string(data, "audience", context=f"role '{name}'")
        issuer = _required_string(data, "issuer", context=f"role '{name}'")
        jwks_uri = _required_string(
            data, "jwks-uri", context=f"role '{name}'", alias="jwks_uri"
        )
        algorithms_value = data.get("algorithms", list(DEFAULT_ALGORITHMS))
        if not isinstance(algorithms_value, list) or not algorithms_value:
            raise ValueError(
                f"role '{name}' algorithms must be a non-empty array of strings"
            )
        algorithms: list[str] = []
        for entry in algorithms_value:
            if not isinstance(entry, str) or entry not in ALLOWED_ALGORITHMS:
                raise ValueError(f"role '{name}' algorithm '{entry}' is not supported")
            algorithms.append(entry)

        raw_claims = data.get("claims", {})
        if not isinstance(raw_claims, dict):
            raise ValueError(f"role '{name}' claims must be a table")
        claims: dict[str, str] = {}
        for key, value in raw_claims.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"role '{name}' claim names must be non-empty strings")
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"role '{name}' claim '{key}' must be a non-empty string"
                )
            claims[key] = value
        return cls(
            name=name,
            audience=audience,
            issuer=issuer,
            jwks_uri=jwks_uri,
            algorithms=tuple(algorithms),
            claims=claims,
        )


@dataclass(frozen=True)
class ValidatedClaims:
    role: str
    claims: dict[str, Any]

    @property
    def subject(self) -> str:
        sub = self.claims.get("sub")
        return sub if isinstance(sub, str) else ""


class TokenValidator:
    def __init__(self, roles: list[RoleConfig]) -> None:
        if not roles:
            raise ValueError("TokenValidator requires at least one role")
        seen: set[str] = set()
        for role in roles:
            if role.name in seen:
                raise ValueError(f"duplicate role '{role.name}'")
            seen.add(role.name)
        self._roles = list(roles)
        self._clients: dict[str, PyJWKClient] = {
            role.name: PyJWKClient(role.jwks_uri, cache_keys=True) for role in roles
        }

    def validate(self, bearer_token: str) -> ValidatedClaims:
        if not bearer_token:
            raise AuthError("empty bearer token")
        last_error: Exception | None = None
        for role in self._roles:
            client = self._clients[role.name]
            try:
                signing_key = client.get_signing_key_from_jwt(bearer_token)
                claims = jwt.decode(
                    bearer_token,
                    key=signing_key.key,
                    algorithms=list(role.algorithms),
                    audience=role.audience,
                    issuer=role.issuer,
                    options={"require": ["exp", "iat", "iss", "aud"]},
                )
            except (InvalidTokenError, PyJWKClientError) as exc:
                last_error = exc
                continue
            if not _claims_match(claims, role.claims):
                last_error = AuthError(
                    f"required claims for role '{role.name}' do not match"
                )
                continue
            return ValidatedClaims(role=role.name, claims=claims)
        raise AuthError(
            f"token did not validate against any configured role: {last_error}"
        )


def extract_bearer_token(authorization_header: str | None) -> str:
    if authorization_header is None:
        raise AuthError("missing Authorization header")
    if not authorization_header.startswith("Bearer "):
        raise AuthError("expected a Bearer token")
    token = authorization_header[len("Bearer ") :].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def _claims_match(token_claims: dict[str, Any], required: dict[str, str]) -> bool:
    for key, expected in required.items():
        if token_claims.get(key) != expected:
            return False
    return True


def _required_string(
    data: dict[str, Any], key: str, *, context: str, alias: str | None = None
) -> str:
    value = data.get(key)
    if value is None and alias is not None:
        value = data.get(alias)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} {key} must be a non-empty string")
    return value
