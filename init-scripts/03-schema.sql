ALTER TABLE public.course ADD COLUMN IF NOT EXISTS embedding vector(${EMBED_DIM});

-- course.id is not unique; ParadeDB's key_field must be.
ALTER TABLE public.course ADD COLUMN IF NOT EXISTS pk bigserial;
CREATE UNIQUE INDEX IF NOT EXISTS idx_course_pk ON public.course (pk);

ALTER TABLE public.course ADD COLUMN IF NOT EXISTS content_text TEXT
    GENERATED ALWAYS AS (
        COALESCE(name, '') || ' ' ||
        COALESCE(nameen, '') || ' ' ||
        COALESCE(objective, '') || ' ' ||
        COALESCE(syllabus, '') || ' ' ||
        COALESCE(schedule, '')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_course_bm25 ON public.course
    USING bm25 (pk, (content_text::pdb.jieba))
    WITH (key_field = 'pk');
