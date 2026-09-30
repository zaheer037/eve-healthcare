"""The in-process payment simulator (spec section 8.1). No I/O, so a test chooses its answer."""

import random
import uuid

import pytest

from app.models.payment import PaymentStatus
from app.payments.settlement import PaymentResult
from app.payments.simulator import FAILURE_REASON, PaymentSimulator

PAYMENT_ID = uuid.UUID("0d4b7a52-3f1e-4d0a-9b8c-2f6a1c7e9d10")
REFERENCE = "sim_0d4b7a523f1e4d0a9b8c2f6a1c7e9d10"


def simulator(rate: float = 0.8, roll: float = 0.0) -> PaymentSimulator:
    return PaymentSimulator(rate, roll=lambda: roll)


# ==== a forced result


def test_a_forced_success_carries_a_reference_that_names_the_payment() -> None:
    result = simulator(rate=0.0).charge(PAYMENT_ID, PaymentStatus.SUCCESS)
    assert result == PaymentResult(PaymentStatus.SUCCESS, REFERENCE)


def test_a_forced_failure_carries_a_reason() -> None:
    result = simulator(rate=1.0).charge(PAYMENT_ID, PaymentStatus.FAILED)
    assert result == PaymentResult(PaymentStatus.FAILED, REFERENCE, FAILURE_REASON)


def test_a_forced_pending_is_no_answer_at_all() -> None:
    """The provider will confirm later by webhook."""
    assert simulator().charge(PAYMENT_ID, PaymentStatus.PENDING) is None


def test_a_forced_result_ignores_the_dice() -> None:
    always_fails = simulator(rate=0.0, roll=0.99)
    result = always_fails.charge(PAYMENT_ID, PaymentStatus.SUCCESS)
    assert result is not None
    assert result.status is PaymentStatus.SUCCESS


# ==== the success rate


@pytest.mark.parametrize(
    ("rate", "roll", "expected"),
    [
        (0.8, 0.0, PaymentStatus.SUCCESS),
        (0.8, 0.79, PaymentStatus.SUCCESS),
        (0.8, 0.8, PaymentStatus.FAILED),  # a roll equal to the rate fails: rolls are below 1
        (0.8, 0.99, PaymentStatus.FAILED),
        (1.0, 0.999999, PaymentStatus.SUCCESS),
        (0.0, 0.0, PaymentStatus.FAILED),
    ],
)
def test_without_an_override_the_dice_decide(
    rate: float, roll: float, expected: PaymentStatus
) -> None:
    result = simulator(rate, roll).charge(PAYMENT_ID, None)
    assert result is not None
    assert result.status is expected
    assert result.provider_payment_id == REFERENCE
    assert (result.failure_reason is None) is (expected is PaymentStatus.SUCCESS)


def test_over_many_payments_the_share_of_successes_follows_the_rate() -> None:
    dice = random.Random(2030)
    simulated = PaymentSimulator(0.8, roll=dice.random)
    results = [simulated.charge(uuid.uuid4(), None) for _ in range(10_000)]
    successes = sum(1 for r in results if r is not None and r.status is PaymentStatus.SUCCESS)
    assert 0.78 < successes / 10_000 < 0.82


def test_the_reference_is_the_same_for_the_same_payment_and_differs_between_payments() -> None:
    first = simulator().charge(PAYMENT_ID, PaymentStatus.SUCCESS)
    again = simulator().charge(PAYMENT_ID, PaymentStatus.SUCCESS)
    other = simulator().charge(uuid.uuid4(), PaymentStatus.SUCCESS)
    assert first == again
    assert other is not None
    assert first is not None
    assert other.provider_payment_id != first.provider_payment_id


def test_the_default_dice_are_python_random() -> None:
    assert PaymentSimulator(1.0).charge(PAYMENT_ID, None) is not None  # a rate of 1 always succeeds
