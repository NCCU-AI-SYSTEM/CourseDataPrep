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
- A checkout of `scheduling_test` next to this repo (or `V4_REPO=<path>`): its
  `prepare_semester` builds the v4 search data (`course_struct.db`)

## One command

```sh
docker compose up -d postgres
uv sync
uv run python -m prep.release build --source <crawler>.db            # build and check
uv run python -m prep.release build --source <crawler>.db --promote  # ... and make it current
uv run python -m prep.release build --from embed                     # resume the last .partial
uv run python -m prep.release list | promote <name> | rollback
```

It runs every step below in order, for the semester in `contract.yaml`, and
writes one folder per build:

```
releases/
  115-1_20261102-0300/
    data.sql.gz        the artifact (schemas public and v4)
    contract.yaml      copy of this repo's, ships with it
    manifest.json      sha256 of each file and of the source, row counts, commits, step times
    prepare_report.md  the v4 coverage gate, the diff against the live build, PostgreSQL counts
    course_struct.db   the v4 data as SQLite, for scheduling_test's offline evaluation
    logs/
  current  -> 115-1_20261102-0300
  previous -> 115-1_20261101-0300
```

A step that fails stops the build in `<name>.partial/`; nothing becomes `current`
until every step, including `prep.check`, has passed. Trial on 114-2 (RTX 5080):
3.5 minutes, of which embed 115 s and dump 48 s.

The sections below are the same steps by hand.

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
    -- The join to information_schema.tables is load-bearing: columns lists views
    -- too, and an image that ships pg_stat_statements puts pg_stat_statements_info
    -- in public with a bigint column. ALTER COLUMN on a view is a hard error.
    FOR r IN (SELECT c.table_name, c.column_name
              FROM information_schema.columns c
              JOIN information_schema.tables t
                ON t.table_schema = c.table_schema AND t.table_name = c.table_name
              WHERE c.table_schema = 'public'
                AND c.data_type = 'bigint'
                AND t.table_type = 'BASE TABLE')
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE integer', r.table_name, r.column_name);
    END LOOP;
END;
\$\$;"
```

Run this immediately after pgloader, before step 4. `course.pk` and
`course_chunk.chunk_id` are `bigserial`, so running it again once the schema is
applied would narrow those keys to `integer` as well.

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

Adds the surrogate key and BM25 index (`pdb.jieba` tokenizer), the structured
time columns and the `course_chunk` table, and records the contract in
`schema_meta`:

```sh
uv run python -m prep.migrate
# [migrate] applying 02-extensions.sql ...
# [migrate] applying 03-schema.sql ...
# [migrate] applying 04-time-columns.sql ...
# [migrate] applying 05-chunks.sql ...
# [migrate] done. schema_meta:
#            schema_version = 9
#            embed_model = BAAI/bge-base-zh-v1.5
#            ...
#            v4_embed_model = intfloat/multilingual-e5-large-instruct
```

Idempotent, so it also upgrades an existing database.

`content_text` — what BM25 sees — is assembled from whichever of `name`,
`nameen`, `teacher`, `objective`, `syllabus`, `schedule`, `evaluation`,
`textbook` the semester actually has. The list is short on purpose: BM25
normalises by document length, so a field that reads the same on every course
depresses the ones that carry signal. `teaching_approach`, `ai_policy` and the
`detail_*` sections are boilerplate by that measure and stay out — the
consumer's `course_detail` tool still returns them once a course is identified.

`teacher` is in the list because vectors cannot replace it: an embedding has
nowhere to put a rare proper noun, so searching a teacher's name returns
unrelated courses unless BM25 can match it literally.

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

Each course is split per field with a recursive character splitter and every
segment is embedded on its own, into `course_chunk`:

```sh
uv run python -m prep.embed
# [embed] loading BAAI/bge-base-zh-v1.5 (device=mps) ...
# [embed] dimension 768 matches the contract.
# [embed] 2925/2925 courses (100%), 28634 chunks
# [embed] all done, 28634 chunks.
```

One vector per course does not work: the model reads 512 tokens and drops the
rest, schedule alone has a median of 464, and 82% of courses overran it — so
whatever came last never reached the model. Chunking removes the ceiling and
lets a question about the textbook match the textbook instead of a blend of
everything the course says.

**There is no vector index, on purpose.** pgvector caps an HNSW scan at
`ef_search` candidates and this ParadeDB build does not expose
`hnsw.ef_search`, so the vector side returned ~40 rows however large the LIMIT
— about 12 distinct courses — while BM25 supplied 400. A sequential scan over
28,634 vectors costs 43-108ms and yields 141-306 courses: exact, uncapped, and
89MB smaller in the artifact.

The device is chosen `cuda` → `mps` → `cpu`, or set it explicitly:

```sh
EMBED_DEVICE=cuda uv run python -m prep.embed
```

Courses that already have chunks are skipped, so an interrupted run resumes by
rerunning. Roughly ten minutes for ~2,925 courses on Apple Silicon;
considerably longer on CPU.

torch is deliberately **not** pinned to the CPU wheel index (see
`pyproject.toml`), so Linux and Windows with NVIDIA get the CUDA build, and the
native macOS wheels already support MPS.

## 6b. Load the v4 search

`scheduling_test`'s `prepare_semester` builds `course_struct.db` for the same
semester (structured fields, sessions, assessment, e5 vectors) and stops when its
coverage gate fails. Load it into the `v4` schema:

```sh
uv run python -m prep.load_v4 --db <scheduling_test>/data/v4/staging/115-1/course_struct.db
```

The file is refused when its semester, embedding model or dimension disagree with
`contract.yaml`. The load replaces `v4` in one transaction and leaves `public` alone.

## 7. Build the artifact

Check first. Two things block shipping — `schema_meta` disagreeing with
`contract.yaml`, and any course without an embedded chunk — and both fail silently
downstream:

```sh
uv run python -m prep.check
# [check] passed: 2795 courses, 27896 chunks, all embedded; v4 114-2: 2655 searchable courses.
```

Then dump. `pg_dump` runs inside the container, since a development machine
usually has no postgres client; `gzip` runs here. `pipefail` makes a failed
`pg_dump` fail the whole pipe instead of leaving a truncated `data.sql.gz`:

```sh
set -o pipefail
docker compose exec -T postgres pg_dump -U postgres -d academic \
  -Fp --no-owner --no-privileges -n public -n v4 \
  | sed 's/^CREATE SCHEMA public;$/CREATE SCHEMA IF NOT EXISTS public;/' | gzip > data.sql.gz
```

`-T` matters: without it compose allocates a TTY and corrupts the pipe.

`-n public -n v4` matters too: the ParadeDB image creates the `paradedb`, `tiger` and
`topology` schemas (pg_search, PostGIS) in every new database, so a full dump restored
into a fresh container stops at `schema "paradedb" already exists`. The extensions the
data needs, `vector` and `pg_search`, come with the image. Naming `public` with `-n` makes pg_dump create
it, which every new database already has; the `sed` turns that into `IF NOT EXISTS`.

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
| `prep.embed` | Splits each field with `chunk.py` and embeds every segment into `course_chunk`, using the model named in `contract.yaml`. Deliberately builds no vector index — see step 6. |
| `prep.check` | Blocks shipping on a contract mismatch or a missing vector. Building the artifact is one shell pipe, so it stays in the README. |
| `prep.build_sqlite` | **Deprecated.** Indexes for the old SQLite/FAISS path. Needs `uv sync --extra sqlite`. |

## Troubleshooting

### Embedding model fails to download (401 / GatedRepoError)

`contract.yaml` names a gated model, which requires access approval on
HuggingFace. Either set `HF_TOKEN`, or switch to an ungated model of the same
dimension and rerun `prep.embed`.

### `could not resize shared memory segment`

Docker gives `/dev/shm` only 64MB by default. This repo's compose sets
`shm_size: 1gb`; add it if you start the container some other way.

### `prep.check` reports courses without an embedded chunk

`prep.embed` did not finish. Rerun it — it skips courses that already have chunks.

### Dimension mismatch

`[embed] model dimension X does not match embed_dim=Y in contract.yaml`.
Changing the model
means changing `embed_dim` too, and `course_chunk.embedding` is already typed
`vector(Y)`, so rerun `migrate` and clear `course_chunk`.
