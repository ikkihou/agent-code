from __future__ import annotations

from pathlib import Path

from .paths import (
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    ensure_memdir,
    get_memdir,
    index_path,
    topic_path,
)
from .types import MEMORY_TYPES, MemoryEntry, make_slug


def load_index(cwd: Path) -> str | None:
    """Read the MEMORY.md index file, truncating past the line/byte caps.

    Returns None when the file does not exist."""
    ipath = index_path(cwd)
    if not ipath.exists():
        return None
    text = ipath.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    # Truncation guard: line cap first, then byte cap.
    lines = text.splitlines()
    if len(lines) > INDEX_MAX_LINES:
        header = lines[:2] if lines[0].startswith("#") else []
        body = lines[2:] if header else lines
        # Keep the header plus the newest 200 lines (the tail); drop the middle.
        lines = header + body[-(INDEX_MAX_LINES - len(header)) :]
        text = "\n".join(lines)
    text_bytes = text.encode("utf-8")
    if len(text_bytes) > INDEX_MAX_BYTES:
        # Cut on a byte boundary: walk back to the last newline of a whole UTF-8 char.
        truncated = text_bytes[:INDEX_MAX_BYTES].decode("utf-8", errors="replace")
        last_nl = truncated.rfind("\n")
        text = truncated[:last_nl] if last_nl > 0 else truncated
    return text


def write_memory(cwd: Path, mem_type: str, title: str, body: str) -> MemoryEntry:
    """Write one long-term memory. Does two things:
    1. writes .agent/memory/<type>/<slug>.md (with frontmatter)
    2. appends one reference line to the tail of the MEMORY.md index

    Both steps live in one function so the happy path completes them together; if
    the index update fails midway, the topic file is already on disk and the recall
    scan still finds it, so no memory is lost."""
    if mem_type not in MEMORY_TYPES:
        raise ValueError(
            f"unknown memory type: {mem_type}, expected one of {MEMORY_TYPES}"
        )

    ensure_memdir(cwd)
    slug = make_slug(title)

    # Avoid filename collisions: if the slug is taken, append a numeric suffix.
    tpath = topic_path(cwd, mem_type, slug)
    counter = 1
    while tpath.exists():
        tpath = topic_path(cwd, mem_type, f"{slug}-{counter}")
        counter += 1

    # Write the topic file. The frontmatter carries two fields:
    #   type  (required) selects the subdirectory and drives the recall filter
    #   title is the human-readable title, also used as the index link text
    # The index hook is derived from the first 60 chars of the body, so these two
    # fields are all the topic file needs.
    frontmatter = f"---\ntype: {mem_type}\ntitle: {title}\n---\n\n"
    tpath.write_text(frontmatter + body, encoding="utf-8")

    # Build a one-line hook: first 60 chars of the body, cut back to a whole word.
    hook = body.strip()[:60]
    if len(body.strip()) > 60:
        hook = hook[: hook.rfind(" ")] + "..."

    # Append the index line.
    ipath = index_path(cwd)
    index_line = f"- [{title}]({mem_type}/{tpath.name}) — {hook}\n"
    if not ipath.exists():
        ipath.write_text("# Memory Index\n\n" + index_line, encoding="utf-8")
    else:
        with open(ipath, "a", encoding="utf-8") as f:
            f.write(index_line)

    return MemoryEntry(
        mem_type=mem_type,
        title=title,
        slug=tpath.stem,
        body=body,
        file_path=str(tpath.relative_to(cwd)),
    )


def recall_memory(cwd: Path, query: str, top_k: int = 5) -> list[MemoryEntry]:
    """Keyword recall: scan every .md file under the four subdirectories, split the
    query on whitespace into keywords, and add one point per keyword hit in
    title + body (case-insensitive). Sort by descending score, then by descending
    mtime, and return the top_k matches."""
    # recall_memory is strictly read-only: return empty when the directory is missing
    # instead of calling mkdir, otherwise plan mode (a hard read-only constraint)
    # would silently create directories.
    memdir = get_memdir(cwd)
    if not memdir.is_dir():
        return []
    keywords = query.lower().split()
    if not keywords:
        return []

    # (score, mtime, entry) triples: higher score wins, ties go to the newer mtime.
    scored: list[tuple[float, float, MemoryEntry]] = []
    for mtype in MEMORY_TYPES:
        type_dir = memdir / mtype
        if not type_dir.is_dir():
            continue
        for md_file in type_dir.glob("*.md"):
            text = md_file.read_text(encoding="utf-8", errors="replace")
            text_lower = text.lower()
            # Plain keyword match: one point per keyword occurrence.
            score = sum(1 for kw in keywords if kw in text_lower)
            if score == 0:
                continue
            # Parse the title from the frontmatter (the title: line between ---).
            title = md_file.stem.replace("-", " ").title()
            body = text
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.startswith("title:"):
                            title = line.split(":", 1)[1].strip()
                    body = parts[2].strip()
            scored.append(
                (
                    score / len(keywords),  # normalized to 0-1
                    md_file.stat().st_mtime,
                    MemoryEntry(
                        mem_type=mtype,
                        title=title,
                        slug=md_file.stem,
                        body=body,
                        file_path=str(md_file.relative_to(cwd)),
                    ),
                )
            )

    # Tuple comparison: score first, then mtime, both descending -- higher score
    # wins, and among equal scores the most recently written memory wins.
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [entry for _, _, entry in scored[:top_k]]
