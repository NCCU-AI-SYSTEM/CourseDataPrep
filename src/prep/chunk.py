"""Split a course's syllabus fields into embeddable chunks.

The model reads 512 tokens and silently drops the rest, and Chinese runs about
one token per character. A course's fields together run well past that —
schedule alone has a median of 464 tokens — so anything packed into a single
vector loses whatever came last.

Each field is split on its own and every segment becomes its own vector, so
nothing is discarded. Retrieval takes the best-matching chunk per course, which
also means a question about the textbook matches the textbook rather than a
blend of everything the course says.
"""
from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

# Fields worth their own vectors. teaching_approach and ai_policy are excluded:
# their wording is identical across courses — only the percentages vary, and
# 1058 courses share one ai_policy value — so they would produce thousands of
# near-identical chunks that match every question about them equally well.
CHUNK_FIELDS = (
    ("課程目標", "objective"),
    ("課程簡介", "syllabus"),
    ("每週進度", "schedule"),
    ("評量方式", "evaluation"),
    ("指定書目", "textbook"),
)
HEADER_COLUMNS = ("name", "nameen", "teacher")
CHUNK_COLUMNS = HEADER_COLUMNS + tuple(column for _, column in CHUNK_FIELDS)

# The crawler joins syllabus sections with "|", which is what actually separates
# ideas here — newlines appear in only 24 of 3650 courses. Priority is strictly
# left to right, so without "|" the splitter falls through to spaces and
# characters and cuts mid-word in Chinese. 。and ，follow so a split that cannot
# land on a field boundary lands on a sentence.
SEPARATORS = ["\n\n", "\n", "|", "。", "；", "，", " ", ""]

# 320 leaves room for the per-chunk header inside the model's 512.
CHUNK_TOKENS = 320
CHUNK_OVERLAP = 40


def make_splitter(tokenizer) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
        tokenizer,
        chunk_size=CHUNK_TOKENS,
        chunk_overlap=CHUNK_OVERLAP,
        separators=SEPARATORS,
        keep_separator=True,
    )


def build_chunks(splitter, course: dict) -> list[tuple[str, str]]:
    """(section, text) pairs for one course.

    Every chunk repeats the course title and the section label: a chunk is
    retrieved on its own, so without them "期中考 40%" says nothing about which
    course it belongs to.
    """
    title = " / ".join(
        x for x in (course.get("name"), course.get("nameen")) if x
    ).strip()
    header = f"課程名稱: {title}"
    teacher = (course.get("teacher") or "").strip()
    if teacher:
        header += f"\n授課老師: {teacher}"

    chunks: list[tuple[str, str]] = []
    for label, column in CHUNK_FIELDS:
        text = (course.get(column) or "").strip()
        if not text:
            continue
        for segment in splitter.split_text(text):
            segment = segment.strip()
            if segment:
                chunks.append((column, f"{header}\n{label}: {segment}"))

    # A course with no syllabus at all still has to be findable by name.
    if not chunks:
        chunks.append(("title", header))
    return chunks
