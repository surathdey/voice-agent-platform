"""Database session handling (SQLAlchemy). Works with PostgreSQL (Docker)
and SQLite (native dev) via DATABASE_URL."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from . import migrations
from .config import settings
from .models import Base

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db():
    """Boot schema path, in order:

    1. The migration runner (app.migrations) brings an EXISTING
       database up to the current shape in place — create_all alone
       never alters existing tables, which is how an old volume used
       to crash a new build at startup (the upgrade-path defect).
    2. create_all then creates any still-missing tables from the
       models (the single source of truth for table shape).
    """
    for version, name, action in migrations.run_migrations(engine):
        print(f"[migrations] {version:03d} {name}: {action}")
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
