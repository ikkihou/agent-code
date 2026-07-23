from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
import pathspec

# 文本文件后缀白名单：直接放行，不用 peek 文件头。
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

# 单文件大小上限：超过就拒绝读取整文件。教学版先用 256 KiB，后面再做 offset/limit。
MAX_READ_BYTES = 256 * 1024

# 单次工具 observation 上限。模型上下文有限，过长直接截尾。
DEFAULT_MAX_CHARS = 8000

# 默认跳过的目录名。任意祖先目录命中名单，整条路径都被剔除。
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
    """控制哪些路径应该被跳过的策略。

    Attributes:
        skip_dirs: 路径任意祖先目录命中此集合则跳过。
        gitignore: 可选的项目 .gitignore 规则。
    """

    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    gitignore: pathspec.PathSpec | None = None

    @classmethod
    def default(cls, gitignore: pathspec.PathSpec | None = None) -> "SkipPolicy":
        """用可选的 gitignore 规则构造默认跳过策略。"""
        return cls(gitignore=gitignore)


@dataclass
class ReadFileState:
    """追踪 agent 已读过的文件，记录修改时间和字符数。

    后续 read-before-edit 安全检查需要用这里的数据判断
    "模型读过文件之后，文件在磁盘上是否又被改过"。
    当前阶段只做记录，不做校验。

    Attributes:
        entries: 路径 -> (mtime_ns, 字符数) 的映射。
    """

    entries: dict[Path, tuple[int, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, path: Path, content: str) -> None:
        """记录一次文件读取。

        保存文件的最后修改时间（纳秒精度）和内容长度，
        供后续判断文件是否在读取后被外部修改过。

        Args:
            path: 被读取的文件路径。
            content: 读取到的文件内容。
        """
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        with self._lock:
            self.entries[path] = (mtime_ns, len(content))


def resolve_in_cwd(cwd: Path, user_path: str) -> Path:
    """把模型给的路径解析成绝对路径，并强制锁在 cwd 子树内。

    Args:
        cwd: 当前工作目录（安全边界）。
        user_path: 模型给出的路径，可以是相对路径或绝对路径。

    Returns:
        解析后的绝对路径。

    Raises:
        ValueError: 如果解析后的路径不在 cwd 子树内（路径越狱）。
    """
    candidate = (cwd / user_path).resolve()
    cwd_resolved = cwd.resolve()
    try:
        candidate.relative_to(cwd_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes cwd: {user_path}") from exc
    return candidate


def ensure_text_file(path: Path) -> None:
    """确保目标文件是文本文件，拒绝二进制文件。

    白名单后缀（TEXT_SUFFIXES）内的文件直接放行；
    其余文件 peek 前 1 KB，发现 NUL 字节（\\x00）就当作二进制拒绝。

    Args:
        path: 要检查的文件路径。

    Raises:
        ValueError: 文件被判定为二进制文件。
    """
    if path.suffix.lower() in TEXT_SUFFIXES:
        return
    with path.open("rb") as f:
        if b"\x00" in f.read(1024):
            raise ValueError(f"binary file: {path.name}")


def ensure_within_size(path: Path, max_bytes: int = MAX_READ_BYTES) -> None:
    """检查文件是否超过读取大小上限。

    超过上限的拒绝读取，提示用户改用 grep 或读更小的文件。
    目前不做 offset/limit 分段读取，留作后续扩展。

    Args:
        path: 要检查的文件路径。
        max_bytes: 允许的最大字节数（默认 256 KiB）。

    Raises:
        ValueError: 文件大小超过上限。
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"file too large: {size} bytes > {max_bytes}; "
            f"read a smaller file or use grep instead"
        )


def should_skip(rel_path: Path, policy: SkipPolicy) -> bool:
    """判断路径是否应该被跳过（不读/不列）。

    检查逻辑：
    1. 路径的任意祖先目录是否在 skip_dirs 里（如 .git, node_modules）。
    2. 路径是否匹配 .gitignore 规则。

    Args:
        rel_path: 相对于 cwd 的路径。
        policy: 跳过策略，包含要跳过的目录名和 gitignore 规则。

    Returns:
        True 表示应该跳过，False 表示可以操作。
    """
    if any(part in policy.skip_dirs for part in rel_path.parts):
        return True
    if policy.gitignore is not None and policy.gitignore.match_file(str(rel_path)):
        return True
    return False


def truncate_output(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """截断过长的输出文本，防止撑爆模型上下文。

    超过 max_chars 的部分被切除，并在末尾追加截断标记
    `[truncated N chars]` 让模型知道自己看到的内容不全。

    Args:
        text: 原始文本（如文件内容、命令输出）。
        max_chars: 允许的最大字符数（默认 8000）。

    Returns:
        截断后的文本，或原文本（如果没超限）。
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[truncated {len(text) - max_chars} chars]"


def load_gitignore(cwd: Path) -> pathspec.PathSpec | None:
    """加载 cwd 根目录下的 .gitignore 文件。

    只读取项目根目录的 .gitignore，不处理嵌套子目录的 .gitignore
    （留作课后挑战）。如果文件不存在则返回 None。

    Args:
        cwd: 项目根目录。

    Returns:
        解析后的 PathSpec 对象，或 None（文件不存在）。
    """
    gitignore = cwd / ".gitignore"
    if not gitignore.exists():
        return None
    lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def ensure_read_before_edit(state: ReadFileState, path: Path) -> str | None:
    """检查文件是否在本次对话中被读过。未读过则返回error

    Args:
        state (ReadFileState): 文件阅读状态注册表
        path (path): 文件路径

    """
    if path not in state.entries:
        return (
            f"error: file has not been read yet. Read {path.name} first before editing."
        )

    return None


def check_mtime_conflict(state: ReadFileState, path: Path) -> str | None:
    """检查文件在read之后是否被外部修改过。mtime变了（表示文件被修改过）则返回error。"""
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
    """在 content 中查找 old 并替换为 new。
    返回 (new_content, error)：成功时 error 为 None，失败时 new_content 为 None。"""
    if old == "":
        # str.count("") 会返回 len+1，str.replace("", x) 会在每个字符之间插入 x。
        # 这两个行为对模型完全没用，直接拒绝。
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
