#!/usr/bin/env python3

"""
DDC Brightness Slider for XFCE4

GTK3 tray icon with a brightness slider that controls monitor brightness
via ddccontrol (DDC/CI protocol over I2C).

Configuration:
  Edit the constants below or use command-line arguments.

Requirements:
  - ddccontrol (apt install ddccontrol)
  - python3-gi (apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1)
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
import re
import sys
import signal
import threading

DEFAULT_I2C_DEV = "/dev/i2c-3"
DEFAULT_DDC_REGISTER = "0x10"       # 0x10 = Brightness in DDC/CI spec
DEFAULT_MIN_BRIGHTNESS = 0
DEFAULT_MAX_BRIGHTNESS = 100
DEFAULT_STEP = 5
DEFAULT_SCROLL_STEP = 1
ICON_NAME = "display-brightness-symbolic"


class BrightnessController:

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


class BrightnessPopup(Gtk.Window):

    def __init__(self, controller: BrightnessController, min_val: int, max_val: int, step: int):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self.controller = controller
        self.is_applying = False
        self._debounce_id = None
        self._visible = False 
        self._position = (0, 0)

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

        title = Gtk.Label()
        title.set_markup("<b>☀ Brightness</b>")
        vbox.pack_start(title, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, True, True, 0)

        adj = Gtk.Adjustment(
            value=50,
            lower=min_val,
            upper=max_val,
            step_increment=step,
            page_increment=step * 2,
            page_size=0
        )
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self.scale.set_digits(0)
        self.scale.set_draw_value(False)
        self.scale.set_size_request(200, -1)
        self.scale.set_can_focus(True)

        for tick in range(min_val, max_val + 1, 25):
            self.scale.add_mark(tick, Gtk.PositionType.BOTTOM, None)

        self.scale.connect("value-changed", self._on_value_changed)
        hbox.pack_start(self.scale, True, True, 0)

        self.value_label = Gtk.Label(label="50%")
        self.value_label.set_width_chars(5)
        hbox.pack_start(self.value_label, False, False, 0)

        vbox.pack_start(Gtk.Separator(), False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_homogeneous(True)
        vbox.pack_start(btn_box, False, False, 0)

        for preset in [1, 10, 25, 50, 75, 100]:
            btn = Gtk.Button(label=f"{preset}%")
            btn.connect("clicked", self._on_preset_clicked, preset)
            btn_box.pack_start(btn, True, True, 0)

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

    def _on_realize(self, widget):
        self.get_window().move_resize(
            self._position[0], self._position[1],
            self.get_allocated_width(), self.get_allocated_height()
        )

    def refresh_value(self):
        current = self.controller.get_brightness()
        if current is not None:
            self.is_applying = True
            self.scale.set_value(current)
            self.value_label.set_text(f"{current}%")
            self.is_applying = False

    def _on_value_changed(self, scale):
        if self.is_applying:
            return
        value = int(scale.get_value())
        self.value_label.set_text(f"{value}%")

        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(150, self._apply_brightness, value)

    def _apply_brightness(self, value):
        self._debounce_id = None
        self.controller.set_brightness(value)
        return False

    def _on_preset_clicked(self, button, value):
        self.is_applying = True
        self.scale.set_value(value)
        self.value_label.set_text(f"{value}%")
        self.is_applying = False
        self.controller.set_brightness(value)

    def update_value(self, value):
        self.is_applying = True
        self.scale.set_value(value)
        self.value_label.set_text(f"{value}%")
        self.is_applying = False

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


class TrayApp:

    def __init__(self, controller: BrightnessController, min_val: int, max_val: int, step: int):
        self.controller = controller
        self.min_val = min_val
        self.max_val = max_val
        self.popup = BrightnessPopup(controller, min_val, max_val, step)
        self._cached_brightness = None
        self._scroll_debounce_id = None

        self.status_icon = None
        self.indicator = None

        if self._setup_status_icon():
            pass
        else:
            try:
                gi.require_version('AyatanaAppIndicator3', '0.1')
                from gi.repository import AyatanaAppIndicator3
                self._setup_appindicator(AyatanaAppIndicator3)
            except (ValueError, ImportError):
                try:
                    gi.require_version('AppIndicator3', '0.1')
                    from gi.repository import AppIndicator3
                    self._setup_appindicator(AppIndicator3)
                except (ValueError, ImportError):
                    print("[ddc-brightness] ERROR: No tray icon backend available!", file=sys.stderr)
                    sys.exit(1)

    def _setup_status_icon(self) -> bool:
        try:
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_icon_name(ICON_NAME)
            self.status_icon.set_tooltip_text("DDC Brightness")
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
            popup_height = 120

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

    def _on_right_click(self, icon, button, time):
        menu = Gtk.Menu()
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda w: Gtk.main_quit())
        menu.append(item_quit)
        menu.show_all()
        menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)

    def _setup_appindicator(self, AppIndicatorLib):
        self.indicator = AppIndicatorLib.Indicator.new(
            "ddc-brightness-slider",
            ICON_NAME,
            AppIndicatorLib.IndicatorCategory.HARDWARE
        )
        self.indicator.set_status(AppIndicatorLib.IndicatorStatus.ACTIVE)
        self.indicator.set_title("DDC Brightness")

        menu = Gtk.Menu()

        item_slider = Gtk.MenuItem(label="☀ Brightness Slider")
        item_slider.connect("activate", self._on_indicator_activate)
        menu.append(item_slider)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda w: Gtk.main_quit())
        menu.append(item_quit)

        menu.show_all()
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
            self._adjust_brightness(DEFAULT_SCROLL_STEP)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self._adjust_brightness(-DEFAULT_SCROLL_STEP)

    def _on_indicator_scroll(self, indicator, delta, direction):
        if direction == Gdk.ScrollDirection.UP:
            self._adjust_brightness(DEFAULT_SCROLL_STEP)
        elif direction == Gdk.ScrollDirection.DOWN:
            self._adjust_brightness(-DEFAULT_SCROLL_STEP)

    def _adjust_brightness(self, delta):
        if self._cached_brightness is None:
            self._cached_brightness = self.controller.get_brightness()
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
        threading.Thread(target=self.controller.set_brightness, args=(value,), daemon=True).start()
        return False


class StandaloneWindow(Gtk.Window):

    def __init__(self, controller: BrightnessController, min_val: int, max_val: int, step: int):
        super().__init__(title="DDC Brightness")
        self.controller = controller
        self.is_applying = False
        self._debounce_id = None

        self.set_default_size(350, 80)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect("destroy", Gtk.main_quit)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_border_width(12)
        self.add(vbox)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, False, False, 0)

        icon = Gtk.Image.new_from_icon_name("display-brightness-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        hbox.pack_start(icon, False, False, 0)

        adj = Gtk.Adjustment(value=50, lower=min_val, upper=max_val,
                             step_increment=step, page_increment=step * 2, page_size=0)
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self.scale.set_digits(0)
        self.scale.set_draw_value(False)
        for tick in range(min_val, max_val + 1, 25):
            self.scale.add_mark(tick, Gtk.PositionType.BOTTOM, None)
        self.scale.connect("value-changed", self._on_value_changed)
        hbox.pack_start(self.scale, True, True, 0)

        self.value_label = Gtk.Label(label="50%")
        self.value_label.set_width_chars(5)
        hbox.pack_start(self.value_label, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_homogeneous(True)
        vbox.pack_start(btn_box, False, False, 0)

        for preset in [1, 10, 25, 50, 75, 100]:
            btn = Gtk.Button(label=f"{preset}%")
            btn.connect("clicked", self._on_preset_clicked, preset)
            btn_box.pack_start(btn, True, True, 0)

        self._refresh()

    def _refresh(self):
        current = self.controller.get_brightness()
        if current is not None:
            self.is_applying = True
            self.scale.set_value(current)
            self.value_label.set_text(f"{current}%")
            self.is_applying = False

    def _on_value_changed(self, scale):
        if self.is_applying:
            return
        value = int(scale.get_value())
        self.value_label.set_text(f"{value}%")
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(150, self._apply, value)

    def _apply(self, value):
        self._debounce_id = None
        self.controller.set_brightness(value)
        return False

    def _on_preset_clicked(self, button, value):
        self.is_applying = True
        self.scale.set_value(value)
        self.value_label.set_text(f"{value}%")
        self.is_applying = False
        self.controller.set_brightness(value)


def main():
    parser = argparse.ArgumentParser(
        description="DDC Brightness Slider for XFCE4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s                          # Tray icon mode (default)
  %(prog)s --standalone             # Floating window mode
  %(prog)s --device /dev/i2c-5      # Use a different I2C bus
"""
    )
    parser.add_argument("-d", "--device", default=DEFAULT_I2C_DEV,
                        help=f"I2C device path (default: {DEFAULT_I2C_DEV})")
    parser.add_argument("-r", "--register", default=DEFAULT_DDC_REGISTER,
                        help=f"DDC register for brightness (default: {DEFAULT_DDC_REGISTER})")
    parser.add_argument("--min", type=int, default=DEFAULT_MIN_BRIGHTNESS,
                        help="Minimum brightness value (default: 0)")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_BRIGHTNESS,
                        help="Maximum brightness value (default: 100)")
    parser.add_argument("--step", type=int, default=DEFAULT_STEP,
                        help="Slider step size (default: 5)")
    parser.add_argument("--standalone", action="store_true",
                        help="Show as a floating window instead of tray icon")
    parser.add_argument("--set", type=int, metavar="VALUE",
                        help="Set brightness to VALUE and exit (no GUI)")
    parser.add_argument("--get", action="store_true",
                        help="Print current brightness and exit (no GUI)")

    args = parser.parse_args()

    controller = BrightnessController(args.device, args.register)

    if args.get:
        val = controller.get_brightness()
        if val is not None:
            print(val)
            sys.exit(0)
        else:
            print("Error: could not read brightness", file=sys.stderr)
            sys.exit(1)

    if args.set is not None:
        ok = controller.set_brightness(args.set)
        sys.exit(0 if ok else 1)

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if args.standalone:
        win = StandaloneWindow(controller, args.min, args.max, args.step)
        win.show_all()
    else:
        app = TrayApp(controller, args.min, args.max, args.step)

    Gtk.main()


if __name__ == "__main__":
    main()
