from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import argparse
import json
import os
import socket
import subprocess
import time

from .engine import (
    ProfileStore,
    apply_layout,
    capture_layout,
    discover_displays,
    fingerprint,
    layout_for_mode,
    profile_name,
    validate_hyprland,
)
from ..config_engine import transactional_write


def _xdg_path(variable: str, fallback: Path) -> Path:
    return Path(os.environ.get(variable, fallback)).expanduser()


CONFIG_HOME = _xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")
STATE_HOME = _xdg_path("XDG_STATE_HOME", Path.home() / ".local/state")
PROFILES_PATH = CONFIG_HOME / "hypr-control/display-profiles.json"
STATE_PATH = STATE_HOME / "hypr-control/display-runtime.json"
MONITORS_PATH = CONFIG_HOME / "hypr/hypr-control-monitors.lua"
PREVIEW_PATH = STATE_HOME / "hypr-control/display-preview.json"


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, STATE_PATH)


def suspend_automation(seconds: int = 30) -> None:
    state = _read_state()
    state["manual_until"] = time.time() + seconds
    _write_state(state)


def resume_automation() -> None:
    state = _read_state()
    state.pop("manual_until", None)
    _write_state(state)


def arm_preview(previous: str, generated: str, seconds: int = 20) -> None:
    PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_PATH.write_text(
        json.dumps(
            {
                "deadline": time.time() + seconds,
                "previous": previous,
                "generated": generated,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def clear_preview() -> None:
    PREVIEW_PATH.unlink(missing_ok=True)


def rollback_expired_preview() -> bool:
    try:
        preview = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if float(preview.get("deadline", 0)) > time.time():
        return False
    current = ""
    try:
        current = MONITORS_PATH.read_text(encoding="utf-8")
    except OSError:
        pass
    if current != preview.get("generated"):
        clear_preview()
        resume_automation()
        return False
    previous = preview.get("previous") or 'hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })\n'
    try:
        ok, _message, _backups = transactional_write(
            {MONITORS_PATH: previous}, validate_hyprland
        )
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    clear_preview()
    resume_automation()
    if ok:
        _notify(
            "Display preview restored",
            "Hypr Control recovered an unconfirmed display configuration.",
        )
    return ok


def _notify(title: str, body: str) -> None:
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=Hypr Control", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        pass


def _event_socket() -> Path | None:
    runtime = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    )
    hypr_root = runtime / "hypr"
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if signature:
        candidate = hypr_root / signature / ".socket2.sock"
        if candidate.exists():
            return candidate
    try:
        candidates = sorted(
            hypr_root.glob("*/.socket2.sock"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


def reconcile(*, force: bool = False, notify_unknown: bool = True) -> bool:
    displays = discover_displays()
    if not displays:
        return False
    signature = "|".join(fingerprint(displays))
    state = _read_state()
    if float(state.get("manual_until", 0)) > time.time():
        return False
    store = ProfileStore(PROFILES_PATH)
    profile = store.match(displays)
    if not profile:
        profile = store.remember(
            displays,
            "extend",
            capture_layout(displays),
            name=profile_name(displays),
            auto_apply=False,
        )
        if notify_unknown and state.get("unknown_signature") != signature:
            _notify(
                "New display setup remembered",
                f"{profile.name} was saved safely. Open Hypr Control to arrange it and enable automatic switching.",
            )
        _write_state(
            {
                **state,
                "unknown_signature": signature,
                "seen_at": time.time(),
            }
        )
        return False
    if not profile.auto_apply:
        return False
    marker = f"{signature}:{profile.profile_id}:{profile.updated_at}"
    if not force and state.get("applied_marker") == marker:
        return True
    layout = profile.resolved_layout(displays)
    if len(layout) != len(displays):
        return False
    ok, message = apply_layout(layout, MONITORS_PATH)
    if ok:
        _write_state(
            {
                "applied_marker": marker,
                "profile_id": profile.profile_id,
                "profile_name": profile.name,
                "mode": profile.default_mode,
                "applied_at": time.time(),
            }
        )
        _notify(
            "Display setup restored",
            f"{profile.name} · {profile.default_mode.replace('_', ' ').title()}",
        )
        return True
    _notify("Display setup failed", message or f"Could not apply {profile.name}")
    return False


def watch(debounce: float = 2.0) -> int:
    while True:
        path = _event_socket()
        if not path:
            time.sleep(2)
            continue
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
            client.settimeout(0.5)
            try:
                reconcile(force=True, notify_unknown=False)
            except (OSError, RuntimeError, json.JSONDecodeError):
                pass
            buffer = b""
            deadline = 0.0
            while True:
                rollback_expired_preview()
                try:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        event = raw.decode(errors="replace")
                        if event.startswith(
                            (
                                "monitoradded>>",
                                "monitoraddedv2>>",
                                "monitorremoved>>",
                                "monitorremovedv2>>",
                            )
                        ):
                            deadline = time.monotonic() + debounce
                except TimeoutError:
                    pass
                if deadline and time.monotonic() >= deadline:
                    deadline = 0.0
                    try:
                        reconcile(force=True)
                    except (OSError, RuntimeError, json.JSONDecodeError):
                        pass
        except OSError:
            time.sleep(2)
        finally:
            client.close()


def status() -> int:
    displays = discover_displays()
    store = ProfileStore(PROFILES_PATH)
    profile = store.match(displays)
    print(
        json.dumps(
            {
                "displays": [asdict(display) for display in displays],
                "matched_profile": profile.to_dict() if profile else None,
                "runtime": _read_state(),
            },
            indent=2,
        )
    )
    return 0


def remember(name: str | None, mode: str, auto_apply: bool) -> int:
    displays = discover_displays()
    if not displays:
        raise RuntimeError("No displays were reported by Hyprland")
    store = ProfileStore(PROFILES_PATH)
    layout = capture_layout(displays)
    profile = store.remember(
        displays,
        mode,
        layout,
        name=name,
        auto_apply=auto_apply,
    )
    print(f"Remembered {profile.name} ({profile.profile_id})")
    return 0


def apply_mode(mode: str, should_remember: bool) -> int:
    displays = discover_displays()
    store = ProfileStore(PROFILES_PATH)
    profile = store.match(displays)
    remembered = (
        profile.layouts.get("extend")
        if profile and mode == "extend"
        else None
    )
    layout = layout_for_mode(displays, mode, remembered)
    ok, message = apply_layout(layout, MONITORS_PATH)
    if not ok:
        raise RuntimeError(message or "Hyprland rejected the display configuration")
    if should_remember:
        store.remember(displays, mode, layout)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="hypr-control-display",
        description="Hypr Control display profile service and command-line helper",
    )
    commands = result.add_subparsers(dest="command", required=True)
    watcher = commands.add_parser("watch", help="Watch Hyprland monitor events")
    watcher.add_argument("--debounce", type=float, default=2.0)
    commands.add_parser("status", help="Show displays, matching profile, and runtime state")
    remember_command = commands.add_parser(
        "remember", help="Remember the current display arrangement"
    )
    remember_command.add_argument("--name")
    remember_command.add_argument(
        "--mode",
        choices=("extend", "mirror", "external", "internal"),
        default="extend",
    )
    remember_command.add_argument(
        "--no-auto", action="store_true", help="Do not apply automatically"
    )
    apply_command = commands.add_parser("apply", help="Apply a quick display mode")
    apply_command.add_argument(
        "mode", choices=("extend", "mirror", "external", "internal")
    )
    apply_command.add_argument("--remember", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "watch":
            return watch(args.debounce)
        if args.command == "status":
            return status()
        if args.command == "remember":
            return remember(args.name, args.mode, not args.no_auto)
        if args.command == "apply":
            return apply_mode(args.mode, args.remember)
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"hypr-control-display: {error}", file=os.sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
