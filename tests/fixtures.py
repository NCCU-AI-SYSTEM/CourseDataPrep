"""A tiny course_struct.db shaped like scheduling_test's build_runtime output."""
from __future__ import annotations

import os
import sqlite3
import struct

from prep.contract import DEFAULT_DATABASE_URL, SEMESTER_LABEL, V4_EMBED_DIM, V4_EMBED_MODEL


def scratch_database() -> bool:
    """True when DATABASE_URL names a database the tests may overwrite.

    The tests replace the v4 schema with a two-course fixture of the contract's semester,
    which would ship if it landed in the build database before a dump."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return False
    if url == DEFAULT_DATABASE_URL:
        raise SystemExit("refusing to run database tests against the build database "
                         f"({DEFAULT_DATABASE_URL}); point DATABASE_URL at a scratch database")
    return True

COURSES = [
    # course_id, name, lang, cats, lmt_kind, point, unit, text, toks, sessions
    ("1151703834001", "自然語言處理", "中文", '["選修"]', "", 3.0, "資訊科學系", "自然語言處理 課程",
     "自 然 語 言 處 理 自然 然語 語言 言處 處理 課 程 課程", [(4, 9, 12)]),
    ("1151000123001", "大學英文", "英文", '["必修", "通識"]', "", 0.0, "", "大學英文 english",
     "大 學 英 文 大學 學英 英文 english", [(1, 8, 10), (3, 13, 15)]),
]


def vector(i: int, dim: int = V4_EMBED_DIM) -> bytes:
    v = [0.0] * dim
    v[i] = 1.0
    return struct.pack(f"{dim}f", *v)


def make_struct_db(path, *, semester: str = SEMESTER_LABEL, hf_id: str = V4_EMBED_MODEL,
                   dim: int = V4_EMBED_DIM, drop_embedding_for: str | None = None,
                   with_search: bool = True) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE course (course_id TEXT PRIMARY KEY, name TEXT, credits REAL, cancelled INTEGER, card TEXT);
        CREATE TABLE session (course_id TEXT, seq INTEGER, weekday INTEGER, start TEXT, end TEXT);
        CREATE TABLE embedding (course_id TEXT, model TEXT, dim INTEGER, vec BLOB, PRIMARY KEY (course_id, model));
    """)
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("semester", semester), ("built_at", "2026/11/02 12:00"),
        ("embedding_hf_id:e5-large-instruct", hf_id)])
    for i, c in enumerate(COURSES):
        cid, name = c[0], c[1]
        con.execute("INSERT INTO course VALUES (?, ?, ?, 0, ?)", (cid, name, c[5], f"{name} card"))
        for seq, (wd, s, e) in enumerate(c[9]):
            con.execute("INSERT INTO session VALUES (?, ?, ?, ?, ?)", (cid, seq, wd, f"{s:02d}:10", f"{e:02d}:00"))
        if cid != drop_embedding_for:
            con.execute("INSERT INTO embedding VALUES (?, 'e5-large-instruct', ?, ?)", (cid, dim, vector(i, dim)))
    if with_search:
        con.executescript("""
            CREATE TABLE search_doc (course_id TEXT PRIMARY KEY, pos INTEGER, name TEXT, lang TEXT, cats TEXT,
              lmt_kind TEXT, point REAL, unit TEXT, text TEXT, toks TEXT);
            CREATE TABLE search_session (course_id TEXT, weekday INTEGER, start_h INTEGER, end_h INTEGER);
        """)
        for i, c in enumerate(COURSES):
            con.execute("INSERT INTO search_doc VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (c[0], i, *c[1:9]))
            con.executemany("INSERT INTO search_session VALUES (?, ?, ?, ?)", [(c[0], *s) for s in c[9]])
    con.commit()
    con.close()
