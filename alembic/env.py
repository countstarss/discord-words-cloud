"""Alembic environment configuration"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Import models for autogenerate
from src.storage.models import Base
from src.common.config import load_config

# This is the Alembic Config object
config = context.config

# Load database URL from config
try:
    app_config = load_config()
    db_cfg = app_config.get("database", {})
    if db_cfg.get("url"):
        database_url = db_cfg["url"]
    else:
        host = db_cfg.get("host", "localhost")
        port = db_cfg.get("port", "5432")
        name = db_cfg.get("name", "rubii_words")
        user = db_cfg.get("user", "postgres")
        password = db_cfg.get("password", "")
        database_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
    config.set_main_option("sqlalchemy.url", database_url)
except Exception:
    # Fall back to environment variable
    pass

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Add your model's MetaData object here
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
