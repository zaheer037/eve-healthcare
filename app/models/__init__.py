"""The whole schema in one folder. Importing this package registers every table on `Base`."""

from app.models.base import Base
from app.models.catalog import Centre, CentreTest, DiagnosticTest
from app.models.user import Role, User

__all__ = ["Base", "Centre", "CentreTest", "DiagnosticTest", "Role", "User"]
