"""Split each course into chunks and embed them. The only GPU-bound step.

    uv run python -m prep.embed
    EMBED_DEVICE=cuda uv run python -m prep.embed

Skips courses that already have chunks, so it resumes after an interrupt.
"""
from __future__ import annotations

import sys

import psycopg2

from .chunk import CHUNK_COLUMNS, build_chunks, make_splitter
from .contract import COURSE_SEMESTER, COURSE_YEAR, EMBED_DIM, EMBED_MODEL, database_url
from .device import pick_device

BATCH_SIZE = 32


def vec_to_pg(embedding) -> str:
    return "[" + ",".join(map(str, embedding.tolist())) + "]"


def main() -> None:
    import torch
    from sentence_transformers import SentenceTransformer

    device = pick_device()
    print(f"[embed] loading {EMBED_MODEL} (device={device}) ...", flush=True)
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
            f"model dimension {actual_dim} does not match embed_dim={EMBED_DIM} in "
            "contract.yaml.\nChanging the model means shipping a new contract too, "
            f"and course_chunk.embedding is already typed vector({EMBED_DIM}) — "
            "rerun migrate and clear course_chunk."
        )
    print(f"[embed] dimension {actual_dim} matches the contract.", flush=True)

    conn = psycopg2.connect(database_url())
    conn.autocommit = False
    cur = conn.cursor()

    splitter = make_splitter(model.tokenizer)
    sem = (COURSE_YEAR, COURSE_SEMESTER)

    # Chunks are per distinct course, not per row: a course appears in course
    # once for every category it belongs to.
    cur.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT id FROM public.course
            WHERE y = %s AND s = %s
              AND id NOT IN (SELECT course_id FROM public.course_chunk)
        ) t
        """,
        sem,
    )
    total = cur.fetchone()[0]
    print(f"[embed] {COURSE_YEAR}-{COURSE_SEMESTER}: {total} courses to process", flush=True)

    if total == 0:
        print("[embed] every course already has chunks.", flush=True)
        cur.close()
        conn.close()
        return

    processed = written = 0
    while True:
        cur.execute(
            f"""
            SELECT DISTINCT ON (id) id, {", ".join(CHUNK_COLUMNS)}
            FROM public.course
            WHERE y = %s AND s = %s
              AND id NOT IN (SELECT course_id FROM public.course_chunk)
            ORDER BY id
            LIMIT %s
            """,
            (*sem, BATCH_SIZE),
        )
        rows = cur.fetchall()
        if not rows:
            break

        pending = []
        for row in rows:
            course_id = row[0]
            course = dict(zip(CHUNK_COLUMNS, row[1:]))
            for section, text in build_chunks(splitter, course):
                pending.append((course_id, section, text))

        embeddings = model.encode(
            [text for _, _, text in pending],
            batch_size=BATCH_SIZE,
            show_progress_bar=False,
        )

        skipped = 0
        for (course_id, section, text), emb in zip(pending, embeddings):
            if torch.isnan(torch.tensor(emb)).any():
                print(f"[embed] warning: {course_id}/{section} produced NaN, storing a zero vector", flush=True)
                emb = torch.zeros(EMBED_DIM)
                skipped += 1
            cur.execute(
                "INSERT INTO public.course_chunk (course_id, section, chunk_text, embedding) "
                "VALUES (%s, %s, %s, %s::vector)",
                (course_id, section, text, vec_to_pg(emb)),
            )

        conn.commit()
        processed += len(rows)
        written += len(pending)
        msg = f"[embed] {processed}/{total} courses ({100 * processed // total}%), {written} chunks"
        if skipped:
            msg += f"  [{skipped} NaN]"
        print(msg, flush=True)

    print(f"[embed] all done, {written} chunks.", flush=True)
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
