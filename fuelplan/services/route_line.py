"""Spatial index over a route: how far along it, and how far off it, is a point?"""
import math
from collections import defaultdict

from .geo import MILES_PER_DEGREE_LAT, haversine_miles

CELL_DEGREES = 0.25
SAMPLE_SPACING_MILES = 0.5     # long straight segments get intermediate samples
GEOMETRY_SPACING_MILES = 0.3   # vertex spacing of the geometry returned to clients


class RouteLine:
    def __init__(self, points, distance_miles):
        self.points = points
        self.distance_miles = distance_miles

        raw = [0.0]
        for (a_lat, a_lon), (b_lat, b_lon) in zip(points, points[1:]):
            raw.append(raw[-1] + haversine_miles(a_lat, a_lon, b_lat, b_lon))
        # Mile markers are scaled so the last vertex equals the provider's distance.
        scale = distance_miles / raw[-1] if raw[-1] > 0 else 1.0
        self.vertex_miles = [m * scale for m in raw]

        self._grid = defaultdict(list)
        for i in range(len(points)):
            lat, lon = points[i]
            mile = self.vertex_miles[i]
            self._add(lat, lon, mile)
            if i + 1 < len(points):
                seg = raw[i + 1] - raw[i]
                if seg > SAMPLE_SPACING_MILES:
                    n_lat, n_lon = points[i + 1]
                    steps = int(seg // SAMPLE_SPACING_MILES)
                    for k in range(1, steps + 1):
                        t = k / (steps + 1)
                        self._add(
                            lat + (n_lat - lat) * t,
                            lon + (n_lon - lon) * t,
                            mile + (self.vertex_miles[i + 1] - mile) * t,
                        )

        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        self.bbox = (min(lats), max(lats), min(lons), max(lons))

    def _add(self, lat, lon, mile):
        self._grid[(int(lat // CELL_DEGREES), int(lon // CELL_DEGREES))].append((lat, lon, mile))

    def locate(self, lat, lon, max_miles):
        """Return (off_route_miles, mile_marker) of the nearest route sample, or None."""
        lat_margin = max_miles / MILES_PER_DEGREE_LAT
        cos_lat = max(math.cos(math.radians(lat)), 0.2)
        lon_margin = max_miles / (MILES_PER_DEGREE_LAT * cos_lat)
        lo_lat, hi_lat, lo_lon, hi_lon = self.bbox
        if not (lo_lat - lat_margin <= lat <= hi_lat + lat_margin
                and lo_lon - lon_margin <= lon <= hi_lon + lon_margin):
            return None

        ci, cj = int(lat // CELL_DEGREES), int(lon // CELL_DEGREES)
        r_lat = int(lat_margin / CELL_DEGREES) + 1
        r_lon = int(lon_margin / CELL_DEGREES) + 1
        best = None
        for a in range(ci - r_lat, ci + r_lat + 1):
            for b in range(cj - r_lon, cj + r_lon + 1):
                for s_lat, s_lon, mile in self._grid.get((a, b), ()):
                    d = haversine_miles(lat, lon, s_lat, s_lon)
                    if best is None or d < best[0]:
                        best = (d, mile)
        if best is None or best[0] > max_miles:
            return None
        return best

    def geojson_between(self, from_mile, to_mile):
        """[lon, lat] pairs for the stretch of route between two mile markers."""
        coords, last = [], None
        for (lat, lon), mile in zip(self.points, self.vertex_miles):
            if from_mile <= mile <= to_mile and (last is None or mile - last >= GEOMETRY_SPACING_MILES):
                coords.append([round(lon, 5), round(lat, 5)])
                last = mile
        return coords

    def geojson_coordinates(self):
        """[lon, lat] pairs thinned to roughly one vertex per 0.3 mile."""
        coords, last = [], None
        for (lat, lon), mile in zip(self.points, self.vertex_miles):
            if last is None or mile - last >= GEOMETRY_SPACING_MILES:
                coords.append([round(lon, 5), round(lat, 5)])
                last = mile
        end_lat, end_lon = self.points[-1]
        end = [round(end_lon, 5), round(end_lat, 5)]
        if coords[-1] != end:
            coords.append(end)
        return coords
