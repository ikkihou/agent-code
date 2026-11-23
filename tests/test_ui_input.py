"""Tests for REPL input ergonomics: @file mention completion and paste placeholdering.

Pure-function tests: the completer is exercised through its frontend-agnostic
``suggestions(text)`` API (no live app, no prompt_toolkit), and the paste cache /
key-binding logic through direct calls.
"""

from __future__ import annotations

from pathlib import Path

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
    return [s.insert for s in completer.suggestions(text)]


# ---- @file mention completion ----------------------------------------------


def test_mention_completes_basename_substring(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    hits = _completions(completer, "please read @main")

    assert hits == ["src/main.py"]


def test_mention_fuzzy_subsequence(tmp_path: Path) -> None:
    """kimi-style subsequence fuzzy: 'mntxt' finds readme.md via its basename."""
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    # Subsequence over the basename.
    assert "readme.md" in _completions(completer, "@rmd")
    # Skipped separator still matches as subsequence across the path ("@"->"/"
    # elision: "srcmain" matches "src/main.py").
    assert "src/main.py" in _completions(completer, "@srcmain")
    # A character absent from the path in order -> no match.
    assert _completions(completer, "@zzzzz") == []


def test_mention_ranks_basename_prefix_first(tmp_path: Path) -> None:
    """A basename prefix match outranks a fuzzy path match for the same query."""
    root = _make_tree(tmp_path)
    (root / "main.c").write_text("x\n", encoding="utf-8")
    (root / "src" / "dominos").mkdir(parents=True)
    (root / "src" / "dominos" / "main.txt").write_text("x\n", encoding="utf-8")
    completer = FileMentionCompleter(root)

    hits = _completions(completer, "@main")

    # Tier 0 (basename prefix) before tier 1 (basename contains) before tier 2
    # (path subsequence).
    assert hits[:2] == ["main.c", "src/main.py"]
    assert "src/dominos/main.txt" in hits


def test_mention_guard_does_not_trigger_on_emails(tmp_path: Path) -> None:
    """An '@' embedded in an identifier (email, decorator-ish text) is not a mention."""
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    assert _completions(completer, "mail me at user@example.com") == []
    assert _completions(completer, "price is 5@3") == []


def test_mention_completed_file_short_circuits(tmp_path: Path) -> None:
    """Once the fragment names an existing file, completion stops (mention done)."""
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    assert _completions(completer, "read @readme.md") == []
    # A prefix that is not yet a file still completes.
    assert _completions(completer, "read @readme.m") == ["readme.md"]


def test_mention_fragment_with_space_does_not_trigger(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    completer = FileMentionCompleter(root)

    # The fragment after @ runs to whitespace; "@read me" leaves "me" as the last
    # word, which is not an @-word, so nothing fires.
    assert _completions(completer, "read @read me") == []


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
    _completions(completer, "@brand")

    first_cache_time = completer._cache_at
    (tmp_path / "brand_new.py").write_text("x\n", encoding="utf-8")
    hits = _completions(completer, "@brand")

    assert hits == []  # the fresh file is not visible until the TTL expires
    assert completer._cache_at == first_cache_time


def test_mention_short_query_lists_top_level_only(tmp_path: Path) -> None:
    """ "@a" (short, no slash) lists top-level entries: instant first keystroke."""
    root = _make_tree(tmp_path)
    (root / "src" / "aardvark.txt").write_text("x\n", encoding="utf-8")
    completer = FileMentionCompleter(root)

    hits = _completions(completer, "@a")

    assert "readme.md" in hits  # top-level basename match
    assert "src/aardvark.txt" not in hits  # deep paths stay out of the short query


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
