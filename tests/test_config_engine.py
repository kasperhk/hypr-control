from pathlib import Path
import os
import tempfile
import unittest

from hypr_control_center.config_engine import (
    atomic_write,
    backup_history,
    get_lua_value,
    get_value,
    idle_timeouts,
    listener_timeout,
    parse_bindings,
    remove_listener,
    render_lua_bindings,
    restore_backup,
    set_listener_timeout,
    set_lua_value,
    set_value,
    shortcut_chord,
    transactional_write,
)


SAMPLE = """# Keep this comment
general {
    gaps_in = 5 # existing comment
}

decoration {
    blur {
        enabled = true
    }
}
"""

LUA_SAMPLE = """-- Managed values
hl.config({
  general = {
    layout = "scrolling", -- existing comment
  },
  decoration = {
    blur = {
      enabled = true,
    },
  },
})
"""


class ConfigEngineTests(unittest.TestCase):
    def test_reads_nested_values(self):
        self.assertEqual(get_value(SAMPLE, ("general",), "gaps_in"), "5")
        self.assertEqual(get_value(SAMPLE, ("decoration", "blur"), "enabled"), "true")

    def test_updates_and_preserves_comment(self):
        changed = set_value(SAMPLE, ("general",), "gaps_in", "12")
        self.assertIn("gaps_in = 12 # existing comment", changed)
        self.assertIn("# Keep this comment", changed)

    def test_inserts_nested_value(self):
        changed = set_value(SAMPLE, ("decoration", "blur"), "passes", "3")
        self.assertEqual(get_value(changed, ("decoration", "blur"), "passes"), "3")

    def test_lua_setting_round_trip(self):
        self.assertEqual(get_lua_value(LUA_SAMPLE, ("general",), "layout"), "scrolling")
        changed = set_lua_value(LUA_SAMPLE, ("general",), "layout", '"dwindle"')
        changed = set_lua_value(changed, ("decoration", "blur"), "passes", "3")
        self.assertEqual(get_lua_value(changed, ("general",), "layout"), "dwindle")
        self.assertEqual(get_lua_value(changed, ("decoration", "blur"), "passes"), "3")
        self.assertIn("-- existing comment", changed)

    def test_renders_managed_bindings_as_lua(self):
        text = """unbind = SUPER SHIFT, X
bindd = SUPER SHIFT, X, Launch thing, exec, app --value=a,b
"""
        rendered = render_lua_bindings(text)
        self.assertIn('hl.unbind("SUPER + SHIFT + X")', rendered)
        self.assertIn('o.bind("SUPER + SHIFT + X", "Launch thing", "app --value=a,b")', rendered)

    def test_listener_round_trip(self):
        idle = """listener {
    timeout = 120
    on-timeout = omarchy-launch-screensaver
}
"""
        changed = set_listener_timeout(idle, "omarchy-launch-screensaver", 300)
        self.assertEqual(listener_timeout(changed, "omarchy-launch-screensaver"), 300)
        changed = set_listener_timeout(changed, "systemctl suspend", 1800, "systemctl suspend")
        self.assertEqual(listener_timeout(changed, "systemctl suspend"), 1800)
        self.assertNotIn("systemctl suspend", remove_listener(changed, "systemctl suspend"))

    def test_binding_parser_handles_command_commas(self):
        line = "bindd = SUPER SHIFT, X, Launch thing, exec, app --value=a,b"
        item = parse_bindings(line)[0]
        self.assertEqual(item["key"], "X")
        self.assertEqual(item["argument"], "app --value=a,b")

    def test_shortcut_chords_normalize_aliases_and_order(self):
        self.assertEqual(
            shortcut_chord("shift control super", "x"),
            shortcut_chord("SUPER CTRL SHIFT", "X"),
        )

    def test_idle_timeouts_make_lock_relative_to_screensaver(self):
        self.assertEqual(idle_timeouts(5, 2, 30), (300, 420, 1800))

    def test_idle_timeouts_reject_suspend_before_lock(self):
        with self.assertRaises(ValueError):
            idle_timeouts(10, 5, 12)

    def test_atomic_write_keeps_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.conf"
            path.write_text("before\n", encoding="utf-8")
            backup = atomic_write(path, "after\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(backup.read_text(encoding="utf-8"), "before\n")

    def test_atomic_write_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.conf"
            path.write_text("before\n", encoding="utf-8")
            path.chmod(0o640)
            atomic_write(path, "after\n")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)

    def test_restore_removes_a_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.conf"
            backup = atomic_write(path, "temporary\n")
            self.assertIsNone(backup)
            restore_backup(path, backup)
            self.assertFalse(path.exists())

    def test_transaction_rolls_back_every_file_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.conf"
            second = Path(directory) / "second.conf"
            first.write_text("before first\n", encoding="utf-8")
            second.write_text("before second\n", encoding="utf-8")
            ok, message, _ = transactional_write(
                {first: "after first\n", second: "after second\n"},
                lambda: (False, "bad configuration"),
            )
            self.assertFalse(ok)
            self.assertEqual(message, "bad configuration")
            self.assertEqual(first.read_text(encoding="utf-8"), "before first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "before second\n")

    def test_transaction_rolls_back_files_when_a_later_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.conf"
            first.write_text("before\n", encoding="utf-8")
            invalid = Path(directory) / "folder"
            invalid.mkdir()
            ok, _message, _ = transactional_write(
                {first: "after\n", invalid: "cannot replace a directory\n"},
                lambda: (True, ""),
            )
            self.assertFalse(ok)
            self.assertEqual(first.read_text(encoding="utf-8"), "before\n")

    def test_backup_history_is_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.conf"
            path.write_text("zero\n", encoding="utf-8")
            atomic_write(path, "one\n")
            atomic_write(path, "two\n")
            history = backup_history(path)
            self.assertEqual(len(history), 2)
            self.assertGreater(history[0].name, history[1].name)


if __name__ == "__main__":
    unittest.main()
