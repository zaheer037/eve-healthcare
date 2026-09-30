"""The error catalogue: one ErrorCode per AppError subclass, with the status of §5.3."""

import pytest

from app.core import errors
from app.core.errors import ERROR_SPECS, AppError, ErrorCode


def all_subclasses(base: type[AppError]) -> list[type[AppError]]:
    found: list[type[AppError]] = []
    for sub in base.__subclasses__():
        found.append(sub)
        found.extend(all_subclasses(sub))
    return found


def test_every_code_has_exactly_one_subclass() -> None:
    codes = [sub.code for sub in all_subclasses(AppError)]
    assert sorted(codes) == sorted(ErrorCode)  # none missing, none twice


def test_every_code_has_a_status_and_a_title() -> None:
    assert set(ERROR_SPECS) == set(ErrorCode)
    assert all(spec.title for spec in ERROR_SPECS.values())


@pytest.mark.parametrize(
    ("error_class", "status"),
    [
        (errors.BadRequest, 400),
        (errors.NotAuthenticated, 401),
        (errors.InvalidToken, 401),
        (errors.InvalidCredentials, 401),
        (errors.WebhookSignatureInvalid, 401),
        (errors.Forbidden, 403),
        (errors.ValidationFailed, 422),
        (errors.AppointmentOutOfWindow, 422),
        (errors.SimulationNotAllowed, 422),
        (errors.CentreNotFound, 404),
        (errors.DiagnosticTestNotFound, 404),
        (errors.CentreTestNotFound, 404),
        (errors.BookingNotFound, 404),
        (errors.PaymentNotFound, 404),
        (errors.RouteNotFound, 404),
        (errors.MethodNotAllowed, 405),
        (errors.EmailAlreadyRegistered, 409),
        (errors.DuplicateCentre, 409),
        (errors.DuplicateDiagnosticTest, 409),
        (errors.DuplicateBooking, 409),
        (errors.BookingLimitReached, 409),
        (errors.InvalidBookingStatus, 409),
        (errors.BookingNotPayable, 409),
        (errors.PaymentInProgress, 409),
        (errors.PayloadTooLarge, 413),
        (errors.RateLimited, 429),
        (errors.ServiceUnavailable, 503),
        (errors.InternalError, 500),
    ],
)
def test_status_codes_follow_the_catalogue(error_class: type[AppError], status: int) -> None:
    assert error_class().status_code == status


def test_the_type_is_a_urn_built_from_the_code() -> None:
    assert errors.BookingNotPayable().type_urn == "urn:eve:problem:booking-not-payable"
    assert errors.ValidationFailed().type_urn == "urn:eve:problem:validation-error"


def test_detail_is_optional_and_kept() -> None:
    assert errors.Forbidden().detail is None
    assert errors.Forbidden("Not your booking.").detail == "Not your booking."


@pytest.mark.parametrize(
    ("error", "challenge"),
    [
        (errors.NotAuthenticated(), "Bearer"),
        (errors.InvalidToken(), 'Bearer error="invalid_token"'),
        (errors.InvalidCredentials(), "Bearer"),
        (errors.WebhookSignatureInvalid(), 'Signature realm="webhook"'),
    ],
)
def test_every_401_carries_a_challenge(error: AppError, challenge: str) -> None:
    assert error.headers() == {"WWW-Authenticate": challenge}


def test_other_errors_carry_no_challenge() -> None:
    assert errors.Forbidden().headers() == {}
    assert errors.BookingNotFound().headers() == {}


def test_a_rate_limit_says_when_to_retry() -> None:
    assert errors.RateLimited(retry_after=42).headers() == {"Retry-After": "42"}


def test_an_unavailable_service_asks_for_a_retry_after_one_second_by_default() -> None:
    assert errors.ServiceUnavailable().headers() == {"Retry-After": "1"}
    assert errors.ServiceUnavailable(retry_after=5).headers() == {"Retry-After": "5"}


def test_an_app_error_is_an_exception_with_its_message() -> None:
    error = errors.DuplicateBooking("You already booked this slot.")
    assert isinstance(error, Exception)
    assert str(error) == "You already booked this slot."
