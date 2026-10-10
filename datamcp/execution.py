"""One concurrency slot and deadline for a complete tool/resource operation, including metadata."""

import asyncio

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from .auth import current_email
from .db import QueryError


class ExecutionMiddleware(Middleware):
    def __init__(self, services):
        self.services = services

    async def on_request(self, context, call_next):
        if context.method not in {"tools/call", "resources/read"}:
            return await call_next(context)
        try:
            async with await self.services.throttle(current_email() or "?"):
                async with asyncio.timeout(self.services.settings.query_timeout_s + 3):
                    return await call_next(context)
        except TimeoutError:
            raise ToolError("operation cancelled at its time limit; narrow the date range or request schema only") from None
        except QueryError as error:
            raise ToolError(str(error)) from None
