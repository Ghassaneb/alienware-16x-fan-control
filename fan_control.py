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
    import cairo
    CAIRO_OK = True
except Exception:
    CAIRO_OK = False

HELPER    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fan_helper.sh')
ACPI_CALL = '/proc/acpi/call'
APP_DIR   = os.path.dirname(os.path.abspath(__file__))
CPU_COLOR = (0.0, 0.898, 1.0)       # #00E5FF cyan
GPU_COLOR = (1.0, 0.176, 0.471)     # #FF2D78 magenta
MAX_RPM   = 6600


def _rgb_to_hsl(r, g, b):
    """Convert RGB (0-1 floats) to HSL (H degrees, S 0-1, L 0-1)."""
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        return (0.0, 0.0, l)
    d = mx - mn
    s = d / (1.0 - abs(2.0 * l - 1.0))
    if mx == r:   h = ((g - b) / d) % 6.0
    elif mx == g: h = (b - r) / d + 2.0
    else:         h = (r - g) / d + 4.0
    return (h * 60.0, s, l)


def _hsl_to_rgb(h, s, l):
    """Convert HSL (H degrees, S 0-1, L 0-1) to RGB (0-1 floats)."""
    h = h % 360.0
    c = (1.0 - abs(2.0 * l - 1.0)) * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = l - c / 2.0
    if   h < 60.0:  r1, g1, b1 = c, x, 0.0
    elif h < 120.0: r1, g1, b1 = x, c, 0.0
    elif h < 180.0: r1, g1, b1 = 0.0, c, x
    elif h < 240.0: r1, g1, b1 = 0.0, x, c
    elif h < 300.0: r1, g1, b1 = x, 0.0, c
    else:           r1, g1, b1 = c, 0.0, x
    return (r1 + m, g1 + m, b1 + m)


def _lerp(a, b, t):
    """Linear interpolation between a and b."""
    return a + (b - a) * max(0.0, min(1.0, t))


def gauge_color_at(identity, percent):
    """Intensity-driven colour: keep identity hue, vary brightness/saturation.

    0-50%:   dimmed/desaturated (still clearly cyan/magenta)
    50-80%:  ramp up to full identity colour
    80-100%: white-hot (lighten toward white ~15%), full opacity
    """
    p = max(0.0, min(100.0, float(percent)))
    # Convert identity to HSL
    h, s_id, l_id = _rgb_to_hsl(*identity)
    
    if p <= 50.0:
        # 0-50%: dimmed/desaturated, ramp up slightly
        t = p / 50.0
        s = _lerp(0.60, 0.80, t)
        l = _lerp(0.30, 0.45, t)
    elif p <= 80.0:
        # 50-80%: ramp to full identity
        t = (p - 50.0) / 30.0
        s = _lerp(0.80, s_id, t)
        l = _lerp(0.45, l_id, t)
    else:
        # 80-100%: white-hot (lighten toward white)
        t = (p - 80.0) / 20.0
        s = 1.0
        l = _lerp(l_id, min(0.95, l_id + 0.15), t)
    
    return _hsl_to_rgb(h, s, l)
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
        cr.new_path()
        cx, cy = w/2, h/2

        # Geometry constants
        TICK_OUTER_R = 68
        TRACK_OUTER_R = 62
        TRACK_INNER_R = 52
        ARC_R = 57
        LABEL_R = 78

        start = math.pi * 0.75      # 135° lower-left
        end = math.pi * 2.25        # 405° lower-right
        span = end - start          # 270°

        # ── 1. Track: 270° arc with clean butt ends (not a full circle) ──
        track_r = (TRACK_OUTER_R + TRACK_INNER_R) / 2   # 57
        cr.set_line_width(TRACK_OUTER_R - TRACK_INNER_R) # 10
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.set_source_rgba(0.071, 0.086, 0.106, 1.0)    # #12161B
        cr.arc(cx, cy, track_r, start, end)
        cr.stroke()

        # Inner hairline (270°)
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.arc(cx, cy, TRACK_INNER_R, start, end)
        cr.stroke()

        # Outer bezel (270°)
        cr.set_source_rgba(1, 1, 1, 0.03)
        cr.arc(cx, cy, TRACK_OUTER_R, start, end)
        cr.stroke()

        # ── Track terminals: 1.2px radial lines at each end (135°/405°) ──
        cr.set_line_width(1.2)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.set_source_rgba(0.54, 0.59, 0.65, 0.35)  # dim steel
        for term_angle in (start, end):
            x1 = cx + TRACK_INNER_R * math.cos(term_angle)
            y1 = cy + TRACK_INNER_R * math.sin(term_angle)
            x2 = cx + TRACK_OUTER_R * math.cos(term_angle)
            y2 = cy + TRACK_OUTER_R * math.sin(term_angle)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()

        # ── 2. Tick ring: 31 positions, labels anchored outward ──
        cr.set_line_width(1)
        for i in range(31):
            rpm = i * 220
            angle = start + span * i / 30
            is_major = (i % 5 == 0)

            tick_len = 6 if is_major else 3
            x1 = cx + TICK_OUTER_R * math.cos(angle)
            y1 = cy + TICK_OUTER_R * math.sin(angle)
            x2 = cx + (TICK_OUTER_R - tick_len) * math.cos(angle)
            y2 = cy + (TICK_OUTER_R - tick_len) * math.sin(angle)

            cr.set_source_rgba(0.54, 0.59, 0.65, 0.75 if is_major else 0.40)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.stroke()

            # Labels at 0, 2.2k, 4.4k, 6.6k — anchored outward to clear arc
            if i in (0, 10, 20, 30):
                label = "0" if rpm == 0 else f"{rpm/1000:.1f}k"
                lx = cx + LABEL_R * math.cos(angle)
                ly = cy + LABEL_R * math.sin(angle)
                cr.select_font_face('DejaVu Sans',
                                    cairo.FONT_SLANT_NORMAL,
                                    cairo.FONT_WEIGHT_NORMAL)
                cr.set_font_size(8.5)
                extents = cr.text_extents(label)
                cr.set_source_rgba(0.54, 0.59, 0.65, 0.8)
                if i in (0, 10):          # left side → right-aligned
                    ox = (lx - 3) - extents.x_bearing - extents.width
                else:                      # right side → left-aligned
                    ox = (lx + 3) - extents.x_bearing
                cr.move_to(ox, ly + extents.height / 2)
                cr.show_text(label)

        # ── 3. Arc: 2° segments with 0.4° overlap, OPAQUE color-blend gradient ──
        head_color = gauge_color_at(self.color, self.percent)
        if self.percent > 0:
            arc_end = start + span * self.percent / 100
            drawn_span = arc_end - start

            segment_span = math.radians(2)
            overlap = math.radians(0.4)
            track_col = (0.071, 0.086, 0.106)  # #12161B
            angle = start
            while angle < arc_end:
                seg_end = min(angle + segment_span, arc_end)
                seg_centre = (angle + seg_end) / 2.0
                t = (seg_centre - start) / drawn_span if drawn_span > 0 else 0

                # Blend factor: 0.55 at tail, 1.0 at head
                blend = 0.55 + 0.45 * t

                # Color = lerp(track, head_color, blend) — always opaque
                r = track_col[0] + (head_color[0] - track_col[0]) * blend
                g = track_col[1] + (head_color[1] - track_col[1]) * blend
                b = track_col[2] + (head_color[2] - track_col[2]) * blend

                # White-lightening at high load (last 15% of arc)
                if self.percent > 85 and t > 0.85:
                    lt = (t - 0.85) / 0.15 * 0.1
                    r = r + (1.0 - r) * lt
                    g = g + (1.0 - g) * lt
                    b = b + (1.0 - b) * lt

                # Overlap each segment by 0.4° on each side (harmless when opaque)
                draw_start = max(start, angle - overlap)
                draw_end = min(arc_end, seg_end + overlap)

                cr.set_line_width(10)
                cr.set_line_cap(cairo.LINE_CAP_BUTT)
                cr.set_source_rgba(r, g, b, 1.0)  # FULLY OPAQUE — no ribbing
                cr.new_path()
                cr.arc(cx, cy, ARC_R, draw_start, draw_end)
                cr.stroke()

                angle = seg_end

            # Localized glow at head: last 12% of drawn arc, two thin layers
            glow_start = arc_end - drawn_span * 0.12
            if glow_start > start:
                gi = self.percent / 100.0
                for lw, ab in [(13, 0.12), (16, 0.06)]:
                    cr.set_line_width(lw)
                    cr.set_line_cap(cairo.LINE_CAP_ROUND)
                    cr.set_source_rgba(*head_color, ab * gi)
                    cr.new_path()
                    cr.arc(cx, cy, ARC_R, glow_start, arc_end)
                    cr.stroke()

        # ── 4. Tip marker: outside the band at ARC_R+6, only when running ──
        if self.percent > 0:
            tip_angle = start + span * self.percent / 100
            tip_r = ARC_R + 6  # Just outside 10px track, inside tick ring
            tx = cx + tip_r * math.cos(tip_angle)
            ty = cy + tip_r * math.sin(tip_angle)
            # Dark outline (1px ring around 2.1px dot = 3.1px total radius)
            cr.set_source_rgba(0.031, 0.035, 0.043, 0.9)  # rgba(8,9,11,0.9)
            cr.new_path()
            cr.arc(tx, ty, 3.1, 0, 2 * math.pi)
            cr.fill()
            # Dot filled with identity colour (near-white at load > 85%)
            if self.percent > 85:
                lt = 0.1
                dot_r = self.color[0] + (1.0 - self.color[0]) * lt
                dot_g = self.color[1] + (1.0 - self.color[1]) * lt
                dot_b = self.color[2] + (1.0 - self.color[2]) * lt
                cr.set_source_rgba(dot_r, dot_g, dot_b, 1.0)
            else:
                cr.set_source_rgba(*self.color, 1.0)
            cr.new_path()
            cr.arc(tx, ty, 2.1, 0, 2 * math.pi)
            cr.fill()

        # ── 5. Text hierarchy ──
        # RPM value (hero)
        cr.select_font_face('DejaVu Sans Mono',
                            cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(26)
        cr.set_source_rgba(0.91, 0.93, 0.96, 1)   # #E8EEF5
        t = f'{self.rpm:,}'
        extents = cr.text_extents(t)
        cr.move_to(cx - extents.width/2 - extents.x_bearing, cy - 8)
        cr.show_text(t)

        # "RPM" label (small caps, dim steel)
        cr.set_font_size(9)
        cr.set_source_rgba(0.54, 0.59, 0.65, 1)   # #8A97A6
        extents = cr.text_extents('RPM')
        cr.move_to(cx - extents.width/2 - extents.x_bearing, cy + 8)
        cr.show_text('RPM')

        # Percentage in identity colour (constant brightness, >= 4.5:1 on
        # every preset background — arc carries the load intensity instead)
        cr.set_font_size(11)
        cr.set_source_rgb(*self.color)
        pt = f'{self.percent}%'
        extents = cr.text_extents(pt)
        cr.move_to(cx - extents.width/2 - extents.x_bearing, cy + 22)
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


def curve_desc(curve):
    return f'{curve[0][0]}–{curve[-1][0]} °C, {curve[0][1]}–{curve[-1][1]}%'


def preset_tooltip(p):
    prof = PROFILE_LABELS.get(p['profile'], 'Balanced')
    c = p.get('cpu_curve')
    if not c:
        return f'Firmware control · {prof} profile'
    g = p.get('gpu_curve') or c
    if g == c:
        return f'Curve {curve_desc(c)} · enforced live · {prof} profile'
    return (f'CPU curve {curve_desc(c)} · GPU curve {curve_desc(g)}'
            f' · enforced live · {prof} profile')


def format_preset_summary(p):
    prof = PROFILE_LABELS.get(p['profile'], 'Balanced')
    c = p.get('cpu_curve')
    if not c:
        return f'Firmware control · {prof} profile'
    g = p.get('gpu_curve') or c
    if g == c:
        return f'Curve {curve_desc(c)} · {prof} profile'
    c_ends = (c[0][0], c[-1][0], c[0][1], c[-1][1])
    g_ends = (g[0][0], g[-1][0], g[0][1], g[-1][1])
    if c_ends == g_ends:
        return f'Curve {curve_desc(c)} · {prof} profile'
    if (c[0][0], c[-1][0]) == (g[0][0], g[-1][0]):
        return (f'Curve {c[0][0]}–{c[-1][0]} °C · CPU {c[0][1]}–{c[-1][1]}%'
                f' · GPU {g[0][1]}–{g[-1][1]}% · {prof} profile')
    return f'CPU {curve_desc(c)} · GPU {curve_desc(g)} · {prof} profile'


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

    THEME_CLASSES = [f'theme-{k}' for k in PRESET_ORDER]

    def apply_theme(self, key):
        """Swap the theme-* CSS class on the application window."""
        if not hasattr(self, 'win') or self.win is None:
            return
        target = f'theme-{key}'
        for cls in self.THEME_CLASSES:
            if cls == target:
                self.win.add_css_class(cls)
            else:
                self.win.remove_css_class(cls)

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
        tstack.set_hexpand(True)
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

        # Set initial slider intensity levels (sliders default to value 50 → level 2)
        self._update_slider_level(self.cpu_slider, 50)
        self._update_slider_level(self.gpu_slider, 50)

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
            self.apply_theme('auto')
            return
        self.mode_badge.set_label(p['label'])
        self.update_preset_summary()
        self.apply_theme(key)
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
        self.apply_theme('auto')

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
        self.apply_theme(key)

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

    def _update_slider_level(self, slider, pct):
        """Update the intensity level class on a slider based on percentage."""
        level = min(4, int(pct) // 20)
        # Remove old level classes
        for lvl in range(5):
            slider.remove_css_class(f'lvl-{lvl}')
        # Add new level class
        slider.add_css_class(f'lvl-{level}')

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
        self._update_slider_level(self.cpu_slider, cpu_pct)
        self._update_slider_level(self.gpu_slider, gpu_pct)
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
        self.preset_summary.set_label(format_preset_summary(p))

    def open_edit_dialog(self, btn):
        if CurveEditor is None:
            self.show_error('curve_editor.py is missing from the app folder')
            return

        dialog = Adw.Dialog()
        dialog.set_title('Edit preset curves')
        dialog.set_content_width(560)
        dialog.set_content_height(680)

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        dlg_title = Gtk.Label(label='Edit preset curves')
        dlg_title.add_css_class('heading')
        dlg_title.set_hexpand(True)
        dlg_title.set_halign(Gtk.Align.CENTER)
        hb.set_title_widget(dlg_title)
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

        hint = Gtk.Label(label='Drag points · click to add · double-click to remove.')
        hint.add_css_class('aw-label')
        hint.set_wrap(True)
        hint.set_xalign(0)
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

        note = Gtk.Label(label='Auto follows firmware control.')
        note.add_css_class('aw-label')
        note.set_xalign(0)
        note.set_wrap(True)
        content.append(note)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_halign(Gtk.Align.END)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        footer.set_margin_bottom(12)
        footer.set_margin_top(8)
        cancel_btn = Gtk.Button(label='Cancel')
        cancel_btn.add_css_class('flat')
        cancel_btn.connect('clicked', lambda b: dialog.close())
        save_btn = Gtk.Button(label='Save')
        save_btn.add_css_class('suggested-action')
        save_btn.connect('clicked', lambda b: self._on_edit_save(dialog, widgets))
        footer.append(cancel_btn)
        footer.append(save_btn)
        tv.add_bottom_bar(footer)

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
