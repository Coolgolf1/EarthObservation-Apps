import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_db_connection() -> psycopg.Connection:
    return psycopg.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_TOKEN"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        sslmode=os.getenv("DB_SSLMODE", "require"),
    )

