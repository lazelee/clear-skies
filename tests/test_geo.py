"""Place search: region filters, merging the two geocoders, and never substituting unrelated towns."""
import pytest

from clearskies import geo


def om(name, admin1, country, code, lat, lon, pop=0):
    return {"name": name, "population": pop, "region": [admin1, None], "country": country, "country_code": code,
            "lat": lat, "lon": lon, "elevation": 100, "timezone": "UTC", "label": f"{name}, {admin1}, {country}",
            "source": "GeoNames"}


def osm(name, state, country, code, lat, lon, city=None):
    return {"name": name, "region": [state, None, city], "country": country, "country_code": code, "lat": lat,
            "lon": lon, "elevation": 0, "timezone": None, "label": ", ".join(x for x in [name, city, state, country] if x),
            "source": "OpenStreetMap"}


SCARBOROUGHS = (
    om("Scarborough", "Tobago", "Trinidad and Tobago", "TT", 11.18, -60.73, 17000),
    om("Scarborough", "Maine", "United States", "US", 43.58, -70.32, 4403),
    om("Scarborough", "England", "United Kingdom", "GB", 54.28, -0.40, 61749),
)


@pytest.fixture
def fake(monkeypatch):
    calls = {"om": {}, "osm": {}}
    monkeypatch.setattr(geo, "_open_meteo", lambda name, count: calls["om"].get(name, ()))
    monkeypatch.setattr(geo, "_photon", lambda text, count: calls["osm"].get(text, ()))
    return calls


def test_region_filter_finds_the_osm_suburb(fake):
    fake["om"]["Scarborough"] = SCARBOROUGHS
    fake["osm"]["Scarborough, ON, Canada"] = (osm("Scarborough", "Ontario", "Canada", "CA", 43.77, -79.23, city="Toronto"),)
    r = geo.search("Scarborough, ON, Canada")
    assert [x["label"] for x in r] == ["Scarborough, Toronto, Ontario, Canada"]


def test_unmatched_region_never_falls_back_to_other_towns(fake):
    fake["om"]["Scarborough"] = SCARBOROUGHS
    assert geo.search("Scarborough, Atlantis") == []


def test_trailing_words_as_region_without_commas(fake):
    fake["om"]["Gravenhurst"] = (om("Gravenhurst", "England", "United Kingdom", "GB", 52.0, -0.37),
                                 om("Gravenhurst", "Ontario", "Canada", "CA", 44.92, -79.37))
    r = geo.search("Gravenhurst Ontario")
    assert r[0]["country_code"] == "CA"


@pytest.mark.parametrize("q,code", [("Paris, TX", "US"), ("Paris, France", "FR"), ("Paris, FR", "FR"),
                                    ("Springfield, IL", "US"), ("London, UK", "GB"), ("London, ON", "CA")])
def test_region_abbreviations_and_codes(fake, q, code):
    fake["om"]["Paris"] = (om("Paris", "Île-de-France", "France", "FR", 48.85, 2.35, 2_000_000),
                           om("Paris", "Texas", "United States", "US", 33.66, -95.56, 25000))
    fake["om"]["Springfield"] = (om("Springfield", "Missouri", "United States", "US", 37.2, -93.3, 160000),
                                 om("Springfield", "Illinois", "United States", "US", 39.8, -89.6, 114000))
    fake["om"]["London"] = (om("London", "England", "United Kingdom", "GB", 51.5, -0.13, 8_900_000),
                            om("London", "Ontario", "Canada", "CA", 42.98, -81.25, 400000))
    r = geo.search(q)
    assert r and r[0]["country_code"] == code
    if q == "Springfield, IL":
        assert "Illinois" in r[0]["label"]
    if q == "London, ON":
        assert "Ontario" in r[0]["label"]


def test_bare_name_ranks_by_population_and_includes_osm_places(fake):
    fake["om"]["Scarborough"] = SCARBOROUGHS
    fake["osm"]["Scarborough"] = (osm("Scarborough", "Ontario", "Canada", "CA", 43.77, -79.23, city="Toronto"),
                                  osm("Scarborough", "England", "United Kingdom", "GB", 54.281, -0.401))
    labels = [x["label"] for x in geo.search("Scarborough")]
    assert labels[0].startswith("Scarborough, England")  # largest
    assert "Scarborough, Toronto, Ontario, Canada" in labels
    assert sum("England" in x for x in labels) == 1  # the OSM duplicate of the same town is merged


def test_non_latin_script_comes_from_osm(fake):
    fake["osm"]["東京"] = (osm("Tokyo", None, "Japan", "JP", 35.68, 139.69),)
    assert geo.search("東京")[0]["country_code"] == "JP"


def test_one_geocoder_down_still_works(monkeypatch):
    import requests

    def down(*a):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(geo, "_open_meteo", down)
    monkeypatch.setattr(geo, "_photon", lambda text, count: (osm("Nuuk", None, "Greenland", "GL", 64.18, -51.72),))
    assert geo.search("Nuuk")[0]["country"] == "Greenland"


def test_both_geocoders_down_is_an_error(monkeypatch):
    import requests

    def down(*a):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(geo, "_open_meteo", down)
    monkeypatch.setattr(geo, "_photon", down)
    with pytest.raises(geo.LookupError_):
        geo.search("Anywhere")


def test_find_uses_country_as_region(fake):
    fake["om"]["Denver"] = (om("Denver", "Colorado", "United States", "US", 39.74, -104.98, 700000),)
    for country in ("US", "USA", "United States"):
        assert geo.find("Denver", country)["country_code"] == "US"
    assert geo.find("Denver", "Canada") is None


def test_short_codes_mean_only_what_they_stand_for():
    london_uk = om("London", "England", "United Kingdom", "GB", 51.5, -0.13)
    london_on = om("London", "Ontario", "Canada", "CA", 42.98, -81.25)
    houston = om("Houston", "Texas", "United States", "US", 29.76, -95.37)
    assert geo._matches(london_on, ["ON"]) and not geo._matches(london_uk, ["ON"])
    assert not geo._matches(houston, ["ON"])  # "Houston" contains "on" but isn't in Ontario
    seattle = om("Seattle", "Washington", "United States", "US", 47.6, -122.3)
    perth = om("Perth", "Western Australia", "Australia", "AU", -31.95, 115.86)
    assert geo._matches(seattle, ["WA"]) and geo._matches(perth, ["WA"])
    toronto = om("Toronto", "Ontario", "Canada", "CA", 43.7, -79.4)
    la = om("Los Angeles", "California", "United States", "US", 34.05, -118.24)
    assert geo._matches(toronto, ["CA"]) and geo._matches(la, ["CA"])  # Canada's code, California's abbreviation


def test_exact_name_beats_bigger_alternate_name_match(fake):
    fake["om"]["Bridgetown"] = (om("Portland", "Oregon", "United States", "US", 45.5, -122.7, 650000),
                                om("Bridgetown", "Saint Michael", "Barbados", "BB", 13.1, -59.6, 110000))
    assert geo.search("Bridgetown")[0]["country_code"] == "BB"


@pytest.mark.parametrize("text,code", [("Canada", "CA"), ("DR Congo", "CD"), ("Ivory Coast", "CI"), ("Côte d'Ivoire", "CI"),
                                       ("Burma", "MM"), ("Czech Republic", "CZ"), ("the Netherlands", "NL"),
                                       ("Caribbean Netherlands", "BQ"), ("Svalbard and Jan Mayen", "SJ"), ("USA", "US"),
                                       ("United Kingdom", "GB"), ("Timor-Leste", "TL"), ("Gibraltar", "GI")])
def test_country_names_and_aliases(text, code):
    assert geo._country_code(text) == code


def test_country_qualifier_matches_code_only_territories():
    gib = om("Gibraltar", None, "Gibraltar", "GI", 36.14, -5.35)
    jam = om("Gibraltar", "Saint Ann Parish", "Jamaica", "JM", 18.4, -77.1)
    assert geo._matches(gib, ["Gibraltar"]) and not geo._matches(jam, ["Gibraltar"])
