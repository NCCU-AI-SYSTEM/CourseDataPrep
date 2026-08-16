"""Write vectors into course.embedding. The only GPU-bound step.

    uv run python -m prep.embed
    EMBED_DEVICE=cuda uv run python -m prep.embed

Only touches rows where embedding IS NULL, so it resumes after an interrupt.
"""
from __future__ import annotations

import sys

import psycopg2

from .contract import COURSE_SEMESTER, COURSE_YEAR, EMBED_DIM, EMBED_MODEL, database_url
from .device import pick_device

BATCH_SIZE = 32
MAX_TEXT_CHARS = 3000


def build_doc_text(name, nameen, objective, syllabus, schedule) -> str:
    title = " / ".join(filter(None, [name, nameen]))
    body = " ".join(filter(None, [objective, syllabus, schedule]))
    return (f"title: {title} | text: {body}")[:MAX_TEXT_CHARS]


def vec_to_pg(embedding) -> str:
    return "[" + ",".join(map(str, embedding.tolist())) + "]"


def create_vector_index(cur, conn) -> None:
    print("[embed] 建立 HNSW cosine 索引 ...", flush=True)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS course_embedding_hnsw_idx
        ON public.course
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
    conn.commit()
    print("[embed] 索引完成。", flush=True)


def main() -> None:
    import torch
    from sentence_transformers import SentenceTransformer

    device = pick_device()
    print(f"[embed] 載入 {EMBED_MODEL}(device={device}) ...", flush=True)
    model = SentenceTransformer(
        EMBED_MODEL, device=device, model_kwargs={"torch_dtype": torch.float32}
    )

    actual_dim = (
        model.get_embedding_dimension()
        if hasattr(model, "get_embedding_dimension")
        else model.get_sentence_embedding_dimension()
    )
    if actual_dim != EMBED_DIM:
        raise SystemExit(
            f"模型維度 {actual_dim} 與 contract.yaml 的 embed_dim={EMBED_DIM} 不符。\n"
            f"兩個 repo 的 contract.yaml 要一起改,而且 course.embedding 欄位型別是 "
            f"vector({EMBED_DIM}) —— 換模型要重跑 migrate 並清掉舊向量。"
        )
    print(f"[embed] 維度 {actual_dim} 與契約相符。", flush=True)

    conn = psycopg2.connect(database_url())
    conn.autocommit = False
    cur = conn.cursor()

    sem = (COURSE_YEAR, COURSE_SEMESTER)
    cur.execute(
        "SELECT COUNT(*) FROM public.course "
        "WHERE y = %s AND s = %s AND embedding IS NULL",
        sem,
    )
    total = cur.fetchone()[0]
    print(f"[embed] {COURSE_YEAR}-{COURSE_SEMESTER} 待處理 {total} 門", flush=True)

    if total == 0:
        print("[embed] 全部都已經有向量了。", flush=True)
        create_vector_index(cur, conn)
        cur.close()
        conn.close()
        return

    processed = 0
    while True:
        cur.execute(
            """
            SELECT id, name, nameen, objective, syllabus, schedule
            FROM public.course
            WHERE y = %s AND s = %s AND embedding IS NULL
            LIMIT %s
            """,
            (*sem, BATCH_SIZE),
        )
        rows = cur.fetchall()
        if not rows:
            break

        ids = [r[0] for r in rows]
        texts = [build_doc_text(*(c or "" for c in r[1:])) for r in rows]
        embeddings = model.encode(texts, batch_size=BATCH_SIZE, show_progress_bar=False)

        skipped = 0
        for course_id, emb in zip(ids, embeddings):
            if torch.isnan(torch.tensor(emb)).any():
                print(f"[embed] 警告:id={course_id} 產生 NaN,改存零向量", flush=True)
                emb = torch.zeros(EMBED_DIM)
                skipped += 1
            cur.execute(
                "UPDATE public.course SET embedding = %s::vector WHERE id = %s",
                (vec_to_pg(emb), course_id),
            )

        conn.commit()
        processed += len(rows)
        msg = f"[embed] {processed}/{total} ({100 * processed // total}%)"
        if skipped:
            msg += f"  [{skipped} 筆 NaN]"
        print(msg, flush=True)

    print("[embed] 全部完成。", flush=True)
    create_vector_index(cur, conn)
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
