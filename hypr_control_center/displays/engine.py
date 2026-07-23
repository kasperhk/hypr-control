from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
import json
import os
import re
import subprocess
import tempfile

from ..config_engine import transactional_write


INTERNAL_CONNECTORS = ("edp-", "lvds-", "dsi-")
MODE_RE = re.compile(
    r"(?P<width>\d+)x(?P<height>\d+)@(?P<refresh>\d+(?:\.\d+)?)"
)
SAFE_FALLBACK = "monitor = , preferred, auto, 1"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "display"


def _format_number(value: float, digits: int = 3) -> str:
    rounded = round(float(value), digits)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.{digits}f}".rstrip("0").rstrip(".")


def _transform_code(value: int | str) -> int:
    if isinstance(value, int):
        return value
    aliases = {
        "normal": 0,
        "90": 1,
        "180": 2,
        "270": 3,
        "flipped": 4,
        "flipped-90": 5,
        "flipped-180": 6,
        "flipped-270": 7,
    }
    return aliases.get(str(value), 0)


def _connector_edid(name: str, drm_root: Path = Path("/sys/class/drm")) -> bytes:
    try:
        entries = tuple(drm_root.iterdir())
    except OSError:
        return b""
    suffix = f"-{name}"
    for entry in entries:
        if entry.name.endswith(suffix):
            try:
                data = (entry / "edid").read_bytes()
            except OSError:
                continue
            if data and any(data):
                return data
    return b""


@dataclass(frozen=True)
class Display:
    name: str
    identity: str
    description: str
    make: str
    model: str
    serial: str
    width: int
    height: int
    refresh: float
    x: int
    y: int
    scale: float
    transform: int = 0
    enabled: bool = True
    focused: bool = False
    internal: bool = False
    ten_bit: bool = False
    available_modes: tuple[str, ...] = ()

    @property
    def logical_width(self) -> int:
        dimension = self.height if self.transform in (1, 3, 5, 7) else self.width
        return max(1, round(dimension / self.scale))

    @property
    def logical_height(self) -> int:
        dimension = self.width if self.transform in (1, 3, 5, 7) else self.height
        return max(1, round(dimension / self.scale))

    @classmethod
    def from_hyprland(
        cls, monitor: dict[str, Any], drm_root: Path = Path("/sys/class/drm")
    ) -> Display:
        name = _clean(monitor.get("name"))
        make = _clean(monitor.get("make"))
        model = _clean(monitor.get("model"))
        serial = _clean(monitor.get("serial"))
        description = _clean(monitor.get("description")) or " ".join(
            value for value in (make, model, serial) if value
        )
        edid = _connector_edid(name, drm_root)
        if edid:
            identity = f"edid:{sha256(edid).hexdigest()[:24]}"
        elif make or model or serial:
            identity = "display:" + _slug("|".join((make, model, serial)))
        else:
            identity = "connector:" + name.lower()
        bit_depth = monitor.get("currentFormat", "")
        return cls(
            name=name,
            identity=identity,
            description=description or name,
            make=make,
            model=model,
            serial=serial,
            width=int(monitor.get("width") or 0),
            height=int(monitor.get("height") or 0),
            refresh=float(monitor.get("refreshRate") or 60),
            x=int(monitor.get("x") or 0),
            y=int(monitor.get("y") or 0),
            scale=float(monitor.get("scale") or 1),
            transform=_transform_code(monitor.get("transform", 0)),
            enabled=not bool(monitor.get("disabled", False)),
            focused=bool(monitor.get("focused", False)),
            internal=name.lower().startswith(INTERNAL_CONNECTORS),
            ten_bit="10" in str(bit_depth),
            available_modes=tuple(monitor.get("availableModes") or ()),
        )


@dataclass
class DisplayLayout:
    identity: str
    connector: str
    enabled: bool
    width: int
    height: int
    refresh: float
    x: int
    y: int
    scale: float
    transform: int = 0
    mirror_of: str = ""
    mirror_identity: str = ""
    ten_bit: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DisplayLayout:
        fields = {
            key: data[key]
            for key in cls.__dataclass_fields__
            if key in data
        }
        return cls(**fields)


@dataclass
class DisplayProfile:
    profile_id: str
    name: str
    fingerprint: tuple[str, ...]
    dock_ids: tuple[str, ...]
    default_mode: str
    layouts: dict[str, list[DisplayLayout]] = field(default_factory=dict)
    auto_apply: bool = True
    connector_aliases: dict[str, list[str]] = field(default_factory=dict)
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DisplayProfile:
        layouts = {
            mode: [DisplayLayout.from_dict(item) for item in items]
            for mode, items in data.get("layouts", {}).items()
        }
        return cls(
            profile_id=data["profile_id"],
            name=data["name"],
            fingerprint=tuple(data.get("fingerprint", ())),
            dock_ids=tuple(data.get("dock_ids", ())),
            default_mode=data.get("default_mode", "extend"),
            layouts=layouts,
            auto_apply=bool(data.get("auto_apply", True)),
            connector_aliases={
                key: list(value)
                for key, value in data.get("connector_aliases", {}).items()
            },
            updated_at=data.get("updated_at", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "fingerprint": list(self.fingerprint),
            "dock_ids": list(self.dock_ids),
            "default_mode": self.default_mode,
            "layouts": {
                mode: [asdict(item) for item in items]
                for mode, items in self.layouts.items()
            },
            "auto_apply": self.auto_apply,
            "connector_aliases": self.connector_aliases,
            "updated_at": self.updated_at,
        }

    def resolved_layout(self, displays: Iterable[Display]) -> list[DisplayLayout]:
        current = {display.identity: display for display in displays}
        stored = self.layouts.get(self.default_mode) or self.layouts.get("extend") or []
        result = []
        for item in stored:
            display = current.get(item.identity)
            if display:
                mirror_of = (
                    current[item.mirror_identity].name
                    if item.mirror_identity in current
                    else item.mirror_of
                )
                result.append(
                    DisplayLayout(
                        **{
                            **asdict(item),
                            "connector": display.name,
                            "mirror_of": mirror_of,
                        }
                    )
                )
        return result


class ProfileStore:
    def __init__(self, path: Path):
        self.path = path
        self.profiles: list[DisplayProfile] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        self.profiles = [
            DisplayProfile.from_dict(item) for item in data.get("profiles", [])
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": 1,
                "profiles": [profile.to_dict() for profile in self.profiles],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_name, self.path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def match(
        self,
        displays: Iterable[Display],
        dock_ids: Iterable[str] | None = None,
    ) -> DisplayProfile | None:
        signature = fingerprint(displays)
        docks = tuple(sorted(discover_dock_ids() if dock_ids is None else dock_ids))
        monitor_matches = [
            profile for profile in self.profiles if profile.fingerprint == signature
        ]
        exact = [profile for profile in monitor_matches if profile.dock_ids == docks]
        if exact:
            return max(exact, key=lambda item: item.updated_at)
        legacy = [profile for profile in monitor_matches if not profile.dock_ids]
        if not docks and legacy:
            return max(legacy, key=lambda item: item.updated_at)
        return None

    def remember(
        self,
        displays: list[Display],
        mode: str,
        layout: list[DisplayLayout],
        name: str | None = None,
        auto_apply: bool = True,
        dock_ids: Iterable[str] | None = None,
    ) -> DisplayProfile:
        docks = tuple(sorted(discover_dock_ids() if dock_ids is None else dock_ids))
        profile = self.match(displays, docks)
        if profile is None:
            generated_name = name or profile_name(displays)
            profile_signature = sha256(
                (topology_id(displays) + "|" + "|".join(docks)).encode()
            ).hexdigest()
            profile = DisplayProfile(
                profile_id=_slug(generated_name) + "-" + profile_signature[:8],
                name=generated_name,
                fingerprint=fingerprint(displays),
                dock_ids=docks,
                default_mode=mode,
            )
            self.profiles.append(profile)
        elif name:
            profile.name = name
        profile.default_mode = mode
        profile.layouts[mode] = [DisplayLayout.from_dict(asdict(item)) for item in layout]
        profile.auto_apply = auto_apply
        profile.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        for display in displays:
            aliases = profile.connector_aliases.setdefault(display.identity, [])
            if display.name not in aliases:
                aliases.append(display.name)
        self.save()
        return profile


def discover_displays(
    raw: str | list[dict[str, Any]] | None = None,
    *,
    drm_root: Path = Path("/sys/class/drm"),
) -> list[Display]:
    if raw is None:
        result = subprocess.run(
            ["hyprctl", "-j", "monitors", "all"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "hyprctl failed")
        raw = result.stdout
    if isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = raw
    return [
        Display.from_hyprland(item, drm_root)
        for item in data
        if item.get("name")
    ]


def discover_dock_ids(
    thunderbolt_root: Path = Path("/sys/bus/thunderbolt/devices"),
    usb_root: Path = Path("/sys/bus/usb/devices"),
) -> tuple[str, ...]:
    """Return stable IDs for connected docks without depending on connector names."""
    identities = set()
    try:
        thunderbolt_entries = tuple(thunderbolt_root.iterdir())
    except OSError:
        thunderbolt_entries = ()
    for entry in thunderbolt_entries:
        if entry.name.startswith("domain"):
            continue
        values = []
        for attribute in ("vendor_name", "device_name", "unique_id"):
            try:
                values.append(_clean((entry / attribute).read_text()))
            except OSError:
                values.append("")
        if any(values):
            identities.add("thunderbolt:" + _slug("|".join(values)))

    try:
        usb_entries = tuple(usb_root.iterdir())
    except OSError:
        usb_entries = ()
    for entry in usb_entries:
        try:
            product = _clean((entry / "product").read_text())
        except OSError:
            continue
        if not re.search(r"\b(dock|docking|displaylink)\b", product, re.I):
            continue
        values = []
        for attribute in ("idVendor", "idProduct", "serial"):
            try:
                values.append(_clean((entry / attribute).read_text()))
            except OSError:
                values.append("")
        identities.add("usb:" + _slug("|".join((product, *values))))
    return tuple(sorted(identities))


def fingerprint(displays: Iterable[Display]) -> tuple[str, ...]:
    return tuple(sorted(display.identity for display in displays))


def topology_id(displays: Iterable[Display]) -> str:
    return sha256("\n".join(fingerprint(displays)).encode()).hexdigest()


def profile_name(displays: Iterable[Display]) -> str:
    external = [display for display in displays if not display.internal]
    selected = external or list(displays)
    names = []
    for display in selected:
        label = display.model or display.description or display.name
        if label not in names:
            names.append(label)
    return " + ".join(names) if names else "Display setup"


def normalize_layout(layout: Iterable[DisplayLayout]) -> list[DisplayLayout]:
    result = [DisplayLayout.from_dict(asdict(item)) for item in layout]
    enabled = [item for item in result if item.enabled and not item.mirror_of]
    if not enabled:
        return result
    min_x = min(item.x for item in enabled)
    min_y = min(item.y for item in enabled)
    for item in result:
        if item.enabled:
            item.x -= min_x
            item.y -= min_y
    return result


def capture_layout(displays: Iterable[Display]) -> list[DisplayLayout]:
    return normalize_layout(
        DisplayLayout(
            identity=display.identity,
            connector=display.name,
            enabled=display.enabled,
            width=display.width,
            height=display.height,
            refresh=display.refresh,
            x=display.x,
            y=display.y,
            scale=display.scale,
            transform=display.transform,
            ten_bit=display.ten_bit,
        )
        for display in displays
    )


def _horizontal(displays: list[Display], include: set[str]) -> list[DisplayLayout]:
    x = 0
    result = []
    for display in sorted(displays, key=lambda item: (not item.internal, item.name)):
        enabled = display.identity in include
        result.append(
            DisplayLayout(
                identity=display.identity,
                connector=display.name,
                enabled=enabled,
                width=display.width,
                height=display.height,
                refresh=display.refresh,
                x=x if enabled else 0,
                y=0,
                scale=display.scale,
                transform=display.transform,
                ten_bit=display.ten_bit,
            )
        )
        if enabled:
            x += display.logical_width
    return result


def _parsed_modes(display: Display) -> dict[tuple[int, int], list[float]]:
    modes: dict[tuple[int, int], list[float]] = {}
    for value in display.available_modes:
        match = MODE_RE.search(value)
        if match:
            size = (int(match["width"]), int(match["height"]))
            modes.setdefault(size, []).append(float(match["refresh"]))
    if not modes and display.width and display.height:
        modes[(display.width, display.height)] = [display.refresh]
    return modes


def common_mirror_mode(displays: list[Display]) -> tuple[int, int, float]:
    if not displays:
        raise ValueError("At least one display is required")
    modes = [_parsed_modes(display) for display in displays]
    common_sizes = set(modes[0])
    for available in modes[1:]:
        common_sizes &= set(available)
    if common_sizes:
        width, height = max(common_sizes, key=lambda size: (size[0] * size[1], size[0]))
    else:
        smallest = min(displays, key=lambda item: item.width * item.height)
        width, height = smallest.width, smallest.height
    refresh_sets = [
        {round(value, 1) for value in available.get((width, height), ())}
        for available in modes
    ]
    common_refresh = refresh_sets[0] if refresh_sets else set()
    for values in refresh_sets[1:]:
        common_refresh &= values
    if common_refresh:
        refresh = max(common_refresh)
    else:
        candidates = [
            min(available.get((width, height), [display.refresh]))
            for available, display in zip(modes, displays)
        ]
        refresh = min(candidates)
    return width, height, refresh


def layout_for_mode(
    displays: list[Display],
    mode: str,
    remembered: Iterable[DisplayLayout] | None = None,
) -> list[DisplayLayout]:
    if not displays:
        return []
    if mode == "extend":
        if remembered:
            stored = {item.identity: item for item in remembered}
            current = {display.identity: display for display in displays}
            resolved = []
            for identity, display in current.items():
                item = stored.get(identity)
                if item:
                    resolved.append(
                        DisplayLayout(**{**asdict(item), "connector": display.name})
                    )
            if len(resolved) == len(displays):
                return normalize_layout(resolved)
        return _horizontal(displays, {display.identity for display in displays})
    if mode == "internal":
        internal = {display.identity for display in displays if display.internal}
        if not internal:
            internal = {displays[0].identity}
        return _horizontal(displays, internal)
    if mode == "external":
        external = {display.identity for display in displays if not display.internal}
        if not external:
            external = {
                next(
                    (display.identity for display in displays if display.internal),
                    displays[0].identity,
                )
            }
        return _horizontal(displays, external)
    if mode == "mirror":
        enabled = [display for display in displays if display.enabled] or displays
        source = next((display for display in enabled if not display.internal), enabled[0])
        parsed = [_parsed_modes(display) for display in enabled]
        common_sizes = set(parsed[0])
        for available in parsed[1:]:
            common_sizes &= set(available)
        shared_mode = common_mirror_mode(enabled) if common_sizes else None
        return [
            DisplayLayout(
                identity=display.identity,
                connector=display.name,
                enabled=True,
                width=shared_mode[0] if shared_mode else display.width,
                height=shared_mode[1] if shared_mode else display.height,
                refresh=shared_mode[2] if shared_mode else display.refresh,
                x=0,
                y=0,
                scale=1 if shared_mode else display.scale,
                transform=0 if shared_mode else display.transform,
                mirror_of="" if display.identity == source.identity else source.name,
                mirror_identity=""
                if display.identity == source.identity
                else source.identity,
                ten_bit=False,
            )
            for display in displays
        ]
    raise ValueError(f"Unknown display mode: {mode}")


def render_monitors_config(
    layout: Iterable[DisplayLayout],
    *,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now().astimezone()
    lines = [
        "# Generated by Hypr Control.",
        f"# Updated {generated_at.isoformat(timespec='seconds')}.",
        "# Edit display profiles in Hypr Control; manual edits may be replaced.",
        "",
    ]
    ordered = sorted(
        layout,
        key=lambda item: (
            not item.enabled,
            bool(item.mirror_of),
            item.connector,
        ),
    )
    for item in ordered:
        if not item.enabled:
            lines.append(f"monitor = {item.connector}, disable")
            continue
        mode = (
            f"{item.width}x{item.height}@{_format_number(item.refresh)}"
            if item.width and item.height
            else "preferred"
        )
        line = (
            f"monitor = {item.connector}, {mode}, {item.x}x{item.y}, "
            f"{_format_number(item.scale)}"
        )
        extras = []
        if item.transform:
            extras.extend(("transform", str(item.transform)))
        if item.mirror_of:
            extras.extend(("mirror", item.mirror_of))
        if item.ten_bit:
            extras.extend(("bitdepth", "10"))
        if extras:
            line += ", " + ", ".join(extras)
        lines.append(line)
    lines.extend(("", "# Safe rule for newly connected, not-yet-remembered outputs.", SAFE_FALLBACK))
    return "\n".join(lines) + "\n"


def validate_hyprland() -> tuple[bool, str]:
    reload_result = subprocess.run(
        ["hyprctl", "reload"], capture_output=True, text=True, timeout=15
    )
    errors = subprocess.run(
        ["hyprctl", "configerrors"], capture_output=True, text=True, timeout=15
    )
    message = (
        reload_result.stdout
        + reload_result.stderr
        + errors.stdout
        + errors.stderr
    ).strip()
    return (
        reload_result.returncode == 0
        and errors.returncode == 0
        and not errors.stdout.strip(),
        message,
    )


def apply_layout(
    layout: Iterable[DisplayLayout],
    monitors_path: Path,
) -> tuple[bool, str]:
    text = render_monitors_config(layout)
    try:
        ok, message, _ = transactional_write(
            {monitors_path: text}, validate_hyprland
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if not ok:
        try:
            subprocess.run(["hyprctl", "reload"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ok, message
