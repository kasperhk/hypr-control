from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .config_engine import (
    atomic_write,
    get_value,
    listener_timeout,
    parse_bindings,
    remove_listener,
    restore_backup,
    set_listener_timeout,
    set_value,
)


APP_ID = "io.github.kasperhk.HyprControl"
ROOT = Path(__file__).resolve().parent
XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser()
HYPR = XDG_CONFIG_HOME / "hypr"
DEFAULT_HYPR = XDG_DATA_HOME / "omarchy/default/hypr"


@dataclass
class Binding:
    path: Path
    block: tuple[str, ...]
    key: str
    widget: Gtk.Widget
    kind: str
    values: list[str] | None = None

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
        self.set_size_request(760, 560)

        self.loading = True
        self.setting_bindings: list[Binding] = []
        self.dirty_bindings: set[int] = set()
        self.shortcut_additions: list[str] = []
        self.shortcut_removals: set[str] = set()
        self.advanced_dirty = False
        self.advanced_current = "hyprland.conf"
        self._advanced_switching = False

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
        self.apply_button = Gtk.Button(icon_name="document-save-symbolic")
        self.apply_button.set_tooltip_text("Apply pending changes")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.set_sensitive(False)
        self.apply_button.connect("clicked", self._save_pending)
        header.pack_end(self.apply_button)
        toolbar.add_top_bar(header)

        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_sidebar_width_fraction(0.24)
        self.split_view.set_min_sidebar_width(220)
        self.split_view.set_max_sidebar_width(280)
        self.split_view.set_show_sidebar(True)
        toolbar.set_content(self.split_view)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.set_size_request(220, -1)
        sidebar.add_css_class("sidebar")
        search = Gtk.SearchEntry(placeholder_text="Find settings")
        search.set_margin_top(16)
        search.set_margin_start(16)
        search.set_margin_end(16)
        search.connect("search-changed", self._filter_pages)
        sidebar.append(search)
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

        self._build_home()
        self._build_displays()
        self._build_appearance()
        self._build_input()
        self._build_power()
        self._build_shortcuts()
        self._build_advanced()
        self.sidebar_list.select_row(self.sidebar_list.get_row_at_index(0))
        self.loading = False
        self.refresh_runtime()

    def toast(self, message: str, timeout: int = 3):
        toast = Adw.Toast(title=message)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)

    def _page(self, title: str, description: str) -> Adw.PreferencesPage:
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
        page.add(group)
        return group

    def _action_row(self, group, title, subtitle="", icon=None):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        if icon:
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        group.add(row)
        return row

    def _page_filter(self, row):
        return not self.page_query or self.page_query in row.search_text

    def _filter_pages(self, entry):
        self.page_query = entry.get_text().strip().lower()
        self.sidebar_list.invalidate_filter()

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

    def _resolved(self, path: Path, block: tuple[str, ...], key: str, fallback: str) -> str:
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
        self.dirty_bindings.add(index)
        self.apply_button.set_sensitive(True)

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

        actions = self._group(page, "Quick actions")
        display = self._action_row(actions, "Display layout", "Arrange monitors, choose modes, and set scaling", "video-display-symbolic")
        display_button = self._button("Open", "go-next-symbolic", lambda *_: self._launch(["nwg-displays"]))
        display_button.set_sensitive(shutil.which("nwg-displays") is not None)
        display.add_suffix(display_button)
        lock = self._action_row(actions, "Lock now", "Secure the session immediately", "system-lock-screen-symbolic")
        lock.add_suffix(self._button("Lock", "system-lock-screen-symbolic", lambda *_: self._launch(["omarchy", "system", "lock"])))
        reload_row = self._action_row(actions, "Reload Hyprland", "Re-read configuration without ending the session", "view-refresh-symbolic")
        reload_row.add_suffix(self._button("Reload", "view-refresh-symbolic", lambda *_: self._run(["hyprctl", "reload"], self._command_toast("Hyprland reloaded"))))

    def _build_displays(self):
        page = self._page("Displays", "Live output details with Omarchy's visual layout editor")
        self.stack.add_named(page, "displays")
        self.display_group = self._group(page, "Connected displays", "Values come directly from the running compositor")
        self.display_rows = []
        tools = self._group(page, "Layout")
        row = self._action_row(tools, "Arrange displays", "Drag outputs, select resolutions, refresh rates, transforms, and scaling", "video-display-symbolic")
        display_button = self._button("Open layout editor", "external-link-symbolic", lambda *_: self._launch(["nwg-displays"]), "suggested-action")
        display_button.set_sensitive(shutil.which("nwg-displays") is not None)
        row.add_suffix(display_button)
        info = self._action_row(tools, "Configuration ownership", "nwg-displays safely maintains monitors.conf and workspace assignments", "dialog-information-symbolic")
        info.set_sensitive(False)

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

        look = HYPR / "looknfeel.conf"
        geometry = self._group(page, "Window geometry")
        self._combo(geometry, "Layout", "How new windows are arranged", look, ("general",), "layout", ["dwindle", "master", "scrolling"], ["Dwindle", "Master", "Scrolling"], "dwindle")
        self._spin(geometry, "Inner gaps", "Space between windows", look, ("general",), "gaps_in", 5, 0, 40, 1)
        self._spin(geometry, "Outer gaps", "Space around the screen edge", look, ("general",), "gaps_out", 10, 0, 60, 1)
        self._spin(geometry, "Border width", "Window border thickness", look, ("general",), "border_size", 2, 0, 10, 1)
        self._spin(geometry, "Corner radius", "Rounded window corners", look, ("decoration",), "rounding", 0, 0, 30, 1)

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

    def _build_input(self):
        page = self._page("Keyboard & Mouse", "Keyboard layout, repeat behavior, pointer, and touchpad")
        self.stack.add_named(page, "input")
        path = HYPR / "input.conf"
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

        idle = self._group(page, "Idle timing", "Timers restart after each action, so lock delay is measured after the screensaver starts")
        idle_text = self._config_text(HYPR / "hypridle.conf")
        saver_seconds = listener_timeout(idle_text, "omarchy-launch-screensaver") or 120
        lock_seconds = listener_timeout(idle_text, "omarchy-system-lock") or 122
        suspend_seconds = listener_timeout(idle_text, "systemctl suspend")
        self.saver_delay = Adw.SpinRow.new_with_range(1, 120, 1)
        self.saver_delay.set_title("Start screensaver after")
        self.saver_delay.set_subtitle("Minutes with no input")
        self.saver_delay.set_value(max(1, round(saver_seconds / 60)))
        idle.add(self.saver_delay)
        self.lock_delay = Adw.SpinRow.new_with_range(1, 120, 1)
        self.lock_delay.set_title("Lock after screensaver")
        self.lock_delay.set_subtitle("Additional idle minutes")
        self.lock_delay.set_value(max(1, round(lock_seconds / 60)))
        idle.add(self.lock_delay)
        self.suspend_idle = Adw.SwitchRow(title="Suspend automatically", subtitle="Put the computer to sleep after extended inactivity")
        self.suspend_idle.set_active(suspend_seconds is not None)
        idle.add(self.suspend_idle)
        self.suspend_delay = Adw.SpinRow.new_with_range(5, 240, 5)
        self.suspend_delay.set_title("Suspend after")
        self.suspend_delay.set_subtitle("Total minutes with no input")
        self.suspend_delay.set_value(max(5, round((suspend_seconds or 1800) / 60)))
        self.suspend_delay.set_sensitive(self.suspend_idle.get_active())
        idle.add(self.suspend_delay)
        for widget in (self.saver_delay, self.lock_delay, self.suspend_idle, self.suspend_delay):
            signal = "notify::active" if isinstance(widget, Adw.SwitchRow) else "notify::value"
            widget.connect(signal, self._mark_idle_dirty)
        self.suspend_idle.connect("notify::active", lambda row, *_: self.suspend_delay.set_sensitive(row.get_active()))

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
        self.apply_button.set_sensitive(True)
        self._idle_dirty = True

    def _build_shortcuts(self):
        page = self._page("Shortcuts", "Personal shortcuts layered over Omarchy defaults")
        self.stack.add_named(page, "shortcuts")
        actions = self._group(page, "Manage")
        add = self._action_row(actions, "Add shortcut", "Create a named command binding", "list-add-symbolic")
        add.add_suffix(self._button("Add", "list-add-symbolic", self._show_add_shortcut, "suggested-action"))
        note = self._action_row(actions, "Default shortcuts", "Omarchy defaults remain protected; this page edits only bindings.conf", "dialog-information-symbolic")
        note.set_sensitive(False)
        self.shortcut_group = self._group(page, "Your shortcuts")
        self.shortcut_rows = []
        self._populate_shortcuts()

    def _populate_shortcuts(self):
        for row in self.shortcut_rows:
            self.shortcut_group.remove(row)
        self.shortcut_rows.clear()
        text = self._config_text(HYPR / "bindings.conf")
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
        command = Adw.EntryRow(title="Command")
        group.add(description)
        group.add(modifiers)
        group.add(key)
        group.add(command)
        toolbar.set_content(page)
        dialog.set_content(toolbar)

        def submit(*_):
            if not key.get_text().strip() or not command.get_text().strip():
                self.toast("Key and command are required")
                return
            mods = modifiers.get_text().strip().upper()
            pressed = key.get_text().strip().upper()
            conflicts = parse_bindings(self._config_text(HYPR / "bindings.conf"))
            if any(item["mods"].upper() == mods and item["key"].upper() == pressed for item in conflicts):
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
                self.apply_button.set_sensitive(True)
                self._populate_shortcuts()
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
        self.apply_button.set_sensitive(True)
        self._populate_shortcuts()

    def _build_advanced(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_start(24)
        page.set_margin_end(24)
        self.stack.add_named(page, "advanced")
        heading = Gtk.Label(label="Raw configuration", xalign=0)
        heading.add_css_class("title-2")
        page.append(heading)
        description = Gtk.Label(label="Edit any user-owned Hyprland file. A backup is created before every save.", xalign=0)
        description.add_css_class("dim-label")
        page.append(description)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.advanced_files = sorted(path.name for path in HYPR.glob("*.conf") if ".bak." not in path.name)
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
        backup = atomic_write(path, text)
        self.advanced_save.set_sensitive(False)
        self._validate_and_restart({path: backup}, lambda ok, msg: self._advanced_saved(ok, msg))

    def _advanced_saved(self, ok, message):
        if ok:
            self.advanced_dirty = False
            self.toast(f"Saved {self.advanced_current}")
        else:
            self._load_advanced(self.advanced_current)
            self._show_error("The change was reverted", message)

    def _save_pending(self, *_args):
        changed: dict[Path, str] = {}
        for index in sorted(self.dirty_bindings):
            binding = self.setting_bindings[index]
            if binding.path not in changed:
                changed[binding.path] = self._config_text(binding.path)
            changed[binding.path] = set_value(changed[binding.path], binding.block, binding.key, binding.serialize())

        if getattr(self, "_idle_dirty", False):
            path = HYPR / "hypridle.conf"
            text = self._config_text(path)
            text = set_listener_timeout(text, "omarchy-launch-screensaver", int(self.saver_delay.get_value() * 60))
            text = set_listener_timeout(text, "omarchy-system-lock", int(self.lock_delay.get_value() * 60))
            if self.suspend_idle.get_active():
                text = set_listener_timeout(text, "systemctl suspend", int(self.suspend_delay.get_value() * 60), "systemctl suspend")
            else:
                text = remove_listener(text, "systemctl suspend")
            changed[path] = text

        if self.shortcut_additions or self.shortcut_removals:
            path = HYPR / "bindings.conf"
            lines = [line for line in self._config_text(path).splitlines() if line not in self.shortcut_removals]
            if self.shortcut_additions:
                lines.extend(["", "# Added by Hypr Control", *self.shortcut_additions])
            changed[path] = "\n".join(lines).rstrip() + "\n"

        if not changed:
            return
        backups = {path: atomic_write(path, text) for path, text in changed.items()}
        self.apply_button.set_sensitive(False)
        self._validate_and_restart(backups, self._settings_saved)

    def _settings_saved(self, ok, message):
        if ok:
            self.dirty_bindings.clear()
            self.shortcut_additions.clear()
            self.shortcut_removals.clear()
            self._idle_dirty = False
            self._populate_shortcuts()
            self.toast("Settings applied")
            self.refresh_runtime()
        else:
            self.apply_button.set_sensitive(True)
            self._show_error("The changes were reverted", message)

    def _validate_and_restart(self, backups: dict[Path, Path | None], callback):
        def work():
            names = {path.name for path in backups}
            hypr_files = names - {"hypridle.conf", "hyprsunset.conf", "hyprlock.conf"}
            ok, message = True, ""
            try:
                if hypr_files:
                    reload_result = subprocess.run(["hyprctl", "reload"], capture_output=True, text=True)
                    errors = subprocess.run(["hyprctl", "configerrors"], capture_output=True, text=True)
                    message = (reload_result.stderr + errors.stdout + errors.stderr).strip()
                    ok = reload_result.returncode == 0 and errors.returncode == 0 and not errors.stdout.strip()
                if ok and "hypridle.conf" in names:
                    result = subprocess.run(["omarchy", "restart", "hypridle"], capture_output=True, text=True)
                    ok, message = result.returncode == 0, (result.stdout + result.stderr).strip()
                if ok and "hyprsunset.conf" in names:
                    result = subprocess.run(["omarchy", "restart", "hyprsunset"], capture_output=True, text=True)
                    ok, message = result.returncode == 0, (result.stdout + result.stderr).strip()
            except OSError as error:
                ok, message = False, str(error)
            if not ok:
                for path, backup in backups.items():
                    restore_backup(path, backup)
                if hypr_files:
                    subprocess.run(["hyprctl", "reload"], capture_output=True)
                if "hypridle.conf" in names:
                    subprocess.run(["omarchy", "restart", "hypridle"], capture_output=True)
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
        return subprocess.run(["pgrep", "-x", "hypridle"], capture_output=True).returncode == 0

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
            try:
                monitors = json.loads(raw)
            except json.JSONDecodeError:
                monitors = []
            GLib.idle_add(self._render_runtime, version, errors, monitors)

        threading.Thread(target=work, daemon=True).start()

    def _render_runtime(self, version, errors, monitors):
        label = version[0] if version else "Compositor unavailable"
        self.status_compositor.set_subtitle(label)
        self.status_config.set_subtitle(errors or "No configuration errors")
        active = next((m for m in monitors if m.get("focused")), monitors[0] if monitors else None)
        if active:
            self.status_display.set_subtitle(f"{active['name']} · {active['width']}×{active['height']} @ {active['refreshRate']:.0f} Hz · {active['scale']:.2g}×")
        else:
            self.status_display.set_subtitle("No live output data")

        for row in self.display_rows:
            self.display_group.remove(row)
        self.display_rows.clear()
        if not monitors:
            row = Adw.ActionRow(title="No outputs reported", subtitle="Hyprland may not be available")
            self.display_group.add(row)
            self.display_rows.append(row)
        for monitor in monitors:
            title = f"{monitor['description']}"
            subtitle = f"{monitor['name']} · {monitor['width']}×{monitor['height']} @ {monitor['refreshRate']:.2f} Hz · scale {monitor['scale']:.2g}×"
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image.new_from_icon_name("video-display-symbolic"))
            badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if monitor.get("focused"):
                badge = Gtk.Label(label="Active")
                badge.add_css_class("status-badge")
                badges.append(badge)
            state = Gtk.Label(label="On" if monitor.get("dpmsStatus") else "Off")
            state.add_css_class("status-badge")
            badges.append(state)
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
