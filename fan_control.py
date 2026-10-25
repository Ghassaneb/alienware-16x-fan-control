#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk
import os, subprocess, math, json, copy

try:
    from curve_editor import CurveEditor
except ImportError:
    CurveEditor = None

try:
    # Needed so Gtk.DrawingArea hands cairo.Context to Python (python3-gi-cairo).
    gi.require_foreign('cairo')
    CAIRO_OK = True
except Exception:
    CAIRO_OK = False

HELPER    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fan_helper.sh')
ACPI_CALL = '/proc/acpi/call'
APP_DIR   = os.path.dirname(os.path.abspath(__file__))
CPU_COLOR = (0.0, 0.898, 1.0)       # #00E5FF cyan
GPU_COLOR = (1.0, 0.176, 0.471)     # #FF2D78 magenta
MAX_RPM   = 6600
HWMON_DIR = '/sys/class/hwmon'

# hwmon indices are not stable across boots, always look them up by name.
_hwmon_cache = {}


def find_hwmon(name):
    if name in _hwmon_cache:
        return _hwmon_cache[name]
    try:
        entries = sorted(os.listdir(HWMON_DIR))
    except OSError:
        entries = []
    for entry in entries:
        base = os.path.join(HWMON_DIR, entry)
        try:
            with open(os.path.join(base, 'name')) as f:
                if f.read().strip() == name:
                    _hwmon_cache[name] = base
                    return base
        except OSError:
            continue
    return None


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def temp_c(base, index):
    """Temperature in degrees Celsius from a hwmon tempN_input (millidegrees)."""
    if not base:
        return 0
    v = read_int(os.path.join(base, f'temp{index}_input'))
    return v // 1000 if v >= 1000 else v


def get_cpu_temp():
    t = temp_c(find_hwmon('alienware_wmi'), 1)
    if t:
        return t
    # fall back to the hottest coretemp package/core sensor
    coretemp = find_hwmon('coretemp')
    if coretemp:
        best = 0
        for i in range(1, 33):
            v = temp_c(coretemp, i)
            if v > best:
                best = v
        return best
    return 0


def get_gpu_temp():
    t = temp_c(find_hwmon('alienware_wmi'), 2)
    if t:
        return t
    # dell_ddv labels the discrete GPU sensor "Video"
    ddv = find_hwmon('dell_ddv')
    if ddv:
        for i in range(1, 12):
            label = os.path.join(ddv, f'temp{i}_label')
            try:
                with open(label) as f:
                    if f.read().strip().lower() == 'video':
                        return temp_c(ddv, i)
            except OSError:
                continue
    return 0


def get_fan_rpm(index):
    hw = find_hwmon('alienware_wmi') or find_hwmon('dell_ddv')
    if not hw:
        return 0
    return read_int(os.path.join(hw, f'fan{index}_input'))


def run_helper(*args):
    if not os.path.exists(ACPI_CALL):
        return False, 'acpi_call module not loaded (/proc/acpi/call missing)'
    if not os.path.exists(HELPER):
        return False, f'helper not found: {HELPER}'
    try:
        r = subprocess.run(['sudo', '-n', HELPER] + [str(a) for a in args],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, 'helper timed out'
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip() or f'exit code {r.returncode}'
        if 'password is required' in msg or 'a terminal is required' in msg:
            msg = 'sudoers rule missing: run sudo ./install.sh'
        return False, msg
    return True, ''


class HexGlyph(Gtk.DrawingArea):
    """Angular hexagon glyph for branding"""
    def __init__(self, size=22):
        super().__init__()
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        if CAIRO_OK:
            self.set_draw_func(self._draw)

    def _draw(self, widget, cr, width, height):
        if not CAIRO_OK:
            return
        cx, cy = width / 2, height / 2
        r = min(width, height) / 2 - 1.5
        # Hexagon vertices (flat-top)
        verts = []
        for i in range(6):
            a = math.pi / 3 * i
            verts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        cr.set_line_width(1.5)
        cr.set_source_rgba(0, 0.898, 1.0, 0.85)
        cr.move_to(*verts[0])
        for v in verts[1:]:
            cr.line_to(*v)
        cr.close_path()
        cr.stroke()
        # Inner chevron
        ir = r * 0.42
        cr.set_line_width(1.5)
        cr.set_source_rgba(0, 0.898, 1.0, 0.6)
        cr.move_to(cx - ir, cy - ir * 0.35)
        cr.line_to(cx, cy + ir * 0.55)
        cr.line_to(cx + ir, cy - ir * 0.35)
        cr.stroke()


class FanGauge(Gtk.DrawingArea):
    def __init__(self, color):
        super().__init__()
        self.color   = color
        self.percent = 0
        self.rpm     = 0
        self.set_size_request(170, 170)
        if CAIRO_OK:
            self.set_draw_func(self.draw)

    def set_value(self, percent, rpm):
        self.percent = max(0, min(100, percent))
        self.rpm     = rpm
        self.queue_draw()

    def draw(self, widget, cr, w, h):
        if not CAIRO_OK:
            return
        cx, cy = w/2, h/2
        r      = min(w,h)/2 - 20
        start  = math.pi * 0.75
        end    = math.pi * 2.25
        span   = end - start

        # Track ring
        cr.set_line_width(14)
        cr.set_line_cap(1)
        cr.set_source_rgba(0.11, 0.137, 0.173, 1.0)  # #1C232C
        cr.arc(cx, cy, r, start, end)
        cr.stroke()

        # Tick marks every 10%
        cr.set_line_width(1)
        for i in range(11):
            frac = i / 10
            angle = start + span * frac
            x1 = cx + (r + 9) * math.cos(angle)
            y1 = cy + (r + 9) * math.sin(angle)
            x2 = cx + (r + 15) * math.cos(angle)
            y2 = cy + (r + 15) * math.sin(angle)
            if i % 5 == 0:
                cr.set_source_rgba(0.54, 0.59, 0.65, 0.7)
            else:
                cr.set_source_rgba(0.54, 0.59, 0.65, 0.35)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()

        # Progress arc with glow
        if self.percent > 0:
            r2, g2, b2 = self.color
            arc_end = start + span * self.percent / 100
            
            # Glow layers
            for lw, alpha in [(22, 0.08), (19, 0.15), (16, 0.25)]:
                cr.set_line_width(lw)
                cr.set_source_rgba(r2, g2, b2, alpha)
                cr.arc(cx, cy, r, start, arc_end)
                cr.stroke()
            
            # Main arc
            cr.set_line_width(14)
            cr.set_source_rgba(r2, g2, b2, 0.95)
            cr.arc(cx, cy, r, start, arc_end)
            cr.stroke()

        # RPM value - monospace
        cr.select_font_face('DejaVu Sans Mono', 0, 0)
        cr.set_font_size(24)
        cr.set_source_rgba(0.91, 0.93, 0.96, 1)  # #E8EEF5
        t = f'{self.rpm:,}'
        e = cr.text_extents(t)
        cr.move_to(cx - e.width/2 - e.x_bearing, cy - 6)
        cr.show_text(t)

        # RPM label
        cr.set_font_size(10)
        cr.set_source_rgba(0.54, 0.59, 0.65, 1)  # #8A97A6
        e2 = cr.text_extents('RPM')
        cr.move_to(cx - e2.width/2 - e2.x_bearing, cy + 12)
        cr.show_text('RPM')

        # Percentage in accent
        cr.set_font_size(14)
        cr.set_source_rgb(*self.color)
        pt = f'{self.percent}%'
        e3 = cr.text_extents(pt)
        cr.move_to(cx - e3.width/2 - e3.x_bearing, cy + 33)
        cr.show_text(pt)


PRESET_ORDER     = ['auto', 'quiet', 'balanced', 'performance', 'gameshift']
EDITABLE_PRESETS = ['quiet', 'balanced', 'performance', 'gameshift']
VALID_PROFILES   = ['quiet', 'balanced', 'performance', 'gameshift']
PROFILE_LABELS   = {
    'quiet': 'Quiet', 'balanced': 'Balanced',
    'performance': 'Performance', 'gameshift': 'Game Shift',
}
PROFILE_INDEX = {v: i for i, v in enumerate(VALID_PROFILES)}

TEMP_MIN, TEMP_MAX = 30, 100
MAX_CURVE_POINTS   = 8
SAFETY_TEMP        = 90
SAFETY_STEP        = 3   # only rewrite fan speed when target moved this much

PRESET_DEFAULTS = {
    'auto': {
        'label': 'Auto', 'profile': 'balanced',
        'cpu_curve': None, 'gpu_curve': None,
    },
    'quiet': {
        'label': 'Quiet', 'profile': 'quiet',
        'cpu_curve': [[35, 15], [50, 20], [65, 35], [80, 60], [92, 90], [97, 100]],
        'gpu_curve': [[35, 15], [50, 20], [65, 35], [80, 60], [92, 90], [97, 100]],
    },
    'balanced': {
        'label': 'Balanced', 'profile': 'balanced',
        'cpu_curve': [[35, 25], [50, 35], [65, 55], [80, 80], [92, 100], [97, 100]],
        'gpu_curve': [[35, 25], [50, 35], [65, 55], [80, 80], [92, 100], [97, 100]],
    },
    'performance': {
        'label': 'Performance', 'profile': 'performance',
        'cpu_curve': [[30, 40], [45, 55], [60, 75], [75, 95], [85, 100], [97, 100]],
        'gpu_curve': [[30, 40], [45, 55], [60, 75], [75, 95], [85, 100], [97, 100]],
    },
    'gameshift': {
        'label': 'Game Shift', 'profile': 'gameshift',
        'cpu_curve': [[30, 60], [45, 80], [60, 100], [97, 100]],
        'gpu_curve': [[30, 60], [45, 80], [60, 100], [97, 100]],
    },
}

CONFIG_DIR  = os.path.join(GLib.get_user_config_dir(), 'alienware-16x-fan-control')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'presets.json')
STATE_PATH  = os.path.join(CONFIG_DIR, 'state.json')


def clamp_curve(points):
    """Validate/sort a temperature->speed curve, or None if unusable."""
    if not isinstance(points, list):
        return None
    out = []
    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            continue
        t, p = pt
        if isinstance(t, bool) or isinstance(p, bool):
            continue
        if not isinstance(t, (int, float)) or not isinstance(p, (int, float)):
            continue
        t = max(TEMP_MIN, min(TEMP_MAX, int(round(t))))
        p = max(0, min(100, int(round(p))))
        if out and t <= out[-1][0]:
            t = out[-1][0] + 1
        if t > TEMP_MAX:
            continue
        out.append([t, p])
    return out if len(out) >= 2 else None


def interp_curve(points, temp):
    """Fan percent for a temperature, linear between control points."""
    if not points:
        return 0
    if temp <= points[0][0]:
        return points[0][1]
    if temp >= points[-1][0]:
        return points[-1][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return p1
            return int(round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0)))
    return points[-1][1]


def preset_tooltip(p):
    prof = PROFILE_LABELS.get(p['profile'], 'Balanced')
    c = p.get('cpu_curve')
    if not c:
        return f'Firmware automatic control ({prof} profile)'
    return (f"CPU curve {c[0][0]}–{c[-1][0]} °C → {c[0][1]}–{c[-1][1]}%"
            f" · enforced live · {prof} profile")


def format_preset_summary(label, p):
    prof = PROFILE_LABELS.get(p['profile'], 'Balanced')
    c = p.get('cpu_curve')
    if not c:
        return f'{label} — firmware automatic ({prof} profile)'
    g = p.get('gpu_curve') or c
    if g == c:
        return (f'{label} — curve {c[0][0]}–{c[-1][0]} °C · '
                f'{c[0][1]}→{c[-1][1]}% · {prof} profile')
    return (f'{label} — CPU {c[0][1]}→{c[-1][1]}% · GPU {g[0][1]}→{g[-1][1]}%'
            f' · {prof} profile')


def load_presets():
    presets = copy.deepcopy(PRESET_DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return presets
    if not isinstance(data, dict):
        return presets
    for key in EDITABLE_PRESETS:
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        profile = entry.get('profile')
        if isinstance(profile, str) and profile in VALID_PROFILES:
            presets[key]['profile'] = profile
        for which in ('cpu', 'gpu'):
            raw = entry.get(f'{which}_curve')
            if raw is None and isinstance(entry.get(which), (int, float)) \
                    and not isinstance(entry.get(which), bool):
                # v1 config: a single fixed percentage, keep it flat
                pct = max(0, min(100, int(round(entry[which]))))
                raw = [[TEMP_MIN, pct], [TEMP_MAX, pct]]
            curve = clamp_curve(raw)
            if curve:
                presets[key][f'{which}_curve'] = curve
    return presets


def save_presets_to_file(presets):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = {}
    for key in EDITABLE_PRESETS:
        p = presets[key]
        data[key] = {
            'profile': p['profile'],
            'cpu_curve': p['cpu_curve'],
            'gpu_curve': p['gpu_curve'],
        }
    tmp = CONFIG_PATH + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


def load_last_preset():
    try:
        with open(STATE_PATH) as f:
            key = json.load(f).get('last_preset')
    except (OSError, ValueError, AttributeError):
        return 'auto'
    return key if key in PRESET_ORDER else 'auto'


def save_last_preset(key):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump({'last_preset': key}, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


class FanApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id='com.hbwal.fancontrol')
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self.connect('activate', self.on_activate)
        self.manual_mode = False
        self._updating = False
        self._pending = {}
        self.presets = load_presets()
        self.current_preset = load_last_preset()
        self.curve_active = False
        self.last_applied = ()
        self.connect('shutdown', self.on_shutdown)

    def _load_css(self):
        css_path = os.path.join(APP_DIR, 'style.css')
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(css_path)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
        except Exception:
            pass

    def on_activate(self, app):
        # GApplication routes a second launch into this instance; present the
        # existing window instead of building a duplicate one.
        if getattr(self, 'win', None) is not None:
            self.win.present()
            return
        self._load_css()
        self.win = Adw.ApplicationWindow(application=app)
        self.win.set_title('Fan Control')
        self.win.set_default_size(500, 600)
        self.win.set_resizable(False)

        tb = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_title(False)
        tb.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        tb.add_top_bar(self.banner)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_start(16)
        root.set_margin_end(16)
        root.set_margin_top(10)
        root.set_margin_bottom(16)

        # Branding row
        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        brand.set_margin_bottom(4)
        glyph = HexGlyph(22)
        brand.append(glyph)
        tstack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        tstack.set_vexpand(True)
        tstack.set_valign(Gtk.Align.CENTER)
        t1 = Gtk.Label(label='ALIENWARE')
        t1.set_halign(Gtk.Align.START)
        t1.add_css_class('aw-brand')
        t2 = Gtk.Label(label='16 AURORA  ·  FAN CONTROL')
        t2.set_halign(Gtk.Align.START)
        t2.add_css_class('aw-subbrand')
        tstack.append(t1)
        tstack.append(t2)
        brand.append(tstack)
        self.mode_badge = Gtk.Label(label='Auto')
        self.mode_badge.add_css_class('aw-tag')
        self.mode_badge.set_valign(Gtk.Align.CENTER)
        brand.append(self.mode_badge)
        root.append(brand)

        # Gauges row
        gbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for side in ('cpu', 'gpu'):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class('aw-card')
            card.set_hexpand(True)

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            hdr.set_margin_start(12)
            hdr.set_margin_end(12)
            hdr.set_margin_top(12)

            lbl = Gtk.Label(label=('CPU FAN' if side=='cpu' else 'GPU FAN'))
            lbl.set_halign(Gtk.Align.START)
            lbl.set_hexpand(True)
            lbl.add_css_class('aw-label')

            badge = Gtk.Label(label='--°C')
            badge.add_css_class('aw-tag')
            badge.add_css_class('success')

            hdr.append(lbl)
            hdr.append(badge)
            card.append(hdr)

            gauge = FanGauge(CPU_COLOR if side=='cpu' else GPU_COLOR)
            gauge.set_halign(Gtk.Align.CENTER)
            card.append(gauge)

            slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            slider.set_value(50)
            slider.set_draw_value(False)
            slider.set_margin_start(12)
            slider.set_margin_end(12)
            slider.set_margin_bottom(12)
            slider.add_mark(0, Gtk.PositionType.BOTTOM, '0%')
            slider.add_mark(50, Gtk.PositionType.BOTTOM, '50%')
            slider.add_mark(100, Gtk.PositionType.BOTTOM, '100%')
            slider.connect('value-changed', self.on_slider, side)
            slider.add_css_class('aw-slider-cpu' if side=='cpu' else 'aw-slider-gpu')
            card.append(slider)

            gbox.append(card)

            if side == 'cpu':
                self.cpu_badge  = badge
                self.cpu_gauge  = gauge
                self.cpu_slider = slider
            else:
                self.gpu_badge  = badge
                self.gpu_gauge  = gauge
                self.gpu_slider = slider

        root.append(gbox)

        # Presets
        pcard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pcard.add_css_class('aw-card')
        phdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        phdr.set_margin_start(12)
        phdr.set_margin_end(12)
        phdr.set_margin_top(12)
        pl = Gtk.Label(label='PRESETS')
        pl.set_halign(Gtk.Align.START)
        pl.set_hexpand(True)
        pl.add_css_class('aw-label')
        edit_btn = Gtk.Button.new_from_icon_name('document-edit-symbolic')
        edit_btn.add_css_class('flat')
        edit_btn.add_css_class('aw-edit')
        edit_btn.set_tooltip_text('Edit presets')
        edit_btn.connect('clicked', self.open_edit_dialog)
        phdr.append(pl)
        phdr.append(edit_btn)
        pcard.append(phdr)

        prow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        prow.set_homogeneous(True)
        prow.set_margin_start(12)
        prow.set_margin_end(12)
        prow.set_margin_bottom(8)
        prow.add_css_class('aw-segmented')

        self.preset_btns = {}
        for key in PRESET_ORDER:
            p = self.presets[key]
            btn = Gtk.Button(label=p['label'])
            btn.set_tooltip_text(preset_tooltip(p))
            btn.connect('clicked', self.on_preset, key)
            if key == self.current_preset:
                btn.add_css_class('suggested-action')
            prow.append(btn)
            self.preset_btns[key] = btn

        pcard.append(prow)

        self.preset_summary = Gtk.Label(label='')
        self.preset_summary.add_css_class('aw-label')
        self.preset_summary.set_margin_start(12)
        self.preset_summary.set_margin_end(12)
        self.preset_summary.set_margin_bottom(12)
        self.preset_summary.set_halign(Gtk.Align.START)
        self.preset_summary.set_wrap(True)
        pcard.append(self.preset_summary)

        root.append(pcard)

        tb.set_content(root)
        self.win.set_content(tb)
        self.win.present()

        self.update_preset_tooltips()
        self.update_preset_summary()
        self.check_environment()
        self.restore_last_preset()
        self.update_sensors()
        GLib.timeout_add(2000, self.update_sensors)

    def check_environment(self):
        if not CAIRO_OK:
            self.show_error('Missing python3-gi-cairo — gauge rendering disabled')
        if not os.path.exists(ACPI_CALL):
            self.show_error('acpi_call is not loaded — fan control unavailable')
            return
        ok, err = run_helper('probe')
        if not ok:
            self.show_error(err)

    def show_error(self, msg):
        self.banner.set_title(msg)
        self.banner.set_revealed(True)

    def restore_last_preset(self):
        """Re-apply the preset that was active when the app last closed."""
        key = self.current_preset
        p = self.presets.get(key)
        if not p:
            self.current_preset = 'auto'
            return
        self.mode_badge.set_label(p['label'])
        self.update_preset_summary()
        if not p.get('cpu_curve'):
            return
        ok, err = run_helper('profile', p['profile'])
        if not ok:
            self.show_error(err)
            return
        self.curve_active = True
        self.apply_curve(force=True)

    def set_manual(self):
        for b in self.preset_btns.values():
            b.remove_css_class('suggested-action')
        self.mode_badge.set_label('Manual')
        self.manual_mode = True
        self.curve_active = False
        self.update_preset_summary()

    def on_slider(self, scale, which):
        if self._updating:
            return
        self.set_manual()
        pct = int(round(scale.get_value()))
        # Debounce: a drag fires dozens of value-changed signals, only the
        # last one should reach the ACPI call.
        if self._pending.get(which):
            GLib.source_remove(self._pending[which])
        self._pending[which] = GLib.timeout_add(180, self._apply_fan, which, pct)

    def _apply_fan(self, which, pct):
        self._pending[which] = 0
        ok, err = run_helper(which, pct)
        if not ok:
            self.show_error(err)
        else:
            self.update_preset_summary()
        return False

    def on_preset(self, btn, key):
        for b in self.preset_btns.values():
            b.remove_css_class('suggested-action')
        btn.add_css_class('suggested-action')
        self.manual_mode = False
        self.current_preset = key
        self.last_applied = ()
        save_last_preset(key)

        p = self.presets[key]
        self.mode_badge.set_label(p['label'])
        self.update_preset_summary()
        ok, err = run_helper('profile', p['profile'])
        if not ok:
            self.show_error(err)
            return

        # Auto hands control back to the firmware; every other preset is a
        # temperature curve enforced by the sensor timer.
        self.curve_active = p.get('cpu_curve') is not None
        if self.curve_active:
            self.apply_curve(force=True)

    def apply_curve(self, force=False):
        """Enforce the active preset's curve for the current temperatures."""
        p = self.presets.get(self.current_preset)
        if not p or not p.get('cpu_curve'):
            return
        cpu_temp = get_cpu_temp()
        gpu_temp = get_gpu_temp()
        cpu = interp_curve(p['cpu_curve'], cpu_temp or TEMP_MIN)
        gpu = interp_curve(p['gpu_curve'], gpu_temp or TEMP_MIN)

        # Safety net: never let a hand-drawn curve starve the fans when hot.
        if max(cpu_temp, gpu_temp) >= SAFETY_TEMP:
            cpu = gpu = 100

        last = self.last_applied
        if not force and last and abs(cpu - last[0]) < SAFETY_STEP \
                and abs(gpu - last[1]) < SAFETY_STEP:
            return
        ok, err = run_helper('both', cpu, gpu)
        if ok:
            self.last_applied = (cpu, gpu)
        else:
            self.show_error(err)

    def update_badge(self, badge, temp):
        badge.remove_css_class('success')
        badge.remove_css_class('warning')
        badge.remove_css_class('error')
        if temp < 70:   badge.add_css_class('success')
        elif temp < 85: badge.add_css_class('warning')
        else:           badge.add_css_class('error')
        badge.set_label(f'{temp}°C' if temp else '--°C')

    def update_sensors(self):
        cpu_rpm  = get_fan_rpm(1)
        gpu_rpm  = get_fan_rpm(2)
        cpu_temp = get_cpu_temp()
        gpu_temp = get_gpu_temp()

        if self.manual_mode:
            cpu_pct = int(self.cpu_slider.get_value())
            gpu_pct = int(self.gpu_slider.get_value())
        else:
            cpu_pct = min(100, int(cpu_rpm / MAX_RPM * 100))
            gpu_pct = min(100, int(gpu_rpm / MAX_RPM * 100))
            # Reflect actual RPM on sliders; guarded so on_slider doesn't fire
            self._updating = True
            try:
                if int(round(self.cpu_slider.get_value())) != cpu_pct:
                    self.cpu_slider.set_value(cpu_pct)
                if int(round(self.gpu_slider.get_value())) != gpu_pct:
                    self.gpu_slider.set_value(gpu_pct)
            finally:
                self._updating = False

        self.cpu_gauge.set_value(cpu_pct, cpu_rpm)
        self.gpu_gauge.set_value(gpu_pct, gpu_rpm)
        self.update_badge(self.cpu_badge, cpu_temp)
        self.update_badge(self.gpu_badge, gpu_temp)

        if self.curve_active and not self.manual_mode:
            self.apply_curve()
        return True

    def update_preset_tooltips(self):
        for key, btn in self.preset_btns.items():
            p = self.presets.get(key, PRESET_DEFAULTS[key])
            btn.set_tooltip_text(preset_tooltip(p))

    def update_preset_summary(self):
        if self.manual_mode:
            self.preset_summary.set_label(
                f'Manual — CPU {int(round(self.cpu_slider.get_value()))}%'
                f' · GPU {int(round(self.gpu_slider.get_value()))}%')
            return
        key = self.current_preset
        p = self.presets.get(key, PRESET_DEFAULTS.get(key, PRESET_DEFAULTS['balanced']))
        self.preset_summary.set_label(format_preset_summary(p['label'], p))

    def open_edit_dialog(self, btn):
        if CurveEditor is None:
            self.show_error('curve_editor.py is missing from the app folder')
            return

        dialog = Adw.Dialog()
        dialog.set_title('Edit preset curves')
        dialog.set_content_width(560)
        dialog.set_content_height(720)

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_show_title(False)
        tv.add_top_bar(hb)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(10)
        content.set_margin_bottom(16)

        hint = Gtk.Label(label='Fan speed follows temperature. Drag points, click to add, double-click to remove.')
        hint.add_css_class('aw-label')
        hint.set_wrap(True)
        hint.set_halign(Gtk.Align.START)
        content.append(hint)

        switcher = Gtk.StackSwitcher(stack=stack)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.add_css_class('aw-segmented')
        switcher.set_margin_top(2)
        switcher.set_margin_bottom(4)
        content.append(switcher)

        widgets = {}
        for key in EDITABLE_PRESETS:
            preset = self.presets[key]
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

            prof_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            prof_lbl = Gtk.Label(label='THERMAL PROFILE')
            prof_lbl.set_halign(Gtk.Align.START)
            prof_lbl.set_hexpand(True)
            prof_lbl.add_css_class('aw-label')
            prof_model = Gtk.StringList.new(['Quiet', 'Balanced', 'Performance', 'Game Shift'])
            prof_drop = Gtk.DropDown(model=prof_model)
            prof_drop.set_selected(PROFILE_INDEX.get(preset['profile'], 1))
            prof_drop.set_valign(Gtk.Align.CENTER)
            prof_row.append(prof_lbl)
            prof_row.append(prof_drop)
            page.append(prof_row)

            cpu_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            cpu_card.add_css_class('aw-chart')
            cpu_lbl = Gtk.Label(label='CPU FAN')
            cpu_lbl.set_halign(Gtk.Align.START)
            cpu_lbl.add_css_class('aw-label')
            cpu_lbl.set_margin_start(8)
            cpu_lbl.set_margin_top(6)
            cpu_ed = CurveEditor(preset['cpu_curve'], CPU_COLOR)
            cpu_card.append(cpu_lbl)
            cpu_card.append(cpu_ed)
            page.append(cpu_card)

            gpu_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            gpu_card.add_css_class('aw-chart')
            gpu_lbl = Gtk.Label(label='GPU FAN')
            gpu_lbl.set_halign(Gtk.Align.START)
            gpu_lbl.add_css_class('aw-label')
            gpu_lbl.set_margin_start(8)
            gpu_lbl.set_margin_top(6)
            gpu_ed = CurveEditor(preset['gpu_curve'], GPU_COLOR)
            gpu_card.append(gpu_lbl)
            gpu_card.append(gpu_ed)
            page.append(gpu_card)

            reset_btn = Gtk.Button(label='Reset to defaults')
            reset_btn.add_css_class('flat')
            reset_btn.add_css_class('aw-reset')
            reset_btn.set_halign(Gtk.Align.END)

            def on_reset(b, k=key, ce=cpu_ed, ge=gpu_ed, drop=prof_drop):
                d = PRESET_DEFAULTS[k]
                ce.set_points(d['cpu_curve'])
                ge.set_points(d['gpu_curve'])
                drop.set_selected(PROFILE_INDEX.get(d['profile'], 1))

            reset_btn.connect('clicked', on_reset)
            page.append(reset_btn)

            stack.add_titled(page, key, preset['label'])
            widgets[key] = (cpu_ed, gpu_ed, prof_drop)

        content.append(stack)

        note = Gtk.Label(label='Auto follows firmware control and has no curve.')
        note.add_css_class('aw-label')
        note.set_halign(Gtk.Align.START)
        note.set_wrap(True)
        content.append(note)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_top(4)
        cancel_btn = Gtk.Button(label='Cancel')
        cancel_btn.add_css_class('flat')
        cancel_btn.connect('clicked', lambda b: dialog.close())
        save_btn = Gtk.Button(label='Save')
        save_btn.add_css_class('suggested-action')
        save_btn.connect('clicked', lambda b: self._on_edit_save(dialog, widgets))
        footer.append(cancel_btn)
        footer.append(save_btn)
        content.append(footer)

        sw.set_child(content)
        tv.set_content(sw)
        dialog.set_child(tv)
        dialog.present(self.win)

    def _on_edit_save(self, dialog, widgets):
        new_presets = {'auto': copy.deepcopy(self.presets['auto'])}
        for key in EDITABLE_PRESETS:
            cpu_ed, gpu_ed, prof_drop = widgets[key]
            cpu_curve = clamp_curve(cpu_ed.get_points())
            gpu_curve = clamp_curve(gpu_ed.get_points())
            if not cpu_curve or not gpu_curve:
                self.show_error('A curve needs at least two valid points')
                return
            idx = prof_drop.get_selected()
            profile = VALID_PROFILES[idx] if 0 <= idx < len(VALID_PROFILES) else 'balanced'
            new_presets[key] = {
                'label': self.presets[key]['label'],
                'profile': profile,
                'cpu_curve': cpu_curve,
                'gpu_curve': gpu_curve,
            }
        self.presets = new_presets
        save_presets_to_file(self.presets)
        self.update_preset_tooltips()
        self.update_preset_summary()
        if self.curve_active and self.current_preset in EDITABLE_PRESETS:
            self.last_applied = ()
            self.apply_curve(force=True)
        dialog.close()

    def on_shutdown(self, app):
        # Hand the fans back to firmware control when the app goes away.
        if self.manual_mode or self.curve_active:
            run_helper('profile', 'balanced')


if __name__ == '__main__':
    FanApp().run()
