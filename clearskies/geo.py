import requests

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"


class LookupError_(Exception):
    """Raised when an external lookup fails in a way worth showing the user."""


def _query(name, count):
    try:
        res = requests.get(GEO_URL, params={"name": name, "count": count, "language": "en", "format": "json"}, timeout=10)
        res.raise_for_status()
    except requests.RequestException as exc:
        raise LookupError_(f"Location search failed: {exc}") from exc
    return res.json().get("results") or []


def search(query, count=8):
    """
    Resolve free text into candidate locations. The geocoder only matches place names,
    so "Gravenhurst Ontario" or "Toronto, Canada" is split into a name plus a region
    filter on the region/country.
    """
    query = " ".join((query or "").replace(",", " , ").split())
    if len(query) < 2:
        return []
    raw = _query(query.replace(" , ", " ").replace(",", ""), count)
    if not raw:
        head, _, tail = query.partition(",")
        words = head.split()
        # "Gravenhurst Ontario" → name "Gravenhurst", filter "Ontario"; also try dropping more words.
        for cut in range(len(words) - 1 if not tail else len(words), 0, -1):
            name = " ".join(words[:cut])
            extra = " ".join(words[cut:] + tail.replace(",", " ").split()).lower()
            found = _query(name, 20)
            if not found:
                continue
            if extra:
                keep = [r for r in found if all(w in f"{r.get('admin1', '')} {r.get('admin2', '')} {r.get('country', '')} {r.get('country_code', '')}".lower() for w in extra.split())]
                found = keep or found
            raw = found[:count]
            break
    out = []
    for r in raw:
        parts = [r.get("name"), r.get("admin1"), r.get("country")]
        out.append({
            "name": r.get("name"),
            "label": ", ".join(dict.fromkeys(p for p in parts if p)),
            "country": r.get("country", ""),
            "country_code": r.get("country_code", ""),
            "lat": r["latitude"],
            "lon": r["longitude"],
            "elevation": r.get("elevation") or 0,
            "timezone": r.get("timezone"),
        })
    return out


# Common ways of writing a country that aren't its name or ISO code.
_COUNTRY_ALIASES = {"usa": "us", "america": "us", "united states of america": "us", "uk": "gb",
                    "britain": "gb", "great britain": "gb", "england": "gb", "scotland": "gb", "wales": "gb"}


def find(city, country=None):
    """Best match for a city, optionally restricted to a country by name or ISO code ("US", "USA", "Canada")."""
    results = search(city, count=10)
    if country:
        want = country.strip().lower()
        want = _COUNTRY_ALIASES.get(want, want)
        results = [r for r in results if want in (r["country"].lower(), r["country_code"].lower())]
    return results[0] if results else None
