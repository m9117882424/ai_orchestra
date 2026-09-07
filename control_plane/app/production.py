from __future__ import annotations

import re

from .main import app as control_app


_EXECUTION_REFRESH_RE = re.compile(r"^/api/executions/[^/]+/refresh$")


class ObserverOnlyExecutionLifecycle:
    """Keep legacy manager UI refresh calls read-only in production.

    Older UI builds POST to /refresh while polling. Production rewrites those calls
    to the authenticated execution-list GET before FastAPI routing, so cached clients
    keep working without owning lifecycle state. The mutating core route has been
    removed; only `execution-worker` can commit completion.
    """

    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and _EXECUTION_REFRESH_RE.fullmatch(scope.get("path") or "")
        ):
            scope = dict(scope)
            scope["method"] = "GET"
            scope["path"] = "/api/executions"
            scope["raw_path"] = b"/api/executions"
            scope["query_string"] = b"limit=100"
        await self.wrapped(scope, receive, send)


app = ObserverOnlyExecutionLifecycle(control_app)
