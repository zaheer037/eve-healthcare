"""The in-process payment provider (architecture §6.1).

It answers at once and does no I/O, so `pay` fits in one short transaction and no lock spans a
network call. A real gateway needs the two-phase flow of architecture §6.6, and this class is
the one boundary that then gets a second implementation.
"""

import random
import uuid
from collections.abc import Callable

from app.models.payment import PaymentStatus
from app.payments.settlement import PaymentResult

FAILURE_REASON = "declined_by_simulator"


class PaymentSimulator:
    """A stand-in for a provider. The dice are Python's `random`: a result is not a secret."""

    def __init__(self, success_rate: float, roll: Callable[[], float] = random.random) -> None:
        self._success_rate = success_rate
        self._roll = roll

    def charge(self, payment_id: uuid.UUID, override: PaymentStatus | None) -> PaymentResult | None:
        """The provider's answer for one payment, or `None` when it will confirm later.

        `override` forces the result: `SUCCESS`, `FAILED`, or `PENDING` for "the provider confirms
        by webhook". Without it, the dice decide with the configured success rate. The reference
        names the payment, so a webhook event that carries the same one is a no-op
        (architecture §4.3).
        """
        if override is PaymentStatus.PENDING:
            return None
        succeeded = (
            override is PaymentStatus.SUCCESS
            if override is not None
            else self._roll() < self._success_rate
        )
        reference = f"sim_{payment_id.hex}"
        if succeeded:
            return PaymentResult(PaymentStatus.SUCCESS, reference)
        return PaymentResult(PaymentStatus.FAILED, reference, FAILURE_REASON)
