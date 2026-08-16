-- "time" is reserved; DO block so migrate stays re-runnable.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'course' AND column_name = 'time'
    ) THEN
        ALTER TABLE public.course RENAME COLUMN "time" TO time_raw;
    END IF;
END $$;

-- Filled by populate_time.py. sessions: [{"weekday":1,"start_hour":8,"end_hour":10}, ...]
ALTER TABLE public.course
    ADD COLUMN IF NOT EXISTS sessions      jsonb,
    ADD COLUMN IF NOT EXISTS weekdays      integer[],
    ADD COLUMN IF NOT EXISTS has_morning   boolean,
    ADD COLUMN IF NOT EXISTS has_noon      boolean,
    ADD COLUMN IF NOT EXISTS has_afternoon boolean,
    ADD COLUMN IF NOT EXISTS has_evening   boolean;

CREATE INDEX IF NOT EXISTS idx_course_weekdays
    ON public.course USING GIN (weekdays);

CREATE INDEX IF NOT EXISTS idx_course_has_morning   ON public.course (has_morning)   WHERE has_morning   = true;
CREATE INDEX IF NOT EXISTS idx_course_has_noon      ON public.course (has_noon)      WHERE has_noon      = true;
CREATE INDEX IF NOT EXISTS idx_course_has_afternoon ON public.course (has_afternoon) WHERE has_afternoon = true;
CREATE INDEX IF NOT EXISTS idx_course_has_evening   ON public.course (has_evening)   WHERE has_evening   = true;

CREATE INDEX IF NOT EXISTS idx_course_semester ON public.course (y, s);
