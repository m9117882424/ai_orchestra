from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative metadata without any database connection side effects."""

    pass
