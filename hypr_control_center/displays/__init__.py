"""Display discovery, layout, persistence, and automatic switching."""

from .engine import (
    Display,
    DisplayLayout,
    DisplayProfile,
    ProfileStore,
    apply_layout,
    capture_layout,
    discover_displays,
    layout_for_mode,
    render_monitors_config,
)

__all__ = [
    "Display",
    "DisplayLayout",
    "DisplayProfile",
    "ProfileStore",
    "apply_layout",
    "capture_layout",
    "discover_displays",
    "layout_for_mode",
    "render_monitors_config",
]
