"""Prepares the data artifact consumed by CourseLangChain.

    prep.migrate        apply DDL, write schema_meta
    prep.populate_time  time_raw -> structured time columns
    prep.embed          course text -> vectors (the GPU step)
    prep.load_v4        course_struct.db (scheduling_test) -> the v4 schema
    prep.check          verify the database is fit to ship
    prep.release        all of the above for one semester, into releases/<semester>_<time>/
"""
