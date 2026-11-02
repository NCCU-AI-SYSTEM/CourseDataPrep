"""One semester's artifact, end to end: the single entry point of this repo.

    uv run python -m prep.release build --source <crawler>.db              # build and check
    uv run python -m prep.release build --source <crawler>.db --promote    # ... and make it current
    uv run python -m prep.release build --from embed                       # resume the latest .partial
    uv run python -m prep.release promote 115-1_20261102-0300
    uv run python -m prep.release rollback
    uv run python -m prep.release list

The semester is contract.yaml's; edit it there (and commit) to build another one. Steps:

  v4             scheduling_test's prepare_semester: structured extraction, runtime tables,
                 coverage gate. Its course_struct.db and prepare_report.md are copied in.
  reset          drop public and v4 in this repo's database (pgloader creates tables)
  pgloader       crawler SQLite -> public; any ERROR or failed row stops the build
  narrow         bigint -> integer (README step 2), then: the semester has course rows
  migrate        DDL + schema_meta (prep.migrate)
  populate_time  time columns (prep.populate_time)
  embed          course chunks -> vectors, the GPU step (prep.embed)
  load_v4        course_struct.db -> v4 schema (prep.load_v4)
  check          prep.check, including the v4 schema
  dump           pg_dump | gzip -> data.sql.gz
  manifest       contract.yaml copy, counts, hashes, commits; renames .partial away

Machine settings come from the environment: RELEASE_DIR (default <repo>/releases), V4_REPO
(default ../scheduling_test next to this repo), EMBED_DEVICE. DATABASE_URL must stay this
repo's own database: pgloader and pg_dump run in its compose service, and reset drops schemas.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg2

from . import releases as R
from .contract import (CONTRACT_PATH, COURSE_SEMESTER, COURSE_YEAR, DEFAULT_DATABASE_URL, REPO_ROOT, SCHEMA_META,
                       SEMESTER_LABEL, database_url)

STEPS = ("v4", "reset", "pgloader", "narrow", "migrate", "populate_time", "embed", "load_v4", "check", "dump",
         "manifest")

NARROW_SQL = """
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN (SELECT c.table_name, c.column_name
              FROM information_schema.columns c
              JOIN information_schema.tables t
                ON t.table_schema = c.table_schema AND t.table_name = c.table_name
              WHERE c.table_schema = 'public' AND c.data_type = 'bigint' AND t.table_type = 'BASE TABLE')
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE integer', r.table_name, r.column_name);
    END LOOP;
END;
$$;
"""
# Only the schemas this repo writes. The ParadeDB image creates paradedb, tiger and topology
# (pg_search, PostGIS) in every new database itself, so a full dump restored into a fresh
# container stops at `schema "paradedb" already exists`. vector and pg_search come with it too.
DUMP_SCHEMAS = ("-n", "public", "-n", "v4")
_SUMMARY_ROW = re.compile(r"^\s*([\w\"]+\.[\w\"]+)\s+(\d+)\s+\d+")


@dataclass(frozen=True)
class Build:
    dir: Path
    source: Path
    v4_repo: Path
    llm: str
    skip_aspects: bool


def steps_from(step: str | None) -> tuple[str, ...]:
    if step is None:
        return STEPS
    if step not in STEPS:
        raise SystemExit(f"[release] unknown step {step!r}; steps: {', '.join(STEPS)}")
    return STEPS[STEPS.index(step):]


def pgloader_errors(output: str) -> list[str]:
    """pgloader exits 0 even when rows or whole constraints fail; read its log instead."""
    problems = [line.strip() for line in output.splitlines() if " ERROR " in line]
    for line in output.splitlines():
        m = _SUMMARY_ROW.match(line)
        if m and int(m.group(2)):
            problems.append(f"{m.group(1)}: {m.group(2)} rows failed")
    return problems


def source_type_problems(path: Path) -> list[str]:
    """Numeric columns holding text: pgloader casts by the declared type and rejects every such row.

    Older crawler files declare COURSE.smtQty INTEGER but store 單學期科目 in it (the crawler
    has since declared it TEXT)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        problems = []
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
            for _, col, decl, *_ in con.execute(f'PRAGMA table_info("{table}")'):
                d = (decl or "").upper()
                if not any(k in d for k in ("INT", "REAL", "FLOA", "DOUB", "NUM")):
                    continue
                (n,) = con.execute(f'SELECT count(*) FROM "{table}" WHERE typeof("{col}") = \'text\'').fetchone()
                if n:
                    problems.append(f"{table}.{col} is declared {decl} but holds text in {n} rows")
        return problems
    finally:
        con.close()


def _log(msg: str) -> None:
    print(f"[release] {msg}", flush=True)


def _run(cmd: list[str], log: Path, cwd: Path = Path(REPO_ROOT), env: dict | None = None) -> tuple[int, str]:
    """Stream a command's output to the console and to log; returns (exit code, output)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    out = []
    with log.open("w", encoding="utf-8") as f, subprocess.Popen(
            cmd, cwd=cwd, env={**os.environ, **(env or {})}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1) as p:
        for line in p.stdout:
            sys.stdout.write(line)
            f.write(line)
            out.append(line)
    return p.returncode, "".join(out)


def _must(cmd: list[str], log: Path, **kw) -> str:
    code, out = _run(cmd, log, **kw)
    if code:
        raise SystemExit(f"[release] {' '.join(map(str, cmd))} exited {code}; log: {log}")
    return out


def _prep(b: Build, module: str, *args: str) -> None:
    _must([sys.executable, "-m", f"prep.{module}", *args], b.dir / "logs" / f"{module}.log")


def _v4_python(repo: Path) -> list[str]:
    venv = repo / ".venv" / "bin" / "python"
    return [str(venv)] if venv.exists() else ["uv", "run", "python"]


def step_v4(b: Build) -> None:
    cmd = _v4_python(b.v4_repo) + ["-m", "scripts.v4.prepare_semester", "--semester", SEMESTER_LABEL,
                                   "--src", str(b.source), "--llm", b.llm] + (["--skip-aspects"] * b.skip_aspects)
    code, _ = _run(cmd, b.dir / "logs" / "v4.log", cwd=b.v4_repo)
    stage = b.v4_repo / "data" / "v4" / "staging" / SEMESTER_LABEL
    for name in ("prepare_report.md", "course_struct.db"):
        if (stage / name).exists():
            shutil.copy2(stage / name, b.dir / name)
    if code == 1:
        raise SystemExit(f"[release] v4 quality gate failed; see {b.dir / 'prepare_report.md'}")
    if code:
        raise SystemExit(f"[release] prepare_semester exited {code}; log: {b.dir / 'logs' / 'v4.log'}")


def _sql(statement: str, params: tuple = ()) -> list[tuple]:
    conn = psycopg2.connect(database_url())
    try:
        with conn, conn.cursor() as cur:
            cur.execute(statement, params or None)   # () would still run %-substitution (the DO block has %I)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def step_reset(b: Build) -> None:
    _sql("DROP SCHEMA IF EXISTS v4 CASCADE; DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")


def step_pgloader(b: Build) -> None:
    problems = source_type_problems(b.source)
    if problems:
        raise SystemExit("[release] the crawler file's column types disagree with its values; pgloader would "
                         "drop those rows:\n" + "\n".join(f"  - {p}" for p in problems)
                         + "\nRe-crawl with the current NCCUCourse (it declares these TEXT), or rebuild the file's schema.")
    out = _must(["docker", "compose", "--profile", "load", "run", "--rm", "pgloader"],
                b.dir / "logs" / "pgloader.log", env={"LOAD_SQLITE": str(b.source)})
    problems = pgloader_errors(out)
    if problems:
        raise SystemExit("[release] pgloader reported failures:\n" + "\n".join(f"  - {p}" for p in problems[:20]))


def step_narrow(b: Build) -> None:
    _sql(NARROW_SQL)
    (n,) = _sql("SELECT count(*) FROM public.course WHERE y = %s AND s = %s", (COURSE_YEAR, COURSE_SEMESTER))[0]
    if not n:
        raise SystemExit(f"[release] {b.source} has no course rows for {SEMESTER_LABEL} (contract.yaml)")
    _log(f"{n} course rows for {SEMESTER_LABEL}")


def dump_line(line: bytes) -> bytes:
    """A dump selecting public with -n creates it, but every new database already has public."""
    return b"CREATE SCHEMA IF NOT EXISTS public;\n" if line == b"CREATE SCHEMA public;\n" else line


def step_dump(b: Build) -> None:
    tmp = b.dir / "data.sql.gz.tmp"
    cmd = ["docker", "compose", "exec", "-T", "postgres", "pg_dump", "-U", "postgres", "-d", "academic",
           "-Fp", "--no-owner", "--no-privileges", *DUMP_SCHEMAS]
    with gzip.open(tmp, "wb") as out, subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE) as p:
        for line in p.stdout:
            out.write(dump_line(line))
    if p.returncode:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"[release] pg_dump exited {p.returncode}")
    os.replace(tmp, b.dir / "data.sql.gz")


def _commit(repo: Path) -> str:
    def git(*args: str) -> str:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    sha = git("rev-parse", "--short=12", "HEAD") or "unknown"
    return sha + ("+dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def _counts() -> dict[str, int]:
    rows = _sql("""
        SELECT 'public.course ' || %s, count(*) FROM public.course WHERE y = %s AND s = %s
        UNION ALL SELECT 'public.course_chunk', count(*) FROM public.course_chunk
        UNION ALL SELECT 'v4.doc', count(*) FROM v4.doc
        UNION ALL SELECT 'v4.doc_session', count(*) FROM v4.doc_session
        UNION ALL SELECT 'v4.eval_item', count(*) FROM v4.eval_item
    """, (SEMESTER_LABEL, COURSE_YEAR, COURSE_SEMESTER))
    return {k: int(v) for k, v in rows}


def step_manifest(b: Build, state: dict) -> None:
    shutil.copy2(CONTRACT_PATH, b.dir / "contract.yaml")
    counts = _counts()
    report = b.dir / "prepare_report.md"
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    text = text.split("\n## PostgreSQL 成品\n")[0].rstrip("\n")   # a resumed build rewrites it
    report.write_text(text + "\n\n## PostgreSQL 成品\n\n" + "\n".join(f"- {k}: {v}" for k, v in counts.items())
                      + "\n", encoding="utf-8")
    files = {p.name: R.file_entry(p) for p in sorted(b.dir.iterdir()) if p.is_file() and p.name != "build.json"}
    final = b.dir.name.removesuffix(R.PARTIAL)
    R.write_manifest(b.dir, {
        "release": final, "semester": SEMESTER_LABEL, "built_at": datetime.now().isoformat(timespec="seconds"),
        "schema_meta": SCHEMA_META, "files": files, "counts": counts,
        "source": {"path": str(b.source), **R.file_entry(b.source)},
        "commits": {"CourseDataPrep": _commit(Path(REPO_ROOT)), "scheduling_test": _commit(b.v4_repo)},
        "step_seconds": state.get("step_seconds", {}),
    })


STEP_FN = {
    "v4": step_v4, "reset": step_reset, "pgloader": step_pgloader, "narrow": step_narrow,
    "migrate": lambda b: _prep(b, "migrate"), "populate_time": lambda b: _prep(b, "populate_time"),
    "embed": lambda b: _prep(b, "embed"), "load_v4": lambda b: _prep(b, "load_v4", "--db", str(b.dir / "course_struct.db")),
    "check": lambda b: _prep(b, "check"), "dump": step_dump,
}


def _guard_database() -> None:
    if database_url() != DEFAULT_DATABASE_URL:
        raise SystemExit("[release] DATABASE_URL points elsewhere; a release only runs against this repo's own "
                         f"database ({DEFAULT_DATABASE_URL}): pgloader and pg_dump use its compose service and "
                         "the reset step drops schemas.")


def build(a: argparse.Namespace) -> None:
    if a.semester and a.semester != SEMESTER_LABEL:
        raise SystemExit(f"[release] contract.yaml is for {SEMESTER_LABEL}, not {a.semester}; "
                         "edit course_year / course_semester there first")
    _guard_database()
    base = R.root()
    base.mkdir(parents=True, exist_ok=True)
    steps = steps_from(a.from_step)
    if a.from_step:
        d = R.latest_partial(base, SEMESTER_LABEL)
        if d is None:
            raise SystemExit(f"[release] no {SEMESTER_LABEL} .partial build to resume in {base}")
    else:
        d = base / (R.release_name(SEMESTER_LABEL, datetime.now()) + R.PARTIAL)
        d.mkdir()
    state_path = d / "build.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    source = (a.source or (Path(state["source"]) if "source" in state else None))
    if source is None or not source.exists():
        raise SystemExit(f"[release] crawler SQLite not found: {source} (pass --source)")
    v4_repo = Path(os.environ.get("V4_REPO", Path(REPO_ROOT).parent / "scheduling_test")).resolve()
    b = Build(d, source.resolve(), v4_repo, a.llm, a.skip_aspects)
    state |= {"source": str(b.source), "v4_repo": str(v4_repo)}
    _log(f"{d.name}: steps {', '.join(steps)}")
    for step in steps:
        t0 = time.time()
        _log(f"--- {step}")
        if step == "manifest":
            step_manifest(b, state)
        else:
            STEP_FN[step](b)
        state.setdefault("step_seconds", {})[step] = round(time.time() - t0, 1)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    final = base / d.name.removesuffix(R.PARTIAL)
    os.rename(d, final)
    _log(f"built {final}")
    if a.promote:
        R.promote(base, final.name)
        _log(f"current -> {final.name}")
    else:
        _log(f"not promoted; `uv run python -m prep.release promote {final.name}` to make it current")


def main() -> None:
    p = argparse.ArgumentParser(description="Build, promote and roll back semester artifacts.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build one release for contract.yaml's semester")
    b.add_argument("--source", type=Path, help="crawler SQLite (NCCUCourse output)")
    b.add_argument("--semester", help="optional: must equal contract.yaml's, as a guard in schedules")
    b.add_argument("--from", dest="from_step", help=f"resume the latest .partial at this step ({', '.join(STEPS)})")
    b.add_argument("--llm", choices=["off", "cloud", "local"], default="off",
                   help="prepare_semester: LLM fallback for fields the rules miss")
    b.add_argument("--skip-aspects", action="store_true", help="prepare_semester: keep the existing review aspects")
    b.add_argument("--promote", action="store_true", help="make it current once every step has passed")
    pr = sub.add_parser("promote", help="point current at a finished release")
    pr.add_argument("name")
    sub.add_parser("rollback", help="swap current and previous")
    sub.add_parser("list", help="releases, newest first")
    a = p.parse_args()
    base = R.root()
    if a.cmd == "build":
        build(a)
    elif a.cmd == "promote":
        R.promote(base, a.name)
        _log(f"current -> {a.name}")
    elif a.cmd == "rollback":
        _log(f"current -> {R.rollback(base)}")
    else:
        cur, prev = R.current(base), R.previous(base)
        for d in R.listing(base):
            mark = "current " if d.name == cur else "previous" if d.name == prev else "        "
            m = d / "manifest.json"
            built = json.loads(m.read_text()).get("built_at", "") if m.exists() else "(unfinished)"
            print(f"{mark}  {d.name:34s} {built}")


if __name__ == "__main__":
    sys.exit(main())
