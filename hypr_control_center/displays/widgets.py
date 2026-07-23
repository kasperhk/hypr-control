from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from .engine import DisplayLayout, normalize_layout


def _rounded_rectangle(cr, x, y, width, height, radius):
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -1.5708, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
    cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
    cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
    cr.close_path()


class DisplayCanvas(Gtk.DrawingArea):
    """Small native layout canvas with draggable enabled outputs."""

    def __init__(self, on_changed: Callable[[], None] | None = None):
        super().__init__()
        self.set_content_width(520)
        self.set_content_height(280)
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.add_css_class("display-canvas")
        self.layouts: list[DisplayLayout] = []
        self.labels: dict[str, str] = {}
        self.boxes: dict[str, tuple[float, float, float, float]] = {}
        self._dragging = ""
        self._drag_origin = (0, 0)
        self._on_changed = on_changed
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

    def set_layout(
        self, layouts: list[DisplayLayout], labels: dict[str, str] | None = None
    ) -> None:
        self.layouts = [
            DisplayLayout(**asdict(item)) for item in normalize_layout(layouts)
        ]
        self.labels = labels or self.labels
        self.queue_draw()

    def get_layout(self) -> list[DisplayLayout]:
        return [DisplayLayout(**asdict(item)) for item in self.layouts]

    def _geometry(self, width: int, height: int):
        enabled = [item for item in self.layouts if item.enabled]
        if not enabled:
            return 1.0, 24.0, 24.0
        logical = []
        for item in enabled:
            w = item.height if item.transform in (1, 3, 5, 7) else item.width
            h = item.width if item.transform in (1, 3, 5, 7) else item.height
            logical.append((item, max(1, w / item.scale), max(1, h / item.scale)))
        min_x = min(item.x for item, _, _ in logical)
        min_y = min(item.y for item, _, _ in logical)
        max_x = max(item.x + w for item, w, _ in logical)
        max_y = max(item.y + h for item, _, h in logical)
        span_x = max(1, max_x - min_x)
        span_y = max(1, max_y - min_y)
        padding = 28
        scale = min((width - padding * 2) / span_x, (height - padding * 2) / span_y)
        return max(0.02, scale), padding - min_x * scale, padding - min_y * scale

    def _draw(self, _area, cr, width, height):
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.06)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        scale, offset_x, offset_y = self._geometry(width, height)
        self.boxes.clear()
        for index, item in enumerate(self.layouts):
            if not item.enabled:
                continue
            logical_w = (
                item.height if item.transform in (1, 3, 5, 7) else item.width
            ) / item.scale
            logical_h = (
                item.width if item.transform in (1, 3, 5, 7) else item.height
            ) / item.scale
            x = offset_x + item.x * scale
            y = offset_y + item.y * scale
            w = max(72, logical_w * scale)
            h = max(48, logical_h * scale)
            self.boxes[item.identity] = (x, y, w, h)

            if item.mirror_of:
                x += index * 8
                y += index * 8
            cr.set_source_rgba(0.20, 0.52, 0.90, 0.24)
            _rounded_rectangle(cr, x, y, w, h, 8)
            cr.fill_preserve()
            cr.set_source_rgba(0.20, 0.52, 0.90, 0.9)
            cr.set_line_width(2)
            cr.stroke()

            cr.set_source_rgba(0.95, 0.95, 0.95, 0.95)
            cr.select_font_face("Sans", 0, 1)
            cr.set_font_size(13)
            label = self.labels.get(item.identity, item.connector)
            label = label if len(label) <= 32 else label[:29] + "…"
            extents = cr.text_extents(label)
            cr.move_to(x + max(8, (w - extents.width) / 2), y + h / 2)
            cr.show_text(label)
            cr.set_font_size(10)
            details = f"{item.width}×{item.height} · {item.scale:g}×"
            extents = cr.text_extents(details)
            cr.move_to(x + max(8, (w - extents.width) / 2), y + h / 2 + 18)
            cr.show_text(details)

    def _drag_begin(self, _gesture, x, y):
        self._dragging = ""
        for identity, box in reversed(tuple(self.boxes.items())):
            bx, by, width, height = box
            if bx <= x <= bx + width and by <= y <= by + height:
                self._dragging = identity
                item = next(
                    layout for layout in self.layouts if layout.identity == identity
                )
                self._drag_origin = (item.x, item.y)
                break

    def _drag_update(self, _gesture, offset_x, offset_y):
        if not self._dragging:
            return
        scale, _, _ = self._geometry(
            max(1, self.get_width()), max(1, self.get_height())
        )
        item = next(
            layout for layout in self.layouts if layout.identity == self._dragging
        )
        item.x = round((self._drag_origin[0] + offset_x / scale) / 10) * 10
        item.y = round((self._drag_origin[1] + offset_y / scale) / 10) * 10
        item.mirror_of = ""
        item.mirror_identity = ""
        self.queue_draw()

    def _drag_end(self, *_args):
        if not self._dragging:
            return
        self.layouts = normalize_layout(self.layouts)
        self._dragging = ""
        self.queue_draw()
        if self._on_changed:
            self._on_changed()
