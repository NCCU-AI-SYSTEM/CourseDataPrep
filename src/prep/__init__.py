"""Prepares the data artifact consumed by CourseLangChain.

    prep.migrate        apply DDL, write schema_meta
    prep.populate_time  time_raw -> structured time columns
    prep.embed          course text -> vectors (the GPU step)
    prep.dump           verify, then emit course-<y><s>.sql.gz
"""
