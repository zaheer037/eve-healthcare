"""The error catalogue: one `ErrorCode` per `AppError` subclass (spec section 7.3).

Clients switch on the code. Status and title live in one table (`ERROR_SPECS`). A subclass only
names its code, so adding an error is one enum member, one table row and one class.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar


class ErrorCode(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    INVALID_TOKEN = "INVALID_TOKEN"  # noqa: S105  # an error code, not a secret
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
    FORBIDDEN = "FORBIDDEN"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    APPOINTMENT_OUT_OF_WINDOW = "APPOINTMENT_OUT_OF_WINDOW"
    SIMULATION_NOT_ALLOWED = "SIMULATION_NOT_ALLOWED"
    CENTRE_NOT_FOUND = "CENTRE_NOT_FOUND"
    DIAGNOSTIC_TEST_NOT_FOUND = "DIAGNOSTIC_TEST_NOT_FOUND"
    CENTRE_TEST_NOT_FOUND = "CENTRE_TEST_NOT_FOUND"
    BOOKING_NOT_FOUND = "BOOKING_NOT_FOUND"
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"
    DUPLICATE_CENTRE = "DUPLICATE_CENTRE"
    DUPLICATE_DIAGNOSTIC_TEST = "DUPLICATE_DIAGNOSTIC_TEST"
    DUPLICATE_BOOKING = "DUPLICATE_BOOKING"
    BOOKING_LIMIT_REACHED = "BOOKING_LIMIT_REACHED"
    INVALID_BOOKING_STATUS = "INVALID_BOOKING_STATUS"
    BOOKING_NOT_PAYABLE = "BOOKING_NOT_PAYABLE"
    PAYMENT_IN_PROGRESS = "PAYMENT_IN_PROGRESS"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    status: int
    title: str


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.BAD_REQUEST: ErrorSpec(400, "Bad request"),
    ErrorCode.NOT_AUTHENTICATED: ErrorSpec(401, "Not authenticated"),
    ErrorCode.INVALID_TOKEN: ErrorSpec(401, "Invalid token"),
    ErrorCode.INVALID_CREDENTIALS: ErrorSpec(401, "Invalid credentials"),
    ErrorCode.WEBHOOK_SIGNATURE_INVALID: ErrorSpec(401, "Invalid webhook signature"),
    ErrorCode.FORBIDDEN: ErrorSpec(403, "Forbidden"),
    ErrorCode.VALIDATION_ERROR: ErrorSpec(422, "Request validation failed"),
    ErrorCode.APPOINTMENT_OUT_OF_WINDOW: ErrorSpec(422, "Appointment outside the allowed window"),
    ErrorCode.SIMULATION_NOT_ALLOWED: ErrorSpec(422, "Payment simulation is not allowed"),
    ErrorCode.CENTRE_NOT_FOUND: ErrorSpec(404, "Centre not found"),
    ErrorCode.DIAGNOSTIC_TEST_NOT_FOUND: ErrorSpec(404, "Diagnostic test not found"),
    ErrorCode.CENTRE_TEST_NOT_FOUND: ErrorSpec(404, "Test not offered at this centre"),
    ErrorCode.BOOKING_NOT_FOUND: ErrorSpec(404, "Booking not found"),
    ErrorCode.PAYMENT_NOT_FOUND: ErrorSpec(404, "Payment not found"),
    ErrorCode.ROUTE_NOT_FOUND: ErrorSpec(404, "Route not found"),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(405, "Method not allowed"),
    ErrorCode.EMAIL_ALREADY_REGISTERED: ErrorSpec(409, "Email already registered"),
    ErrorCode.DUPLICATE_CENTRE: ErrorSpec(409, "Centre already exists"),
    ErrorCode.DUPLICATE_DIAGNOSTIC_TEST: ErrorSpec(409, "Diagnostic test already exists"),
    ErrorCode.DUPLICATE_BOOKING: ErrorSpec(409, "Duplicate booking"),
    ErrorCode.BOOKING_LIMIT_REACHED: ErrorSpec(409, "Booking limit reached"),
    ErrorCode.INVALID_BOOKING_STATUS: ErrorSpec(409, "Invalid booking status"),
    ErrorCode.BOOKING_NOT_PAYABLE: ErrorSpec(409, "Booking is not payable"),
    ErrorCode.PAYMENT_IN_PROGRESS: ErrorSpec(409, "Payment in progress"),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(413, "Payload too large"),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, "Too many requests"),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(503, "Service unavailable"),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, "Internal server error"),
}


class AppError(Exception):
    """An expected failure. The handler in `core.problem` turns it into a problem response."""

    code: ClassVar[ErrorCode]
    challenge: ClassVar[str | None] = None  # the WWW-Authenticate value of a 401
    default_retry_after: ClassVar[int | None] = None  # seconds, for 429 and 503

    def __init__(self, detail: str | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(detail or ERROR_SPECS[self.code].title)
        self.detail = detail
        self.retry_after = retry_after if retry_after is not None else self.default_retry_after

    @property
    def status_code(self) -> int:
        return ERROR_SPECS[self.code].status

    @property
    def title(self) -> str:
        return ERROR_SPECS[self.code].title

    @property
    def type_urn(self) -> str:
        return "urn:eve:problem:" + self.code.lower().replace("_", "-")

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.challenge is not None:
            headers["WWW-Authenticate"] = self.challenge
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        return headers


class BadRequest(AppError):
    """The server cannot read the request body at all."""

    code = ErrorCode.BAD_REQUEST


class NotAuthenticated(AppError):
    """The Authorization header is missing or malformed."""

    code = ErrorCode.NOT_AUTHENTICATED
    challenge = "Bearer"


class InvalidToken(AppError):
    """Bad signature, wrong algorithm, expired token, or a token for an unknown user."""

    code = ErrorCode.INVALID_TOKEN
    challenge = 'Bearer error="invalid_token"'


class InvalidCredentials(AppError):
    """Wrong email or wrong password. The response is identical for both."""

    code = ErrorCode.INVALID_CREDENTIALS
    challenge = "Bearer"


class WebhookSignatureInvalid(AppError):
    """Missing, malformed, wrong or stale signature. One code for all four."""

    code = ErrorCode.WEBHOOK_SIGNATURE_INVALID
    challenge = 'Signature realm="webhook"'


class Forbidden(AppError):
    """The role is too low, or the resource belongs to another user."""

    code = ErrorCode.FORBIDDEN


class ValidationFailed(AppError):
    """The path, query or body is invalid."""

    code = ErrorCode.VALIDATION_ERROR


class AppointmentOutOfWindow(AppError):
    """The appointment is too soon or too far ahead."""

    code = ErrorCode.APPOINTMENT_OUT_OF_WINDOW


class SimulationNotAllowed(AppError):
    """The request sets `simulate` while the setting forbids it."""

    code = ErrorCode.SIMULATION_NOT_ALLOWED


class CentreNotFound(AppError):
    code = ErrorCode.CENTRE_NOT_FOUND


class DiagnosticTestNotFound(AppError):
    code = ErrorCode.DIAGNOSTIC_TEST_NOT_FOUND


class CentreTestNotFound(AppError):
    code = ErrorCode.CENTRE_TEST_NOT_FOUND


class BookingNotFound(AppError):
    code = ErrorCode.BOOKING_NOT_FOUND


class PaymentNotFound(AppError):
    code = ErrorCode.PAYMENT_NOT_FOUND


class RouteNotFound(AppError):
    """The path matches no route."""

    code = ErrorCode.ROUTE_NOT_FOUND


class MethodNotAllowed(AppError):
    """The path exists and the method does not."""

    code = ErrorCode.METHOD_NOT_ALLOWED


class EmailAlreadyRegistered(AppError):
    code = ErrorCode.EMAIL_ALREADY_REGISTERED


class DuplicateCentre(AppError):
    code = ErrorCode.DUPLICATE_CENTRE


class DuplicateDiagnosticTest(AppError):
    code = ErrorCode.DUPLICATE_DIAGNOSTIC_TEST


class DuplicateBooking(AppError):
    """The user already holds an active booking for this test, centre and minute."""

    code = ErrorCode.DUPLICATE_BOOKING


class BookingLimitReached(AppError):
    code = ErrorCode.BOOKING_LIMIT_REACHED


class InvalidBookingStatus(AppError):
    """The status transition is not in the transition table."""

    code = ErrorCode.INVALID_BOOKING_STATUS


class BookingNotPayable(AppError):
    """The booking is not PENDING, or its appointment has started, and no live payment exists."""

    code = ErrorCode.BOOKING_NOT_PAYABLE


class PaymentInProgress(AppError):
    """A cancellation meets a live PENDING payment."""

    code = ErrorCode.PAYMENT_IN_PROGRESS


class PayloadTooLarge(AppError):
    code = ErrorCode.PAYLOAD_TOO_LARGE


class RateLimited(AppError):
    code = ErrorCode.RATE_LIMITED
    default_retry_after = 1


class ServiceUnavailable(AppError):
    """The database is unreachable, a timeout fired, or every hashing slot is taken."""

    code = ErrorCode.SERVICE_UNAVAILABLE
    default_retry_after = 1


class InternalError(AppError):
    """An unexpected error. The body carries no internal detail."""

    code = ErrorCode.INTERNAL_ERROR
