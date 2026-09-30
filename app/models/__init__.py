"""The whole schema in one folder. Importing this package registers every table on `Base`."""

from app.models.base import Base
from app.models.booking import Booking, BookingStatus
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.payment import Payment, PaymentStatus, WebhookEvent, WebhookOutcome
from app.models.user import Role, User

__all__ = [
    "Base",
    "Booking",
    "BookingStatus",
    "Centre",
    "CentreTest",
    "DiagnosticTest",
    "Payment",
    "PaymentStatus",
    "Role",
    "User",
    "WebhookEvent",
    "WebhookOutcome",
]
