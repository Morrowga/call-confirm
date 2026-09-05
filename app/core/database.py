from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings

# FastAPI-facing engine: one long-lived event loop (uvicorn), so normal
# connection pooling is correct and desirable here.
engine = create_async_engine(settings.database_url, echo=settings.debug, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


# --- Celery-task-facing async engine ---------------------------------------
# Celery's default (prefork) worker calls asyncio.run() fresh for EACH task,
# which creates and tears down a new event loop every time. A pooled engine's
# connections stay bound to whichever loop was running when they were opened,
# so on the second task the pool hands back a connection tied to an already-
# closed loop -> "Future attached to a different loop". NullPool disables
# pooling entirely: every checkout opens a fresh asyncpg connection scoped to
# the *current* event loop, and it's simply closed at the end of that task's
# `async with` block instead of being returned to a pool. This engine must
# NOT be reused by FastAPI's request path (that path already has correct,
# efficient pooling via `engine`/`SessionLocal` above).
celery_engine = create_async_engine(settings.database_url, echo=settings.debug, poolclass=NullPool)
CelerySessionLocal = async_sessionmaker(celery_engine, class_=AsyncSession, expire_on_commit=False)

# Synchronous session for any Celery tasks that prefer sync SQLAlchemy directly
# (Celery tasks are sync by default) — unrelated to the async/event-loop issue
# above, kept for tasks written against the sync API.
sync_engine = create_engine(settings.sync_db_url, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(bind=sync_engine)