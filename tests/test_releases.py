"""prep.releases (release folders, promote, rollback, manifest) and prep.release's pure parts.

    uv run python -m tests.test_releases
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

import sqlite3

from prep import releases as R
from prep.release import STEPS, dump_line, pgloader_errors, resume_problems, source_type_problems, steps_from

# pgloader 3.6.10 summaries from the 114-2 trial (hopelab, 2026/11/02): a clean load, and
# the old crawler file whose COURSE.smtQty held text (every row rejected, exit code 0).
PGLOADER_OK = """\
2026-11-02T01:32:45.911958Z LOG report summary reset
             table name     errors       rows      bytes      total time
-----------------------  ---------  ---------  ---------  --------------
                  fetch          0          0                     0.000s
        fetch meta data          0          8                     0.014s
          Create tables          0          8                     0.007s
-----------------------  ---------  ---------  ---------  --------------
          public.course          0     101883   207.2 MB          2.019s
         public.teacher          0       1067    17.7 kB          0.070s
            public.rate          0     269241    31.5 MB          0.519s
          public.result          0       5396   379.2 kB          0.045s
-----------------------  ---------  ---------  ---------  --------------
COPY Threads Completion          0          2                     2.540s
"""
PGLOADER_REJECTED = """\
2026-11-02T01:29:05.165997Z ERROR Error while formatting a row from "public.course":
2026-11-02T01:29:05.165997Z ERROR junk in string "單學期科目"
          public.course     101883          0                     1.579s
         public.teacher          0       1067    17.7 kB          0.049s
      Total import time     101883     275704    31.9 MB          2.288s
"""


def _dirs(root: Path, *names: str) -> None:
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "manifest.json").write_text(json.dumps({"semester": n.split("_")[0], "built_at": n}))


def test_release_name() -> None:
    assert R.release_name("115-1", datetime(2026, 11, 2, 3, 7)) == "115-1_20261102-0307"


def test_promote_and_rollback() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _dirs(root, "115-1_20261101-0300", "115-1_20261102-0300")
        assert R.current(root) is None
        R.promote(root, "115-1_20261101-0300")
        assert R.current(root) == "115-1_20261101-0300" and R.previous(root) is None
        R.promote(root, "115-1_20261102-0300")
        assert (R.current(root), R.previous(root)) == ("115-1_20261102-0300", "115-1_20261101-0300")
        assert (root / "current").is_symlink() and not Path(R.os.readlink(root / "current")).is_absolute()
        R.rollback(root)
        assert (R.current(root), R.previous(root)) == ("115-1_20261101-0300", "115-1_20261102-0300")
        R.promote(root, "115-1_20261101-0300")      # promoting what is already current changes nothing
        assert (R.current(root), R.previous(root)) == ("115-1_20261101-0300", "115-1_20261102-0300")


def test_promote_refuses_partial_or_missing() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _dirs(root, "115-1_20261102-0300.partial")
        for name in ("115-1_20261102-0300.partial", "115-1_19990101-0000"):
            try:
                R.promote(root, name)
            except SystemExit:
                continue
            raise AssertionError(f"promoted {name}")


def test_rollback_without_previous_fails() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _dirs(root, "115-1_20261101-0300")
        R.promote(root, "115-1_20261101-0300")
        try:
            R.rollback(root)
        except SystemExit:
            return
        raise AssertionError("rolled back with no previous release")


def test_listing_and_latest_partial() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _dirs(root, "114-2_20261020-0300", "115-1_20261101-0300", "115-1_20261102-0300.partial",
              "115-1_20261102-0400.partial")
        assert [r.name for r in R.listing(root)] == [
            "115-1_20261102-0400.partial", "115-1_20261102-0300.partial", "115-1_20261101-0300",
            "114-2_20261020-0300"]
        assert R.latest_partial(root, "115-1").name == "115-1_20261102-0400.partial"
        assert R.latest_partial(root, "114-2") is None


def test_file_entry_hashes() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.txt"
        p.write_bytes(b"abc")
        assert R.file_entry(p) == {
            "bytes": 3, "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}


def test_steps_from() -> None:
    assert steps_from(None) == STEPS
    assert steps_from("load_v4")[0] == "load_v4" and steps_from("load_v4")[-1] == STEPS[-1]
    try:
        steps_from("nope")
    except SystemExit:
        return
    raise AssertionError("accepted an unknown step")


def test_pgloader_errors() -> None:
    ok = "2026-11-02T03:00:00 LOG report summary reset\n   table name  errors  rows\n public.course  0  3650\n"
    assert pgloader_errors(ok) == []
    bad = ok + "2026-11-02T03:00:01 ERROR Database error 23502: null value in column \"id\"\n"
    assert len(pgloader_errors(bad)) == 1
    counted = ok.replace("public.course  0  3650", "public.course  2  3648")
    assert pgloader_errors(counted) == ["public.course: 2 rows failed"]
    assert pgloader_errors(PGLOADER_OK) == []
    rejected = pgloader_errors(PGLOADER_REJECTED)
    assert "public.course: 101883 rows failed" in rejected and len(rejected) == 3, rejected


def test_resume_problems() -> None:
    done = {"step_seconds": {s: 1.0 for s in STEPS[:STEPS.index("dump")]}}
    assert resume_problems(done, "dump", "115-1_a.partial", "115-1_a.partial") == []
    # dump failed, then --from manifest: dump never finished
    assert resume_problems(done, "manifest", "115-1_a.partial", "115-1_a.partial") == [
        "step dump has not finished in this build"]
    # another build reset the shared database since
    assert any("115-1_b.partial" in p for p in resume_problems(done, "dump", "115-1_a.partial", "115-1_b.partial"))
    # resuming at or before reset does not care who reset the database last
    assert resume_problems({"step_seconds": {"v4": 1.0}}, "reset", "115-1_a.partial", None) == []


def test_source_type_problems() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "crawl.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE COURSE (id TEXT, smtQty INTEGER, kind INTEGER, point REAL)")
        con.executemany("INSERT INTO COURSE VALUES (?, ?, ?, ?)",
                        [("a", "單學期科目", 1, 3.0), ("b", "", 2, 2.0), ("c", None, 1, "3")])
        con.commit()
        con.close()
        problems = source_type_problems(path)
        assert len(problems) == 1 and "COURSE.smtQty" in problems[0] and " 2 " in problems[0], problems   # "" counts too


def test_dump_line() -> None:
    assert dump_line(b"CREATE SCHEMA public;\n") == b"CREATE SCHEMA IF NOT EXISTS public;\n"
    assert dump_line(b"CREATE SCHEMA v4;\n") == b"CREATE SCHEMA v4;\n"
    assert dump_line(b"INSERT INTO t VALUES ('CREATE SCHEMA public;');\n").startswith(b"INSERT")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            result = fn()
            print(f"  {result or 'ok'}  {name}")
    print("all passed")
