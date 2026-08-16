-- One row per embeddable segment of a course, instead of one vector per course.
--
-- The model reads 512 tokens and drops the rest. A course's fields together run
-- well past that — schedule alone has a median of 464 — so a single vector
-- always lost whatever came last, and evaluation and textbook never reached the
-- model at all. Splitting each field and embedding every segment removes the
-- ceiling, and lets a question about the textbook match the textbook.
--
-- course_id is course.id, which is NOT unique in course (a course appears once
-- per category it belongs to), so this cannot carry a foreign key. Chunks are
-- built once per distinct id.
CREATE TABLE IF NOT EXISTS public.course_chunk (
    chunk_id   bigserial PRIMARY KEY,
    course_id  text NOT NULL,
    section    text NOT NULL,          -- the column it came from
    chunk_text text NOT NULL,
    embedding  vector(${EMBED_DIM})
);

CREATE INDEX IF NOT EXISTS idx_course_chunk_course
    ON public.course_chunk (course_id);

-- Deliberately no HNSW index. pgvector caps an index scan at ef_search
-- candidates, and this ParadeDB build does not expose hnsw.ef_search to raise
-- it, so the vector side returned only ~40 rows however large the LIMIT — about
-- 12 distinct courses once chunks are counted, while BM25 supplied 400. RRF was
-- fusing lopsided candidate sets.
--
-- A sequential scan over 28,634 vectors costs 43-108ms and returns 141-306
-- distinct courses for the same queries: exact instead of approximate, no
-- ceiling, and 89MB smaller in the shipped artifact. Revisit if the corpus
-- grows by an order of magnitude.
DROP INDEX IF EXISTS course_chunk_hnsw_idx;

-- Vectors live in course_chunk now. Leaving the old column would make it
-- ambiguous in any query that joins the two tables, and it ships in the
-- artifact as a misleading column of NULLs.
ALTER TABLE public.course DROP COLUMN IF EXISTS embedding;
