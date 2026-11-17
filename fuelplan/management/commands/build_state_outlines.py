"""One-time build: US state outlines for checking that a lat,lon is inside the USA.

State bounding boxes alone cannot do this: Windsor ON, Toronto, Ciudad Juarez and Nuevo
Laredo all fall inside the box of a US state. The outlines come from the US Census
cartographic boundary file (public domain), read with a small pure-Python shapefile reader
so no GIS dependency is needed:

  https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_5m.zip

Usage:
  python manage.py build_state_outlines --shapefile gazetteers/cb_2023_us_state_5m.shp
Writes data/us_states.json: {"MI": [[[lon, lat], ...], ...], ...}, 50 states plus DC.
"""
import json
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuelplan.services.geo import US_STATES


def read_dbf(path):
    """Attribute records of a dBASE file, as a list of {field: text}."""
    data = Path(path).read_bytes()
    count, header_len, record_len = struct.unpack("<xxxxIHH", data[:12])
    fields, offset = [], 32
    while data[offset] != 0x0D:
        name = data[offset:offset + 11].split(b"\0")[0].decode()
        fields.append((name, data[offset + 16]))
        offset += 32
    records = []
    for i in range(count):
        raw = data[header_len + i * record_len + 1: header_len + (i + 1) * record_len]
        record, pos = {}, 0
        for name, length in fields:
            record[name] = raw[pos:pos + length].decode("latin-1").strip()
            pos += length
        records.append(record)
    return records


def read_polygons(path):
    """Polygon shapes of a .shp file, each a list of rings of (lon, lat)."""
    data = Path(path).read_bytes()
    offset, shapes = 100, []
    while offset < len(data):
        _, content_words = struct.unpack(">ii", data[offset:offset + 8])
        offset += 8
        body = data[offset:offset + content_words * 2]
        offset += content_words * 2
        if struct.unpack("<i", body[:4])[0] == 0:          # null shape
            shapes.append([])
            continue
        n_parts, n_points = struct.unpack("<ii", body[36:44])
        parts = struct.unpack("<%di" % n_parts, body[44:44 + 4 * n_parts])
        start = 44 + 4 * n_parts
        coords = struct.unpack("<%dd" % (2 * n_points), body[start:start + 16 * n_points])
        points = list(zip(coords[0::2], coords[1::2]))
        ends = list(parts[1:]) + [n_points]
        shapes.append([points[a:b] for a, b in zip(parts, ends)])
    return shapes


def compact(ring):
    """Round to 4 decimals (about 11 m) and drop repeated points."""
    out = []
    for lon, lat in ring:
        point = [round(lon, 4), round(lat, 4)]
        if not out or out[-1] != point:
            out.append(point)
    return out


class Command(BaseCommand):
    help = "Convert the Census state shapefile into data/us_states.json."

    def add_arguments(self, parser):
        parser.add_argument("--shapefile", required=True, help="Path to cb_2023_us_state_5m.shp")

    def handle(self, *args, **opts):
        shp = Path(opts["shapefile"])
        dbf = shp.with_suffix(".dbf")
        if not shp.exists() or not dbf.exists():
            raise CommandError(f"Need both {shp} and {dbf}.")
        outlines = {}
        for record, rings in zip(read_dbf(dbf), read_polygons(shp)):
            state = record.get("STUSPS")
            if state in US_STATES:
                outlines[state] = [compact(ring) for ring in rings if len(ring) >= 4]
        missing = set(US_STATES) - set(outlines)
        if missing:
            raise CommandError(f"Shapefile is missing: {', '.join(sorted(missing))}")
        out = Path(settings.DATA_DIR) / "us_states.json"
        out.write_text(json.dumps(outlines, separators=(",", ":")))
        vertices = sum(len(r) for rings in outlines.values() for r in rings)
        self.stdout.write(f"Wrote {len(outlines)} outlines, {vertices} points, to {out}.")
