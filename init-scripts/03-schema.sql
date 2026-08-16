-- course.id is not unique; ParadeDB's key_field must be.
ALTER TABLE public.course ADD COLUMN IF NOT EXISTS pk bigserial;
CREATE UNIQUE INDEX IF NOT EXISTS idx_course_pk ON public.course (pk);

-- Built from whichever of these the semester actually has, because the crawler's
-- column set grows: NCCU adds syllabus sections over time and each new one
-- becomes its own column. A fixed expression breaks on a semester missing any.
--
-- The list is deliberately short. Everything here has to discriminate between
-- courses, and BM25 normalises by document length, so a field that reads the
-- same on every course does not merely add noise — it depresses the score of
-- the fields that do carry signal. Measured over 115-1:
--
--   teaching_approach   identical wording on all 3568 courses, only the
--                       percentages vary — 討論 matches even a 0% course
--   ai_policy           'This Course Does Not Involve the Use of AI' (1058
--                       courses) contains 'AI', so it answers the opposite query
--   detail_*            1853 and 2387 chars of grading-rubric boilerplate;
--                       one has 242 distinct values across 1859 courses
--
-- Those four are still returned by the consumer's course_detail tool, so the
-- model can discuss them once a course is identified — they just should not be
-- what surfaces it.
--
-- teacher is in the list for the opposite reason. A name is a handful of
-- characters, so it costs nothing against length normalisation, and it is the
-- one field vectors cannot substitute for: an embedding has no way to place a
-- rare proper noun, so "翁堃嵐 老師開的課" returned unrelated language courses
-- until BM25 could match the name literally.
DO $$
DECLARE expr text;
BEGIN
    SELECT string_agg(
               format('COALESCE(%I, %L)', column_name, ''),
               ' || '' '' || ' ORDER BY ordinal_position)
      INTO expr
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'course'
      AND column_name IN ('name', 'nameen', 'teacher', 'objective', 'syllabus',
                          'schedule', 'evaluation', 'textbook');

    IF expr IS NULL THEN
        RAISE EXCEPTION 'course has none of the expected text columns';
    END IF;

    -- A generated column's expression cannot be altered in place. Dropping it
    -- takes idx_course_bm25 with it, which the CREATE INDEX below restores.
    ALTER TABLE public.course DROP COLUMN IF EXISTS content_text;
    EXECUTE format(
        'ALTER TABLE public.course ADD COLUMN content_text TEXT '
        'GENERATED ALWAYS AS (%s) STORED', expr);
END;
$$;

CREATE INDEX IF NOT EXISTS idx_course_bm25 ON public.course
    USING bm25 (pk, (content_text::pdb.jieba))
    WITH (key_field = 'pk');
