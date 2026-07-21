from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Mapping
import os
import re
import shutil
import stat
import tempfile


BLOCK_RE = re.compile(r"^\s*([\w.-]+)\s*\{\s*(?:#.*)?$")
ASSIGN_RE = re.compile(r"^(\s*)([\w.:-]+)\s*=\s*(.*?)(\s+#.*)?$")
MODIFIER_ALIASES = {"CONTROL": "CTRL", "MOD1": "ALT", "MOD4": "SUPER", "WIN": "SUPER"}
MODIFIER_ORDER = {"SUPER": 0, "CTRL": 1, "ALT": 2, "SHIFT": 3}


@dataclass(frozen=True)
class Block:
    path: tuple[str, ...]
    start: int
    end: int
    indent: str


def _active_part(line: str) -> str:
    stripped = line.lstrip()
    return "" if stripped.startswith("#") else line.split("#", 1)[0].rstrip()


def blocks(text: str) -> list[Block]:
    lines = text.splitlines()
    stack: list[tuple[str, int, str]] = []
    result: list[Block] = []
    for index, line in enumerate(lines):
        active = _active_part(line)
        match = BLOCK_RE.match(active)
        if match:
            stack.append((match.group(1), index, line[: len(line) - len(line.lstrip())]))
            continue
        if active.strip() == "}" and stack:
            name, start, indent = stack.pop()
            path = tuple(item[0] for item in stack) + (name,)
            result.append(Block(path, start, index, indent))
    return result


def get_value(text: str, path: tuple[str, ...], key: str) -> str | None:
    lines = text.splitlines()
    matches = [block for block in blocks(text) if block.path == path]
    value = None
    for block in matches:
        child_ranges = [
            (child.start, child.end)
            for child in blocks(text)
            if len(child.path) > len(path)
            and child.path[: len(path)] == path
            and child.start > block.start
            and child.end < block.end
        ]
        for index in range(block.start + 1, block.end):
            if any(start <= index <= end for start, end in child_ranges):
                continue
            line = lines[index]
            if line.lstrip().startswith("#"):
                continue
            match = ASSIGN_RE.match(line)
            if match and match.group(2) == key:
                value = match.group(3).strip()
    return value


def set_value(text: str, path: tuple[str, ...], key: str, value: str) -> str:
    lines = text.splitlines()
    candidates = [block for block in blocks(text) if block.path == path]
    if not candidates:
        suffix = "\n" if text and not text.endswith("\n") else ""
        nested = ""
        indent = ""
        for name in path:
            nested += f"{indent}{name} {{\n"
            indent += "    "
        nested += f"{indent}{key} = {value}\n"
        for _ in path:
            indent = indent[:-4]
            nested += f"{indent}}}\n"
        return text + suffix + ("\n" if text else "") + nested

    block = candidates[-1]
    nested_ranges = [
        (child.start, child.end)
        for child in blocks(text)
        if len(child.path) > len(path)
        and child.path[: len(path)] == path
        and child.start > block.start
        and child.end < block.end
    ]
    found = None
    for index in range(block.start + 1, block.end):
        if any(start <= index <= end for start, end in nested_ranges):
            continue
        line = lines[index]
        if line.lstrip().startswith("#"):
            continue
        match = ASSIGN_RE.match(line)
        if match and match.group(2) == key:
            found = (index, match)

    if found:
        index, match = found
        comment = match.group(4) or ""
        lines[index] = f"{match.group(1)}{key} = {value}{comment}"
    else:
        lines.insert(block.end, f"{block.indent}    {key} = {value}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def listener_timeout(text: str, command_fragment: str) -> int | None:
    lines = text.splitlines()
    for block in blocks(text):
        if block.path != ("listener",):
            continue
        body = "\n".join(lines[block.start : block.end + 1])
        if command_fragment not in body:
            continue
        raw = get_value(body, ("listener",), "timeout")
        if raw:
            try:
                return int(float(raw))
            except ValueError:
                return None
    return None


def set_listener_timeout(
    text: str, command_fragment: str, seconds: int, command: str | None = None
) -> str:
    lines = text.splitlines()
    for block in blocks(text):
        if block.path != ("listener",):
            continue
        body = "\n".join(lines[block.start : block.end + 1])
        if command_fragment not in body:
            continue
        updated = set_value(body, ("listener",), "timeout", str(seconds)).splitlines()
        lines[block.start : block.end + 1] = updated
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    if command is None:
        raise ValueError(f"No listener contains {command_fragment!r}")
    suffix = "\n" if text and not text.endswith("\n") else ""
    addition = (
        "\n# Managed by Hypr Control\n"
        "listener {\n"
        f"    timeout = {seconds}\n"
        f"    on-timeout = {command}\n"
        "}\n"
    )
    return text + suffix + addition


def remove_listener(text: str, command_fragment: str) -> str:
    lines = text.splitlines()
    for block in blocks(text):
        if block.path != ("listener",):
            continue
        body = "\n".join(lines[block.start : block.end + 1])
        if command_fragment in body:
            start = block.start
            if start > 0 and lines[start - 1].strip() == "# Managed by Hypr Control":
                start -= 1
            del lines[start : block.end + 1]
            while start < len(lines) and start > 0 and not lines[start].strip() and not lines[start - 1].strip():
                del lines[start]
            return "\n".join(lines).rstrip() + "\n"
    return text


def parse_bindings(text: str) -> list[dict[str, str | int]]:
    result = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        kind, payload = (part.strip() for part in stripped.split("=", 1))
        if not kind.startswith("bind") or kind == "binds":
            continue
        if kind.startswith("bindd"):
            parts = [part.strip() for part in payload.split(",", 4)]
            if len(parts) < 4:
                continue
            mods, key, description, dispatcher = parts[:4]
            argument = parts[4] if len(parts) == 5 else ""
        else:
            parts = [part.strip() for part in payload.split(",", 3)]
            if len(parts) < 3:
                continue
            mods, key, dispatcher = parts[:3]
            description = dispatcher.replace("exec", "Command").title()
            argument = parts[3] if len(parts) == 4 else ""
        result.append(
            {
                "index": index,
                "line": line,
                "kind": kind,
                "mods": mods,
                "key": key,
                "description": description,
                "dispatcher": dispatcher,
                "argument": argument,
            }
        )
    return result


def shortcut_chord(modifiers: str, key: str) -> tuple[tuple[str, ...], str]:
    """Return a stable representation for shortcut conflict comparisons."""
    normalized = {
        MODIFIER_ALIASES.get(item.upper(), item.upper())
        for item in re.split(r"[ +]+", modifiers.strip())
        if item
    }
    ordered = tuple(sorted(normalized, key=lambda item: (MODIFIER_ORDER.get(item, 99), item)))
    return ordered, key.strip().upper()


def idle_timeouts(
    screensaver_minutes: float,
    lock_after_minutes: float,
    suspend_minutes: float | None,
) -> tuple[int, int, int | None]:
    """Convert the friendly sequential idle model to absolute Hypridle seconds."""
    screensaver = round(screensaver_minutes * 60)
    lock = screensaver + round(lock_after_minutes * 60)
    suspend = None if suspend_minutes is None else round(suspend_minutes * 60)
    if screensaver < 0 or lock < screensaver:
        raise ValueError("Idle delays cannot be negative")
    if suspend is not None and suspend <= lock:
        raise ValueError("Suspend must occur after lock")
    return screensaver, lock, suspend


def atomic_write(path: Path, text: str) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak.hypr-control-{stamp}")
    mode = None
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        shutil.copy2(path, backup)
    else:
        backup = None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return backup


def restore_backup(path: Path, backup: Path | None) -> None:
    if backup is None:
        path.unlink(missing_ok=True)
    else:
        shutil.copy2(backup, path)


def backup_history(path: Path, limit: int = 20) -> list[Path]:
    """Return the newest Hypr Control backups for *path*."""
    pattern = f"{path.name}.bak.hypr-control-*"
    return sorted(path.parent.glob(pattern), key=lambda item: item.name, reverse=True)[:limit]


def prune_backups(path: Path, keep: int = 20) -> None:
    """Bound automatic backup growth without touching unrelated backups."""
    for backup in backup_history(path, limit=10_000)[keep:]:
        backup.unlink(missing_ok=True)


def transactional_write(
    changes: Mapping[Path, str],
    validate: Callable[[], tuple[bool, str]],
    *,
    keep_backups: int = 20,
) -> tuple[bool, str, dict[Path, Path | None]]:
    """Write and validate a set of files as one reversible operation.

    Files must temporarily exist for Hyprland and service validators to inspect
    them. Any write or validation failure restores every file already touched.
    Successful backups are retained for user-facing recovery.
    """
    backups: dict[Path, Path | None] = {}
    try:
        for path, text in changes.items():
            backups[path] = atomic_write(path, text)
        ok, message = validate()
        if not ok:
            raise ValueError(message or "Configuration validation failed")
    except Exception as error:
        for path, backup in reversed(tuple(backups.items())):
            try:
                restore_backup(path, backup)
            except OSError:
                pass
        return False, str(error), backups

    for path in changes:
        prune_backups(path, keep_backups)
    return True, "", backups
