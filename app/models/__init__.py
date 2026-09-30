"""The whole schema in one folder. Importing this package registers every table on `Base`."""

from app.models.base import Base

__all__ = ["Base"]
