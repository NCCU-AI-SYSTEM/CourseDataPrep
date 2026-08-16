"""Verify the database is fit to ship.

    uv run python -m prep.check

Two things must hold, and both fail silently downstream if they do not:
schema_meta has to agree with contract.yaml, and every course needs a vector —
a dump missing vectors quietly degrades the consumer to BM25-only.

Building the artifact is one shell pipe; it lives in the README.
"""
from __future__ import annotations

import sys

import psycopg2

from .contract import COURSE_SEMESTER, COURSE_YEAR, SCHEMA_META, database_url


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
    print(f"[check] passed: {courses} courses, {chunks} chunks, all embedded.", flush=True)
    cur.close()


def main() -> None:
    conn = psycopg2.connect(database_url())
    try:
        _verify(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
