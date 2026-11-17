from django.db import models


class Station(models.Model):
    """A US fuel station from the client's price sheet, one row per OPIS truck stop ID."""

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    # Mean of the sheet's rows for this station (the sheet has no date column).
    price = models.DecimalField(max_digits=8, decimal_places=5)
    price_samples = models.PositiveSmallIntegerField(default=1)
    # Centroid of the station's city; the sheet carries no coordinates.
    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_source = models.CharField(max_length=20)

    class Meta:
        indexes = [models.Index(fields=["latitude", "longitude"], name="station_lat_lon")]
        ordering = ["opis_id"]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"


class Place(models.Model):
    """A US city/town used to resolve and suggest start and finish locations.

    `key` is the normalised search form of a name. A place can have more than one
    row when it answers to an alias (e.g. "BOISE" and "BOISE CITY").
    """

    key = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    latitude = models.FloatField()
    longitude = models.FloatField()
    population = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["key", "state"], name="place_key_state"),
            models.Index(fields=["state", "-population"], name="place_state_pop"),
            models.Index(fields=["latitude", "longitude"], name="place_lat_lon"),
        ]

    @property
    def label(self):
        return f"{self.name}, {self.state}"

    def __str__(self):
        return self.label
