"""Fan curve editor widget for GTK4/libadwaita"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GObject
import cairo
import math


class CurveEditor(Gtk.DrawingArea):
    """Interactive fan curve editor with draggable control points"""
    
    # GObject signal for change notifications
    __gsignals__ = {
        'changed': (GObject.SignalFlags.RUN_FIRST, None, ())
    }
    
    def __init__(self, points, color=(0.216, 0.540, 0.867), temp_min=30, temp_max=100):
        """Initialize curve editor
        
        Args:
            points: List of [temp, percent] pairs
            color: RGB tuple for accent color (0-1 range)
            temp_min: Minimum temperature (default 30)
            temp_max: Maximum temperature (default 100)
        """
        super().__init__()
        
        self.color = color
        self.temp_min = temp_min
        self.temp_max = temp_max
        
        # Store points as list of [temp, percent]
        self.points = []
        if points:
            for p in points:
                temp = int(max(temp_min, min(temp_max, p[0])))
                percent = int(max(0, min(100, p[1])))
                self.points.append([temp, percent])
            self.points.sort(key=lambda p: p[0])
        else:
            # Default fallback
            self.points = [[temp_min, 0], [temp_max, 100]]
        
        # Dragging state
        self.dragging_index = -1
        self.drag_start_x = 0
        self.drag_start_y = 0
        
        # Hover state
        self.hover_index = -1
        
        # Sizing
        self.set_size_request(420, 160)
        self.set_hexpand(True)
        
        # Setup gestures
        self._setup_gestures()
        
        # Connect draw function
        self.set_draw_func(self._draw)
    
    def _setup_gestures(self):
        """Setup mouse interaction gestures"""
        # Drag gesture for moving points
        self.drag_gesture = Gtk.GestureDrag.new()
        self.drag_gesture.connect('drag-begin', self._on_drag_begin)
        self.drag_gesture.connect('drag-update', self._on_drag_update)
        self.drag_gesture.connect('drag-end', self._on_drag_end)
        self.add_controller(self.drag_gesture)
        
        # Click gesture for add/delete
        self.click_gesture = Gtk.GestureClick.new()
        self.click_gesture.connect('pressed', self._on_click)
        self.add_controller(self.click_gesture)
        
        # Motion controller for hover cursor
        self.motion_controller = Gtk.EventControllerMotion.new()
        self.motion_controller.connect('motion', self._on_motion)
        self.add_controller(self.motion_controller)
    
    def get_points(self):
        """Get current points as list of [temp, percent] pairs, sorted by temp"""
        return [p.copy() for p in self.points]
    
    def set_points(self, points):
        """Set points without emitting 'changed' signal"""
        self.points = []
        if points:
            for p in points:
                temp = int(max(self.temp_min, min(self.temp_max, p[0])))
                percent = int(max(0, min(100, p[1])))
                self.points.append([temp, percent])
            self.points.sort(key=lambda p: p[0])
        else:
            self.points = [[self.temp_min, 0], [self.temp_max, 100]]
        self.queue_draw()
    
    def set_color(self, color):
        """Set accent color as (r, g, b) floats"""
        self.color = color
        self.queue_draw()
    
    def _get_plot_area(self, width, height):
        """Calculate plot area dimensions with margins"""
        left_margin = 28
        right_margin = 22
        top_margin = 20
        bottom_margin = 18
        
        plot_left = left_margin
        plot_right = width - right_margin
        plot_top = top_margin
        plot_bottom = height - bottom_margin
        plot_width = plot_right - plot_left
        plot_height = plot_bottom - plot_top
        
        return plot_left, plot_top, plot_width, plot_height
    
    def _temp_to_x(self, temp, plot_left, plot_width):
        """Convert temperature to x coordinate"""
        return plot_left + (temp - self.temp_min) / (self.temp_max - self.temp_min) * plot_width
    
    def _percent_to_y(self, percent, plot_top, plot_height):
        """Convert percentage to y coordinate (inverted)"""
        return plot_top + (1 - percent / 100) * plot_height
    
    def _x_to_temp(self, x, plot_left, plot_width):
        """Convert x coordinate to temperature"""
        return self.temp_min + (x - plot_left) / plot_width * (self.temp_max - self.temp_min)
    
    def _y_to_percent(self, y, plot_top, plot_height):
        """Convert y coordinate to percentage"""
        return (1 - (y - plot_top) / plot_height) * 100
    
    def _find_point_at(self, x, y, plot_left, plot_top, plot_width, plot_height):
        """Find point index at coordinate, or -1 if none"""
        radius = 10
        for i, (temp, percent) in enumerate(self.points):
            px = self._temp_to_x(temp, plot_left, plot_width)
            py = self._percent_to_y(percent, plot_top, plot_height)
            dist = math.sqrt((x - px) ** 2 + (y - py) ** 2)
            if dist <= radius:
                return i
        return -1
    
    def _on_motion(self, controller, x, y):
        """Handle mouse motion for cursor updates"""
        if self.dragging_index >= 0:
            # Already dragging, set grabbing cursor
            self.set_cursor(Gdk.Cursor.new_from_name('grabbing', None))
            return
        
        width = self.get_width()
        height = self.get_height()
        plot_left, plot_top, plot_width, plot_height = self._get_plot_area(width, height)
        
        idx = self._find_point_at(x, y, plot_left, plot_top, plot_width, plot_height)
        
        if idx != self.hover_index:
            self.hover_index = idx
            if idx >= 0:
                self.set_cursor(Gdk.Cursor.new_from_name('grab', None))
            else:
                self.set_cursor(Gdk.Cursor.new_from_name('default', None))
    
    def _on_drag_begin(self, gesture, x, y):
        """Handle drag start"""
        width = self.get_width()
        height = self.get_height()
        plot_left, plot_top, plot_width, plot_height = self._get_plot_area(width, height)
        
        idx = self._find_point_at(x, y, plot_left, plot_top, plot_width, plot_height)
        if idx >= 0:
            self.dragging_index = idx
            self.drag_start_x = x
            self.drag_start_y = y
            self.set_cursor(Gdk.Cursor.new_from_name('grabbing', None))
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        else:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
    
    def _on_drag_update(self, gesture, offset_x, offset_y):
        """Handle drag update"""
        if self.dragging_index < 0:
            return
        
        width = self.get_width()
        height = self.get_height()
        plot_left, plot_top, plot_width, plot_height = self._get_plot_area(width, height)
        
        # Calculate new position
        start_x = self.drag_start_x
        start_y = self.drag_start_y
        x = start_x + offset_x
        y = start_y + offset_y
        
        # Convert to temp/percent
        temp = self._x_to_temp(x, plot_left, plot_width)
        percent = self._y_to_percent(y, plot_top, plot_height)
        
        # Clamp
        temp = max(self.temp_min, min(self.temp_max, temp))
        percent = max(0, min(100, percent))
        
        # Snap to integers
        temp = int(round(temp))
        percent = int(round(percent))
        
        # Enforce ordering constraint (1°C gap from neighbors)
        idx = self.dragging_index
        if idx > 0:
            min_temp = self.points[idx - 1][0] + 1
            temp = max(min_temp, temp)
        if idx < len(self.points) - 1:
            max_temp = self.points[idx + 1][0] - 1
            temp = min(max_temp, temp)
        
        # Update point
        self.points[idx] = [temp, percent]
        self.queue_draw()
    
    def _on_drag_end(self, gesture, offset_x, offset_y):
        """Handle drag end"""
        if self.dragging_index >= 0:
            self.dragging_index = -1
            self.set_cursor(Gdk.Cursor.new_from_name('default', None))
            self.emit('changed')
    
    def _on_click(self, gesture, n_press, x, y):
        """Handle click for add/delete"""
        width = self.get_width()
        height = self.get_height()
        plot_left, plot_top, plot_width, plot_height = self._get_plot_area(width, height)
        
        # Double-click to delete
        if n_press == 2:
            idx = self._find_point_at(x, y, plot_left, plot_top, plot_width, plot_height)
            if idx >= 0 and len(self.points) > 2:
                self.points.pop(idx)
                self.emit('changed')
                self.queue_draw()
            return
        
        # Single click to add (only if not on existing point)
        if n_press == 1 and len(self.points) < 8:
            idx = self._find_point_at(x, y, plot_left, plot_top, plot_width, plot_height)
            if idx < 0:
                # Check if in plot area
                if plot_left <= x <= plot_left + plot_width and plot_top <= y <= plot_top + plot_height:
                    temp = self._x_to_temp(x, plot_left, plot_width)
                    percent = self._y_to_percent(y, plot_top, plot_height)
                    
                    # Clamp and snap
                    temp = int(max(self.temp_min, min(self.temp_max, round(temp))))
                    percent = int(max(0, min(100, round(percent))))
                    
                    # Check if point already exists within 1°C
                    for p in self.points:
                        if abs(p[0] - temp) <= 1:
                            return
                    
                    # Insert in sorted order
                    new_point = [temp, percent]
                    inserted = False
                    for i, p in enumerate(self.points):
                        if p[0] > temp:
                            self.points.insert(i, new_point)
                            inserted = True
                            break
                    if not inserted:
                        self.points.append(new_point)
                    
                    self.emit('changed')
                    self.queue_draw()
    
    def _draw(self, area, cr, width, height):
        """Draw the curve editor"""
        plot_left, plot_top, plot_width, plot_height = self._get_plot_area(width, height)
        
        # Background
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        
        # Grid lines
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.15)
        cr.set_line_width(1)
        
        # Vertical grid (every 10°C)
        for temp in range(self.temp_min, self.temp_max + 1, 10):
            x = self._temp_to_x(temp, plot_left, plot_width)
            cr.move_to(x, plot_top)
            cr.line_to(x, plot_top + plot_height)
            cr.stroke()
        
        # Horizontal grid (every 25%)
        for percent in [0, 25, 50, 75, 100]:
            y = self._percent_to_y(percent, plot_top, plot_height)
            cr.move_to(plot_left, y)
            cr.line_to(plot_left + plot_width, y)
            cr.stroke()
        
        # Axis frame (stronger)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.3)
        cr.set_line_width(1.5)
        cr.rectangle(plot_left, plot_top, plot_width, plot_height)
        cr.stroke()
        
        # Axis labels
        cr.set_source_rgba(0.6, 0.6, 0.6, 1)
        cr.select_font_face("Sans")
        cr.set_font_size(9)
        
        # X-axis labels (every 10°C, skip every other when narrow)
        cr.set_font_size(9)
        skip_x_labels = plot_width < 360
        for i, temp in enumerate(range(self.temp_min, self.temp_max + 1, 10)):
            if skip_x_labels and i % 2 == 1:
                continue
            x = self._temp_to_x(temp, plot_left, plot_width)
            text = str(temp)
            extents = cr.text_extents(text)
            cr.move_to(x - extents.width / 2, plot_top + plot_height + 14)
            cr.show_text(text)
        
        # X-axis unit label "°C" after rightmost tick
        x_100 = self._temp_to_x(self.temp_max, plot_left, plot_width)
        text_100 = str(self.temp_max)
        extents_100 = cr.text_extents(text_100)
        cr.set_source_rgba(0.6, 0.6, 0.6, 1)
        cr.move_to(x_100 + extents_100.width / 2 + 3, plot_top + plot_height + 14)
        cr.show_text("°C")
        
        # Y-axis labels (every 25%)
        for percent in [0, 25, 50, 75, 100]:
            y = self._percent_to_y(percent, plot_top, plot_height)
            text = str(percent)
            extents = cr.text_extents(text)
            cr.move_to(plot_left - 8 - extents.width, y + extents.height / 2 - 1)
            cr.show_text(text)
        
        # Y-axis unit label "%" above 100% tick
        cr.set_source_rgba(0.6, 0.6, 0.6, 1)
        extents_pct = cr.text_extents("%")
        cr.move_to(plot_left - 8 - extents_pct.width, plot_top - 7)
        cr.show_text("%")
        
        # Filled area under curve
        if len(self.points) >= 2:
            cr.set_source_rgba(*self.color, 0.12)
            cr.move_to(self._temp_to_x(self.points[0][0], plot_left, plot_width),
                      plot_top + plot_height)
            for temp, percent in self.points:
                x = self._temp_to_x(temp, plot_left, plot_width)
                y = self._percent_to_y(percent, plot_top, plot_height)
                cr.line_to(x, y)
            cr.line_to(self._temp_to_x(self.points[-1][0], plot_left, plot_width),
                      plot_top + plot_height)
            cr.close_path()
            cr.fill()
        
        # Curve line
        if len(self.points) >= 2:
            cr.set_source_rgba(*self.color, 1.0)
            cr.set_line_width(2.5)
            cr.move_to(self._temp_to_x(self.points[0][0], plot_left, plot_width),
                      self._percent_to_y(self.points[0][1], plot_top, plot_height))
            for temp, percent in self.points[1:]:
                x = self._temp_to_x(temp, plot_left, plot_width)
                y = self._percent_to_y(percent, plot_top, plot_height)
                cr.line_to(x, y)
            cr.stroke()
        
        # Control points
        for i, (temp, percent) in enumerate(self.points):
            x = self._temp_to_x(temp, plot_left, plot_width)
            y = self._percent_to_y(percent, plot_top, plot_height)
            
            is_active = (i == self.dragging_index)
            radius = 6 if is_active else 4.5
            
            # Background ring
            cr.set_source_rgba(1, 1, 1, 1)
            cr.arc(x, y, radius + 1.5, 0, 2 * math.pi)
            cr.fill()
            
            # Filled point
            cr.set_source_rgba(*self.color, 1.0)
            cr.arc(x, y, radius, 0, 2 * math.pi)
            cr.fill()
        
        # Drag label
        if self.dragging_index >= 0:
            temp, percent = self.points[self.dragging_index]
            x = self._temp_to_x(temp, plot_left, plot_width)
            y = self._percent_to_y(percent, plot_top, plot_height)
            
            text = f"{temp}°C · {percent}%"
            cr.select_font_face("Sans")
            cr.set_font_size(10)
            extents = cr.text_extents(text)
            
            # Position label above point
            label_x = x - extents.width / 2
            label_y = y - 20
            
            # Draw background pill
            padding = 6
            pill_x = label_x - padding
            pill_y = label_y - extents.height - padding + 2
            pill_width = extents.width + padding * 2
            pill_height = extents.height + padding * 2
            
            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.arc(pill_x + 4, pill_y + 4, 4, math.pi, 1.5 * math.pi)
            cr.arc(pill_x + pill_width - 4, pill_y + 4, 4, 1.5 * math.pi, 2 * math.pi)
            cr.arc(pill_x + pill_width - 4, pill_y + pill_height - 4, 4, 0, 0.5 * math.pi)
            cr.arc(pill_x + 4, pill_y + pill_height - 4, 4, 0.5 * math.pi, math.pi)
            cr.close_path()
            cr.fill()
            
            # Draw text
            cr.set_source_rgba(1, 1, 1, 1)
            cr.move_to(label_x, label_y)
            cr.show_text(text)
