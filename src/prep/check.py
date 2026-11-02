"""Verify the database is fit to ship.

    uv run python -m prep.check

Three things must hold, and all fail silently downstream if they do not:
schema_meta has to agree with contract.yaml, every course needs a vector — a dump
missing vectors quietly degrades the consumer to BM25-only — and the v4 schema
(prep.load_v4) has to be loaded for the same semester, with its bm25 index and a
vector of the contract's dimension on every searchable course.

Building the artifact is one shell pipe; it lives in the README.
"""
from __future__ import annotations

import sys

import psycopg2

from .contract import COURSE_SEMESTER, COURSE_YEAR, SCHEMA_META, SEMESTER_LABEL, V4_EMBED_DIM, database_url


def verify_v4(cur) -> list[str]:
    """Problems with the v4 schema; empty when it is fit to ship."""
    cur.execute("SELECT to_regclass('v4.doc'), to_regclass('v4.meta')")
    if None in cur.fetchone():
        return ["no v4 schema — run `uv run python -m prep.load_v4 --db <course_struct.db>`"]
    problems = []
    cur.execute("SELECT value FROM v4.meta WHERE key = 'semester'")
    row = cur.fetchone()
    if not row or row[0] != SEMESTER_LABEL:
        problems.append(f"v4 semester: database has {row and row[0]!r}, contract.yaml has {SEMESTER_LABEL!r}")
    cur.execute("SELECT count(*), count(*) FILTER (WHERE vector_dims(emb) <> %s) FROM v4.doc", (V4_EMBED_DIM,))
    docs, wrong_dim = cur.fetchone()
    if not docs:
        problems.append("v4.doc is empty")
    if wrong_dim:
        problems.append(f"{wrong_dim} v4.doc vectors are not {V4_EMBED_DIM}-dimensional")
    cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = 'v4' AND indexname = 'doc_bm25'")
    if cur.fetchone() is None:
        problems.append("v4.doc has no bm25 index (doc_bm25) — rerun prep.load_v4")
    else:
        cur.execute("SELECT toks FROM v4.doc WHERE toks <> '' LIMIT 1")
        sample = cur.fetchone()
        if sample:   # the index answers a query made of a course's own tokens
            cur.execute("SELECT count(*) FROM v4.doc WHERE toks ||| %s", (" ".join(sample[0].split()[:3]),))
            if not cur.fetchone()[0]:
                problems.append("v4 bm25 query on a course's own tokens returned nothing")
    return problems


def _verify(conn) -> None:
    cur = conn.cursor()

    cur.execute("SELECT to_regclass('public.schema_meta')")
    if cur.fetchone()[0] is None:
        raise SystemExit("no schema_meta — run `uv run python -m prep.migrate` first.")

    cur.execute("SELECT key, value FROM public.schema_meta")
    meta = dict(cur.fetchall())
    diffs = [
        f"  - {k}: database has {meta.get(k)!r}, contract.yaml has {v!r}"
        for k, v in SCHEMA_META.items()
        if meta.get(k) != v
    ]
    if diffs:
        raise SystemExit(
            "schema_meta disagrees with contract.yaml:\n"
            + "\n".join(diffs)
            + "\nRerun migrate, or check whether contract.yaml changed without a reload."
        )

    cur.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT id FROM public.course
            WHERE y = %s AND s = %s
              AND id NOT IN (SELECT course_id FROM public.course_chunk
                             WHERE embedding IS NOT NULL)
        ) t
        """,
        (COURSE_YEAR, COURSE_SEMESTER),
    )
    missing = cur.fetchone()[0]
    if missing:
        raise SystemExit(
            f"{missing} courses have no embedded chunk — run `uv run python -m prep.embed`.\n"
            "(Shipping a dump with missing vectors silently degrades the consumer "
            "to BM25-only, and nobody notices.)"
        )

    cur.execute(
        "SELECT COUNT(DISTINCT id), (SELECT COUNT(*) FROM public.course_chunk) "
        "FROM public.course WHERE y = %s AND s = %s",
        (COURSE_YEAR, COURSE_SEMESTER),
    )
    courses, chunks = cur.fetchone()

    problems = verify_v4(cur)
    if problems:
        raise SystemExit("v4 schema is not fit to ship:\n" + "\n".join(f"  - {p}" for p in problems))
    cur.execute("SELECT count(*) FROM v4.doc")
    v4_docs = cur.fetchone()[0]
    print(f"[check] passed: {courses} courses, {chunks} chunks, all embedded; "
          f"v4 {SEMESTER_LABEL}: {v4_docs} searchable courses.", flush=True)
    cur.close()


def main() -> None:
    conn = psycopg2.connect(database_url())
    try:
        _verify(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
