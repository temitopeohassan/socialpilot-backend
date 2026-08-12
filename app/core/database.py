from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings
import os

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)


def create_db_and_tables():
    # Import all model modules so their tables register on SQLModel.metadata
    # before we create them. (ai_models holds the AI-first tables.)
    from app.models import user as _user  # noqa: F401
    from app.models import ai_models as _ai  # noqa: F401
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
