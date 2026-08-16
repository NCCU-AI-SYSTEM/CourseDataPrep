# CourseDataPrep

Turns the crawler's SQLite database into a PostgreSQL artifact — one
`data.sql.gz` plus the `contract.yaml` that describes it.

This repo carries its own PostgreSQL (`docker compose`, port **5433**). Raw
course data and every intermediate state stay here; only the finished artifact
leaves.

Run once per semester, by whoever maintains the data. If you already have an
artifact and just want to run the application, you do not need this repo.

## Requirements

- `python >= 3.13` and [`uv`](https://github.com/astral-sh/uv)
- Docker
- A SQLite database from the crawler ([NCCUCourse](https://github.com/NCCUCourseScheduling/NCCUCourse)), not in git

---

# Steps

## 1. Start the database

```sh
docker compose up -d postgres
```

## 2. Load the crawler's SQLite

The crawler produces SQLite; everything after this is PostgreSQL. pgloader moves
the rows straight into the database from step 1:

```sh
LOAD_SQLITE=<crawler>.db docker compose --profile load run --rm pgloader
#           table name     errors       rows
#  ---------------------  ---------  ---------
#          public.course          0       3650
#            public.rate          0     232652
#          public.result          0       1308
#         public.teacher          0         11
```

Then narrow the integers. SQLite has no integer width, so pgloader maps every
INTEGER to `bigint`:

```sh
docker compose exec postgres psql -U postgres -d academic -c "
DO \$\$
DECLARE r RECORD;
BEGIN
    FOR r IN (SELECT table_name, column_name FROM information_schema.columns
              WHERE table_schema='public' AND data_type='bigint')
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE integer', r.table_name, r.column_name);
    END LOOP;
END;
\$\$;"
```

Check:

```sh
docker compose exec postgres psql -U postgres -d academic \
  -c "SELECT y, s, COUNT(*) FROM course GROUP BY y,s ORDER BY y DESC LIMIT 3;"
#   y  | s | count
#  115 | 1 |  3650     <- the semester contract.yaml names
```

pgloader also folds `COURSE` to `public.course`, which is what everything
downstream queries, and recreates the primary key from the SQLite schema.

> [!IMPORTANT]
> pgloader **creates** tables, so clear the old ones before reloading:
> ```sh
> docker compose exec postgres psql -U postgres -d academic \
>   -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
> ```
> `CASCADE` also drops the `vector` and `pg_search` extensions, since they live
> in `public` — the `drop cascades to extension ...` lines are expected, and
> step 4 recreates them.
>
> pgloader logs constraint failures and **exits 0 regardless**. A NULL in a
> primary-key column, for one, makes the whole PRIMARY KEY disappear. If the
> output contains `ERROR`, do not continue.

## 3. Install

```sh
uv sync
```

`DATABASE_URL` needs no setting; it defaults to this repo's own database
(`postgresql://postgres:postgres@localhost:5433/academic`).

## 4. Apply the schema

Adds the vector column, the BM25 index (`pdb.jieba` tokenizer) and the
structured time columns, and records the contract in `schema_meta`:

```sh
uv run python -m prep.migrate
# [migrate] applying 02-extensions.sql ...
# [migrate] applying 03-schema.sql ...
# [migrate] applying 04-time-columns.sql ...
# [migrate] done. schema_meta:
#            schema_version = 6
#            embed_model = BAAI/bge-base-zh-v1.5
```

Idempotent, so it also upgrades an existing database.

## 5. Parse class times

Splits `time_raw` (such as `五234`) into `sessions` / `weekdays` /
`has_morning`:

```sh
uv run python -m prep.populate_time
# [populate] 3650/3650
# [populate] done, 3650 rows updated.
```

Spot check:

```sh
docker compose exec postgres psql -U postgres -d academic \
  -c "SELECT time_raw, sessions FROM course WHERE time_raw='五234' LIMIT 1;"
#  五234 | [{"weekday": 5, "end_hour": 12, "start_hour": 9}]
```

`五234` is Friday periods 2–4, which is 09:00–12:00.

## 6. Generate vectors — the GPU step

```sh
uv run python -m prep.embed
# [embed] loading BAAI/bge-base-zh-v1.5 (device=mps) ...
# [embed] dimension 768 matches the contract.
# [embed] 3650/3650 (100%)
# [embed] building the HNSW cosine index ...
```

The device is chosen `cuda` → `mps` → `cpu`, or set it explicitly:

```sh
EMBED_DEVICE=cuda uv run python -m prep.embed
```

Only rows with `embedding IS NULL` are processed, so an interrupted run resumes
by rerunning. A few minutes for ~3,650 courses on Apple Silicon; considerably
longer on CPU.

torch is deliberately **not** pinned to the CPU wheel index (see
`pyproject.toml`), so Linux and Windows with NVIDIA get the CUDA build, and the
native macOS wheels already support MPS.

## 7. Build the artifact

Check first. Two things block shipping — `schema_meta` disagreeing with
`contract.yaml`, and any course without a vector — and both fail silently
downstream:

```sh
uv run python -m prep.check
# [check] passed: 3650 courses, all with vectors.
```

Then dump. `pg_dump` runs inside the container, since a development machine
usually has no postgres client; `gzip` runs here:

```sh
docker compose exec -T postgres pg_dump -U postgres -d academic \
  -Fp --no-owner --no-privileges | gzip > data.sql.gz
```

`-T` matters: without it compose allocates a TTY and corrupts the pipe.

The format is `-Fp … | gzip`, not `-Fc`: the postgres entrypoint restores
`*.sql` and `*.sql.gz` on first start but ignores `.dump`, so plain SQL needs no
restore script on the consuming side. Both formats measured the same size with
byte-identical data.

Two dumps of one database are not byte-identical — PG18 writes a random token
into the `\restrict` line — but the data is.

## 8. Ship it

Two files make up a release:

```
data.sql.gz      the data
contract.yaml    what the data is — which semester, which embedding model
```

The filename carries no semester because `contract.yaml` already states it, and
the two only mean anything together.

Copy **both** to whoever consumes them. The artifact is restored by the
postgres entrypoint; `contract.yaml` is copied as-is, not edited on the other
side — see below.

Raw course data stays here. It has no place in a runtime repo.

---

## contract.yaml

Describes what the artifact contains: schema version, embedding model,
dimension, and semester. `prep.migrate` writes these values into the database's
`schema_meta` table, and they travel inside the artifact.

The consumer keeps a copy of this file and compares it against `schema_meta` at
startup, refusing to serve on a mismatch. That is why it ships **as a copy**
rather than being maintained separately in two places — a comparison someone has
to remember to run is not a safeguard.

What this prevents: re-embedding here with a different model while the consumer
still expects the old one. Query vectors and stored vectors then come from
different models, and **retrieval does not fail — it quietly returns
semantically unrelated courses**, which is the hardest kind of bug to notice.

To change the contract: edit it here, rerun `migrate`, clear the old vectors,
rerun `embed`, build a new artifact, and ship the file alongside it.

## What each module does

| Module | Purpose |
|---|---|
| `prep.migrate` | Applies `init-scripts/02~04` and creates `schema_meta`. `03-schema.sql` builds the BM25 index `USING bm25 (pk, (content_text::pdb.jieba))` — `pk` is a surrogate key, because ParadeDB's `key_field` must be unique and `course.id` is not (101,883 rows carry 81,226 distinct ids). `content_text` is assembled from whichever text columns exist, so a semester with extra syllabus sections indexes them without a code change. |
| `prep.populate_time` | `time_raw` → `sessions` jsonb, `weekdays`, and the `has_*` flags. The parser in `course_time.py` is a hardened copy of the consumer's; the two deliberately share no code. |
| `prep.embed` | Encodes with the model named in `contract.yaml`, writes `course.embedding`, then builds the HNSW cosine index. |
| `prep.check` | Blocks shipping on a contract mismatch or a missing vector. Building the artifact is one shell pipe, so it stays in the README. |
| `prep.build_sqlite` | **Deprecated.** Indexes for the old SQLite/FAISS path. Needs `uv sync --extra sqlite`. |

## Troubleshooting

### Embedding model fails to download (401 / GatedRepoError)

`contract.yaml` names a gated model, which requires access approval on
HuggingFace. Either set `HF_TOKEN`, or switch to an ungated model of the same
dimension and rerun `prep.embed`.

### `could not resize shared memory segment` while building the HNSW index

Docker gives `/dev/shm` only 64MB by default. This repo's compose sets
`shm_size: 1gb`; add it if you start the container some other way.

### `prep.check` reports courses without vectors

`prep.embed` did not finish. Rerun it — it only processes what is still missing.

### Dimension mismatch

`[embed] model dimension X does not match embed_dim=Y in contract.yaml`.
Changing the model
means changing `embed_dim` too, and `course.embedding` is already typed
`vector(Y)`, so rerun `migrate` and clear the old vectors.
