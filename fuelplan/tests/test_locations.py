import pytest

from fuelplan.models import Place, Station
from fuelplan.services import gazetteer
from fuelplan.services.errors import LocationError
from fuelplan.services.geo import decode_polyline, haversine_miles


def labels(query):
    return [p.label for p in gazetteer.suggest(query)]


def test_decode_polyline_reference_example():
    # Example from Google's encoded polyline documentation.
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [
        (38.5, -120.2), (40.7, -120.95), (43.252, -126.453),
    ]


def test_haversine_new_york_to_los_angeles():
    assert haversine_miles(40.7128, -74.0060, 34.0522, -118.2437) == pytest.approx(2446, abs=5)


# --- resolving -----------------------------------------------------------

@pytest.mark.parametrize("query,lat,lon", [
    ("Chicago, IL", 41.84, -87.68),
    ("chicago , illinois", 41.84, -87.68),
    ("Chicago", 41.84, -87.68),              # unambiguous without a state
    ("St. Louis, MO", 38.64, -90.24),
    ("Saint Louis, MO", 38.64, -90.24),
    ("Nashville, TN", 36.17, -86.79),
    ("Boise, ID", 43.60, -116.23),
])
def test_resolves_city(query, lat, lon):
    loc = gazetteer.resolve(query)
    assert loc.lat == pytest.approx(lat, abs=0.1)
    assert loc.lon == pytest.approx(lon, abs=0.1)


def test_resolves_coordinates_inside_usa():
    loc = gazetteer.resolve("41.8781,-87.6298")
    assert (loc.lat, loc.lon) == (41.8781, -87.6298)


@pytest.mark.parametrize("query", [
    "", "Toronto, ON", "Paris, France", "Zzzzqx, TX",
    "48.8566,2.3522",      # Paris
    "19.4326,-99.1332",    # Mexico City
    "95,-200",
])
def test_rejects_unknown_or_non_us_locations(query):
    with pytest.raises(LocationError):
        gazetteer.resolve(query)


@pytest.mark.parametrize("name,coords", [
    ("Windsor ON", "42.3149,-83.0364"),        # inside Michigan's bounding box
    ("Toronto ON", "43.6532,-79.3832"),        # inside New York's bounding box
    ("Niagara Falls ON", "43.0896,-79.0849"),
    ("Ciudad Juarez MX", "31.6904,-106.4245"),  # inside Texas's bounding box
    ("Nuevo Laredo MX", "27.4763,-99.5164"),
    ("Gulf of Mexico", "26.0,-90.0"),
])
def test_coordinates_outside_the_usa_are_rejected(name, coords):
    with pytest.raises(LocationError) as info:
        gazetteer.resolve(coords, "start")
    assert "not within the USA" in info.value.message


@pytest.mark.parametrize("name,coords", [
    ("Detroit MI", "42.3314,-83.0458"),
    ("El Paso TX", "31.7619,-106.4850"),
    ("Laredo TX", "27.5306,-99.4803"),
    ("Buffalo NY", "42.8864,-78.8784"),
    ("Blaine WA", "48.9937,-122.7471"),
    ("Key West FL", "24.5551,-81.7800"),
    ("Washington DC", "38.9072,-77.0369"),
])
def test_coordinates_inside_the_usa_are_accepted(name, coords):
    loc = gazetteer.resolve(coords, "start")
    assert (loc.lat, loc.lon) == tuple(float(x) for x in coords.split(","))


def test_misspelling_error_offers_the_intended_city():
    with pytest.raises(LocationError) as info:
        gazetteer.resolve("Chcago, IL", "start")
    assert info.value.field == "start"
    assert info.value.suggestions[0] == "Chicago, IL"
    assert "Did you mean Chicago, IL?" in info.value.message


def test_ambiguous_city_without_state_lists_candidates():
    with pytest.raises(LocationError) as info:
        gazetteer.resolve("Springfield")
    assert "Add the state" in info.value.message
    assert "Springfield, IL" in info.value.suggestions


def test_state_alone_asks_for_a_city():
    with pytest.raises(LocationError) as info:
        gazetteer.resolve("FL")
    assert "Miami, FL" in info.value.suggestions


# --- suggestions ---------------------------------------------------------

def test_suggest_prefix_ranks_largest_city_first():
    assert labels("chi")[0] == "Chicago, IL"
    assert labels("hou")[0] == "Houston, TX"
    assert labels("new y")[0] == "New York, NY"


def test_suggest_state_code_lists_cities_in_that_state():
    found = labels("FL")
    assert found[:5] == ["Jacksonville, FL", "Miami, FL", "Tampa, FL", "Orlando, FL", "St. Petersburg, FL"]


def test_suggest_full_state_name():
    assert all(label.endswith(", FL") for label in labels("Florida"))


def test_suggest_city_with_partial_state():
    assert labels("miami, f")[0] == "Miami, FL"
    assert all(label.endswith(", FL") for label in labels("miami, f"))


@pytest.mark.parametrize("typo,expected", [
    ("Chcago", "Chicago, IL"),
    ("Sprngfield, IL", "Springfield, IL"),
    ("Huston", "Houston, TX"),
])
def test_suggest_tolerates_spelling_mistakes(typo, expected):
    assert expected in labels(typo)


def test_suggest_has_no_duplicates_and_respects_limit():
    found = labels("san")
    assert len(found) == len(set(found)) <= 8


@pytest.mark.parametrize("query", ["", " ", "41.8,-87.6", "zzzzqxv"])
def test_suggest_returns_nothing_for_empty_or_unmatched(query):
    assert labels(query) == []


# --- data in the database -------------------------------------------------

def test_station_table_is_clean():
    assert Station.objects.count() > 6000
    assert not Station.objects.filter(state__in=["ON", "AB", "BC", "MB", "SK", "QC", "YT", "NS", "NB"]).exists()
    assert not Station.objects.filter(price__lte=0).exists()
    for st in Station.objects.all()[:500]:
        assert st.city == st.city.strip() and len(st.state) == 2
        assert 17 < st.latitude < 72 and -180 < st.longitude < -64


def test_repeated_station_price_is_the_mean_of_its_rows():
    # OPIS 105 appears six times in the sheet: 3.269 3.339 3.429 3.289 3.399 3.299
    station = Station.objects.get(opis_id=105)
    assert station.price_samples == 6
    assert float(station.price) == pytest.approx(3.33733, abs=1e-4)


def test_offshore_city_centres_are_moved_onto_land():
    from fuelplan.services.borders import state_at
    sf = gazetteer.resolve("San Francisco, CA")
    assert haversine_miles(sf.lat, sf.lon, 37.77, -122.42) < 5      # Census put it 34 mi offshore
    corpus = gazetteer.resolve("Corpus Christi, TX")
    assert state_at(corpus.lat, corpus.lon) == "TX"
    # Stations in those cities moved with them.
    assert all(state_at(st.latitude, st.longitude) == "TX"
               for st in Station.objects.filter(city="Corpus Christi", state="TX"))


def test_place_table_loaded():
    assert Place.objects.count() > 30000
    assert Place.objects.get(key="CHICAGO", state="IL").population > 2_000_000
