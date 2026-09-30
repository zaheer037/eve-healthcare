"""Signup and login: the two use cases that hash or verify a password."""

from sqlalchemy.orm import Session

from app.auth.repository import UserRepository
from app.core.config import Settings
from app.core.db import domain_errors
from app.core.errors import EmailAlreadyRegistered, InvalidCredentials
from app.core.security import create_access_token, hash_password, verify_password
from app.core.throttle import LoginThrottle
from app.models.user import User


class AuthService:
    def __init__(
        self, session: Session, settings: Settings, throttle: LoginThrottle, dummy_hash: str
    ) -> None:
        self._session = session
        self._settings = settings
        self._throttle = throttle
        self._dummy_hash = dummy_hash
        self._users = UserRepository(session)

    def signup(
        self, *, email: str, password: str, full_name: str | None, phone: str | None
    ) -> User:
        """Create a patient. There is no pre-check `SELECT`: the unique index decides.

        Two simultaneous signups with one email cannot both pass. The loser gets a 409.

        Raises:
            EmailAlreadyRegistered: an account with this email exists, in any letter case.

        Commits: yes.
        """
        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name=full_name,
            phone=phone,
        )
        self._session.add(user)
        taken = {
            "uq_users_email": EmailAlreadyRegistered("An account with this email already exists.")
        }
        with domain_errors(self._session, taken):
            self._session.commit()  # one INSERT ... RETURNING
        return user

    def login(self, email: str, password: str) -> tuple[str, int]:
        """Verify the password. Return the token and its lifetime in seconds.

        The email is already lower-cased.

        An unknown email is verified against the dummy hash, so both failures take the same time
        and give the same answer. Only a failure counts against the account.

        Raises:
            InvalidCredentials: the email is unknown or the password is wrong.

        Commits: no.
        """
        user = self._users.get_by_email(email)
        verified = verify_password(password, user.password_hash if user else self._dummy_hash)
        if user is None or not verified:
            self._throttle.record_failure(email)
            raise InvalidCredentials("The email or the password is wrong.")
        self._throttle.reset(email)
        return create_access_token(user.id, self._settings)
