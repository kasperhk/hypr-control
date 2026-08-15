from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import difflib
import json
import os
import re
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .config_engine import (
    backup_history,
    get_lua_value,
    get_value,
    lua_scalar,
    parse_bindings,
    render_lua_bindings,
    set_lua_value,
    set_value,
    shortcut_chord,
    transactional_write,
)
from .displays.engine import (
    Display,
    DisplayLayout,
    MODE_RE,
    ProfileStore,
    capture_layout,
    discover_displays,
    layout_for_mode,
    profile_name,
    render_monitors_config,
)
from .displays.widgets import DisplayCanvas
from .displays.service import (
    arm_preview,
    clear_preview,
    resume_automation,
    rollback_expired_preview,
    suspend_automation,
)


APP_ID = "io.github.kasperhk.HyprControl"
ROOT = Path(__file__).resolve().parent
XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser()
OMARCHY_PATH = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")).expanduser()
HYPR = XDG_CONFIG_HOME / "hypr"
DEFAULT_HYPR = OMARCHY_PATH / "default/hypr"
DISPLAY_PROFILES = XDG_CONFIG_HOME / "hypr-control/display-profiles.json"
HYPR_CONTROL_CONFIG = HYPR / "hypr-control.lua"
MONITORS_CONFIG = HYPR / "hypr-control-monitors.lua"
BINDINGS_CONFIG = HYPR / "hypr-control-bindings.lua"
BINDINGS_STORE = XDG_CONFIG_HOME / "hypr-control/bindings.conf"
SHELL_CONFIG = XDG_CONFIG_HOME / "omarchy/shell.json"


@dataclass
class Binding:
    path: Path
    block: tuple[str, ...]
    key: str
    widget: Gtk.Widget
    kind: str
    values: list[str] | None = None
    original: str = ""
    page: str = ""

    def serialize(self) -> str:
        if self.kind == "spin":
            value = self.widget.get_value()
            return str(int(value)) if self.widget.get_digits() == 0 else f"{value:.2f}".rstrip("0").rstrip(".")
        if self.kind == "switch":
            return "true" if self.widget.get_active() else "false"
        if self.kind == "entry":
            return self.widget.get_text().strip()
        if self.kind == "combo" and self.values:
            return self.values[self.widget.get_selected()]
        raise ValueError(f"Unknown setting kind: {self.kind}")


class HyprControlWindow(Adw.ApplicationWindow):
    pages = [
        ("home", "Overview", "preferences-system-symbolic"),
        ("displays", "Displays", "video-display-symbolic"),
        ("appearance", "Appearance", "applications-graphics-symbolic"),
        ("input", "Keyboard & Mouse", "input-keyboard-symbolic"),
        ("power", "Power & Idle", "preferences-system-power-symbolic"),
        ("shortcuts", "Shortcuts", "preferences-desktop-keyboard-shortcuts-symbolic"),
        ("advanced", "Advanced", "text-x-generic-symbolic"),
    ]

    def __init__(self, application: Adw.Application):
        super().__init__(application=application)
        self.set_title("Hypr Control")
        self.set_default_size(1180, 760)
        self.set_size_request(640, 520)

        self.loading = True
        self.saving = False
        self.setting_bindings: list[Binding] = []
        self.dirty_bindings: set[int] = set()
        self.shortcut_additions: list[str] = []
        self.shortcut_removals: set[str] = set()
        self.advanced_dirty = False
        self.advanced_current = "hyprland.lua"
        self._advanced_switching = False
        self._idle_dirty = False
        self._idle_original: tuple[float, float] | None = None
        self.file_snapshots: dict[Path, str | None] = {}
        self.recent_change = "No changes applied in this session"
        self._building_page = "home"
        self.display_store = ProfileStore(DISPLAY_PROFILES)
        try:
            rollback_expired_preview()
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.display_live: list[Display] = []
        self.display_mode = "extend"
        self._display_layout_dirty = False
        self._display_preview: dict | None = None
        self._display_preview_timer = 0
        self._display_preview_dialog = None
        self.connect("close-request", self._close_requested)

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Overview", subtitle="Hyprland and Omarchy")
        header.set_title_widget(self.window_title)
        navigation_button = Gtk.Button(icon_name="sidebar-show-symbolic")
        navigation_button.set_tooltip_text("Show navigation")
        navigation_button.connect("clicked", self._toggle_sidebar)
        header.pack_start(navigation_button)
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text("Refresh system status")
        refresh_button.connect("clicked", lambda *_: self.refresh_runtime())
        header.pack_end(refresh_button)
        toolbar.add_top_bar(header)

        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_sidebar_width_fraction(0.24)
        self.split_view.set_min_sidebar_width(220)
        self.split_view.set_max_sidebar_width(280)
        self.split_view.set_show_sidebar(True)
        toolbar.set_content(self.split_view)

        self.pending_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            reveal_child=False,
        )
        pending_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        pending_bar.add_css_class("pending-bar")
        self.pending_label = Gtk.Label(label="Unsaved changes", xalign=0)
        self.pending_label.set_hexpand(True)
        pending_bar.append(self.pending_label)
        discard_button = self._button("Discard", "edit-undo-symbolic", self._discard_pending)
        pending_bar.append(discard_button)
        self.apply_button = self._button("Apply", "object-select-symbolic", self._save_pending, "suggested-action")
        pending_bar.append(self.apply_button)
        self.pending_revealer.set_child(pending_bar)
        toolbar.add_bottom_bar(self.pending_revealer)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.set_size_request(220, -1)
        sidebar.add_css_class("sidebar")
        self.search_entry = Gtk.SearchEntry(placeholder_text="Find settings")
        self.search_entry.set_tooltip_text("Search settings (Ctrl+K)")
        self.search_entry.set_margin_top(16)
        self.search_entry.set_margin_start(16)
        self.search_entry.set_margin_end(16)
        self.search_entry.connect("search-changed", self._filter_pages)
        sidebar.append(self.search_entry)
        self.sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar_list.add_css_class("navigation-sidebar")
        self.sidebar_list.set_filter_func(self._page_filter)
        self.page_query = ""
        for name, label, icon in self.pages:
            row = Gtk.ListBoxRow()
            row.page_name = name
            row.search_text = f"{name} {label}".lower()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            content.append(Gtk.Image.new_from_icon_name(icon))
            title = Gtk.Label(label=label, xalign=0)
            title.set_hexpand(True)
            content.append(title)
            row.set_child(content)
            self.sidebar_list.append(row)
        self.sidebar_list.connect("row-selected", self._navigate)
        sidebar.append(self.sidebar_list)
        self.split_view.set_sidebar(sidebar)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.split_view.set_content(self.stack)

        narrow = Adw.Breakpoint(
            condition=Adw.BreakpointCondition.new_length(
                Adw.BreakpointConditionLengthType.MAX_WIDTH, 900, Adw.LengthUnit.SP
            )
        )
        narrow.add_setter(self.split_view, "collapsed", True)
        narrow.add_setter(self.split_view, "show-sidebar", False)
        self.add_breakpoint(narrow)

        shortcuts = Gtk.EventControllerKey()
        shortcuts.connect("key-pressed", self._window_key_pressed)
        self.add_controller(shortcuts)

        self._build_home()
        self._build_displays()
        self._build_appearance()
        self._build_input()
        self._build_power()
        self._build_shortcuts()
        self._build_advanced()
        initial_page = os.environ.get("HYPR_CONTROL_PAGE", "home")
        initial_index = next(
            (
                index
                for index, (name, _label, _icon) in enumerate(self.pages)
                if name == initial_page
            ),
            0,
        )
        self.sidebar_list.select_row(
            self.sidebar_list.get_row_at_index(initial_index)
        )
        self.loading = False
        self._capture_file_snapshots()
        self.refresh_runtime()

    def toast(self, message: str, timeout: int = 3):
        toast = Adw.Toast(title=message)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)

    def _page(self, title: str, description: str) -> Adw.PreferencesPage:
        self._building_page = next((key for key, label, _icon in self.pages if label == title), self._building_page)
        page = Adw.PreferencesPage()
        page.set_title(title)
        page.set_description(description)
        page.set_margin_top(12)
        page.set_margin_bottom(24)
        page.set_margin_start(12)
        page.set_margin_end(12)
        return page

    def _group(self, page: Adw.PreferencesPage, title: str, description: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        group._page_name = self._building_page
        page.add(group)
        return group

    def _action_row(self, group, title, subtitle="", icon=None):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        self._add_page_search_terms(getattr(group, "_page_name", self._building_page), title, subtitle)
        if icon:
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        group.add(row)
        return row

    def _page_filter(self, row):
        return not self.page_query or self.page_query in row.search_text

    def _filter_pages(self, entry):
        self.page_query = entry.get_text().strip().lower()
        self.sidebar_list.invalidate_filter()
        visible = [row for row in self._sidebar_rows() if self._page_filter(row)]
        if self.page_query and len(visible) == 1:
            self.sidebar_list.select_row(visible[0])

    def _sidebar_rows(self):
        index = 0
        while (row := self.sidebar_list.get_row_at_index(index)) is not None:
            yield row
            index += 1

    def _add_page_search_terms(self, page_name: str, *terms: str):
        row = next((item for item in self._sidebar_rows() if item.page_name == page_name), None)
        if row:
            row.search_text += " " + " ".join(terms).lower()

    def _navigate(self, _listbox, row):
        if not row:
            return
        name = row.page_name
        label = next(label for key, label, _ in self.pages if key == name)
        self.stack.set_visible_child_name(name)
        self.window_title.set_title(label)
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def _toggle_sidebar(self, *_args):
        self.split_view.set_show_sidebar(not self.split_view.get_show_sidebar())

    def _window_key_pressed(self, _controller, keyval, _keycode, state):
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if control and keyval in (Gdk.KEY_k, Gdk.KEY_K):
            self.split_view.set_show_sidebar(True)
            self.search_entry.grab_focus()
            return True
        if control and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self._pending_count():
            self._save_pending()
            return True
        if keyval == Gdk.KEY_Escape and self.search_entry.get_text():
            self.search_entry.set_text("")
            return True
        return False

    def _resolved(self, path: Path, block: tuple[str, ...], key: str, fallback: str) -> str:
        if path.suffix == ".lua":
            value = get_lua_value(self._config_text(path), block, key)
        else:
            value = get_value(self._config_text(path), block, key)
        if value is not None:
            return value
        return fallback

    @staticmethod
    def _config_text(path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        default_path = DEFAULT_HYPR / path.name
        if default_path.exists():
            return default_path.read_text(encoding="utf-8")
        return ""

    @staticmethod
    def _bool(value: str) -> bool:
        return value.strip().lower().split(",", 1)[0] in {"1", "true", "yes", "on"}

    def _register(self, binding: Binding):
        binding.original = binding.serialize()
        binding.page = self._building_page
        subtitle = binding.widget.get_subtitle() if hasattr(binding.widget, "get_subtitle") else ""
        self._add_page_search_terms(binding.page, binding.widget.get_title(), subtitle or "", binding.key)
        index = len(self.setting_bindings)
        self.setting_bindings.append(binding)
        signal = {
            "spin": "notify::value",
            "switch": "notify::active",
            "entry": "notify::text",
            "combo": "notify::selected",
        }[binding.kind]
        binding.widget.connect(signal, lambda *_args, i=index: self._mark_dirty(i))

    def _mark_dirty(self, index: int):
        if self.loading:
            return
        binding = self.setting_bindings[index]
        if binding.serialize() == binding.original:
            self.dirty_bindings.discard(index)
            binding.widget.remove_css_class("pending-setting")
        else:
            self.dirty_bindings.add(index)
            binding.widget.add_css_class("pending-setting")
        self._update_pending_bar()

    def _pending_count(self) -> int:
        return len(self.dirty_bindings) + int(self._idle_dirty) + len(self.shortcut_additions) + len(self.shortcut_removals)

    def _update_pending_bar(self):
        count = self._pending_count()
        self.pending_label.set_label(f"{count} unsaved change{'s' if count != 1 else ''}")
        self.apply_button.set_sensitive(bool(count) and not self.saving)
        self.pending_revealer.set_reveal_child(bool(count))

    @staticmethod
    def _set_binding_widget(binding: Binding, value: str):
        if binding.kind == "spin":
            binding.widget.set_value(float(value))
        elif binding.kind == "switch":
            binding.widget.set_active(value == "true")
        elif binding.kind == "entry":
            binding.widget.set_text(value)
        elif binding.kind == "combo" and binding.values:
            binding.widget.set_selected(binding.values.index(value))

    def _discard_pending(self, *_args):
        self.loading = True
        for binding in self.setting_bindings:
            self._set_binding_widget(binding, binding.original)
            binding.widget.remove_css_class("pending-setting")
        if self._idle_original:
            saver, lock = self._idle_original
            self.saver_delay.set_value(saver)
            self.lock_delay.set_value(lock)
        self.loading = False
        self.dirty_bindings.clear()
        self._idle_dirty = False
        self.shortcut_additions.clear()
        self.shortcut_removals.clear()
        self._populate_shortcuts()
        self._update_idle_timeline()
        self._update_pending_bar()
        self.toast("Pending changes discarded")

    def _close_requested(self, *_args):
        if self._display_preview:
            self._finish_display_preview(False)
            self.toast("Display preview reverted before closing")
            return True
        if not self._pending_count() and not self.advanced_dirty:
            return False
        dialog = Adw.AlertDialog(
            heading="Discard unsaved changes?",
            body="Hypr Control has changes that have not been applied.",
        )
        dialog.add_response("cancel", "Keep editing")
        dialog.add_response("discard", "Discard")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _dialog, response: self.destroy() if response == "discard" else None)
        dialog.present(self)
        return True

    @staticmethod
    def _owned_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _capture_file_snapshots(self):
        paths = {binding.path for binding in self.setting_bindings}
        paths.update(
            {
                SHELL_CONFIG,
                BINDINGS_STORE,
                BINDINGS_CONFIG,
                MONITORS_CONFIG,
            }
        )
        self.file_snapshots.update({path: self._owned_text(path) for path in paths})

    def _externally_changed(self, paths):
        return [path for path in paths if path in self.file_snapshots and self._owned_text(path) != self.file_snapshots[path]]

    def _spin(self, group, title, subtitle, path, block, key, fallback, minimum, maximum, step, digits=0):
        row = Adw.SpinRow.new_with_range(minimum, maximum, step)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_digits(digits)
        try:
            row.set_value(float(self._resolved(path, block, key, str(fallback)).split()[0]))
        except ValueError:
            row.set_value(float(fallback))
        group.add(row)
        self._register(Binding(path, block, key, row, "spin"))
        return row

    def _switch(self, group, title, subtitle, path, block, key, fallback=False):
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(self._bool(self._resolved(path, block, key, "true" if fallback else "false")))
        group.add(row)
        self._register(Binding(path, block, key, row, "switch"))
        return row

    def _entry(self, group, title, path, block, key, fallback=""):
        row = Adw.EntryRow(title=title)
        row.set_text(self._resolved(path, block, key, fallback))
        group.add(row)
        self._register(Binding(path, block, key, row, "entry"))
        return row

    def _combo(self, group, title, subtitle, path, block, key, values, labels=None, fallback=None):
        labels = labels or values
        row = Adw.ComboRow(title=title, subtitle=subtitle)
        row.set_model(Gtk.StringList.new(labels))
        current = self._resolved(path, block, key, fallback or values[0])
        row.set_selected(values.index(current) if current in values else 0)
        group.add(row)
        self._register(Binding(path, block, key, row, "combo", values))
        return row

    def _button(self, label, icon, callback, css=None):
        button = Gtk.Button(label=label)
        if icon:
            button.set_icon_name(icon)
        button.connect("clicked", callback)
        if css:
            button.add_css_class(css)
        return button

    def _build_home(self):
        page = self._page("Overview", "Live compositor status and common actions")
        self.stack.add_named(page, "home")
        status = self._group(page, "System status")
        self.status_compositor = self._action_row(status, "Hyprland", "Checking compositor", "preferences-system-symbolic")
        self.status_config = self._action_row(status, "Configuration", "Checking for errors", "emblem-ok-symbolic")
        self.status_display = self._action_row(status, "Active display", "Checking outputs", "video-display-symbolic")

        desktop = self._group(page, "Desktop at a glance")
        self.status_style = self._action_row(desktop, "Theme and font", "Checking Omarchy style", "applications-graphics-symbolic")
        self.status_input = self._action_row(desktop, "Input devices", "Checking connected devices", "input-mouse-symbolic")
        self.recent_change_row = self._action_row(desktop, "Recent change", self.recent_change, "document-open-recent-symbolic")

        actions = self._group(page, "Quick actions")
        display = self._action_row(actions, "Display layout", "Arrange outputs and switch remembered setups", "video-display-symbolic")
        display_button = self._button(
            "Open",
            "go-next-symbolic",
            lambda *_: self.sidebar_list.select_row(
                next(row for row in self._sidebar_rows() if row.page_name == "displays")
            ),
        )
        display.add_suffix(display_button)
        lock = self._action_row(actions, "Lock now", "Secure the session immediately", "system-lock-screen-symbolic")
        lock.add_suffix(self._button("Lock", "system-lock-screen-symbolic", lambda *_: self._launch(["omarchy", "system", "lock"])))
        reload_row = self._action_row(actions, "Reload Hyprland", "Re-read configuration without ending the session", "view-refresh-symbolic")
        reload_row.add_suffix(self._button("Reload", "view-refresh-symbolic", lambda *_: self._run(["hyprctl", "reload"], self._command_toast("Hyprland reloaded"))))

    def _build_displays(self):
        self._building_page = "displays"
        self._add_page_search_terms(
            "displays",
            "monitor display layout mirror extend external only internal only profiles automatic dock",
        )
        page = self._page(
            "Displays",
            "Native layouts, remembered hardware setups, and automatic switching",
        )
        self.stack.add_named(page, "displays")

        modes = self._group(
            page,
            "Quick modes",
            "Changes are previewed for 15 seconds and revert unless confirmed",
        )
        mode_box = Gtk.Grid(
            column_spacing=8,
            row_spacing=8,
            column_homogeneous=True,
        )
        for index, (mode, label, icon) in enumerate((
            ("extend", "Extend", "view-dual-symbolic"),
            ("mirror", "Mirror", "object-flip-horizontal-symbolic"),
            ("external", "External only", "video-display-symbolic"),
            ("internal", "Internal only", "computer-symbolic"),
        )):
            button = Gtk.Button(hexpand=True)
            content = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
                halign=Gtk.Align.CENTER,
            )
            content.append(Gtk.Image.new_from_icon_name(icon))
            content.append(Gtk.Label(label=label))
            button.set_child(content)
            button.connect(
                "clicked",
                lambda _button, selected=mode: self._select_display_mode(
                    selected
                ),
            )
            button.add_css_class("display-mode-button")
            mode_box.attach(button, index % 2, index // 2, 1, 1)
        modes.add(mode_box)

        layout = self._group(
            page,
            "Arrangement",
            "Drag displays to position them; coordinates are normalized automatically",
        )
        self.display_canvas = DisplayCanvas(self._display_canvas_changed)
        layout.add(self.display_canvas)
        layout_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
        )
        self.display_apply_button = self._button(
            "Apply arrangement",
            "object-select-symbolic",
            self._apply_canvas_layout,
            "suggested-action",
        )
        self.display_apply_button.set_sensitive(False)
        layout_actions.append(self.display_apply_button)
        self.display_remember_button = self._button(
            "Remember setup",
            "document-save-symbolic",
            self._remember_display_setup,
        )
        self.display_remember_button.set_sensitive(False)
        layout_actions.append(self.display_remember_button)
        layout.add(layout_actions)

        memory = self._group(
            page,
            "Setup memory",
            "Profiles match stable display identity instead of changing connector names",
        )
        self.display_profile_row = self._action_row(
            memory,
            "Current setup",
            "Waiting for compositor data",
            "document-open-recent-symbolic",
        )
        self.display_auto_switch = Adw.SwitchRow(
            title="Apply automatically",
            subtitle="Restore this setup whenever the same displays reconnect",
        )
        self.display_auto_switch.set_sensitive(False)
        self.display_auto_switch.connect(
            "notify::active", self._display_auto_switch_changed
        )
        memory.add(self.display_auto_switch)

        self.display_group = self._group(
            page,
            "Connected displays",
            "Live values from Hyprland; configure mode, scale, rotation, and position",
        )
        self.display_rows = []

    def _display_labels(self):
        return {
            display.identity: (
                display.model or display.description or display.name
            )
            for display in self.display_live
        }

    def _display_canvas_changed(self):
        self._display_layout_dirty = True
        self.display_mode = "extend"
        self.display_apply_button.set_sensitive(bool(self.display_live))

    def _select_display_mode(self, mode):
        if not self.display_live or self._display_preview:
            return
        profile = self.display_store.match(self.display_live)
        remembered = None
        if profile and mode == "extend":
            remembered = profile.layouts.get("extend")
        try:
            layout = layout_for_mode(self.display_live, mode, remembered)
        except ValueError as error:
            self._show_error("The display mode could not be prepared", str(error))
            return
        self.display_mode = mode
        self.display_canvas.set_layout(layout, self._display_labels())
        self._display_layout_dirty = False
        self.display_apply_button.set_sensitive(False)
        self._apply_display_layout(mode, layout)

    def _apply_canvas_layout(self, *_args):
        if not self.display_live or self._display_preview:
            return
        self._apply_display_layout("extend", self.display_canvas.get_layout())

    def _apply_display_layout(self, mode, layout):
        previous = self._owned_text(MONITORS_CONFIG)
        conflict = self._externally_changed({MONITORS_CONFIG: ""})
        if conflict:
            self._show_error(
                "Display configuration changed outside Hypr Control",
                "Refresh Hypr Control before applying so its native display configuration is not overwritten.",
            )
            return
        generated = render_monitors_config(layout)
        self.display_apply_button.set_sensitive(False)
        suspend_automation(30)
        self._apply_transaction(
            {MONITORS_CONFIG: generated},
            lambda ok, message: self._display_layout_applied(
                ok, message, mode, layout, previous, generated
            ),
        )

    def _display_layout_applied(
        self, ok, message, mode, layout, previous, generated
    ):
        if not ok:
            clear_preview()
            resume_automation()
            self.display_apply_button.set_sensitive(self._display_layout_dirty)
            self._show_error("The display change was reverted", message)
            return
        self._display_preview = {
            "mode": mode,
            "layout": [DisplayLayout(**item.__dict__) for item in layout],
            "previous": previous,
            "generated": generated,
            "remaining": 15,
        }
        arm_preview(
            previous
            or 'hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })\n',
            generated,
        )
        dialog = Adw.AlertDialog(
            heading="Keep this display configuration?",
            body="The previous configuration will be restored automatically in 15 seconds.",
        )
        dialog.add_response("revert", "Revert")
        dialog.add_response("keep", "Keep")
        dialog.set_default_response("keep")
        dialog.set_close_response("revert")
        dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._display_preview_response)
        self._display_preview_dialog = dialog
        self._display_preview_timer = GLib.timeout_add_seconds(
            1, self._display_preview_tick
        )
        dialog.present(self)

    def _display_preview_tick(self):
        if not self._display_preview:
            return GLib.SOURCE_REMOVE
        self._display_preview["remaining"] -= 1
        remaining = self._display_preview["remaining"]
        if remaining <= 0:
            self._finish_display_preview(False)
            return GLib.SOURCE_REMOVE
        if self._display_preview_dialog:
            self._display_preview_dialog.set_body(
                f"The previous configuration will be restored automatically in {remaining} seconds."
            )
        return GLib.SOURCE_CONTINUE

    def _display_preview_response(self, _dialog, response):
        if self._display_preview:
            self._finish_display_preview(response == "keep")

    def _finish_display_preview(self, keep):
        preview = self._display_preview
        if not preview:
            return
        self._display_preview = None
        if self._display_preview_timer:
            GLib.source_remove(self._display_preview_timer)
            self._display_preview_timer = 0
        dialog = self._display_preview_dialog
        self._display_preview_dialog = None
        if dialog:
            dialog.close()
        if keep:
            clear_preview()
            profile = self.display_store.remember(
                self.display_live,
                preview["mode"],
                preview["layout"],
                auto_apply=True,
            )
            self.file_snapshots[MONITORS_CONFIG] = preview["generated"]
            self._display_layout_dirty = False
            self.display_apply_button.set_sensitive(False)
            self.display_remember_button.set_sensitive(True)
            self.display_profile_row.set_subtitle(
                f"{profile.name} · {profile.default_mode.replace('_', ' ').title()}"
            )
            self.loading = True
            self.display_auto_switch.set_sensitive(True)
            self.display_auto_switch.set_active(profile.auto_apply)
            self.loading = False
            self.recent_change = f"Display setup changed to {profile.name}"
            self.recent_change_row.set_subtitle(self.recent_change)
            self.toast(f"Remembered {profile.name}")
            resume_automation()
            self.refresh_runtime()
            return

        previous = preview["previous"]
        clear_preview()
        if previous is None:
            previous = 'hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })\n'
        self._apply_transaction(
            {MONITORS_CONFIG: previous},
            self._display_layout_reverted,
        )

    def _display_layout_reverted(self, ok, message):
        resume_automation()
        if ok:
            self.file_snapshots[MONITORS_CONFIG] = self._owned_text(
                MONITORS_CONFIG
            )
            self.toast("Previous display configuration restored")
            self.refresh_runtime()
        else:
            self._show_error(
                "The previous display configuration could not be restored",
                message,
            )

    def _remember_display_setup(self, *_args):
        if not self.display_live:
            return
        layout = self.display_canvas.get_layout() or capture_layout(
            self.display_live
        )
        profile = self.display_store.remember(
            self.display_live,
            self.display_mode,
            layout,
            auto_apply=self.display_auto_switch.get_active()
            if self.display_auto_switch.get_sensitive()
            else True,
        )
        self.display_profile_row.set_subtitle(
            f"{profile.name} · {profile.default_mode.replace('_', ' ').title()}"
        )
        self.loading = True
        self.display_auto_switch.set_sensitive(True)
        self.display_auto_switch.set_active(profile.auto_apply)
        self.loading = False
        self.toast(f"Remembered {profile.name}")

    def _display_auto_switch_changed(self, row, _param):
        if self.loading or not self.display_live:
            return
        profile = self.display_store.match(self.display_live)
        if not profile:
            return
        profile.auto_apply = row.get_active()
        profile.updated_at = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        self.display_store.save()
        self.toast(
            "Automatic switching enabled"
            if profile.auto_apply
            else "Automatic switching disabled"
        )

    def _show_display_settings(self, _button, identity):
        display = next(
            (item for item in self.display_live if item.identity == identity),
            None,
        )
        if not display:
            return
        layout = self.display_canvas.get_layout() or capture_layout(
            self.display_live
        )
        item = next(
            (candidate for candidate in layout if candidate.identity == identity),
            None,
        )
        if not item:
            return

        dialog = Adw.Window(
            transient_for=self,
            modal=True,
            title=f"Configure {display.model or display.name}",
            default_width=560,
            default_height=580,
        )
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title=display.model or display.name,
                subtitle=display.description,
            )
        )
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        header.pack_start(cancel)
        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        header.pack_end(apply_button)
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        dialog.set_content(toolbar)

        output = Adw.PreferencesGroup(
            title="Output",
            description="Changes are previewed with automatic rollback",
        )
        page.add(output)
        enabled = Adw.SwitchRow(
            title="Enabled",
            subtitle="At least one display must remain enabled",
        )
        enabled.set_active(item.enabled)
        output.add(enabled)

        mode_values = []
        for raw in display.available_modes:
            match = MODE_RE.search(raw)
            if match:
                value = (
                    int(match["width"]),
                    int(match["height"]),
                    float(match["refresh"]),
                )
                if value not in mode_values:
                    mode_values.append(value)
        current_mode = (item.width, item.height, item.refresh)
        if not mode_values:
            mode_values.append(current_mode)
        selected_mode = min(
            range(len(mode_values)),
            key=lambda index: (
                abs(mode_values[index][0] - item.width)
                + abs(mode_values[index][1] - item.height)
                + abs(mode_values[index][2] - item.refresh)
            ),
        )
        mode_row = Adw.ComboRow(
            title="Resolution and refresh rate",
            model=Gtk.StringList.new(
                [
                    f"{width} × {height} @ {refresh:.2f} Hz"
                    for width, height, refresh in mode_values
                ]
            ),
        )
        mode_row.set_selected(selected_mode)
        output.add(mode_row)
        scale = Adw.SpinRow.new_with_range(0.5, 4, 0.05)
        scale.set_title("Scale")
        scale.set_subtitle("Logical size multiplier")
        scale.set_digits(2)
        scale.set_value(item.scale)
        output.add(scale)
        rotations = ["Normal", "90°", "180°", "270°"]
        rotation_values = [0, 1, 2, 3]
        rotation = Adw.ComboRow(
            title="Rotation", model=Gtk.StringList.new(rotations)
        )
        rotation.set_selected(
            rotation_values.index(item.transform)
            if item.transform in rotation_values
            else 0
        )
        output.add(rotation)
        ten_bit = Adw.SwitchRow(
            title="10-bit output",
            subtitle="May affect screen capture compatibility",
        )
        ten_bit.set_active(item.ten_bit)
        output.add(ten_bit)

        position = Adw.PreferencesGroup(
            title="Position",
            description="Coordinates use scaled logical pixels",
        )
        page.add(position)
        x_row = Adw.SpinRow.new_with_range(-20000, 20000, 10)
        x_row.set_title("X")
        x_row.set_value(item.x)
        position.add(x_row)
        y_row = Adw.SpinRow.new_with_range(-20000, 20000, 10)
        y_row.set_title("Y")
        y_row.set_value(item.y)
        position.add(y_row)

        def apply_settings(*_args):
            if not enabled.get_active() and sum(
                candidate.enabled
                for candidate in layout
                if candidate.identity != identity
            ) == 0:
                self.toast("At least one display must remain enabled")
                return
            width, height, refresh = mode_values[mode_row.get_selected()]
            item.enabled = enabled.get_active()
            item.width = width
            item.height = height
            item.refresh = refresh
            item.scale = scale.get_value()
            item.transform = rotation_values[rotation.get_selected()]
            item.ten_bit = ten_bit.get_active()
            item.x = int(x_row.get_value())
            item.y = int(y_row.get_value())
            item.mirror_of = ""
            item.mirror_identity = ""
            dialog.close()
            self.display_mode = "extend"
            self.display_canvas.set_layout(layout, self._display_labels())
            self._apply_display_layout("extend", self.display_canvas.get_layout())

        apply_button.connect("clicked", apply_settings)
        dialog.present()

    def _build_appearance(self):
        page = self._page("Appearance", "Windows, layout, themes, fonts, and wallpaper")
        self.stack.add_named(page, "appearance")
        theme = self._group(page, "Desktop style")
        self.theme_names = self._output(["omarchy", "theme", "list"]).splitlines()
        current_theme = self._output(["omarchy", "theme", "current"]).strip()
        self.theme_combo = Adw.ComboRow(title="Theme", subtitle="Applies across Omarchy applications")
        self.theme_combo.set_model(Gtk.StringList.new(self.theme_names))
        if current_theme in self.theme_names:
            self.theme_combo.set_selected(self.theme_names.index(current_theme))
        theme.add(self.theme_combo)
        theme_button = self._button("Apply", "object-select-symbolic", self._apply_theme)
        theme_button.set_sensitive(bool(self.theme_names))
        self.theme_combo.add_suffix(theme_button)
        wallpaper = self._action_row(theme, "Wallpaper", "Move to the next background in the current theme", "preferences-desktop-wallpaper-symbolic")
        wallpaper.add_suffix(self._button("Next", "media-skip-forward-symbolic", lambda *_: self._run(["omarchy", "theme", "bg", "next"], self._command_toast("Wallpaper changed"))))

        self.font_names = self._output(["omarchy", "font", "list"]).splitlines()
        current_font = self._output(["omarchy", "font", "current"]).strip()
        self.font_combo = Adw.ComboRow(title="Monospace font", subtitle="Used throughout the terminal-focused desktop")
        self.font_combo.set_model(Gtk.StringList.new(self.font_names))
        if current_font in self.font_names:
            self.font_combo.set_selected(self.font_names.index(current_font))
        theme.add(self.font_combo)
        font_button = self._button("Apply", "object-select-symbolic", self._apply_font)
        font_button.set_sensitive(bool(self.font_names))
        self.font_combo.add_suffix(font_button)

        look = HYPR_CONTROL_CONFIG
        geometry = self._group(page, "Window geometry")
        presets = self._action_row(geometry, "Presets", "Apply a coordinated starting point", "applications-graphics-symbolic")
        preset_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, values in (
            ("Compact", (3, 5, 1, 0)),
            ("Balanced", (5, 10, 2, 8)),
            ("Spacious", (10, 18, 2, 14)),
        ):
            preset_buttons.append(self._button(label, "", lambda _button, preset=values: self._apply_appearance_preset(preset)))
        presets.add_suffix(preset_buttons)
        self._combo(geometry, "Layout", "How new windows are arranged", look, ("general",), "layout", ["dwindle", "master", "scrolling"], ["Dwindle", "Master", "Scrolling"], "dwindle")
        self.inner_gaps = self._spin(geometry, "Inner gaps", "Space between windows", look, ("general",), "gaps_in", 5, 0, 40, 1)
        self.outer_gaps = self._spin(geometry, "Outer gaps", "Space around the screen edge", look, ("general",), "gaps_out", 10, 0, 60, 1)
        self.border_width = self._spin(geometry, "Border width", "Window border thickness", look, ("general",), "border_size", 2, 0, 10, 1)
        self.corner_radius = self._spin(geometry, "Corner radius", "Rounded window corners", look, ("decoration",), "rounding", 0, 0, 30, 1)

        effects = self._group(page, "Effects")
        self._switch(effects, "Animations", "Animate windows, layers, and workspaces", look, ("animations",), "enabled", True)
        self._switch(effects, "Blur", "Blur translucent surfaces", look, ("decoration", "blur"), "enabled", True)
        self._spin(effects, "Blur size", "Radius of each blur pass", look, ("decoration", "blur"), "size", 2, 1, 20, 1)
        self._spin(effects, "Blur passes", "More passes are smoother but cost more GPU time", look, ("decoration", "blur"), "passes", 2, 1, 8, 1)
        self._switch(effects, "Window shadows", "Draw shadows behind floating surfaces", look, ("decoration", "shadow"), "enabled", True)
        self._switch(effects, "Dim inactive windows", "Reduce distraction from unfocused windows", look, ("decoration",), "dim_inactive", False)
        self._spin(effects, "Inactive dim strength", "0 is unchanged; 1 is fully dimmed", look, ("decoration",), "dim_strength", 0.15, 0, 1, 0.05, 2)

        opacity = self._group(page, "Opacity")
        self._spin(opacity, "Active windows", "Opacity of the focused window", look, ("decoration",), "active_opacity", 1, 0.2, 1, 0.05, 2)
        self._spin(opacity, "Inactive windows", "Opacity of unfocused windows", look, ("decoration",), "inactive_opacity", 1, 0.2, 1, 0.05, 2)

    def _apply_appearance_preset(self, values):
        for widget, value in zip(
            (self.inner_gaps, self.outer_gaps, self.border_width, self.corner_radius), values
        ):
            widget.set_value(value)
        self.toast("Appearance preset staged — apply to keep it")

    def _build_input(self):
        page = self._page("Keyboard & Mouse", "Keyboard layout, repeat behavior, pointer, and touchpad")
        self.stack.add_named(page, "input")
        path = HYPR_CONTROL_CONFIG
        keyboard = self._group(page, "Keyboard")
        self._entry(keyboard, "Layout codes", path, ("input",), "kb_layout", "us")
        self._entry(keyboard, "Variant", path, ("input",), "kb_variant", "")
        self._entry(keyboard, "XKB options", path, ("input",), "kb_options", "compose:caps")
        self._spin(keyboard, "Repeat rate", "Characters repeated per second", path, ("input",), "repeat_rate", 25, 1, 100, 1)
        self._spin(keyboard, "Repeat delay", "Milliseconds before repeating starts", path, ("input",), "repeat_delay", 600, 100, 2000, 25)
        self._switch(keyboard, "Num Lock on startup", "Enable the number pad by default", path, ("input",), "numlock_by_default", False)

        pointer = self._group(page, "Pointer")
        self._spin(pointer, "Pointer sensitivity", "-1 is slowest; 1 is fastest", path, ("input",), "sensitivity", 0, -1, 1, 0.05, 2)
        self._combo(pointer, "Acceleration profile", "Adaptive acceleration or direct flat movement", path, ("input",), "accel_profile", ["adaptive", "flat"], ["Adaptive", "Flat"], "adaptive")
        self._combo(pointer, "Focus follows pointer", "When moving the pointer between windows", path, ("input",), "follow_mouse", ["0", "1", "2", "3"], ["Disabled", "Focus immediately", "Loose focus", "Keyboard focus only"], "1")

        touchpad = self._group(page, "Touchpad")
        self._switch(touchpad, "Natural scrolling", "Move content in the same direction as your fingers", path, ("input", "touchpad"), "natural_scroll", False)
        self._switch(touchpad, "Clickfinger behavior", "Use finger count to choose left, middle, or right click", path, ("input", "touchpad"), "clickfinger_behavior", False)
        self._switch(touchpad, "Disable while typing", "Prevent accidental pointer movement", path, ("input", "touchpad"), "disable_while_typing", True)
        self._spin(touchpad, "Scroll speed", "Multiplier for two-finger scrolling", path, ("input", "touchpad"), "scroll_factor", 1, 0.1, 3, 0.1, 1)

    def _build_power(self):
        page = self._page("Power & Idle", "Idle automation, lock timing, screensaver, and night light")
        self.stack.add_named(page, "power")
        services = self._group(page, "Automation")
        self.idle_toggle = self._runtime_switch(services, "Idle automation", "Run screensaver and lock actions", "process-idle-symbolic", self._idle_enabled, ["omarchy", "toggle", "idle"])
        self.screensaver_toggle = self._runtime_switch(services, "Screensaver", "Allow the Omarchy screensaver to start", "preferences-desktop-screensaver-symbolic", self._screensaver_enabled, ["omarchy", "toggle", "screensaver"])
        self.suspend_toggle = self._runtime_switch(services, "Suspend in system menu", "Show suspend as a session action", "preferences-system-power-symbolic", self._suspend_enabled, ["omarchy", "toggle", "suspend"])
        self.nightlight_toggle = self._runtime_switch(services, "Night light", "Reduce blue light with a warmer display", "weather-clear-night-symbolic", self._nightlight_enabled, ["omarchy", "toggle", "nightlight"])

        idle = self._group(page, "Idle timeline", "Managed by Omarchy Shell; each point is measured from the last input")
        try:
            shell_config = json.loads(self._config_text(SHELL_CONFIG) or "{}")
        except json.JSONDecodeError:
            shell_config = {}
        idle_config = shell_config.get("idle", {})
        saver_seconds = int(idle_config.get("screensaver", 150))
        lock_seconds = int(idle_config.get("lock", 300))
        self.saver_delay = Adw.SpinRow.new_with_range(1, 120, 1)
        self.saver_delay.set_title("Start screensaver after")
        self.saver_delay.set_subtitle("Minutes with no input")
        self.saver_delay.set_value(max(1, round(saver_seconds / 60)))
        idle.add(self.saver_delay)
        self.lock_delay = Adw.SpinRow.new_with_range(0, 120, 1)
        self.lock_delay.set_title("Lock after screensaver")
        self.lock_delay.set_subtitle("Additional minutes; 0 locks immediately")
        self.lock_delay.set_value(max(0, round((lock_seconds - saver_seconds) / 60)))
        idle.add(self.lock_delay)
        for widget in (self.saver_delay, self.lock_delay):
            widget.connect("notify::value", self._mark_idle_dirty)
        self.idle_timeline = self._action_row(idle, "Current sequence", "", "appointment-soon-symbolic")
        self.idle_timeline.set_sensitive(False)
        self.idle_timeline.add_css_class("timeline-row")
        self._idle_original = self._idle_state()
        self._update_idle_timeline()

        night = self._group(page, "Night light temperature")
        self.night_temp = Adw.SpinRow.new_with_range(2500, 6000, 100)
        self.night_temp.set_title("Warm temperature")
        self.night_temp.set_subtitle("Lower values appear warmer")
        self.night_temp.set_value(4000)
        night.add(self.night_temp)
        temp_row = self._action_row(night, "Apply temperature now", "Starts hyprsunset if necessary", "weather-clear-night-symbolic")
        temp_row.add_suffix(self._button("Apply", "object-select-symbolic", self._set_night_temperature))

    def _runtime_switch(self, group, title, subtitle, icon, getter, command):
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        row.set_active(getter())
        row._command = command
        row.connect("notify::active", self._toggle_runtime)
        group.add(row)
        return row

    def _toggle_runtime(self, row, _param):
        if self.loading or getattr(row, "_refreshing", False):
            return
        row.set_sensitive(False)
        self._run(row._command, lambda ok, out: self._runtime_toggled(row, ok, out))

    def _runtime_toggled(self, row, ok, output):
        row.set_sensitive(True)
        if not ok:
            row._refreshing = True
            row.set_active(not row.get_active())
            row._refreshing = False
            self.toast(output.strip() or "Could not change setting")

    def _mark_idle_dirty(self, *_args):
        if self.loading:
            return
        self._idle_dirty = self._idle_state() != self._idle_original
        self._update_idle_timeline()
        self._update_pending_bar()

    def _idle_state(self):
        return (
            self.saver_delay.get_value(),
            self.lock_delay.get_value(),
        )

    def _update_idle_timeline(self):
        saver = int(self.saver_delay.get_value())
        lock = saver + int(self.lock_delay.get_value())
        stages = [f"{saver} min  Screensaver", f"{lock} min  Lock"]
        self.idle_timeline.set_subtitle("   →   ".join(stages))

    def _build_shortcuts(self):
        page = self._page("Shortcuts", "Personal shortcuts layered over Omarchy defaults")
        self.stack.add_named(page, "shortcuts")
        actions = self._group(page, "Manage")
        add = self._action_row(actions, "Add shortcut", "Create a named command binding", "list-add-symbolic")
        add.add_suffix(self._button("Add", "list-add-symbolic", self._show_add_shortcut, "suggested-action"))
        note = self._action_row(actions, "Default shortcuts", "Omarchy defaults remain protected; this page edits only Hypr Control shortcuts", "dialog-information-symbolic")
        note.set_sensitive(False)
        self.shortcut_group = self._group(page, "Your shortcuts")
        self.shortcut_rows = []
        self._populate_shortcuts()

    def _populate_shortcuts(self):
        for row in self.shortcut_rows:
            self.shortcut_group.remove(row)
        self.shortcut_rows.clear()
        text = self._config_text(BINDINGS_STORE)
        bindings = [item for item in parse_bindings(text) if item["line"] not in self.shortcut_removals]
        for line in self.shortcut_additions:
            bindings.extend(parse_bindings(line))
        if not bindings:
            row = Adw.ActionRow(title="No personal shortcuts")
            self.shortcut_group.add(row)
            self.shortcut_rows.append(row)
            return
        for item in bindings:
            keycap = Gtk.Label(label=f"{item['mods'].replace(' ', ' + ')} + {item['key']}")
            keycap.add_css_class("keycap")
            row = Adw.ActionRow(title=str(item["description"]), subtitle=f"{item['dispatcher']} {item['argument']}".strip())
            row.add_prefix(keycap)
            remove = Gtk.Button(icon_name="user-trash-symbolic")
            remove.set_tooltip_text("Remove shortcut")
            remove.add_css_class("flat")
            remove.connect("clicked", self._remove_shortcut, str(item["line"]))
            row.add_suffix(remove)
            self.shortcut_group.add(row)
            self.shortcut_rows.append(row)

    def _show_add_shortcut(self, *_args):
        dialog = Adw.Window(transient_for=self, modal=True, title="Add shortcut")
        dialog.set_default_size(520, 410)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Add shortcut", subtitle="Bind a command to a key combination"))
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        header.pack_start(cancel)
        add = Gtk.Button(label="Add")
        add.add_css_class("suggested-action")
        header.pack_end(add)
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Shortcut")
        page.add(group)
        description = Adw.EntryRow(title="Name")
        description.set_text("New shortcut")
        modifiers = Adw.EntryRow(title="Modifiers")
        modifiers.set_text("SUPER SHIFT")
        key = Adw.EntryRow(title="Key")
        capture = Adw.ActionRow(title="Capture shortcut", subtitle="Press the button, then type the key combination")
        capture_button = self._button("Record", "media-record-symbolic", lambda *_: None)
        capture.add_suffix(capture_button)
        command = Adw.EntryRow(title="Command")
        group.add(description)
        group.add(capture)
        group.add(modifiers)
        group.add(key)
        group.add(command)
        toolbar.set_content(page)
        dialog.set_content(toolbar)

        capture_state = {"active": False}

        def begin_capture(*_args):
            capture_state["active"] = True
            capture_button.set_label("Press keys…")
            capture.add_css_class("pending-setting")

        def captured(_controller, keyval, _keycode, state):
            if not capture_state["active"]:
                return False
            if keyval == Gdk.KEY_Escape:
                capture_state["active"] = False
                capture_button.set_label("Record")
                capture.remove_css_class("pending-setting")
                return True
            if keyval in {
                Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
                Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
            }:
                return True
            masks = (
                (Gdk.ModifierType.SUPER_MASK, "SUPER"),
                (Gdk.ModifierType.CONTROL_MASK, "CTRL"),
                (Gdk.ModifierType.ALT_MASK, "ALT"),
                (Gdk.ModifierType.SHIFT_MASK, "SHIFT"),
            )
            modifiers.set_text(" ".join(name for mask, name in masks if state & mask))
            key.set_text(Gdk.keyval_name(keyval) or "")
            capture_state["active"] = False
            capture_button.set_label("Record again")
            capture.remove_css_class("pending-setting")
            return True

        capture_button.connect("clicked", begin_capture)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", captured)
        dialog.add_controller(key_controller)

        def submit(*_):
            if not key.get_text().strip() or not command.get_text().strip():
                self.toast("Key and command are required")
                return
            mods = modifiers.get_text().strip().upper()
            pressed = key.get_text().strip().upper()
            conflicts = parse_bindings(self._config_text(BINDINGS_STORE))
            chord = shortcut_chord(mods, pressed)
            if any(shortcut_chord(str(item["mods"]), str(item["key"])) == chord for item in conflicts):
                self.toast("That combination already exists in your personal shortcuts")
                return
            line = f"bindd = {mods}, {pressed}, {description.get_text().strip()}, exec, {command.get_text().strip()}"
            modifier_bits = {"SHIFT": 1, "CTRL": 4, "CONTROL": 4, "ALT": 8, "SUPER": 64}
            modmask = sum(modifier_bits.get(part, 0) for part in mods.split())
            try:
                live = json.loads(self._output(["hyprctl", "-j", "binds"]) or "[]")
            except json.JSONDecodeError:
                live = []
            conflict = next(
                (
                    item
                    for item in live
                    if int(item.get("modmask", -1)) == modmask
                    and str(item.get("key", "")).upper() == pressed
                ),
                None,
            )

            def finish(replace=False):
                if replace:
                    self.shortcut_additions.append(f"unbind = {mods}, {pressed}")
                self.shortcut_additions.append(line)
                self._populate_shortcuts()
                self._update_pending_bar()
                dialog.close()

            if conflict:
                previous = conflict.get("description") or conflict.get("dispatcher") or "another action"
                confirm = Adw.AlertDialog(
                    heading="Replace existing shortcut?",
                    body=f"{mods.replace(' ', ' + ')} + {pressed} currently runs “{previous}”. Hypr Control will add an explicit unbind before your new shortcut.",
                )
                confirm.add_response("cancel", "Cancel")
                confirm.add_response("replace", "Replace")
                confirm.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
                confirm.connect("response", lambda _alert, response: finish(True) if response == "replace" else None)
                confirm.present(dialog)
            else:
                finish()

        add.connect("clicked", submit)
        dialog.present()

    def _remove_shortcut(self, _button, line):
        if line in self.shortcut_additions:
            index = self.shortcut_additions.index(line)
            self.shortcut_additions.pop(index)
            if index > 0 and self.shortcut_additions[index - 1].lstrip().startswith("unbind ="):
                self.shortcut_additions.pop(index - 1)
        else:
            self.shortcut_removals.add(line)
            parsed = parse_bindings(line)
            store_lines = self._config_text(BINDINGS_STORE).splitlines()
            if parsed and line in store_lines:
                index = store_lines.index(line)
                if index > 0 and store_lines[index - 1].lstrip().startswith("unbind ="):
                    unbind = store_lines[index - 1]
                    payload = unbind.split("=", 1)[1]
                    parts = [part.strip() for part in payload.split(",", 1)]
                    item = parsed[0]
                    if len(parts) == 2 and shortcut_chord(parts[0], parts[1]) == shortcut_chord(
                        str(item["mods"]), str(item["key"])
                    ):
                        self.shortcut_removals.add(unbind)
        self._populate_shortcuts()
        self._update_pending_bar()

    def _build_advanced(self):
        self._building_page = "advanced"
        self._add_page_search_terms("advanced", "raw configuration editor backups restore diff files")
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_start(24)
        page.set_margin_end(24)
        self.stack.add_named(page, "advanced")
        heading = Gtk.Label(label="Raw configuration", xalign=0)
        heading.add_css_class("title-2")
        page.append(heading)
        description = Gtk.Label(label="Edit any user-owned Hyprland Lua or legacy config file. A backup is created before every save.", xalign=0)
        description.add_css_class("dim-label")
        page.append(description)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.advanced_files = sorted(
            path.name
            for pattern in ("*.lua", "*.conf")
            for path in HYPR.glob(pattern)
            if ".bak." not in path.name and ".omarchy-upgrade-" not in path.name
        )
        if not self.advanced_files:
            self.advanced_files = ["hyprland.conf"]
        if self.advanced_current not in self.advanced_files:
            self.advanced_current = self.advanced_files[0]
        self.advanced_combo = Gtk.DropDown(model=Gtk.StringList.new(self.advanced_files))
        self.advanced_combo.set_hexpand(True)
        if self.advanced_current in self.advanced_files:
            self.advanced_combo.set_selected(self.advanced_files.index(self.advanced_current))
        self.advanced_combo.connect("notify::selected", self._advanced_selected)
        controls.append(self.advanced_combo)
        discard = self._button("Discard edits", "edit-undo-symbolic", self._advanced_discard)
        controls.append(discard)
        history = self._button("Backups", "document-revert-symbolic", self._show_backups)
        controls.append(history)
        self.advanced_save = self._button("Save file", "document-save-symbolic", self._save_advanced, "suggested-action")
        self.advanced_save.set_sensitive(False)
        controls.append(self.advanced_save)
        page.append(controls)

        frame = Gtk.Frame()
        frame.set_vexpand(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.editor = Gtk.TextView(monospace=True, top_margin=12, bottom_margin=12, left_margin=14, right_margin=14)
        self.editor.add_css_class("config-editor")
        self.editor.get_buffer().connect("changed", self._advanced_changed)
        scroll.set_child(self.editor)
        frame.set_child(scroll)
        page.append(frame)
        self._load_advanced(self.advanced_current)

    def _load_advanced(self, name):
        self._advanced_switching = True
        self.advanced_current = name
        self.editor.get_buffer().set_text(self._config_text(HYPR / name))
        self.advanced_snapshot = self._owned_text(HYPR / name)
        self.advanced_dirty = False
        self.advanced_save.set_sensitive(False)
        self._advanced_switching = False

    def _advanced_changed(self, *_args):
        if self._advanced_switching:
            return
        self.advanced_dirty = True
        self.advanced_save.set_sensitive(True)

    def _advanced_selected(self, dropdown, _param):
        if self._advanced_switching:
            return
        selected = self.advanced_files[dropdown.get_selected()]
        if self.advanced_dirty:
            self._advanced_switching = True
            dropdown.set_selected(self.advanced_files.index(self.advanced_current))
            self._advanced_switching = False
            self.toast("Save or discard the current file before switching")
            return
        self._load_advanced(selected)

    def _advanced_discard(self, *_args):
        self._load_advanced(self.advanced_current)
        self.toast("Unsaved edits discarded")

    def _save_advanced(self, *_args):
        buffer = self.editor.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        path = HYPR / self.advanced_current
        current = self._owned_text(path)
        snapshot = getattr(self, "advanced_snapshot", current)
        if current != snapshot:
            self._show_error("File changed outside Hypr Control", "Reload the file before applying your edits so another program's changes are not overwritten.")
            return
        diff = "\n".join(
            difflib.unified_diff(
                (current or "").splitlines(), text.splitlines(),
                fromfile=f"{self.advanced_current} (current)", tofile=f"{self.advanced_current} (proposed)", lineterm="",
            )
        )
        preview = diff if len(diff) <= 5000 else diff[:5000] + "\n… diff shortened …"
        dialog = Adw.AlertDialog(heading=f"Apply changes to {self.advanced_current}?", body=preview or "No textual changes")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _dialog, response: self._commit_advanced(path, text) if response == "apply" else None)
        dialog.present(self)

    def _commit_advanced(self, path, text):
        self.advanced_save.set_sensitive(False)
        self._apply_transaction({path: text}, lambda ok, msg: self._advanced_saved(ok, msg))

    def _advanced_saved(self, ok, message):
        if ok:
            self.advanced_dirty = False
            self.advanced_snapshot = self._owned_text(HYPR / self.advanced_current)
            self.file_snapshots[HYPR / self.advanced_current] = self.advanced_snapshot
            self.toast(f"Saved {self.advanced_current}")
        else:
            self._load_advanced(self.advanced_current)
            self._show_error("The change was reverted", message)

    def _show_backups(self, *_args):
        path = HYPR / self.advanced_current
        history = backup_history(path)
        if not history:
            self.toast(f"No backups found for {self.advanced_current}")
            return
        dialog = Adw.Window(transient_for=self, modal=True, title="Configuration backups")
        dialog.set_default_size(620, 480)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Backups", subtitle=self.advanced_current))
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Newest first", description="Restoring also validates and can be rolled back")
        page.add(group)
        for backup in history:
            stamp = backup.name.rsplit(".bak.hypr-control-", 1)[-1]
            row = Adw.ActionRow(title=stamp, subtitle=f"{backup.stat().st_size:,} bytes")
            button = self._button("Restore", "document-revert-symbolic", lambda _button, item=backup: self._restore_backup(path, item, dialog))
            row.add_suffix(button)
            group.add(row)
        toolbar.set_content(page)
        dialog.set_content(toolbar)
        dialog.present()

    def _restore_backup(self, path, backup, dialog):
        try:
            text = backup.read_text(encoding="utf-8")
        except OSError as error:
            self._show_error("Could not read backup", str(error))
            return
        dialog.close()
        self._apply_transaction({path: text}, lambda ok, msg: self._backup_restored(path, ok, msg))

    def _backup_restored(self, path, ok, message):
        if ok:
            self._load_advanced(path.name)
            self.file_snapshots[path] = self._owned_text(path)
            self.toast(f"Restored {path.name}")
        else:
            self._show_error("The restore was reverted", message)

    def _save_pending(self, *_args):
        changed: dict[Path, str] = {}
        for index in sorted(self.dirty_bindings):
            binding = self.setting_bindings[index]
            if binding.path not in changed:
                changed[binding.path] = self._config_text(binding.path)
            if binding.path.suffix == ".lua":
                changed[binding.path] = set_lua_value(
                    changed[binding.path],
                    binding.block,
                    binding.key,
                    lua_scalar(binding.serialize()),
                )
            else:
                changed[binding.path] = set_value(
                    changed[binding.path], binding.block, binding.key, binding.serialize()
                )

        if getattr(self, "_idle_dirty", False):
            try:
                shell_config = json.loads(self._config_text(SHELL_CONFIG) or "{}")
            except json.JSONDecodeError:
                self._show_error("Omarchy Shell configuration is invalid", "Fix shell.json before changing idle timing.")
                return
            idle_config = shell_config.setdefault("idle", {})
            saver_seconds = round(self.saver_delay.get_value() * 60)
            idle_config["screensaver"] = saver_seconds
            idle_config["lock"] = saver_seconds + round(self.lock_delay.get_value() * 60)
            changed[SHELL_CONFIG] = json.dumps(shell_config, indent=2) + "\n"

        if self.shortcut_additions or self.shortcut_removals:
            lines = [line for line in self._config_text(BINDINGS_STORE).splitlines() if line not in self.shortcut_removals]
            if self.shortcut_additions:
                lines.extend(["", "# Added by Hypr Control", *self.shortcut_additions])
            store_text = "\n".join(lines).rstrip() + "\n"
            changed[BINDINGS_STORE] = store_text
            changed[BINDINGS_CONFIG] = render_lua_bindings(store_text)

        if not changed:
            return
        conflicts = self._externally_changed(changed)
        if conflicts:
            names = ", ".join(path.name for path in conflicts)
            self._show_error("Configuration changed outside Hypr Control", f"Reload Hypr Control before applying so changes to {names} are not overwritten.")
            return
        self.saving = True
        self._update_pending_bar()
        self._apply_transaction(changed, self._settings_saved)

    def _settings_saved(self, ok, message):
        self.saving = False
        if ok:
            for binding in self.setting_bindings:
                binding.original = binding.serialize()
                binding.widget.remove_css_class("pending-setting")
            self.dirty_bindings.clear()
            self.shortcut_additions.clear()
            self.shortcut_removals.clear()
            self._idle_dirty = False
            self._idle_original = self._idle_state()
            self._populate_shortcuts()
            self._capture_file_snapshots()
            self.recent_change = "Settings applied successfully"
            if hasattr(self, "recent_change_row"):
                self.recent_change_row.set_subtitle(self.recent_change)
            self._update_pending_bar()
            self.toast("Settings applied safely")
            self.refresh_runtime()
        else:
            self._update_pending_bar()
            self._show_error("The changes were reverted", message)

    def _validate_changes(self, names: set[str]):
        hypr_files = names - {"shell.json", "bindings.conf", "hyprsunset.conf", "hyprlock.conf"}
        ok, message = True, ""
        if hypr_files:
            reload_result = subprocess.run(["hyprctl", "reload"], capture_output=True, text=True, timeout=15)
            errors = subprocess.run(["hyprctl", "configerrors"], capture_output=True, text=True, timeout=15)
            message = (reload_result.stderr + errors.stdout + errors.stderr).strip()
            ok = reload_result.returncode == 0 and errors.returncode == 0 and not errors.stdout.strip()
        if ok and "hyprsunset.conf" in names:
            result = subprocess.run(["omarchy", "restart", "hyprsunset"], capture_output=True, text=True, timeout=30)
            ok, message = result.returncode == 0, (result.stdout + result.stderr).strip()
        return ok, message

    def _apply_transaction(self, changes: dict[Path, str], callback):
        def work():
            names = {path.name for path in changes}
            hypr_files = names - {"shell.json", "bindings.conf", "hyprsunset.conf", "hyprlock.conf"}
            try:
                ok, message, _backups = transactional_write(changes, lambda: self._validate_changes(names))
            except (OSError, subprocess.TimeoutExpired) as error:
                ok, message = False, str(error)
            if not ok:
                if hypr_files:
                    subprocess.run(["hyprctl", "reload"], capture_output=True)
                if "hyprsunset.conf" in names:
                    subprocess.run(["omarchy", "restart", "hyprsunset"], capture_output=True)
            GLib.idle_add(callback, ok, message or "Hyprland rejected the configuration")

        threading.Thread(target=work, daemon=True).start()

    def _apply_theme(self, *_args):
        if not self.theme_names:
            return
        theme = self.theme_names[self.theme_combo.get_selected()]
        self._run(["omarchy", "theme", "set", theme], self._command_toast(f"Theme changed to {theme}"))

    def _apply_font(self, *_args):
        if not self.font_names:
            return
        font = self.font_names[self.font_combo.get_selected()]
        self._run(["omarchy", "font", "set", font], self._command_toast(f"Font changed to {font}"))

    def _set_night_temperature(self, *_args):
        temperature = str(int(self.night_temp.get_value()))
        def work():
            if not self._nightlight_process():
                self._launch(["uwsm-app", "--", "hyprsunset"])
                time.sleep(1)
            result = subprocess.run(
                ["hyprctl", "hyprsunset", "temperature", temperature],
                capture_output=True,
                text=True,
            )
            output = (result.stdout + result.stderr).strip()
            GLib.idle_add(
                self._command_toast(f"Night light set to {temperature} K"),
                result.returncode == 0,
                output,
            )
        threading.Thread(target=work, daemon=True).start()

    def _idle_enabled(self):
        try:
            status = json.loads(self._output(["omarchy", "toggle", "idle", "--status"]) or "{}")
        except json.JSONDecodeError:
            return True
        return not bool(status.get("enabled", False))

    def _nightlight_process(self):
        return subprocess.run(["pgrep", "-x", "hyprsunset"], capture_output=True).returncode == 0

    def _nightlight_enabled(self):
        if not self._nightlight_process():
            return False
        output = self._output(["hyprctl", "hyprsunset", "temperature"])
        match = re.search(r"\d+", output)
        return bool(match and int(match.group()) < 5500)

    @staticmethod
    def _screensaver_enabled():
        return not (Path.home() / ".local/state/omarchy/toggles/screensaver-off").exists()

    @staticmethod
    def _suspend_enabled():
        return not (Path.home() / ".local/state/omarchy/toggles/suspend-off").exists()

    def refresh_runtime(self):
        def work():
            version = self._output(["hyprctl", "version"]).splitlines()
            errors = self._output(["hyprctl", "configerrors"]).strip()
            raw = self._output(["hyprctl", "-j", "monitors", "all"])
            devices_raw = self._output(["hyprctl", "-j", "devices"])
            theme = self._output(["omarchy", "theme", "current"])
            font = self._output(["omarchy", "font", "current"])
            try:
                monitors = json.loads(raw)
            except json.JSONDecodeError:
                monitors = []
            try:
                devices = json.loads(devices_raw)
            except json.JSONDecodeError:
                devices = {}
            GLib.idle_add(self._render_runtime, version, errors, monitors, devices, theme, font)

        threading.Thread(target=work, daemon=True).start()

    def _render_runtime(self, version, errors, monitors, devices, theme, font):
        label = version[0] if version else "Compositor unavailable"
        self.status_compositor.set_subtitle(label)
        self.status_config.set_subtitle(errors or "No configuration errors")
        active = next((m for m in monitors if m.get("focused")), monitors[0] if monitors else None)
        if active:
            self.status_display.set_subtitle(f"{active['name']} · {active['width']}×{active['height']} @ {active['refreshRate']:.0f} Hz · {active['scale']:.2g}×")
        else:
            self.status_display.set_subtitle("No live output data")
        self.status_style.set_subtitle(" · ".join(value for value in (theme, font) if value) or "Omarchy style unavailable")
        device_count = sum(len(devices.get(kind, [])) for kind in ("keyboards", "mice", "touchpads", "tablets"))
        self.status_input.set_subtitle(f"{device_count} connected device{'s' if device_count != 1 else ''}")

        try:
            self.display_live = discover_displays(monitors)
        except (OSError, ValueError, json.JSONDecodeError):
            self.display_live = []
        self.display_store.load()
        profile = self.display_store.match(self.display_live)
        if self.display_live and not self._display_layout_dirty and not self._display_preview:
            self.file_snapshots[MONITORS_CONFIG] = self._owned_text(
                MONITORS_CONFIG
            )
            if profile:
                resolved = profile.resolved_layout(self.display_live)
                self.display_mode = profile.default_mode
                if self.display_mode == "extend":
                    canvas_layout = layout_for_mode(
                        self.display_live, "extend", resolved
                    )
                else:
                    canvas_layout = resolved or capture_layout(self.display_live)
            else:
                canvas_layout = capture_layout(self.display_live)
                self.display_mode = "extend"
            self.display_canvas.set_layout(canvas_layout, self._display_labels())
        self.display_remember_button.set_sensitive(bool(self.display_live))
        if profile:
            self.display_profile_row.set_subtitle(
                f"{profile.name} · {profile.default_mode.replace('_', ' ').title()}"
            )
            self.loading = True
            self.display_auto_switch.set_sensitive(True)
            self.display_auto_switch.set_active(profile.auto_apply)
            self.loading = False
        elif self.display_live:
            self.display_profile_row.set_subtitle(
                f"New setup · {profile_name(self.display_live)}"
            )
            self.loading = True
            self.display_auto_switch.set_active(False)
            self.display_auto_switch.set_sensitive(False)
            self.loading = False
        else:
            self.display_profile_row.set_subtitle("No compositor data")
            self.display_auto_switch.set_sensitive(False)

        for row in self.display_rows:
            self.display_group.remove(row)
        self.display_rows.clear()
        if not monitors:
            row = Adw.ActionRow(title="No outputs reported", subtitle="Hyprland may not be available")
            self.display_group.add(row)
            self.display_rows.append(row)
        for display in self.display_live:
            title = display.description
            subtitle = f"{display.name} · {display.width}×{display.height} @ {display.refresh:.2f} Hz · scale {display.scale:.2g}×"
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image.new_from_icon_name("video-display-symbolic"))
            badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if display.focused:
                badge = Gtk.Label(label="Active")
                badge.add_css_class("status-badge")
                badges.append(badge)
            state = Gtk.Label(label="Internal" if display.internal else "External")
            state.add_css_class("status-badge")
            badges.append(state)
            configure = Gtk.Button(
                icon_name="emblem-system-symbolic",
                tooltip_text=f"Configure {display.name}",
                valign=Gtk.Align.CENTER,
            )
            configure.connect(
                "clicked", self._show_display_settings, display.identity
            )
            badges.append(configure)
            row.add_suffix(badges)
            self.display_group.add(row)
            self.display_rows.append(row)

        runtime = [
            (self.idle_toggle, self._idle_enabled()),
            (self.screensaver_toggle, self._screensaver_enabled()),
            (self.suspend_toggle, self._suspend_enabled()),
            (self.nightlight_toggle, self._nightlight_enabled()),
        ]
        for row, value in runtime:
            row._refreshing = True
            row.set_active(value)
            row._refreshing = False

    @staticmethod
    def _output(args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=8).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def _run(self, args, callback):
        def work():
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=45)
                output = (result.stdout + result.stderr).strip()
                GLib.idle_add(callback, result.returncode == 0, output)
            except (FileNotFoundError, subprocess.TimeoutExpired) as error:
                GLib.idle_add(callback, False, str(error))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _launch(args):
        try:
            subprocess.Popen(args, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass

    def _command_toast(self, success_message):
        def callback(ok, output):
            self.toast(success_message if ok else (output or "Command failed"))
            if ok:
                self.refresh_runtime()
        return callback

    def _show_error(self, title, detail):
        dialog = Adw.AlertDialog(heading=title, body=detail)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.present(self)


class HyprControlApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("activate", self._activate)
        self.connect("startup", self._startup)

    def _startup(self, *_args):
        provider = Gtk.CssProvider()
        provider.load_from_path(str(ROOT / "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _activate(self, *_args):
        window = self.props.active_window
        if not window:
            window = HyprControlWindow(self)
        window.present()


def main():
    app = HyprControlApplication()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
