from alembic import context

from app.core.config import get_settings
from app.database import models  # noqa: F401  (registers tables)
from app.database.base import Base, make_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = context.config.attributes.get("engine") or make_engine(get_settings().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
