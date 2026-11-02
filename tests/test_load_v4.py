"""prep.load_v4: the course_struct.db checks run without a database; the load itself needs one.

    uv run python -m tests.test_load_v4
    DATABASE_URL=postgresql://...  uv run python -m tests.test_load_v4    # also the load into PostgreSQL

The load test writes only to the `v4` schema of DATABASE_URL, which load_v4 drops and recreates;
point it at a scratch database.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from prep.contract import SEMESTER_LABEL, V4_EMBED_DIM
from prep.load_v4 import pg_type, preflight, vector_literal
from tests.fixtures import COURSES, make_struct_db, vector


def _problems(**kw) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "course_struct.db"
        make_struct_db(path, **kw)
        con = sqlite3.connect(path)
        try:
            return preflight(con)[1]
        finally:
            con.close()


def test_pg_type() -> None:
    assert pg_type("INTEGER") == "bigint"
    assert pg_type("REAL") == "double precision"
    assert pg_type("BLOB") == "bytea"
    assert pg_type("TEXT") == "text"
    assert pg_type("") == "text"


def test_vector_literal_round_trips_float32() -> None:
    lit = vector_literal(vector(1, 4), 4)
    assert lit == "[0.0,1.0,0.0,0.0]"


def test_preflight_accepts_a_matching_file() -> None:
    assert _problems() == []


def test_preflight_rejects_mismatches() -> None:
    assert any("semester" in p for p in _problems(semester="000-0"))
    assert any("model" in p for p in _problems(hf_id="BAAI/bge-m3"))
    assert any("dim" in p for p in _problems(dim=8))
    assert any("search_doc" in p for p in _problems(with_search=False))
    assert any("without a vector" in p for p in _problems(drop_embedding_for=COURSES[1][0]))


def test_load_into_postgres() -> None:
    if not os.environ.get("DATABASE_URL"):
        return "skipped: DATABASE_URL not set"
    import psycopg2

    from prep.contract import database_url
    from prep.load_v4 import load

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "course_struct.db"
        make_struct_db(path)
        conn = psycopg2.connect(database_url())
        try:
            counts = load(conn, path)
            cur = conn.cursor()
            assert counts["v4.doc"] == 2 and counts["v4.doc_session"] == 3
            cur.execute('SELECT count(*) FROM v4.session WHERE "end" IS NOT NULL')    # reserved word kept
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT value FROM v4.meta WHERE key = 'semester'")
            assert cur.fetchone()[0] == SEMESTER_LABEL
            cur.execute("SELECT vector_dims(emb), cats FROM v4.doc WHERE course_id = %s", (COURSES[1][0],))
            assert cur.fetchone() == (V4_EMBED_DIM, ["必修", "通識"])
            cur.execute("SELECT course_id FROM v4.doc WHERE toks ||| %s ORDER BY pdb.score(course_id) DESC",
                        ("語言 處理",))
            assert [r[0] for r in cur.fetchall()] == [COURSES[0][0]]
            cur.execute("SELECT course_id FROM v4.doc ORDER BY emb <#> %s::vector LIMIT 1",
                        (vector_literal(vector(1), V4_EMBED_DIM),))
            assert cur.fetchone()[0] == COURSES[1][0]
            # loading again replaces the schema instead of failing or doubling rows
            assert load(conn, path)["v4.doc"] == 2
        finally:
            conn.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            result = fn()
            print(f"  {result or 'ok'}  {name}")
    print("all passed")
