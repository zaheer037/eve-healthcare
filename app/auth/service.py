"""Signup and login: the two use cases that hash or verify a password."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.repository import UserRepository
from app.auth.schemas import SignupRequest, TokenRead
from app.core.config import Settings
from app.core.db import violated_constraint
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

    def signup(self, data: SignupRequest) -> User:
        """Create a patient. There is no pre-check `SELECT`: the unique index decides.

        Two simultaneous signups with one email cannot both pass. The loser gets a 409.
        """
        user = User(
            email=data.email,
            password_hash=hash_password(data.password.get_secret_value()),
            full_name=data.full_name,
            phone=data.phone,
        )
        self._session.add(user)
        try:
            self._session.commit()  # one INSERT ... RETURNING
        except IntegrityError as error:
            self._session.rollback()  # first, so the session stays usable
            if violated_constraint(error) == "uq_users_email":
                raise EmailAlreadyRegistered(
                    "An account with this email already exists."
                ) from error
            raise
        return user

    def login(self, email: str, password: str) -> TokenRead:
        """Verify the password and return a token. The email is already lower-cased.

        An unknown email is verified against the dummy hash, so both failures take the same time
        and give the same answer. Only a failure counts against the account.
        """
        user = self._users.get_by_email(email)
        verified = verify_password(password, user.password_hash if user else self._dummy_hash)
        if user is None or not verified:
            self._throttle.record_failure(email)
            raise InvalidCredentials("The email or the password is wrong.")
        self._throttle.reset(email)
        token, expires_in = create_access_token(user.id, self._settings)
        return TokenRead(access_token=token, expires_in=expires_in)
