import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
load_dotenv(env_path)


@contextmanager
def get_db_connection() -> Iterator[psycopg.Connection]:
    """Yield a Postgres connection and always close it.

    psycopg3's own connection context manager commits/rolls back but does NOT
    close the connection, which leaks handles under concurrency. We wrap it so
    the transaction is still managed (inner ``with conn``) and the socket is
    explicitly closed in ``finally``.
    """
    conn = psycopg.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_TOKEN"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        sslmode=os.getenv("DB_SSLMODE", "require"),
    )
    try:
        with conn:
            yield conn
    finally:
        conn.close()
