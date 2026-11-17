"""Load the cleaned station and place files into the database.

  python manage.py migrate
  python manage.py load_data

Safe to run again: both tables are replaced inside one transaction.
"""
import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelplan.models import Place, Station

BATCH = 2000


class Command(BaseCommand):
    help = "Load data/stations.csv and data/us_places.csv into the database."

    def handle(self, *args, **opts):
        data_dir = Path(settings.DATA_DIR)
        stations_path, places_path = data_dir / "stations.csv", data_dir / "us_places.csv"
        for path in (stations_path, places_path):
            if not path.exists():
                raise CommandError(f"Missing {path}. Run build_station_data first.")

        with open(stations_path, newline="", encoding="utf-8") as fh:
            stations = [
                Station(
                    opis_id=int(r["opis_id"]), name=r["name"], address=r["address"],
                    city=r["city"], state=r["state"],
                    rack_id=int(r["rack_id"]) if r["rack_id"].isdigit() else None,
                    price=Decimal(r["price"]), price_samples=int(r["price_samples"]),
                    latitude=float(r["lat"]), longitude=float(r["lon"]),
                    geocode_source=r["geocode_source"],
                )
                for r in csv.DictReader(fh)
            ]
        with open(places_path, newline="", encoding="utf-8") as fh:
            places = [
                Place(
                    key=r["key"], name=r["name"], state=r["state"],
                    latitude=float(r["lat"]), longitude=float(r["lon"]),
                    population=int(r["population"]),
                )
                for r in csv.DictReader(fh)
            ]

        with transaction.atomic():
            Station.objects.all().delete()
            Place.objects.all().delete()
            Station.objects.bulk_create(stations, batch_size=BATCH)
            Place.objects.bulk_create(places, batch_size=BATCH)

        self.stdout.write(f"Loaded {len(stations)} stations and {len(places)} places.")
