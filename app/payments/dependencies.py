"""The webhook's raw-body dependency (architecture §§2.4 and 6.5)."""

import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core.clock import utcnow
from app.core.config import Settings
from app.payments.schemas import WebhookPayload
from app.payments.signature import verify

log = structlog.get_logger()
SIGNATURE_HEADER = "x-webhook-signature"


async def verified_webhook_payload(request: Request) -> WebhookPayload:
    """Verify the signature on the raw bytes, then parse the body.

    The route declares no body parameter, so FastAPI never parses it: a bad signature is a 401
    even when the JSON is invalid. A body that fails validation is not an event yet. It becomes an
    ordinary 422 (a raw `ValidationError` raised in a dependency would be a 500), nothing is
    stored, and the log names the failing fields and no values.
    """
    settings: Settings = request.app.state.settings
    body = await request.body()
    if settings.webhook_signature_required:
        verify(
            settings.webhook_secret.get_secret_value(),
            request.headers.get(SIGNATURE_HEADER),
            body,
            now=utcnow(),
            tolerance_seconds=settings.webhook_tolerance_seconds,
        )
    try:
        return WebhookPayload.model_validate_json(body)
    except ValidationError as error:
        details = error.errors(include_url=False, include_context=False, include_input=False)
        # The same `loc` that FastAPI gives a body error, so the client sees an ordinary 422.
        problems = [{**item, "loc": ("body", *item["loc"])} for item in details]
        fields = [".".join(str(part) for part in ("body", *item["loc"])) for item in details]
        log.warning("webhook.invalid", fields=fields)
        raise RequestValidationError(problems) from None
