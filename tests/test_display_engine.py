from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from hypr_control_center.displays.engine import (
    Display,
    ProfileStore,
    capture_layout,
    common_mirror_mode,
    layout_for_mode,
    normalize_layout,
    render_monitors_config,
)


def display(
    name,
    identity,
    width,
    height,
    x,
    y,
    scale,
    *,
    internal=False,
    modes=(),
):
    return Display(
        name=name,
        identity=identity,
        description=name,
        make="Test",
        model=name,
        serial=identity,
        width=width,
        height=height,
        refresh=60,
        x=x,
        y=y,
        scale=scale,
        internal=internal,
        available_modes=tuple(modes),
    )


class DisplayEngineTests(unittest.TestCase):
    def setUp(self):
        self.internal = display(
            "eDP-1", "internal", 2880, 1800, 5903, 2408, 2, internal=True
        )
        self.external = display(
            "DP-5", "dell", 2560, 1440, 5356, 968, 1
        )
        self.displays = [self.internal, self.external]

    def test_capture_normalizes_large_virtual_offset(self):
        layout = capture_layout(self.displays)
        external = next(item for item in layout if item.identity == "dell")
        internal = next(item for item in layout if item.identity == "internal")
        self.assertEqual((external.x, external.y), (0, 0))
        self.assertEqual((internal.x, internal.y), (547, 1440))

    def test_external_mode_never_leaves_internal_enabled(self):
        layout = layout_for_mode(self.displays, "external")
        states = {item.identity: item.enabled for item in layout}
        self.assertEqual(states, {"internal": False, "dell": True})

    def test_internal_mode_disables_external(self):
        layout = layout_for_mode(self.displays, "internal")
        states = {item.identity: item.enabled for item in layout}
        self.assertEqual(states, {"internal": True, "dell": False})

    def test_external_mode_falls_back_when_no_external_exists(self):
        layout = layout_for_mode([self.internal], "external")
        self.assertTrue(layout[0].enabled)

    def test_mirror_chooses_best_shared_mode(self):
        first = display(
            "DP-1",
            "one",
            2560,
            1440,
            0,
            0,
            1,
            modes=("2560x1440@60.00Hz", "1920x1080@60.00Hz"),
        )
        second = display(
            "eDP-1",
            "two",
            2880,
            1800,
            0,
            0,
            2,
            internal=True,
            modes=("2880x1800@120.00Hz", "1920x1080@60.00Hz"),
        )
        self.assertEqual(common_mirror_mode([first, second]), (1920, 1080, 60))
        layout = layout_for_mode([first, second], "mirror")
        self.assertEqual(sum(not item.mirror_of for item in layout), 1)
        self.assertTrue(all((item.width, item.height) == (1920, 1080) for item in layout))

    def test_mirror_keeps_supported_native_modes_without_common_resolution(self):
        first = display(
            "DP-1",
            "one",
            2560,
            1440,
            0,
            0,
            1,
            modes=("2560x1440@60.00Hz",),
        )
        second = display(
            "eDP-1",
            "two",
            2880,
            1800,
            0,
            0,
            2,
            internal=True,
            modes=("2880x1800@120.00Hz",),
        )
        layout = layout_for_mode([first, second], "mirror")
        sizes = {item.identity: (item.width, item.height) for item in layout}
        self.assertEqual(sizes, {"one": (2560, 1440), "two": (2880, 1800)})

    def test_render_includes_transform_mirror_disable_and_fallback(self):
        layout = layout_for_mode(self.displays, "mirror")
        layout[0].transform = 1
        layout[0].ten_bit = True
        text = render_monitors_config(
            layout, generated_at=datetime(2026, 7, 23, tzinfo=timezone.utc)
        )
        self.assertIn("transform, 1", text)
        self.assertIn("bitdepth, 10", text)
        self.assertIn("mirror, DP-5", text)
        self.assertIn("monitor = , preferred, auto, 1", text)

    def test_profile_matches_after_connector_name_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            layout = capture_layout(self.displays)
            saved = store.remember(
                self.displays, "extend", layout, "Home", dock_ids=()
            )
            moved = [
                Display(**{**self.internal.__dict__, "name": "eDP-2"}),
                Display(**{**self.external.__dict__, "name": "DP-1"}),
            ]
            matched = store.match(moved, dock_ids=())
            self.assertEqual(matched.profile_id, saved.profile_id)
            resolved = matched.resolved_layout(moved)
            connectors = {item.identity: item.connector for item in resolved}
            self.assertEqual(connectors, {"internal": "eDP-2", "dell": "DP-1"})

    def test_same_monitors_on_different_docks_are_distinct_setups(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            store.remember(
                self.displays,
                "extend",
                capture_layout(self.displays),
                "Home dock",
                dock_ids=("usb:home",),
            )
            self.assertIsNone(
                store.match(self.displays, dock_ids=("thunderbolt:work",))
            )

    def test_normalize_ignores_disabled_outputs(self):
        layout = layout_for_mode(self.displays, "external")
        normalized = normalize_layout(layout)
        external = next(item for item in normalized if item.identity == "dell")
        self.assertEqual((external.x, external.y), (0, 0))


if __name__ == "__main__":
    unittest.main()
