from __future__ import annotations

import locale
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

# Text-file suffix allowlist: these are read directly without peeking at the header.
TEXT_SUFFIXES = {
    ".py",
    ".pyi",
    ".md",
    ".rst",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".env",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".sql",
    ".lock",
    ".gitignore",
}

# Per-file size cap: past this, refuse to read the whole file. The teaching version
# starts at 256 KiB; offset/limit support comes later.
MAX_READ_BYTES = 256 * 1024


def decode_subprocess_output(data: bytes | None, *, encodings: list[str] | None = None) -> str:
    """Subprocess byte output -> text.

    Order: UTF-8, then the system preferred encoding (GBK on Chinese Windows, which
    system commands commonly emit), then a UTF-8 replacement fallback. Never use
    subprocess' text=True: it decodes with the system encoding and raises
    UnicodeDecodeError on UTF-8 content ("gbk codec can't decode byte ...").
    """
    if not data:
        return ""
    candidates = encodings or ["utf-8", locale.getpreferredencoding(False)]
    for encoding in dict.fromkeys(candidates):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# Candidate encodings for reading source files: UTF-8 first, then the GBK/GB18030
# commonly seen on Chinese Windows. GB18030 rather than GBK: it is a superset, so it
# decodes a wider set of rare characters and never misses anything GBK can decode.
SOURCE_FILE_ENCODINGS = ("utf-8", "gb18030", "cp936")


def read_text_file(
    path: Path, *, encodings: tuple[str, ...] | None = None
) -> tuple[str, str]:
    """Read a source file and return ``(text, encoding)``.

    Source files on Chinese Windows are often GBK; a strict UTF-8 read raises
    ``'utf-8' codec can't decode byte ...``, which surfaces as an ``[error]`` line in
    the REPL output panel. Try the candidate encodings in order (UTF-8, then
    GB18030/GBK) and fall back to replacement characters only when all fail, so a
    tool call never fails outright because of encoding.

    Returns the encoding actually used so a write-back keeps the original encoding
    instead of silently turning a GBK file into UTF-8.
    """
    raw = path.read_bytes()
    for encoding in encodings or SOURCE_FILE_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def write_text_file(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write a source file back in the given encoding (preserving the read encoding)."""
    path.write_text(content, encoding=encoding, newline="")



# Per-observation cap for a single tool. The model context is finite, so very long
# results are truncated at the tail.
DEFAULT_MAX_CHARS = 8000

# Default skip directory names. A hit on any ancestor directory drops the whole path.
DEFAULT_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


@dataclass
class SkipPolicy:
    """Policy deciding which paths should be skipped.

    Attributes:
        skip_dirs: skip when any ancestor directory is in this set.
        gitignore: optional project .gitignore rules.
    """

    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    gitignore: pathspec.PathSpec | None = None

    @classmethod
    def default(cls, gitignore: pathspec.PathSpec | None = None) -> "SkipPolicy":
        """Build the default skip policy from optional gitignore rules."""
        return cls(gitignore=gitignore)


@dataclass
class ReadFileState:
    """Tracks files the agent has already read, recording mtime and character count.

    The read-before-edit safety check uses this data to decide "did the file change on
    disk after the model read it?". At this stage it only records, no validation.

    Attributes:
        entries: mapping of path -> (mtime_ns, char count).
    """

    entries: dict[Path, tuple[int, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, path: Path, content: str) -> None:
        """Record one file read.

        Stores the file's last modification time (nanosecond precision) and content
        length, so later checks can tell whether the file was changed externally after
        it was read.

        Args:
            path: path of the file that was read.
            content: the content that was read.
        """
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        with self._lock:
            self.entries[path] = (mtime_ns, len(content))


def resolve_in_cwd(cwd: Path, user_path: str) -> Path:
    """Resolve a model-supplied path to an absolute path, pinned inside the cwd subtree.

    Args:
        cwd: current working directory (the safety boundary).
        user_path: path supplied by the model, either relative or absolute.

    Returns:
        The resolved absolute path.

    Raises:
        ValueError: when the resolved path falls outside the cwd subtree (path escape).
    """
    candidate = (cwd / user_path).resolve()
    cwd_resolved = cwd.resolve()
    try:
        candidate.relative_to(cwd_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes cwd: {user_path}") from exc
    return candidate


def ensure_text_file(path: Path) -> None:
    """Ensure the target file is a text file; reject binaries.

    Files with an allowlisted suffix (TEXT_SUFFIXES) pass straight through; for the
    rest, peek at the first 1 KB and treat a NUL byte (\\x00) as binary.

    Args:
        path: path of the file to check.

    Raises:
        ValueError: the file is judged binary.
    """
    if path.suffix.lower() in TEXT_SUFFIXES:
        return
    with path.open("rb") as f:
        if b"\x00" in f.read(1024):
            raise ValueError(f"binary file: {path.name}")


def ensure_within_size(path: Path, max_bytes: int = MAX_READ_BYTES) -> None:
    """Check whether the file exceeds the read size cap.

    Files past the cap are refused, and the user is told to use grep or read a smaller
    file. Offset/limit chunked reads are not implemented yet; left as a later
    extension.

    Args:
        path: path of the file to check.
        max_bytes: allowed maximum in bytes (default 256 KiB).

    Raises:
        ValueError: the file exceeds the cap.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"file too large: {size} bytes > {max_bytes}; "
            f"read a smaller file or use grep instead"
        )


def should_skip(rel_path: Path, policy: SkipPolicy) -> bool:
    """Decide whether a path should be skipped (not read, not listed).

    Check logic:
    1. Is any ancestor directory of the path in skip_dirs (e.g. .git, node_modules)?
    2. Does the path match a .gitignore rule?

    Args:
        rel_path: path relative to cwd.
        policy: skip policy holding the directory names and gitignore rules.

    Returns:
        True means skip; False means it can be operated on.
    """
    if any(part in policy.skip_dirs for part in rel_path.parts):
        return True
    if policy.gitignore is not None and policy.gitignore.match_file(str(rel_path)):
        return True
    return False


def truncate_output(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Truncate over-long output text so it cannot blow up the model context.

    Anything past max_chars is cut and a `[truncated N chars]` marker is appended so
    the model knows what it saw is incomplete.

    Args:
        text: original text (file content, command output, ...).
        max_chars: allowed maximum characters (default 8000).

    Returns:
        The truncated text, or the original text when under the cap.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[truncated {len(text) - max_chars} chars]"


def load_gitignore(cwd: Path) -> pathspec.PathSpec | None:
    """Load the .gitignore at the cwd root.

    Only the project root .gitignore is read; nested .gitignore files are not handled
    (left as an exercise). Returns None when the file does not exist.

    Args:
        cwd: project root.

    Returns:
        The parsed PathSpec, or None when the file does not exist.
    """
    gitignore = cwd / ".gitignore"
    if not gitignore.exists():
        return None
    lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def ensure_read_before_edit(state: ReadFileState, path: Path) -> str | None:
    """Check whether the file has been read in this conversation. Returns an error
    string when it has not.

    Args:
        state (ReadFileState): registry of file read state
        path (path): file path

    """
    if path not in state.entries:
        return (
            f"error: file has not been read yet. Read {path.name} first before editing."
        )

    return None


def check_mtime_conflict(state: ReadFileState, path: Path) -> str | None:
    """Check whether the file was modified externally after being read. Returns an
    error string when mtime moved (i.e. the file changed)."""
    entry = state.entries.get(path)
    if entry is None:
        return None

    read_mtime_ns, _ = entry

    try:
        current_time_ns = path.stat().st_mtime_ns
    except OSError:
        return None

    if current_time_ns > read_mtime_ns:
        return f"error: file was modified after read. Read {path.name} again before editing."

    return None


def apply_single_replace(
    content: str, old: str, new: str, replace_all: bool
) -> tuple[str | None, str | None]:
    """Find old in content and replace it with new.
    Returns (new_content, error): error is None on success, new_content is None on failure."""
    if old == "":
        # str.count("") returns len+1 and str.replace("", x) inserts x between every
        # character. Neither is useful to the model, so reject outright.
        return None, "error: old_string must not be empty."
    if old == new:
        return None, "error: old_string and new_string are exactly the same."

    count = content.count(old)
    if count == 0:
        return None, "error: string to replace not found in file."
    if count > 1 and not replace_all:
        return None, (
            f"error: found {count} matches for old_string. "
            f"Use replace_all=True to replace all, or make old_string more specific."
        )

    if replace_all:
        return content.replace(old, new), None
    else:
        return content.replace(old, new, 1), None
