"""contract.yaml — data properties shared with CourseLangChain.

Nothing here is env-overridable: a setting should have exactly one source. Machine
properties (DATABASE_URL, EMBED_DEVICE) come from the environment instead.
"""
from __future__ import annotations

import os

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTRACT_PATH = os.path.join(REPO_ROOT, "contract.yaml")
INIT_SCRIPTS_DIR = os.path.join(REPO_ROOT, "init-scripts")
OUT_DIR = os.path.join(REPO_ROOT, "out")

with open(CONTRACT_PATH, encoding="utf-8") as _f:
    CONTRACT: dict = yaml.safe_load(_f)

SCHEMA_VERSION: int = CONTRACT["schema_version"]
EMBED_MODEL: str = CONTRACT["embed_model"]
EMBED_DIM: int = int(CONTRACT["embed_dim"])
SQLITE_EMBED_MODEL: str = CONTRACT["sqlite_embed_model"]
COURSE_YEAR: str = str(CONTRACT["course_year"])
COURSE_SEMESTER: str = str(CONTRACT["course_semester"])

SCHEMA_META = {
    "schema_version": str(SCHEMA_VERSION),
    "embed_model": EMBED_MODEL,
    "embed_dim": str(EMBED_DIM),
    "course_year": COURSE_YEAR,
    "course_semester": COURSE_SEMESTER,
}


DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5433/academic"


def database_url() -> str:
    """Defaults to this repo's own database.

    Don't point it at the app's 5432 — that side only receives finished artifacts.
    """
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
