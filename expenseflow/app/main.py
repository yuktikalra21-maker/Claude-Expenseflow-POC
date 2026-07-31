"""FastAPI application entry point for ExpenseFlow.

Thin composition root (ARCHITECTURE.md §3): create the app, ensure database
tables exist on startup, and register the expenses router. Configuration and
``.env`` loading happen in :mod:`app.db`; business logic lives in
:mod:`app.routes`.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.db import init_db
from app.routes import reports_router, router as expenses_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create database tables before the app starts serving requests."""
    init_db()
    yield


# ``docs_url=None`` disables the built-in Swagger page so we can serve our own at
# ``/docs`` below (the OpenAPI schema and ReDoc are left at their defaults).
app = FastAPI(
    title="ExpenseFlow API",
    description="A small expense submission and approval API (PoC).",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
)

app.include_router(expenses_router)
app.include_router(reports_router)


@app.get("/docs", include_in_schema=False)
def custom_swagger_ui() -> HTMLResponse:
    """Serve Swagger UI that auto-attaches ``X-API-Key`` from the server's env.

    Reads ``API_KEY`` on the server and injects it into a Swagger
    ``requestInterceptor`` so write endpoints work in ``/docs`` without a manual
    key prompt. Local-dev convenience only: the configured key is embedded in
    this page's JavaScript, so do not expose ``/docs`` (or use a real key)
    outside local use.
    """
    openapi_url = app.openapi_url or "/openapi.json"
    api_key = os.getenv("API_KEY", "")
    key_json = json.dumps(api_key)  # safely escaped for embedding in JS
    note = (
        "🔑 Writes are auto-authenticated with the server's API_KEY (local dev)."
        if api_key
        else "⚠️ API_KEY is not set on the server; writes will return 401."
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ExpenseFlow API — Swagger UI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <p style="font-family: sans-serif; padding: 8px 16px; margin: 0; background: #f6f6f6;">{note}</p>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
  <script>
    window.onload = function () {{
      window.ui = SwaggerUIBundle({{
        url: '{openapi_url}',
        dom_id: '#swagger-ui',
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
        layout: 'BaseLayout',
        deepLinking: true,
        requestInterceptor: function (request) {{
          var apiKey = {key_json};
          if (apiKey) {{ request.headers['X-API-Key'] = apiKey; }}
          return request;
        }}
      }});
    }};
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness probe returning a static OK payload."""
    return {"status": "ok"}
