"""prep.check's v4 checks, against a fixture loaded by prep.load_v4.

    DATABASE_URL=postgresql://...  uv run python -m tests.test_check

Writes only the `v4` schema of DATABASE_URL; point it at a scratch database.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from tests.fixtures import make_struct_db, scratch_database


def _loaded_conn():
    import psycopg2

    from prep.contract import database_url
    from prep.load_v4 import load

    conn = psycopg2.connect(database_url())
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "course_struct.db"
        make_struct_db(path)
        load(conn, path)
    return conn


def test_verify_v4() -> str | None:
    if not scratch_database():
        return "skipped: DATABASE_URL not set"
    from prep.check import verify_v4

    conn = _loaded_conn()
    try:
        cur = conn.cursor()
        assert verify_v4(cur) == []
        cur.execute("UPDATE v4.meta SET value = '000-0' WHERE key = 'semester'")
        assert any("semester" in p for p in verify_v4(cur))
        conn.rollback()
        cur.execute("DROP INDEX v4.doc_bm25")
        assert any("bm25" in p for p in verify_v4(cur))
        conn.rollback()
        cur.execute("DROP SCHEMA v4 CASCADE")
        assert any("no v4 schema" in p for p in verify_v4(cur))
        conn.rollback()
    finally:
        conn.close()
    return None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            result = fn()
            print(f"  {result or 'ok'}  {name}")
    print("all passed")
