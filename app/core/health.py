"""Liveness and readiness probes. Compose and Docker use them (spec section 10.2)."""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.db import SessionDep
from app.core.errors import ServiceUnavailable
from app.core.routing import TolerantRouter

router = TolerantRouter(prefix="/health", tags=["health"])


class HealthRead(BaseModel):
    status: Literal["ok"]


@router.get("/live", summary="The process is up")
async def health_live() -> HealthRead:
    # async def on purpose: no thread is needed, so the probe still answers when every
    # thread of the pool is busy. Blocking work never belongs in this handler.
    return HealthRead(status="ok")


@router.get("/ready", summary="The database answers")
def health_ready(session: SessionDep) -> HealthRead:
    try:
        session.execute(text("SELECT 1"))
    except (OperationalError, InterfaceError, PoolTimeoutError) as error:
        raise ServiceUnavailable("The database does not answer.") from error
    return HealthRead(status="ok")
