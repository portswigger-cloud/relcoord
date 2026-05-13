# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from relcoord.errors import EquivalentVersionExistsError, ValidationError
from relcoord.in_memory_repository import InMemoryImageVersionRepository
from relcoord.repository import ImageVersionRepository
from relcoord.service import ImageVersionService


def create_app(repository: ImageVersionRepository | None = None) -> Starlette:
    repo = repository or InMemoryImageVersionRepository()
    service = ImageVersionService(repository=repo)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def register_image_version(request: Request) -> Response:
        try:
            payload = await _read_json(request)
            image = payload.get("image")
            version = payload.get("version")
            result = await service.register_version(image=image, version=version)
        except ValidationError as exc:
            return _json_error(status_code=400, error=exc.error, message=exc.message)
        except EquivalentVersionExistsError as exc:
            return _json_error(
                status_code=409,
                error="conflicting_version",
                message=str(exc),
            )

        status_code = 201 if result.created else 200
        return JSONResponse(
            {
                "image": result.image,
                "version": result.version,
                "created": result.created,
            },
            status_code=status_code,
        )

    async def latest_versions(request: Request) -> Response:
        try:
            payload = await _read_json(request)
            images = payload.get("images")
            versions = await service.latest_versions(images=images)
        except ValidationError as exc:
            return _json_error(status_code=400, error=exc.error, message=exc.message)

        return JSONResponse({"versions": versions})

    return Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/v1/image-versions", register_image_version, methods=["POST"]),
            Route("/v1/images/latest", latest_versions, methods=["POST"]),
        ]
    )


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise ValidationError(
            error="invalid_json",
            message="request body must be valid JSON",
        )

    if not isinstance(payload, dict):
        raise ValidationError(
            error="invalid_json",
            message="request body must be a JSON object",
        )
    return payload


def _json_error(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "message": message},
        status_code=status_code,
    )
