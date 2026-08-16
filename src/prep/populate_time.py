"""Parse time_raw (e.g. 三234) into structured time columns.

    uv run python -m prep.populate_time

Only touches rows where sessions IS NULL, so it can be re-run.
"""
from __future__ import annotations

import json
import sys

import psycopg2
import psycopg2.extras

from .contract import database_url
from .course_time import parse_sessions

BATCH_SIZE = 500

UPDATE_SQL = """
    UPDATE public.course SET
        sessions      = %(sessions)s::jsonb,
        weekdays      = %(weekdays)s::integer[],
        has_morning   = %(has_morning)s,
        has_noon      = %(has_noon)s,
        has_afternoon = %(has_afternoon)s,
        has_evening   = %(has_evening)s
    WHERE id = %(id)s
"""


def _row_payload(course_id: str, time_raw: str) -> dict:
    sessions = parse_sessions(time_raw)
    return {
        "id": course_id,
        "sessions": json.dumps(sessions, ensure_ascii=False),
        "weekdays": sorted({s["weekday"] for s in sessions}),
        "has_morning": any(s["start_hour"] < 12 and s["end_hour"] > 8 for s in sessions),
        "has_noon": any(s["start_hour"] < 13 and s["end_hour"] > 12 for s in sessions),
        "has_afternoon": any(s["start_hour"] < 18 and s["end_hour"] > 13 for s in sessions),
        "has_evening": any(s["end_hour"] > 18 for s in sessions),
    }


def main() -> None:
    conn = psycopg2.connect(database_url())
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("SELECT COUNT(*) FROM public.course WHERE sessions IS NULL")
    total = cur.fetchone()[0]
    print(f"[populate] 待處理 {total} 列", flush=True)

    if total == 0:
        print("[populate] 沒事可做。", flush=True)
        cur.close()
        conn.close()
        return

    cur.execute("SELECT id, time_raw FROM public.course WHERE sessions IS NULL")
    rows = cur.fetchall()

    upd = conn.cursor()
    batch: list[dict] = []
    processed = 0

    def flush() -> None:
        nonlocal processed, batch
        if not batch:
            return
        psycopg2.extras.execute_batch(upd, UPDATE_SQL, batch, page_size=BATCH_SIZE)
        conn.commit()
        processed += len(batch)
        print(f"[populate] {processed}/{total}", flush=True)
        batch = []

    for row in rows:
        batch.append(_row_payload(row["id"], row["time_raw"] or ""))
        if len(batch) >= BATCH_SIZE:
            flush()
    flush()

    upd.close()
    cur.close()
    conn.close()
    print(f"[populate] 完成,共更新 {processed} 列。", flush=True)


if __name__ == "__main__":
    sys.exit(main())
