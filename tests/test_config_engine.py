from pathlib import Path
import os
import tempfile
import unittest

from hypr_control_center.config_engine import (
    atomic_write,
    get_value,
    listener_timeout,
    parse_bindings,
    remove_listener,
    restore_backup,
    set_listener_timeout,
    set_value,
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


if __name__ == "__main__":
    unittest.main()
