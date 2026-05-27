"""Parse a Google My Maps KML export for seeding a myspots instance.

My Maps placemarks carry only a name, a point, an optional description, and
their containing folder (layer). They have no address or Google Place ID, so
the importer re-resolves each placemark against the Google Places API; this
module just handles the parsing and the geo helper used to pick the closest
match.
"""

import difflib
import re
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import lxml.etree

KML_NS = "http://www.opengis.net/kml/2.2"


@dataclass(frozen=True)
class KmlPlacemark:
    name: str
    latitude: float
    longitude: float
    description: str | None
    folder: str | None


def _clean_description(raw: str | None) -> str | None:
    """Flatten My Maps description HTML/CDATA into plain note text."""
    if not raw:
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(r"<[^>]+>", "", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text or None


def _nearest_folder(placemark) -> str | None:
    anc = placemark.getparent()
    while anc is not None:
        if anc.tag == f"{{{KML_NS}}}Folder":
            name_el = anc.find(f"{{{KML_NS}}}name")
            if name_el is not None and name_el.text:
                return name_el.text.strip()
            return None
        anc = anc.getparent()
    return None


def parse_kml(path: str | Path) -> list[KmlPlacemark]:
    """Extract point placemarks (name, lat/lon, description, folder)."""
    tree = lxml.etree.parse(str(path))
    placemarks = []
    for pm in tree.iterfind(f".//{{{KML_NS}}}Placemark"):
        name_el = pm.find(f"{{{KML_NS}}}name")
        coord_el = pm.find(f".//{{{KML_NS}}}Point/{{{KML_NS}}}coordinates")
        if name_el is None or not name_el.text or coord_el is None or not coord_el.text:
            continue
        # KML coordinates are "lon,lat[,alt]".
        lon_str, lat_str, *_ = coord_el.text.strip().split(",")
        desc_el = pm.find(f"{{{KML_NS}}}description")
        placemarks.append(
            KmlPlacemark(
                name=name_el.text.strip(),
                latitude=float(lat_str),
                longitude=float(lon_str),
                description=_clean_description(desc_el.text if desc_el is not None else None),
                folder=_nearest_folder(pm),
            )
        )
    return placemarks


def _normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", " ", name)  # \w keeps Unicode letters (Hebrew, accents)
    return re.sub(r"\s+", " ", name).strip()


def name_mismatch(kml_name: str, google_name: str) -> bool:
    """True when the two names are comparable yet look different.

    Returns False (no flag) when the names are similar OR when they can't be
    reliably compared — e.g. one is Hebrew and the other a transliterated Latin
    form, or either contains non-ASCII characters. This deliberately only
    catches same-script mismatches (like ``Onza`` vs ``Akbar``) and never flags
    cross-script matches, where textual similarity is meaningless.
    """
    a, b = _normalize_name(kml_name), _normalize_name(google_name)
    if not a or not b:
        return False
    if not (a.isascii() and b.isascii()):
        return False
    if a in b or b in a:
        return False
    # Token containment handles Google's added descriptors (e.g. "Levontin 7"
    # vs "Levontin St 7").
    ta, tb = set(a.split()), set(b.split())
    if ta and tb:
        small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if len(small & large) / len(small) >= 0.5:
            return False
    return difflib.SequenceMatcher(None, a, b).ratio() < 0.6


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points."""
    r = 6371000.0
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = (
        sin(dphi / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlambda / 2) ** 2
    )
    return 2 * r * asin(sqrt(a))
