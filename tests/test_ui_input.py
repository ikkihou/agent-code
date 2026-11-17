"""Tests for REPL input ergonomics: @file mention completion and paste placeholdering.

Pure-function tests: the completer is exercised through prompt_toolkit's Document
(no live app), and the paste cache / key-binding logic through direct calls.
"""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.document import Document

from agent_code.ui.completer import FileMentionCompleter
from agent_code.ui.paste import PasteCache, is_oversized_paste


def _make_tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "src" / "util").mkdir()
    (tmp_path / "src" / "util" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("doc\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "secret.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.py").write_text("x\n", encoding="utf-8")
    return tmp_path


def _completions(completer: FileMentionCompleter, text: str) -> list[str]:
    doc = Document(text, len(text))
    return [c.text for c in completer.get_completions(doc, None)]


# ---- @file mention completion ----------------------------------------------


def test_mention_completes_basename_substring(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    hits = _completions(completer, "please read @main")

    assert hits == ["src/main.py"]


def test_mention_empty_query_lists_tree(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    hits = _completions(completer, "@")

    assert "readme.md" in hits
    assert "src/" in hits


def test_mention_respects_gitignore_and_skip_dirs(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    hits = _completions(completer, "@")

    assert not any("secret" in hit for hit in hits)  # .gitignore'd
    assert not any("node_modules" in hit for hit in hits)  # skip dir


def test_mention_with_path_separator_matches_full_path(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    hits = _completions(completer, "see @util/help")

    assert hits == ["src/util/helper.py"]


def test_mention_mid_sentence_and_no_trigger(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    # Mid-line mention after other words still fires.
    assert _completions(completer, "fix @read") == ["readme.md"]
    # No @ word -> no completions.
    assert _completions(completer, "plain words here") == []


def test_mention_caches_scan(tmp_path: Path) -> None:
    """Two completions within the TTL reuse one filesystem walk."""
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)
    _completions(completer, "@")

    first_cache_time = completer._cache_at
    (tmp_path / "brand_new.py").write_text("x\n", encoding="utf-8")
    hits = _completions(completer, "@brand")

    assert hits == []  # the fresh file is not visible until the TTL expires
    assert completer._cache_at == first_cache_time


def test_mention_directory_completion_keeps_trailing_slash(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    assert _completions(completer, "@read") == ["readme.md"]
    hits = _completions(completer, "@src/u")
    # The directory completes with a trailing slash; the file inside it also matches
    # ("util" appears in its path), which is intentional -- the menu offers both.
    assert hits[0] == "src/util/"
    assert "src/util/helper.py" in hits


# ---- paste placeholdering ---------------------------------------------------


def test_oversized_paste_threshold() -> None:
    assert not is_oversized_paste("one line")
    assert not is_oversized_paste("a\nb\nc")  # exactly 3 lines, tiny
    assert not is_oversized_paste("\n".join(f"l{i}" for i in range(30)))
    assert is_oversized_paste("\n".join(f"l{i}" for i in range(31)))


def test_paste_cache_register_and_expand_roundtrip() -> None:
    cache = PasteCache()
    content = "\n".join(f"line {i}" for i in range(50))
    token = cache.register(content)

    assert token == f"[Pasted text #1 +{content.count(chr(10)) + 1} lines]"

    submitted = f"look at this: {token}\nthanks"
    expanded = cache.expand(submitted)

    assert expanded == f"look at this: {content}\nthanks"


def test_paste_cache_multiple_entries_and_unknown_token() -> None:
    cache = PasteCache()
    t1 = cache.register("\n".join("a" for _ in range(40)))
    t2 = cache.register("\n".join("b" for _ in range(40)))

    expanded = cache.expand(f"{t1} and {t2} and [Pasted text #99 +1 lines]")

    assert "a\na" in expanded and t1 not in expanded
    assert "b\nb" in expanded and t2 not in expanded
    assert "[Pasted text #99 +1 lines]" in expanded  # hand-typed token passes through


def test_paste_cache_truncates_huge_content() -> None:
    cache = PasteCache()
    content = "x" * 300_000
    token = cache.register(content)
    expanded = cache.expand(token)

    assert len(expanded) < 300_000
    assert "truncated" in expanded
