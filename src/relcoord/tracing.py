# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""OpenTelemetry tracing, active only when a [tracing] endpoint is configured.

Code is instrumented through :func:`span` unconditionally. Until
:func:`configure_tracing` installs a provider the OpenTelemetry API hands out
non-recording spans, so an unconfigured deployment pays next to nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from relcoord.config import TracingSettings
from relcoord.version import relcoord_version

logger = logging.getLogger(__name__)

SERVICE_NAME = "relcoord"

_tracer = trace.get_tracer(SERVICE_NAME)


@contextmanager
def span(name: str, **attributes: Any) -> Generator[Span]:
    """Run a block inside a child span of the current one.

    An exception leaving the block is recorded on the span and marks it failed.
    Attributes whose value is None are left out.
    """
    with _tracer.start_as_current_span(
        name,
        attributes={k: v for k, v in attributes.items() if v is not None},
    ) as current:
        yield current


def set_attributes(**attributes: Any) -> None:
    """Add attributes to the current span; None values are left out."""
    current = trace.get_current_span()
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value)


def mark_error(message: str) -> None:
    """Mark the current span failed without an exception, for a handled error."""
    current = trace.get_current_span()
    current.set_status(Status(StatusCode.ERROR, message))


def configure_tracing(settings: TracingSettings | None) -> TracerProvider | None:
    """Install an OTLP exporting tracer provider, if tracing is configured."""
    if settings is None:
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": SERVICE_NAME, "service.version": relcoord_version()}
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.endpoint,
                insecure=settings.endpoint.startswith("http://"),
            )
        )
    )
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    logger.info("exporting traces to %s", settings.endpoint)
    return provider


def annotate_asgi_span(span: Span, scope: dict[str, Any], message: Any) -> None:
    """Record where a request went on the ASGI receive and send spans.

    Those internal spans carry only the ASGI event type by default, which says
    nothing about which request they belong to once a trace is opened on its
    own. The query string is left out of the URL.
    """
    if not span.is_recording():
        return
    host = next(
        (
            value.decode("latin-1")
            for name, value in scope.get("headers", ())
            if name == b"host"
        ),
        None,
    )
    path = scope.get("path")
    span.set_attribute("url.path", path or "")
    if host is not None:
        span.set_attribute("server.address", host)
        span.set_attribute("url.full", f"{scope.get('scheme', 'http')}://{host}{path}")
