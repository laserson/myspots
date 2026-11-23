"""Import a Google My Maps KML export into "MySpots DB v2", in two steps.

1. resolve: find each placemark on Google (Places API (New)), collapse
   placemarks that point to the same place, and write a review file. Nothing
   is written to Notion.

       uv run python scripts/import_kml.py resolve "Mexico City.kml" \\
           --map cdmx --tag list:mymaps-cdmx-import --out imports/cdmx.yaml

2. Edit the review file: categories, tags, notes, `skip: true`, or a corrected
   `place_id`. Then create the rows:

       uv run python scripts/import_kml.py write imports/cdmx.yaml           # dry run
       uv run python scripts/import_kml.py write imports/cdmx.yaml --write

`write` re-fetches Google details for each place_id (so a corrected ID is
picked up), skips places already in v2, and never adds a spot without a
google_place_id (per the MySpots Instructions page).

Google lookups are cached in ~/.cache/myspots/places-new.json. Review files
hold personal notes, so imports/ and *.kml are git-ignored.
"""

import datetime as dt
import html
import json
import math
import pathlib
import re
import xml.etree.ElementTree as ET

import click
import unicodedata
from rapidfuzz import fuzz
import yaml
from google.maps import places_v1
from google.type import latlng_pb2
from notion_client import Client
from tqdm import tqdm

from myspots import _install_notion_rate_limit_retry, get_config

V2_DATABASE_ID = "4185ebf8-0ae5-81f1-95eb-c5f143cf4035"
TODAY = dt.date.today().isoformat()
KML = "{http://www.opengis.net/kml/2.2}"
# websiteUri bills a search at the Enterprise tier, so searches leave it out and
# only `write` (one Place Details call per spot) fetches it.
SEARCH_FIELDS = ["id", "displayName", "formattedAddress", "location", "primaryType", "types", "businessStatus"]
FIELDS = [*SEARCH_FIELDS, "websiteUri"]
NEAR_METERS = 75       # pins sit on the place; within this, a loose name match is enough
NEAR_NAME_SCORE = 60
FAR_NAME_SCORE = 85    # further away, require a strong name match (and flag it)
MAPS_YAML = pathlib.Path(__file__).resolve().parent.parent / "maps.yaml"
CACHE = pathlib.Path("~/.cache/myspots/places-new.json").expanduser()


############################################
# KML + Google
############################################


def kml_title(path: pathlib.Path) -> str:
    return ET.parse(path).getroot().findtext(f"{KML}Document/{KML}name", path.stem).strip()


def parse_kml(path: pathlib.Path) -> list[dict]:
    root = ET.parse(path).getroot()
    out = []
    for folder in root.iter(f"{KML}Folder"):
        layer = folder.findtext(f"{KML}name", "").strip()
        for pm in folder.iter(f"{KML}Placemark"):
            coords = pm.findtext(f".//{KML}coordinates")
            if not coords:
                continue
            lon, lat = map(float, coords.strip().split(",")[:2])
            raw = pm.findtext(f"{KML}description") or ""
            text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", raw)))
            text = "\n".join(line.strip() for line in text.strip().splitlines())
            out.append({"name": pm.findtext(f"{KML}name", "").strip(), "layer": layer,
                        "lat": lat, "lon": lon, "text": text, "description": " ".join(text.split())})
    return out


def _norm(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", name)


def name_score(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    # token_set handles extra words ("Mariscos Don Vergas" / "Don Vergas");
    # partial_ratio without spaces handles run-together names ("CHICOJULIO").
    return max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a.replace(" ", ""), b.replace(" ", "")))


def meters(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12_742_000 * math.asin(math.sqrt(a))


class Places:
    def __init__(self, api_key: str):
        self.client = places_v1.PlacesClient(client_options={"api_key": api_key})
        self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    def _save(self):
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False))

    @staticmethod
    def _to_dict(p) -> dict:
        return {"id": p.id, "name": p.display_name.text, "address": p.formatted_address,
                "lat": p.location.latitude, "lon": p.location.longitude,
                "website": p.website_uri or None, "primary_type": p.primary_type,
                "types": list(p.types), "status": places_v1.Place.BusinessStatus(p.business_status).name}

    def _search(self, key: str, request: places_v1.SearchTextRequest) -> list[dict]:
        if key not in self.cache:
            r = self.client.search_text(
                request=request, metadata=[("x-goog-fieldmask", ",".join(f"places.{f}" for f in SEARCH_FIELDS))])
            self.cache[key] = [self._to_dict(p) for p in r.places]
            self._save()
        return self.cache[key]

    def search(self, query: str, lat: float, lon: float) -> tuple[dict | None, list[str], list[str]]:
        """Pick the place this placemark means. Returns (place, review notes, alternatives).

        My Maps pins sit on the place itself, so location decides first: a
        candidate within NEAR_METERS of the pin whose name roughly matches wins,
        even if it has since closed. Failing that, a strong name match further
        away is taken but flagged (moved, or another branch of a chain).

        Candidates: a name search biased tightly to the pin, then a wider one,
        plus any earlier cached searches for this pin (so targeted manual
        queries made while reviewing are considered too).
        """
        def biased(radius: float) -> places_v1.SearchTextRequest:
            bias = places_v1.SearchTextRequest.LocationBias(circle=places_v1.Circle(
                center=latlng_pb2.LatLng(latitude=lat, longitude=lon), radius=radius))
            return places_v1.SearchTextRequest(text_query=query, location_bias=bias, max_result_count=10)

        pin = f"|{lat:.6f}|{lon:.6f}"
        seen = {}

        def add(results):
            for c in results or []:
                seen.setdefault(c["id"], {**c, "score": name_score(query, c["name"]),
                                          "dist": meters(lat, lon, c["lat"], c["lon"])})

        for key, results in list(self.cache.items()):
            if key.endswith(pin) and isinstance(results, list):
                add(results)
        for key, req in [(f"bias100x10|{query}{pin}", biased(100.0)), (f"bias5000x10|{query}{pin}", biased(5000.0))]:
            near = [c for c in seen.values() if c["dist"] <= NEAR_METERS and c["score"] >= NEAR_NAME_SCORE]
            if near:
                break
            add(self._search(key, req))

        near = [c for c in seen.values() if c["dist"] <= NEAR_METERS and c["score"] >= NEAR_NAME_SCORE]
        if near:
            best = max(near, key=lambda c: (c["score"], -c["dist"]))
            return best, [], self._alternatives(seen, best)
        strong = [c for c in seen.values() if c["score"] >= FAR_NAME_SCORE]
        if strong:
            best = min(strong, key=lambda c: c["dist"])
            return best, [f"{best['dist']:,.0f} m from the pin for {query!r}: moved, or another branch?"], \
                self._alternatives(seen, best)
        if not seen:
            return None, ["no Google match: set place_id or skip"], []
        best = min(seen.values(), key=lambda c: c["dist"])
        return best, [f"NO MATCH: nearest result {best['name']!r} ({best['dist']:,.0f} m) is probably wrong; "
                      "set place_id or skip"], self._alternatives(seen, best)

    @staticmethod
    def _alternatives(seen: dict, best: dict) -> list[str]:
        """Other candidates, best first, as one-line strings for the review file."""
        others = sorted((c for c in seen.values() if c["id"] != best["id"]),
                        key=lambda c: (-c["score"], c["dist"]))[:5]
        return [f"{c['id']} | {c['name']} | {c['dist']:,.0f} m | score {c['score']:.0f} | "
                f"{c['primary_type']} | {c['address']}" for c in others]

    def get(self, place_id: str) -> dict:
        key = f"get|{place_id}"
        if key not in self.cache:
            p = self.client.get_place(request={"name": f"places/{place_id}"},
                                      metadata=[("x-goog-fieldmask", ",".join(FIELDS))])
            self.cache[key] = self._to_dict(p)
            self._save()
        return self.cache[key]


SOURCES = {"nyt": r"nytimes\.com|\bNY ?Times\b", "afar": r"afar\.com", "eater": r"eater\.com|\bEater\b"}
INLINE_NOTE_MAX = 100


def source_tags(text: str) -> list[str]:
    """Publication source tags (one per publication; friends go in notes, not tags)."""
    return [tag for tag, pattern in SOURCES.items() if re.search(pattern, text, re.I)]


def note_and_body(pm: dict, title: str) -> tuple[str, dict | None]:
    """A one-line notes entry, plus a verbatim page-body section when there is source text.

    Follows the MySpots Instructions page: source text goes verbatim into the
    page body under "## <date> · <source> (<where>)"; notes get a short pointer.
    """
    where = f'Google My Maps "{title}", layer "{pm["layer"]}"'
    if not pm["description"]:
        return f"{TODAY} · import: {where}", None
    if len(pm["description"]) <= INLINE_NOTE_MAX and "http" not in pm["description"]:
        note = f"{TODAY} · import: {where}: {pm['description']}"
    else:
        url = re.search(r"https?://\S+", pm["description"])
        note = f"{TODAY} · import: {where}: see page body" + (f" {url.group(0)}" if url else "")
    return note, {"heading": f"{TODAY} · import ({where})", "text": pm["text"]}


def v2_place_ids(notion: Client) -> tuple[str, dict[str, str]]:
    ds = notion.databases.retrieve(database_id=V2_DATABASE_ID)["data_sources"][0]["id"]
    ids, cursor = {}, None
    while True:
        r = notion.data_sources.query(data_source_id=ds, page_size=100, **({"start_cursor": cursor} if cursor else {}))
        for page in r["results"]:
            pid = "".join(x["plain_text"] for x in page["properties"]["google_place_id"]["rich_text"])
            ids[pid] = "".join(x["plain_text"] for x in page["properties"]["name"]["title"])
        if not r["has_more"]:
            return ds, ids
        cursor = r["next_cursor"]


def notion_client(config) -> Client:
    notion = Client(auth=config["notion_api_token"])
    _install_notion_rate_limit_retry(notion)
    return notion


############################################
# resolve
############################################


@click.group()
def cli():
    pass


@cli.command()
@click.argument("kml", type=click.Path(exists=True, path_type=pathlib.Path))
@click.option("--map", "map_slug", required=True, help="v2 `map` value, e.g. cdmx")
@click.option("--tag", "list_tag", required=True, help="list tag added to every spot")
@click.option("--out", type=click.Path(path_type=pathlib.Path), required=True, help="review file to write")
@click.option("--force", is_flag=True, help="overwrite an existing review file")
@click.option("--decisions", type=click.Path(exists=True, path_type=pathlib.Path), default=None,
              help="YAML of match decisions by placemark name (or 'name @lat,lon'): {place_id: ...} or {skip: reason}")
def resolve(kml, map_slug, list_tag, out, force, decisions):
    """Find each placemark on Google and write a review file."""
    decisions = yaml.safe_load(decisions.read_text()) if decisions else {}
    skipped = []
    if out.exists() and not force:
        raise click.ClickException(f"{out} exists (it may hold your edits); pass --force to overwrite")
    config = get_config()
    title = kml_title(kml)
    places = Places(config["google_api_key"])
    _, existing = v2_place_ids(notion_client(config))

    spots, unresolved = {}, []
    for pm in tqdm(parse_kml(kml), desc="resolving"):
        # A decision can target one pin ("Name @lat,lon", 3 decimals) when names repeat.
        decision = decisions.get(f"{pm['name']} @{pm['lat']:.3f},{pm['lon']:.3f}") or decisions.get(pm["name"], {})
        if "skip" in decision:
            skipped.append(f"{pm['name']!r} ({pm['layer']}): {decision['skip']}")
            continue
        if decision.get("place_id"):
            g, match_notes, alternatives = places.get(decision["place_id"]), [], []
        else:
            g, match_notes, alternatives = places.search(pm["name"], pm["lat"], pm["lon"])
        kml_info = {"name": pm["name"], "layer": pm["layer"], "lat": pm["lat"], "lon": pm["lon"]}
        note, body = note_and_body(pm, title)
        if g is None:
            unresolved.append({"name": pm["name"], "place_id": None, "skip": False,
                               "category": [], "tags": [list_tag, *source_tags(pm["text"])],
                               "notes": [note], "body": [body] if body else [],
                               "review": match_notes, "kml": [kml_info]})
            continue
        s = spots.setdefault(g["id"], {
            "name": g["name"], "place_id": g["id"], "skip": False, "category": [],
            "tags": [list_tag], "notes": [], "body": [], "review": [], "kml": [], "google": {
                k: g[k] for k in ("address", "primary_type", "types", "status")}})
        s["kml"].append(kml_info)
        if match_notes:
            s["review"] += match_notes
            s.setdefault("alternatives", []).extend(a for a in alternatives if a not in s.get("alternatives", []))
        if note not in s["notes"]:
            s["notes"].append(note)
        if body and body not in s["body"]:
            s["body"].append(body)
        s["tags"] = list(dict.fromkeys(s["tags"] + source_tags(pm["text"])))

    for s in spots.values():
        if len(s["kml"]) > 1:
            s["review"].append("merged placemarks: " + ", ".join(repr(k["name"]) for k in s["kml"]))
        if s["google"]["status"] != "OPERATIONAL":
            s["review"].append(f"Google status {s['google']['status']}")
        if s["place_id"] in existing:
            s["review"].append(f"already in v2 as {existing[s['place_id']]!r}; write will skip it")

    entries = sorted(spots.values(), key=lambda s: (s["kml"][0]["layer"], s["name"])) + unresolved
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump({"map": map_slug, "spots": entries}, allow_unicode=True,
                                  sort_keys=False, width=1000))
    flagged = sum(bool(s["review"]) for s in entries)
    print(f"{len(entries)} spots ({len(unresolved)} unresolved, {flagged} flagged for review) → {out}")
    if skipped:
        print(f"skipped {len(skipped)} placemarks by decision:\n  " + "\n  ".join(skipped))


############################################
# write
############################################


def _rich_text(s: str) -> list[dict]:
    return [{"type": "text", "text": {"content": s[i:i + 2000]}} for i in range(0, len(s), 2000)]


def to_children(s: dict) -> list[dict]:
    """Page body: one heading + verbatim paragraphs per source section."""
    blocks = []
    for section in s.get("body", []):
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": _rich_text(section["heading"])}})
        for para in re.split(r"\n\s*\n", section["text"]):
            if para.strip():
                blocks.append({"type": "paragraph", "paragraph": {"rich_text": _rich_text(para.strip())}})
    return blocks


def to_properties(s: dict, g: dict, map_slug: str) -> dict:
    props = {
        "name": {"title": _rich_text(s["name"])},
        "map": {"select": {"name": map_slug}},
        "category": {"multi_select": [{"name": c} for c in s["category"]]},
        "flags": {"multi_select": [{"name": f} for f in s.get("flags", [])]},
        "closed": {"checkbox": g["status"] == "CLOSED_PERMANENTLY"},
        "tags": {"multi_select": [{"name": t} for t in dict.fromkeys(s["tags"])]},
        "notes": {"rich_text": _rich_text("\n".join(s["notes"]))},
        "added": {"date": {"start": TODAY}},
        "address": {"rich_text": _rich_text(g["address"])},
        "latitude": {"number": g["lat"]},
        "longitude": {"number": g["lon"]},
        "google_place_id": {"rich_text": _rich_text(g["id"])},
    }
    if g["website"]:
        props["website"] = {"url": g["website"]}
    return props


@cli.command()
@click.argument("review_file", type=click.Path(exists=True, path_type=pathlib.Path))
@click.option("--write", is_flag=True, help="create the rows in v2 (default: dry run)")
def write(review_file, write):
    """Create v2 rows from a reviewed file."""
    doc = yaml.safe_load(review_file.read_text())
    config = get_config()
    places = Places(config["google_api_key"])
    notion = notion_client(config)
    ds, existing = v2_place_ids(notion)
    categories = set(yaml.safe_load(MAPS_YAML.read_text())["categories"])

    todo, problems = [], []
    for s in doc["spots"]:
        if s.get("skip"):
            continue
        if not s.get("place_id"):
            problems.append(f"{s['name']!r}: no place_id (set one or skip: true)")
            continue
        if not s["category"] or set(s["category"]) - categories:
            problems.append(f"{s['name']!r}: category must be one or more of {sorted(categories)}")
        if any("," in t for t in s["tags"]):
            problems.append(f"{s['name']!r}: tags can't contain commas")
        if s["place_id"] in existing:
            print(f"skip (already in v2 as {existing[s['place_id']]!r}): {s['name']}")
            continue
        todo.append(s)
    if problems:
        raise click.ClickException("fix the review file first:\n  " + "\n  ".join(problems))
    if len({s["place_id"] for s in todo}) != len(todo):
        raise click.ClickException("the same place_id appears twice in the review file")

    print(f"{len(todo)} spots to create (map={doc['map']})")
    if not write:
        print("Dry run: nothing written. Re-run with --write.")
        return
    for s in tqdm(todo, desc="writing v2"):
        g = places.get(s["place_id"])
        notion.pages.create(parent={"type": "data_source_id", "data_source_id": ds},
                            properties=to_properties(s, g, doc["map"]), children=to_children(s))
    print(f"Created {len(todo)} spots in v2.")


if __name__ == "__main__":
    cli()
