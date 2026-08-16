"""DEPRECATED — index for the old SQLite/FAISS path.

    uv sync --extra sqlite
    uv run python -m prep.build_sqlite

Exists only so unmigrated environments keep working; don't build on it. Emits a FAISS
directory + courses.jsonl + meta.json rather than pickling a retriever, which would
serialise the class, the langchain version and the embedding device with it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

from .contract import (
    COURSE_SEMESTER,
    COURSE_YEAR,
    OUT_DIR,
    SCHEMA_VERSION,
    SQLITE_EMBED_MODEL,
)
from .course_time import getSessionArray
from .device import pick_device

DATA_DB = os.getenv("DATA_DB", "data.db")


def _page_content(row: dict) -> str:
    sessions = getSessionArray(row.get("time") or "")
    if sessions:
        time_str = "、".join(
            f"星期{s['origin_str'][0]} {s['start_time']}:00-{s['end_time']}:00"
            for s in sessions
        )
    else:
        time_str = "未定"
    return (
        f"課程名稱是{row.get('name', '')}, 上課時間是{time_str}, "
        f"這堂課的老師是{row.get('teacher', '')}"
    )


def main() -> None:
    from langchain_community.vectorstores import FAISS
    from langchain_core.documents import Document
    from langchain_huggingface import HuggingFaceEmbeddings

    conn = sqlite3.connect(DATA_DB)
    conn.row_factory = lambda c, r: {d[0]: r[i] for i, d in enumerate(c.description)}
    rows = conn.execute(
        "SELECT * FROM COURSE WHERE y = ? AND s = ?", (COURSE_YEAR, COURSE_SEMESTER)
    ).fetchall()
    conn.close()
    print(f"[build_sqlite] {COURSE_YEAR}-{COURSE_SEMESTER} 共 {len(rows)} 門課", flush=True)

    docs = [Document(page_content=_page_content(r), metadata=r) for r in rows]

    device = pick_device()
    print(f"[build_sqlite] 載入 {SQLITE_EMBED_MODEL}(device={device}) ...", flush=True)
    embeddings = HuggingFaceEmbeddings(
        model_name=SQLITE_EMBED_MODEL, model_kwargs={"device": device}
    )
    vectorstore = FAISS.from_documents(docs, embedding=embeddings)

    os.makedirs(OUT_DIR, exist_ok=True)
    index_dir = os.path.join(OUT_DIR, "faiss_index")
    vectorstore.save_local(index_dir)

    jsonl_path = os.path.join(OUT_DIR, "courses.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for d in docs:
            f.write(
                json.dumps(
                    {"page_content": d.page_content, "metadata": d.metadata},
                    ensure_ascii=False,
                )
                + "\n"
            )

    with open(os.path.join(index_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema_version": SCHEMA_VERSION,
                "sqlite_embed_model": SQLITE_EMBED_MODEL,
                "course_year": COURSE_YEAR,
                "course_semester": COURSE_SEMESTER,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"[build_sqlite] 完成:{index_dir} 與 {jsonl_path}", flush=True)
    print("[build_sqlite] 複製到 CourseLangChain/ 專案根目錄即可。", flush=True)


if __name__ == "__main__":
    sys.exit(main())
