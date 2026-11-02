"""course_struct.db (the v4 search's data, built in scheduling_test) -> the `v4` schema.

    uv run python -m prep.load_v4 --db <scheduling_test>/data/v4/staging/115-1/course_struct.db

Every table of the file is copied as is, except the three that only exist to be folded into
the search tables:

  v4.doc          one row per searchable course: condition fields, document text, its char
                  n-grams (bm25 index, pdb.whitespace) and the e5 vector (pgvector)
  v4.doc_session  class sessions in whole hours, for the time conditions

Nothing here derives search fields: build_runtime already did (search_doc, search_session), so
the database matches what scheduling_test measured. The file is refused if its semester,
embedding model or dimension disagree with contract.yaml.

The whole load is one transaction that drops and recreates `v4`, so a failure leaves the
previous load intact and rerunning is safe. public is not touched.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from .contract import SEMESTER_LABEL, V4_EMBED_DIM, V4_EMBED_MODEL, database_url

SCHEMA = "v4"
# folded into v4.doc / v4.doc_session instead of copied
FOLDED = {"embedding", "search_doc", "search_session"}

SEARCH_DDL = """
CREATE TABLE v4.doc (
  course_id text PRIMARY KEY, pos integer NOT NULL, name text, lang text, cats text[],
  lmt_kind text, point real, unit text, doc text, toks text, emb vector(%(dim)d) NOT NULL);
CREATE TABLE v4.doc_session (
  course_id text NOT NULL REFERENCES v4.doc, weekday integer, start_h integer, end_h integer);
CREATE INDEX doc_session_course ON v4.doc_session (course_id);
CREATE INDEX doc_bm25 ON v4.doc USING bm25 (course_id, (toks::pdb.whitespace)) WITH (key_field = 'course_id');
"""


def pg_type(decl: str) -> str:
    """SQLite type affinity rules, reduced to the four types course_struct.db uses."""
    d = (decl or "").upper()
    if "INT" in d:
        return "bigint"
    if any(k in d for k in ("REAL", "FLOA", "DOUB")):
        return "double precision"
    if "BLOB" in d:
        return "bytea"
    return "text"


def vector_literal(blob: bytes, dim: int) -> str:
    vals = struct.unpack(f"{dim}f", blob)
    return "[" + ",".join(repr(float(x)) for x in vals) + "]"


def _embedding_name(con: sqlite3.Connection) -> str | None:
    """The encoder name in course_struct.db whose Hugging Face id is the contract's model."""
    rows = con.execute("SELECT key FROM meta WHERE key LIKE 'embedding_hf_id:%' AND value = ?",
                       (V4_EMBED_MODEL,)).fetchall()
    return rows[0][0].split(":", 1)[1] if rows else None


def preflight(con: sqlite3.Connection) -> tuple[str | None, list[str]]:
    """(encoder name, problems). An empty list means the file can be loaded."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = {"meta", "search_doc", "search_session", "embedding"} - tables
    if missing:
        return None, [f"missing tables {sorted(missing)}: rebuild with scheduling_test's "
                      "scripts.v4.build_runtime (search_doc and search_session came in 2026/11/02)"]
    problems = []
    meta = dict(con.execute("SELECT key, value FROM meta"))
    if meta.get("semester") != SEMESTER_LABEL:
        problems.append(f"semester: file has {meta.get('semester')!r}, contract.yaml has {SEMESTER_LABEL!r}")
    name = _embedding_name(con)
    if name is None:
        problems.append(f"embedding model: no vectors from {V4_EMBED_MODEL} "
                        f"(file has {sorted(v for k, v in meta.items() if k.startswith('embedding_hf_id:'))})")
        return None, problems
    dims = {r[0] for r in con.execute("SELECT DISTINCT dim FROM embedding WHERE model = ?", (name,))}
    if dims != {V4_EMBED_DIM}:
        problems.append(f"embedding dim: file has {sorted(dims)}, contract.yaml has {V4_EMBED_DIM}")
    (orphans,) = con.execute(
        "SELECT count(*) FROM search_doc d WHERE NOT EXISTS "
        "(SELECT 1 FROM embedding e WHERE e.course_id = d.course_id AND e.model = ?)", (name,)).fetchone()
    if orphans:
        problems.append(f"{orphans} search_doc courses without a vector")
    (docs,) = con.execute("SELECT count(*) FROM search_doc").fetchone()
    if not docs:
        problems.append("search_doc is empty")
    return name, problems


def _copy_table(scon: sqlite3.Connection, cur, table: str) -> int:
    cols = scon.execute(f'PRAGMA table_info("{table}")').fetchall()   # cid, name, type, notnull, dflt, pk
    defs = ", ".join(f'"{c[1]}" {pg_type(c[2])}' for c in cols)
    cur.execute(f'CREATE TABLE {SCHEMA}."{table}" ({defs})')
    names = [c[1] for c in cols]
    blobs = [i for i, c in enumerate(cols) if pg_type(c[2]) == "bytea"]
    rows = scon.execute(f'SELECT {", ".join(chr(34) + n + chr(34) for n in names)} FROM "{table}"').fetchall()
    if blobs:
        rows = [tuple(psycopg2.Binary(v) if i in blobs and v is not None else v for i, v in enumerate(r))
                for r in rows]
    if rows:
        execute_values(cur, f'INSERT INTO {SCHEMA}."{table}" VALUES %s', rows, page_size=1000)
    if "course_id" in names:
        cur.execute(f'CREATE INDEX "{table}_course" ON {SCHEMA}."{table}" (course_id)')
    return len(rows)


def _load_search(scon: sqlite3.Connection, cur, encoder: str) -> tuple[int, int]:
    rows = scon.execute(
        "SELECT d.course_id, d.pos, d.name, d.lang, d.cats, d.lmt_kind, d.point, d.unit, d.text, d.toks, e.vec "
        "FROM search_doc d JOIN embedding e ON e.course_id = d.course_id AND e.model = ? ORDER BY d.pos",
        (encoder,)).fetchall()
    docs = [(*r[:4], json.loads(r[4] or "[]"), *r[5:10], vector_literal(r[10], V4_EMBED_DIM)) for r in rows]
    execute_values(cur, f"INSERT INTO {SCHEMA}.doc VALUES %s", docs, page_size=200,
                   template="(%s, %s, %s, %s, %s::text[], %s, %s, %s, %s, %s, %s::vector)")
    ses = scon.execute("SELECT course_id, weekday, start_h, end_h FROM search_session").fetchall()
    if ses:
        execute_values(cur, f"INSERT INTO {SCHEMA}.doc_session VALUES %s", ses, page_size=1000)
    return len(docs), len(ses)


def load(conn, path: Path) -> dict[str, int]:
    """Replace the v4 schema with the contents of `path`; returns row counts per table."""
    scon = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        encoder, problems = preflight(scon)
        if problems:
            raise SystemExit(f"[load_v4] refusing {path}:\n" + "\n".join(f"  - {p}" for p in problems))
        tables = [r[0] for r in scon.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts: dict[str, int] = {}
        cur = conn.cursor()
        try:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {SCHEMA}")
            for t in tables:
                if t not in FOLDED:
                    counts[f"{SCHEMA}.{t}"] = _copy_table(scon, cur, t)
            cur.execute(SEARCH_DDL % {"dim": V4_EMBED_DIM})
            counts[f"{SCHEMA}.doc"], counts[f"{SCHEMA}.doc_session"] = _load_search(scon, cur, encoder)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        cur.execute(f"ANALYZE {SCHEMA}.doc")
        cur.execute(f"ANALYZE {SCHEMA}.doc_session")
        conn.commit()
        cur.close()
        return counts
    finally:
        scon.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=Path, required=True, help="course_struct.db from scheduling_test")
    a = p.parse_args()
    if not a.db.exists():
        raise SystemExit(f"[load_v4] no such file: {a.db}")
    conn = psycopg2.connect(database_url())
    try:
        counts = load(conn, a.db)
    finally:
        conn.close()
    width = max(len(k) for k in counts)
    print(f"[load_v4] loaded {a.db} ({SEMESTER_LABEL}):", flush=True)
    for k, n in counts.items():
        print(f"           {k:<{width}}  {n}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
