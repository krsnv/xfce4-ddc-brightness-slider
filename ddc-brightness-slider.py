#!/usr/bin/env python3

"""
DDC Brightness Slider for XFCE4

GTK3 tray icon with a brightness slider that controls monitor brightness
via ddccontrol (DDC/CI protocol over I2C).

Configuration:
  Edit the constants below or use command-line arguments.

Requirements:
  - ddccontrol
  - redshift
  - python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1
  - User must be in the 'i2c' group: sudo usermod -aG i2c $USER

Author: Vladimir Krasnov
License: MIT
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import gi
gi.require_version('Gtk', '3.0')

from gi.repository import Gtk, Gdk, GLib

import subprocess
import argparse
import dataclasses
import json
import os
import re
import sys
import signal
import threading

DEFAULT_CONFIG_DIR = os.path.expanduser("~/.config/ddc-brightness")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.json")
DEFAULT_CONFIG = {
    "scroll_step": 1,
    "presets": [
        {"name": "Movie", "brightness": 30, "contrast": 60, "color_temp": 3500},
        {"name": "Reading", "brightness": 80, "contrast": 40, "color_temp": 5500},
    ],
}
DEFAULT_I2C_DEV = "auto"
DEFAULT_DDC_REGISTER = "0x10"           # 0x10 = Brightness in DDC/CI spec
DEFAULT_DDC_CONTRAST_REGISTER = "0x12"  # 0x12 = Contrast in DDC/CI spec
DEFAULT_MIN_BRIGHTNESS = 0
DEFAULT_MAX_BRIGHTNESS = 100
DEFAULT_STEP = 5
DEFAULT_SCROLL_STEP = 1
ICON_NAME = "display-brightness-symbolic"


@dataclasses.dataclass
class MonitorInfo:
    device: str           # "/dev/i2c-3"
    name: str             # "Dell U2515H"
    brightness: 'DDCController'
    contrast: 'DDCController'


def detect_i2c_devices() -> list[tuple[str, str]]:
    """Probe for DDC-capable monitors via ddccontrol -p.
    Returns list of (device_path, monitor_name) tuples."""
    devices = []
    try:
        result = subprocess.run(
            ["ddccontrol", "-p"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            m = re.search(r'Device:\s*dev:(/dev/i2c-\d+)', line)
            if m:
                device_path = m.group(1)
                if i + 1 < len(lines) and "DDC/CI supported: Yes" in lines[i + 1]:
                    monitor_name = ""
                    if i + 2 < len(lines):
                        nm = re.search(r'Monitor Name:\s*(.+)', lines[i + 2])
                        if nm:
                            monitor_name = nm.group(1).strip()
                    devices.append((device_path, monitor_name))
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        print(f"[ddc-brightness] Error probing for monitors: {e}", file=sys.stderr)
    return devices


class DDCController:

    def __init__(self, i2c_dev: str, register: str):
        self.device = f"dev:{i2c_dev}"
        self.register = register

    def get_brightness(self) -> int | None:
        try:
            result = subprocess.run(
                ["ddccontrol", "-r", self.register, self.device],
                capture_output=True, text=True, timeout=5
            )
            # Parse output like: "Control 0x10: +/70/100 [Brightness]"
            # or: " > current value = 70"
            for line in result.stdout.splitlines():
                m = re.search(r'\+/(\d+)/(\d+)', line)
                if m:
                    return int(m.group(1))
                m = re.search(r'current\s+value\s*=\s*(\d+)', line)
                if m:
                    return int(m.group(1))
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            print(f"[ddc-brightness] Error reading brightness: {e}", file=sys.stderr)
            return None

    def set_brightness(self, value: int) -> bool:
        value = max(0, min(100, int(value)))
        try:
            result = subprocess.run(
                ["ddccontrol", "-r", self.register, "-w", str(value), self.device],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            print(f"[ddc-brightness] Error setting brightness to {value}: {e}", file=sys.stderr)
            return False


class _SliderGroup:
    """A pair of brightness/contrast sliders for one monitor (or master)."""

    def __init__(self, vbox, monitor: MonitorInfo | None, min_val, max_val, step,
                 on_brightness, on_contrast, show_presets=False):
        self.monitor = monitor
        self.is_applying_brightness = False
        self.is_applying_contrast = False
        self._brightness_debounce = None
        self._contrast_debounce = None
        self._on_brightness = on_brightness
        self._on_contrast = on_contrast

        # Brightness
        title = Gtk.Label()
        title.set_markup("<b>☀ Brightness</b>")
        vbox.pack_start(title, False, False, 0)
        vbox.pack_start(Gtk.Separator(), False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, True, True, 0)

        adj = Gtk.Adjustment(value=50, lower=min_val, upper=max_val,
                             step_increment=step, page_increment=step * 2, page_size=0)
        self.brightness_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self.brightness_scale.set_digits(0)
        self.brightness_scale.set_draw_value(False)
        self.brightness_scale.set_size_request(200, -1)
        for tick in range(min_val, max_val + 1, 25):
            self.brightness_scale.add_mark(tick, Gtk.PositionType.BOTTOM, None)
        self.brightness_scale.connect("value-changed", self._on_brightness_changed)
        hbox.pack_start(self.brightness_scale, True, True, 0)

        self.brightness_label = Gtk.Label(label="50%")
        self.brightness_label.set_width_chars(5)
        hbox.pack_start(self.brightness_label, False, False, 0)

        if show_presets:
            vbox.pack_start(Gtk.Separator(), False, False, 0)
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            btn_box.set_homogeneous(True)
            vbox.pack_start(btn_box, False, False, 0)
            for preset in [1, 10, 25, 50, 75, 100]:
                btn = Gtk.Button(label=f"{preset}%")
                btn.connect("clicked", self._on_preset_clicked, preset)
                btn_box.pack_start(btn, True, True, 0)

        # Contrast
        vbox.pack_start(Gtk.Separator(), False, False, 0)
        contrast_title = Gtk.Label()
        contrast_title.set_markup("<b>◑ Contrast</b>")
        vbox.pack_start(contrast_title, False, False, 0)
        vbox.pack_start(Gtk.Separator(), False, False, 0)

        contrast_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(contrast_hbox, True, True, 0)

        contrast_adj = Gtk.Adjustment(value=50, lower=min_val, upper=max_val,
                                      step_increment=step, page_increment=step * 2, page_size=0)
        self.contrast_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=contrast_adj)
        self.contrast_scale.set_digits(0)
        self.contrast_scale.set_draw_value(False)
        self.contrast_scale.set_size_request(200, -1)
        for tick in range(min_val, max_val + 1, 25):
            self.contrast_scale.add_mark(tick, Gtk.PositionType.BOTTOM, None)
        self.contrast_scale.connect("value-changed", self._on_contrast_changed)
        contrast_hbox.pack_start(self.contrast_scale, True, True, 0)

        self.contrast_label = Gtk.Label(label="50%")
        self.contrast_label.set_width_chars(5)
        contrast_hbox.pack_start(self.contrast_label, False, False, 0)

    def refresh(self):
        if self.monitor is None:
            return
        val = self.monitor.brightness.get_brightness()
        if val is not None:
            self.set_brightness(val)
        con = self.monitor.contrast.get_brightness()
        if con is not None:
            self.set_contrast(con)

    def set_brightness(self, value):
        self.is_applying_brightness = True
        self.brightness_scale.set_value(value)
        self.brightness_label.set_text(f"{value}%")
        self.is_applying_brightness = False

    def set_contrast(self, value):
        self.is_applying_contrast = True
        self.contrast_scale.set_value(value)
        self.contrast_label.set_text(f"{value}%")
        self.is_applying_contrast = False

    def _on_brightness_changed(self, scale):
        if self.is_applying_brightness:
            return
        value = int(scale.get_value())
        self.brightness_label.set_text(f"{value}%")
        if self._brightness_debounce:
            GLib.source_remove(self._brightness_debounce)
        self._brightness_debounce = GLib.timeout_add(150, self._apply_brightness, value)

    def _apply_brightness(self, value):
        self._brightness_debounce = None
        self._on_brightness(self, value)
        return False

    def _on_contrast_changed(self, scale):
        if self.is_applying_contrast:
            return
        value = int(scale.get_value())
        self.contrast_label.set_text(f"{value}%")
        if self._contrast_debounce:
            GLib.source_remove(self._contrast_debounce)
        self._contrast_debounce = GLib.timeout_add(150, self._apply_contrast, value)

    def _apply_contrast(self, value):
        self._contrast_debounce = None
        self._on_contrast(self, value)
        return False

    def _on_preset_clicked(self, button, value):
        self.brightness_label.set_text(f"{value}%")
        self.is_applying_brightness = True
        self.brightness_scale.set_value(value)
        self.is_applying_brightness = False
        self._on_brightness(self, value)


class BrightnessPopup(Gtk.Window):

    COLOR_TEMP_PRESETS = [
        (3000, "3000K"),
        (4000, "4000K"),
        (5500, "5500K"),
        (6500, "6500K"),
    ]

    def __init__(self, monitors: list[MonitorInfo], min_val: int, max_val: int, step: int,
                 on_color_temp=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self.monitors = monitors
        self._on_color_temp = on_color_temp
        self._visible = False
        self._position = (0, 0)
        self._monitor_groups: list[_SliderGroup] = []
        self._master_group: _SliderGroup | None = None

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_border_width(8)
        self.set_accept_focus(True)
        self.set_can_focus(True)
        self.set_gravity(Gdk.Gravity.NORTH_WEST)

        self.set_position(Gtk.WindowPosition.NONE)

        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key_press)
        self.connect("show", lambda w: self._set_visible(True))
        self.connect("hide", lambda w: self._set_visible(False))
        self.connect("realize", self._on_realize)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        multi = len(monitors) > 1

        if multi:
            header = Gtk.Label()
            header.set_markup("<b>🖥 All Monitors</b>")
            vbox.pack_start(header, False, False, 0)
            master_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(master_box, False, False, 0)
            self._master_group = _SliderGroup(
                master_box, None, min_val, max_val, step,
                on_brightness=self._on_master_brightness,
                on_contrast=self._on_master_contrast,
                show_presets=True)

        for i, mon in enumerate(monitors):
            if multi:
                vbox.pack_start(Gtk.Separator(), False, False, 4)
                header = Gtk.Label()
                header.set_markup(f"<b>▪ {GLib.markup_escape_text(mon.name or mon.device)}</b>")
                vbox.pack_start(header, False, False, 0)

            mon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(mon_box, False, False, 0)
            group = _SliderGroup(
                mon_box, mon, min_val, max_val, step,
                on_brightness=self._on_monitor_brightness,
                on_contrast=self._on_monitor_contrast,
                show_presets=not multi)
            self._monitor_groups.append(group)

        if on_color_temp is not None:
            vbox.pack_start(Gtk.Separator(), False, False, 0)
            temp_title = Gtk.Label()
            temp_title.set_markup("<b>🌡 Color Temp</b>")
            vbox.pack_start(temp_title, False, False, 0)
            temp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            temp_box.set_homogeneous(True)
            for temp, label in self.COLOR_TEMP_PRESETS:
                btn = Gtk.Button(label=label)
                btn.connect("clicked", self._on_temp_clicked, temp)
                temp_box.pack_start(btn, True, True, 0)
            vbox.pack_start(temp_box, False, False, 0)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: @theme_bg_color;
                border: 1px solid @borders;
            }
            button {
                min-height: 24px;
                padding: 2px 4px;
                font-size: 11px;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _on_temp_clicked(self, button, temp: int):
        if self._on_color_temp:
            self._on_color_temp(temp)

    def _on_monitor_brightness(self, group: _SliderGroup, value: int):
        group.monitor.brightness.set_brightness(value)

    def _on_monitor_contrast(self, group: _SliderGroup, value: int):
        group.monitor.contrast.set_brightness(value)

    def _on_master_brightness(self, group: _SliderGroup, value: int):
        for mg in self._monitor_groups:
            mg.set_brightness(value)
            mg.monitor.brightness.set_brightness(value)

    def _on_master_contrast(self, group: _SliderGroup, value: int):
        for mg in self._monitor_groups:
            mg.set_contrast(value)
            mg.monitor.contrast.set_brightness(value)

    def _on_realize(self, widget):
        self.get_window().move_resize(
            self._position[0], self._position[1],
            self.get_allocated_width(), self.get_allocated_height()
        )

    def refresh_value(self):
        for group in self._monitor_groups:
            group.refresh()
        if self._master_group and self._monitor_groups:
            first = self._monitor_groups[0]
            self._master_group.set_brightness(int(first.brightness_scale.get_value()))
            self._master_group.set_contrast(int(first.contrast_scale.get_value()))

    def update_all(self, brightness: int, contrast: int):
        """Update all sliders to given values."""
        for group in self._monitor_groups:
            group.set_brightness(brightness)
            group.set_contrast(contrast)
        if self._master_group:
            self._master_group.set_brightness(brightness)
            self._master_group.set_contrast(contrast)

    def update_value(self, value):
        """Update all monitor brightness sliders (used by scroll)."""
        for group in self._monitor_groups:
            group.set_brightness(value)
        if self._master_group:
            self._master_group.set_brightness(value)

    def _set_visible(self, visible):
        self._visible = visible

    def _on_focus_out(self, widget, event):
        GLib.timeout_add(100, self._check_focus)
        return False

    def _check_focus(self):
        if not self.is_active():
            self.hide()
        return False

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def toggle_at(self, x: int, y: int):
        if self._visible:
            self.hide()
            self.unrealize()
        else:
            self.refresh_value()
            self._position = (x, y)
            self.move(x, y)
            self.show_all()
            self.move(x, y)
            self.present()
            if self.get_window():
                self.get_window().focus(Gdk.CURRENT_TIME)


def load_config(path: str) -> dict:
    """Load config from JSON file. Returns parsed config or empty dict on error."""
    try:
        with open(path) as f:
            cfg = json.load(f)
        presets = [{
            "name": p.get("name", "Preset"),
            "brightness": p.get("brightness", 50),
            "contrast": p.get("contrast", 50),
            "color_temp": p.get("color_temp"),
        } for p in cfg.get("presets", [])]
        result = {
            "scroll_step": cfg.get("scroll_step", DEFAULT_SCROLL_STEP),
            "presets": presets,
        }
        if presets:
            print(f"[ddc-brightness] Config loaded: {len(presets)} presets", file=sys.stderr)
        return result
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        print(f"[ddc-brightness] Error loading config: {e}", file=sys.stderr)
        return {}


class TrayApp:

    def __init__(self, monitors: list[MonitorInfo], min_val: int, max_val: int, step: int,
                 scroll_step: int = DEFAULT_SCROLL_STEP, presets: list | None = None):
        self.monitors = monitors
        self.min_val = min_val
        self.max_val = max_val
        self.scroll_step = scroll_step
        self.presets = presets or []
        self.popup = BrightnessPopup(monitors, min_val, max_val, step,
                                     on_color_temp=self._on_color_temp)
        self._cached_brightness = None
        self._scroll_debounce_id = None
        self._redshift_paused = False

        self.status_icon = None
        self.indicator = None

        n = len(monitors)
        tooltip = f"DDC Brightness ({n} monitors)" if n > 1 else "DDC Brightness"

        if not self._setup_status_icon(tooltip):
            try:
                gi.require_version('AyatanaAppIndicator3', '0.1')
                from gi.repository import AyatanaAppIndicator3
                self._setup_appindicator(AyatanaAppIndicator3, tooltip)
            except (ValueError, ImportError):
                try:
                    gi.require_version('AppIndicator3', '0.1')
                    from gi.repository import AppIndicator3
                    self._setup_appindicator(AppIndicator3, tooltip)
                except (ValueError, ImportError):
                    print("[ddc-brightness] ERROR: No tray icon backend available!", file=sys.stderr)
                    sys.exit(1)

    def _setup_status_icon(self, tooltip: str) -> bool:
        try:
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_icon_name(ICON_NAME)
            self.status_icon.set_tooltip_text(tooltip)
            self.status_icon.set_visible(True)
            self.status_icon.connect("activate", self._on_left_click)
            self.status_icon.connect("popup-menu", self._on_right_click)
            self.status_icon.connect("scroll-event", self._on_scroll_event)
            print("[ddc-brightness] Using GtkStatusIcon tray icon", file=sys.stderr)
            return True
        except Exception:
            return False

    def _on_left_click(self, icon):
        success, screen, area, orientation = icon.get_geometry()
        if success:
            display = Gdk.Display.get_default()
            monitor = display.get_monitor_at_point(area.x, area.y)
            popup_width = 280
            popup_height = 210 * len(self.monitors) + (60 if len(self.monitors) > 1 else 0)

            x = area.x + area.width // 2 - popup_width // 2

            if monitor:
                workarea = monitor.get_workarea()
                x = max(workarea.x, min(x, workarea.x + workarea.width - popup_width))

                if area.y > workarea.y + workarea.height // 2:
                    y = area.y - popup_height - 4
                else:
                    y = area.y + area.height + 4
            else:
                y = area.y + area.height + 4

            self.popup.toggle_at(x, y)
        else:
            display = Gdk.Display.get_default()
            seat = display.get_default_seat()
            pointer = seat.get_pointer()
            _, x, y = pointer.get_position()
            self.popup.toggle_at(x - 140, y + 10)

    @staticmethod
    def _apply_redshift(temp: int):
        """Apply color temperature via redshift one-shot."""
        try:
            subprocess.Popen(
                ["redshift", "-P", "-O", str(temp)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            print("[ddc-brightness] redshift not found, skipping color_temp", file=sys.stderr)

    def _on_color_temp(self, temp: int):
        """Called when user clicks a color temperature button."""
        self._apply_redshift(temp)

    def _on_apply_preset(self, widget, preset_index):
        """Apply a preset immediately."""
        if preset_index >= len(self.presets):
            return
        preset = self.presets[preset_index]
        for mon in self.monitors:
            mon.brightness.set_brightness(preset["brightness"])
            mon.contrast.set_brightness(preset["contrast"])
        self.popup.update_all(preset["brightness"], preset["contrast"])
        if preset.get("color_temp"):
            self._apply_redshift(preset["color_temp"])

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        if self.presets:
            for i, preset in enumerate(self.presets):
                label = f"{preset['name']}  ☀{preset['brightness']}% ◑{preset['contrast']}%"
                if preset.get("color_temp"):
                    label += f" 🌡{preset['color_temp']}K"
                item = Gtk.MenuItem(label=label)
                item.connect("activate", self._on_apply_preset, i)
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
        if self._is_redshift_running():
            label = "▶ Resume Redshift" if self._redshift_paused else "⏸ Pause Redshift"
            item_rs = Gtk.MenuItem(label=label)
            item_rs.connect("activate", self._on_toggle_redshift)
            menu.append(item_rs)
            menu.append(Gtk.SeparatorMenuItem())
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda w: Gtk.main_quit())
        menu.append(item_quit)
        menu.show_all()
        return menu

    def _on_right_click(self, icon, button, time):
        menu = self._build_menu()
        menu.popup(None, None, None, None, button, time)

    @staticmethod
    def _is_redshift_running() -> bool:
        try:
            result = subprocess.run(["pgrep", "-x", "redshift"], capture_output=True)
            return result.returncode == 0
        except Exception:
            return False

    def _on_toggle_redshift(self, widget):
        try:
            subprocess.run(["pkill", "-USR1", "redshift"], capture_output=True)
            self._redshift_paused = not self._redshift_paused
        except Exception:
            pass

    def _setup_appindicator(self, AppIndicatorLib, tooltip: str):
        self.indicator = AppIndicatorLib.Indicator.new(
            "ddc-brightness-slider",
            ICON_NAME,
            AppIndicatorLib.IndicatorCategory.HARDWARE
        )
        self.indicator.set_status(AppIndicatorLib.IndicatorStatus.ACTIVE)
        self.indicator.set_title(tooltip)

        menu = self._build_menu()

        item_slider = Gtk.MenuItem(label="☀ Brightness Slider")
        item_slider.connect("activate", self._on_indicator_activate)
        item_slider.show()
        sep = Gtk.SeparatorMenuItem()
        sep.show()
        menu.prepend(sep)
        menu.prepend(item_slider)
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(item_slider)
        self.indicator.connect("scroll-event", self._on_indicator_scroll)

        print("[ddc-brightness] Using AppIndicator tray icon", file=sys.stderr)

    def _on_indicator_activate(self, widget):
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        pointer = seat.get_pointer()
        _, x, y = pointer.get_position()
        self.popup.toggle_at(x, y)

    def _on_scroll_event(self, icon, event):
        if event.direction == Gdk.ScrollDirection.UP:
            self._adjust_brightness(self.scroll_step)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self._adjust_brightness(-self.scroll_step)

    def _on_indicator_scroll(self, indicator, delta, direction):
        if direction == Gdk.ScrollDirection.UP:
            self._adjust_brightness(self.scroll_step)
        elif direction == Gdk.ScrollDirection.DOWN:
            self._adjust_brightness(-self.scroll_step)

    def _adjust_brightness(self, delta):
        if self._cached_brightness is None:
            self._cached_brightness = self.monitors[0].brightness.get_brightness()
            if self._cached_brightness is None:
                return
        new_val = max(self.min_val, min(self.max_val, self._cached_brightness + delta))
        if new_val == self._cached_brightness:
            return
        self._cached_brightness = new_val
        if self.popup._visible:
            self.popup.update_value(new_val)
        if self._scroll_debounce_id:
            GLib.source_remove(self._scroll_debounce_id)
        self._scroll_debounce_id = GLib.timeout_add(100, self._apply_scroll_brightness, new_val)

    def _apply_scroll_brightness(self, value):
        self._scroll_debounce_id = None
        def _set_all():
            for mon in self.monitors:
                mon.brightness.set_brightness(value)
        threading.Thread(target=_set_all, daemon=True).start()
        return False


class StandaloneWindow(Gtk.Window):

    def __init__(self, monitors: list[MonitorInfo], min_val: int, max_val: int, step: int):
        super().__init__(title="DDC Brightness & Contrast")
        self.monitors = monitors
        self._monitor_groups: list[_SliderGroup] = []
        self._master_group: _SliderGroup | None = None

        self.set_default_size(350, 160)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect("destroy", Gtk.main_quit)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_border_width(12)
        self.add(vbox)

        multi = len(monitors) > 1

        if multi:
            header = Gtk.Label()
            header.set_markup("<b>🖥 All Monitors</b>")
            vbox.pack_start(header, False, False, 0)
            master_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(master_box, False, False, 0)
            self._master_group = _SliderGroup(
                master_box, None, min_val, max_val, step,
                on_brightness=self._on_master_brightness,
                on_contrast=self._on_master_contrast,
                show_presets=True)

        for mon in monitors:
            if multi:
                vbox.pack_start(Gtk.Separator(), False, False, 4)
                header = Gtk.Label()
                header.set_markup(f"<b>▪ {GLib.markup_escape_text(mon.name or mon.device)}</b>")
                vbox.pack_start(header, False, False, 0)

            mon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            vbox.pack_start(mon_box, False, False, 0)
            group = _SliderGroup(
                mon_box, mon, min_val, max_val, step,
                on_brightness=self._on_monitor_brightness,
                on_contrast=self._on_monitor_contrast,
                show_presets=not multi)
            self._monitor_groups.append(group)

        self._refresh()

    def _on_monitor_brightness(self, group: _SliderGroup, value: int):
        group.monitor.brightness.set_brightness(value)

    def _on_monitor_contrast(self, group: _SliderGroup, value: int):
        group.monitor.contrast.set_brightness(value)

    def _on_master_brightness(self, group: _SliderGroup, value: int):
        for mg in self._monitor_groups:
            mg.set_brightness(value)
            mg.monitor.brightness.set_brightness(value)

    def _on_master_contrast(self, group: _SliderGroup, value: int):
        for mg in self._monitor_groups:
            mg.set_contrast(value)
            mg.monitor.contrast.set_brightness(value)

    def _refresh(self):
        for group in self._monitor_groups:
            group.refresh()
        if self._master_group and self._monitor_groups:
            first = self._monitor_groups[0]
            self._master_group.set_brightness(int(first.brightness_scale.get_value()))
            self._master_group.set_contrast(int(first.contrast_scale.get_value()))


def main():
    parser = argparse.ArgumentParser(
        description="DDC Brightness & Contrast Slider for XFCE4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s                          # Tray icon mode (default)
  %(prog)s --standalone             # Floating window mode
  %(prog)s --device /dev/i2c-5      # Use a different I2C bus
  %(prog)s --get-contrast           # Print current contrast
  %(prog)s --set-contrast 50        # Set contrast to 50
  %(prog)s --config config.json     # Custom config with presets
"""
    )
    parser.add_argument("-d", "--device", default=DEFAULT_I2C_DEV,
                        help="I2C device path (default: auto-detect)")
    parser.add_argument("-r", "--register", default=DEFAULT_DDC_REGISTER,
                        help=f"DDC register for brightness (default: {DEFAULT_DDC_REGISTER})")
    parser.add_argument("--contrast-register", default=DEFAULT_DDC_CONTRAST_REGISTER,
                        help=f"DDC register for contrast (default: {DEFAULT_DDC_CONTRAST_REGISTER})")
    parser.add_argument("--min", type=int, default=DEFAULT_MIN_BRIGHTNESS,
                        help="Minimum brightness value (default: 0)")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_BRIGHTNESS,
                        help="Maximum brightness value (default: 100)")
    parser.add_argument("--step", type=int, default=DEFAULT_STEP,
                        help="Slider step size (default: 5)")
    parser.add_argument("--scroll-step", type=int, default=DEFAULT_SCROLL_STEP,
                        help="Scroll wheel step size (default: 1)")
    parser.add_argument("--standalone", action="store_true",
                        help="Show as a floating window instead of tray icon")
    parser.add_argument("--set", type=int, metavar="VALUE",
                        help="Set brightness to VALUE and exit (no GUI)")
    parser.add_argument("--get", action="store_true",
                        help="Print current brightness and exit (no GUI)")
    parser.add_argument("--set-contrast", type=int, metavar="VALUE",
                        help="Set contrast to VALUE and exit (no GUI)")
    parser.add_argument("--get-contrast", action="store_true",
                        help="Print current contrast and exit (no GUI)")
    parser.add_argument("--config", metavar="FILE",
                        help=f"JSON config file with presets (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--no-config", action="store_true",
                        help="Disable config file loading")

    args = parser.parse_args()

    if args.device == "auto":
        detected = detect_i2c_devices()
        if not detected:
            print("[ddc-brightness] ERROR: No DDC-capable monitor found. "
                  "Use --device to specify manually.", file=sys.stderr)
            sys.exit(1)
        monitors = []
        for dev_path, mon_name in detected:
            label = f" ({mon_name})" if mon_name else ""
            print(f"[ddc-brightness] Found: {dev_path}{label}", file=sys.stderr)
            monitors.append(MonitorInfo(
                device=dev_path, name=mon_name,
                brightness=DDCController(dev_path, args.register),
                contrast=DDCController(dev_path, args.contrast_register),
            ))
    else:
        monitors = [MonitorInfo(
            device=args.device, name=args.device,
            brightness=DDCController(args.device, args.register),
            contrast=DDCController(args.device, args.contrast_register),
        )]

    if args.get:
        val = monitors[0].brightness.get_brightness()
        if val is not None:
            print(val)
            sys.exit(0)
        else:
            print("Error: could not read brightness", file=sys.stderr)
            sys.exit(1)

    if args.get_contrast:
        val = monitors[0].contrast.get_brightness()
        if val is not None:
            print(val)
            sys.exit(0)
        else:
            print("Error: could not read contrast", file=sys.stderr)
            sys.exit(1)

    if args.set is not None:
        ok = monitors[0].brightness.set_brightness(args.set)
        sys.exit(0 if ok else 1)

    if args.set_contrast is not None:
        ok = monitors[0].contrast.set_brightness(args.set_contrast)
        sys.exit(0 if ok else 1)

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    config = {}
    if not args.no_config:
        config_path = args.config or DEFAULT_CONFIG_PATH
        if not os.path.exists(config_path) and not args.config:
            os.makedirs(DEFAULT_CONFIG_DIR, exist_ok=True)
            with open(config_path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            print(f"[ddc-brightness] Created default config: {config_path}", file=sys.stderr)
        if os.path.exists(config_path):
            config = load_config(config_path)

    scroll_step = config.get("scroll_step", args.scroll_step)
    presets = config.get("presets", [])

    if args.standalone:
        win = StandaloneWindow(monitors, args.min, args.max, args.step)
        win.show_all()
    else:
        app = TrayApp(monitors, args.min, args.max, args.step,
                      scroll_step=scroll_step, presets=presets)

    Gtk.main()


if __name__ == "__main__":
    main()
