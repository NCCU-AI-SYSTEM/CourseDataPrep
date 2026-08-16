"""Apply the init-scripts DDL and record the contract in schema_meta.

    uv run python -m prep.migrate

Idempotent, so it also upgrades an existing database. Raw course data is loaded
separately by the container entrypoint — it uses COPY FROM stdin and psql
meta-commands that psycopg2 cannot execute.
"""
from __future__ import annotations

import os
import sys

import psycopg2

from .contract import EMBED_DIM, INIT_SCRIPTS_DIR, SCHEMA_META, database_url

DDL_SCRIPTS = ("02-extensions.sql", "03-schema.sql", "04-time-columns.sql")


def _require_course_table(cur) -> None:
    cur.execute("SELECT to_regclass('public.course')")
    if cur.fetchone()[0] is None:
        raise SystemExit(
            "資料庫裡沒有 public.course。\n"
            "請先把原始課程 SQL 放進 CourseLangChain/dumps/ 再 "
            "`docker compose down -v && docker compose up -d postgres`,\n"
            "容器起來時會用 psql 灌進去,之後再回來跑 migrate。"
        )


def _write_schema_meta(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.schema_meta (
            key   text PRIMARY KEY,
            value text NOT NULL
        )
    """)
    for key, value in SCHEMA_META.items():
        cur.execute(
            """
            INSERT INTO public.schema_meta (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )


def main() -> None:
    conn = psycopg2.connect(database_url())
    conn.autocommit = False
    cur = conn.cursor()
    try:
        _require_course_table(cur)

        for name in DDL_SCRIPTS:
            path = os.path.join(INIT_SCRIPTS_DIR, name)
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            sql = sql.replace("${EMBED_DIM}", str(EMBED_DIM))
            print(f"[migrate] 套用 {name} ...", flush=True)
            cur.execute(sql)

        _write_schema_meta(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    print("[migrate] 完成。schema_meta:", flush=True)
    for key, value in SCHEMA_META.items():
        print(f"           {key} = {value}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
