"""Which US state (50 states plus DC) contains a point, if any.

A bounding-box test rejects most points cheaply; the state outline then decides, because
boxes alone accept places such as Windsor ON or Ciudad Juarez that sit inside a US state's
box. Outlines come from data/us_states.json (see the build_state_outlines command).
"""
import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings


@lru_cache(maxsize=1)
def _outlines():
    """[(state, state_bbox, [(ring_bbox, ring), ...]), ...] loaded once per process."""
    raw = json.loads((Path(settings.DATA_DIR) / "us_states.json").read_text())
    out = []
    for state, rings in raw.items():
        boxed = []
        for ring in rings:
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            boxed.append(((min(xs), min(ys), max(xs), max(ys)), ring))
        box = (min(b[0] for b, _ in boxed), min(b[1] for b, _ in boxed),
               max(b[2] for b, _ in boxed), max(b[3] for b, _ in boxed))
        out.append((state, box, boxed))
    return out


def _in_box(lon, lat, box):
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def _in_ring(lon, lat, ring):
    """Ray casting: is the point inside this closed ring?"""
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def state_at(lat, lon):
    """Two-letter code of the state containing the point, or None outside the USA."""
    for state, box, rings in _outlines():
        if not _in_box(lon, lat, box):
            continue
        # Even-odd rule over all rings handles islands and holes alike.
        crossings = sum(1 for ring_box, ring in rings
                        if _in_box(lon, lat, ring_box) and _in_ring(lon, lat, ring))
        if crossings % 2:
            return state
    return None
