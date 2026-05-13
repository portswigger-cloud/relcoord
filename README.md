# relcoord version service

Small HTTP service for registering image versions and resolving the latest known version per image.

## Development

Install dependencies with `uv`:

```bash
uv sync --extra dev
```

Run the test suite:

```bash
uv run pytest
```

Start the service locally:

```bash
uv run relcoord
```
