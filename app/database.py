import sqlite3
from contextlib import contextmanager

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS empresas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj TEXT UNIQUE NOT NULL,
    nome_fantasia TEXT NOT NULL,
    api_token TEXT NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # Bancos criados antes da mudança para login com usuário/senha do próprio
        # Meu ERP Online ainda têm essa coluna; login não depende mais dela.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(empresas)")}
        if "senha_hash" in columns:
            conn.execute("ALTER TABLE empresas DROP COLUMN senha_hash")


@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
