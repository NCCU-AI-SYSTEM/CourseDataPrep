"""Emit the .sql.gz artifact consumed by CourseLangChain.

    uv run python -m prep.dump

Refuses to ship on a contract mismatch or missing vectors — both break the query
side silently. pg_dump runs in the container since dev machines lack the client.

    PG_CONTAINER=<name>   override container detection
"""
from __future__ import annotations

import os
import subprocess
import sys

import psycopg2

from .contract import (
    COURSE_SEMESTER,
    COURSE_YEAR,
    OUT_DIR,
    REPO_ROOT,
    SCHEMA_META,
    database_url,
)


def _verify(conn) -> None:
    cur = conn.cursor()

    cur.execute("SELECT to_regclass('public.schema_meta')")
    if cur.fetchone()[0] is None:
        raise SystemExit("沒有 schema_meta —— 先跑 `uv run python -m prep.migrate`。")

    cur.execute("SELECT key, value FROM public.schema_meta")
    meta = dict(cur.fetchall())
    diffs = [
        f"  - {k}: DB 是 {meta.get(k)!r},contract.yaml 是 {v!r}"
        for k, v in SCHEMA_META.items()
        if meta.get(k) != v
    ]
    if diffs:
        raise SystemExit(
            "schema_meta 與 contract.yaml 不符:\n"
            + "\n".join(diffs)
            + "\n重跑 migrate,或確認 contract.yaml 是不是改了沒重灌。"
        )

    cur.execute(
        "SELECT COUNT(*) FROM public.course "
        "WHERE y = %s AND s = %s AND embedding IS NULL",
        (COURSE_YEAR, COURSE_SEMESTER),
    )
    missing = cur.fetchone()[0]
    if missing:
        raise SystemExit(
            f"還有 {missing} 門課沒有向量 —— 先跑 `uv run python -m prep.embed`。\n"
            "(帶著缺向量的 dump 出貨,下游會退回純 BM25,而且不會有人發現。)"
        )

    cur.execute(
        "SELECT COUNT(*) FROM public.course WHERE y = %s AND s = %s",
        (COURSE_YEAR, COURSE_SEMESTER),
    )
    print(f"[dump] 檢查通過:{cur.fetchone()[0]} 門課,全部有向量。", flush=True)
    cur.close()


def _find_container() -> str:
    """Locate this repo's own postgres container.

    `docker compose ps`, not `docker ps --filter expose=5432`: several postgres
    containers are usually running and all expose 5432 internally, so filtering by
    port picks one at random — and dumping the wrong database would ship bad data.
    """
    explicit = os.getenv("PG_CONTAINER")
    if explicit:
        return explicit

    out = subprocess.run(
        ["docker", "compose", "ps", "-q", "postgres"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    cid = out.stdout.strip().splitlines()
    if not cid or not cid[0]:
        raise SystemExit(
            "找不到這個 repo 的 postgres 容器。先在 course-data-prep 底下跑 "
            "`docker compose up -d postgres`,或用 PG_CONTAINER 指定名稱。"
        )
    return cid[0]


def main() -> None:
    conn = psycopg2.connect(database_url())
    try:
        _verify(conn)
    finally:
        conn.close()

    os.makedirs(OUT_DIR, exist_ok=True)
    name = f"course-{COURSE_YEAR}{COURSE_SEMESTER}.sql.gz"
    host_path = os.path.join(OUT_DIR, name)

    container = _find_container()
    print(f"[dump] 在容器 {container} 裡跑 pg_dump ...", flush=True)

    subprocess.run(
        [
            "docker", "exec", container, "bash", "-c",
            "pg_dump -U postgres -d academic -Fp --no-owner --no-privileges "
            f"| gzip -c > /tmp/{name}",
        ],
        check=True,
    )
    subprocess.run(
        ["docker", "cp", f"{container}:/tmp/{name}", host_path], check=True
    )
    subprocess.run(["docker", "exec", container, "rm", "-f", f"/tmp/{name}"], check=True)

    size_mb = os.path.getsize(host_path) / 1024 / 1024
    print(f"[dump] 完成:{host_path}({size_mb:.1f} MB)", flush=True)
    print("[dump] 把它複製到 CourseLangChain/db/init/,然後:", flush=True)
    print("[dump]   docker compose down -v && docker compose up -d postgres", flush=True)


if __name__ == "__main__":
    sys.exit(main())
