# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from collections.abc import Generator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from relcoord import tracing
from relcoord.config import TracingSettings


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Generator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


def test_span_nests_and_drops_none_attributes(
    exporter: InMemorySpanExporter,
) -> None:
    with tracing.span("outer", kept="yes", dropped=None), tracing.span("inner"):
        tracing.set_attributes(extra=1, skipped=None)

    inner, outer = exporter.get_finished_spans()
    assert (inner.name, outer.name) == ("inner", "outer")
    assert inner.parent is not None
    assert inner.parent.span_id == outer.context.span_id
    assert dict(outer.attributes or {}) == {"kept": "yes"}
    assert dict(inner.attributes or {}) == {"extra": 1}


def test_span_records_exception(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), tracing.span("failing"):
        raise RuntimeError("boom")

    (finished,) = exporter.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR
    assert [event.name for event in finished.events] == ["exception"]


def test_span_without_provider_is_harmless() -> None:
    with tracing.span("untraced"):
        tracing.set_attributes(a=1)
        tracing.mark_error("ignored")
    assert not trace.get_current_span().get_span_context().is_valid


def test_configure_tracing_does_nothing_without_settings() -> None:
    assert tracing.configure_tracing(None) is None


def test_configure_tracing_builds_provider() -> None:
    provider = tracing.configure_tracing(TracingSettings(endpoint="http://localhost:1"))
    try:
        assert provider is not None
    finally:
        if provider is not None:
            provider.shutdown()


def test_annotate_asgi_span_records_host_and_path(
    exporter: InMemorySpanExporter,
) -> None:
    scope = {
        "scheme": "https",
        "path": "/v1/change",
        "query_string": b"secret=1",
        "headers": [(b"host", b"relcoord.example.com")],
    }
    with tracing.span("POST /v1/change http receive") as current:
        tracing.annotate_asgi_span(current, scope, {})

    (finished,) = exporter.get_finished_spans()
    assert dict(finished.attributes or {}) == {
        "url.path": "/v1/change",
        "server.address": "relcoord.example.com",
        "url.full": "https://relcoord.example.com/v1/change",
    }
