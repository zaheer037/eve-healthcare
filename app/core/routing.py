"""A router that answers both slash spellings and HEAD, and never redirects."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter


class TolerantRouter(APIRouter):
    """Serve every route with and without the trailing slash, and answer HEAD wherever GET works.

    The assignment fixes `/payments/` and `/payments/webhook/`. A sender such as a payment provider
    treats a redirect as a failed delivery, so the other spelling is a second route, not a 307.
    Only the canonical spelling appears in OpenAPI. The app must also set
    `redirect_slashes=False`.
    """

    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        super().add_api_route(path, endpoint, **kwargs)
        hidden = {**kwargs, "include_in_schema": False}
        has_get = "GET" in (kwargs.get("methods") or ())
        if has_get:
            super().add_api_route(path, endpoint, **{**hidden, "methods": ["HEAD"]})
        alternate = path[:-1] if path.endswith("/") else path + "/"
        if alternate != path and (
            alternate or self.prefix
        ):  # "" is a valid path only under a prefix
            super().add_api_route(alternate, endpoint, **hidden)
            if has_get:
                super().add_api_route(alternate, endpoint, **{**hidden, "methods": ["HEAD"]})
