import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import lxml.etree
import ultimate_notion as uno
import yaml
from googlemaps import Client
from loguru import logger
from networkx import DiGraph

############################################
# Utils
############################################


def get_config(path=None):
    if path is None:
        path = Path.home() / ".config/myspots/cred.yaml"
    with open(path, "r") as ip:
        config = yaml.safe_load(ip)
    return config


def resolve_instance_config(config: dict, instance: str) -> dict:
    """Merge shared credentials with a named instance's settings.

    The config holds shared credentials (API tokens) at the top level and a
    per-instance ``instances`` map. Each instance contributes its own
    ``title`` and Notion database IDs. The returned dict is flat — shared keys
    overlaid with the instance's keys — plus an ``instance`` slug.
    """
    instances = config.get("instances")
    if not instances:
        raise ValueError(
            "No 'instances' section found in config. Add an 'instances:' map "
            "with one block per city/map (see README)."
        )
    if instance not in instances:
        available = ", ".join(sorted(instances)) or "(none)"
        raise ValueError(
            f"Unknown instance '{instance}'. Available instances: {available}"
        )
    resolved = {k: v for k, v in config.items() if k != "instances"}
    resolved.update(instances[instance])
    resolved["instance"] = instance
    return resolved


############################################
# Google Maps API
############################################

GooglePlaceID: TypeAlias = str


@dataclass(frozen=True)
class GooglePlace:
    name: str
    address: str
    latitude: float
    longitude: float
    google_place_id: GooglePlaceID
    google_json_data: str
    website: str | None = None


def get_google_maps_client(config) -> Client:
    return Client(config["google_api_key"])


# Google Places text-search caps the location-bias radius at 50 km.
PLACES_RADIUS_MAX_M = 50000


def geocode_location(google_maps_client, location: str):
    """Geocode a free-text location to its geometry (center + viewport).

    Returns the Geocoding API ``geometry`` dict (with ``location`` and
    ``viewport``), or None if the location can't be geocoded.
    """
    geocode_api_response = google_maps_client.geocode(location)
    if len(geocode_api_response) == 0:
        logger.warning("No results for location geocode: {}", location)
        return None
    logger.debug("Found location: {}", geocode_api_response[0]["formatted_address"])
    return geocode_api_response[0]["geometry"]


def _viewport_radius_m(geometry: dict) -> float | None:
    """Approximate radius (m) from a geocode geometry's viewport corner."""
    viewport = geometry.get("viewport")
    if not viewport:
        return None
    from math import asin, cos, radians, sin, sqrt

    center, ne = geometry["location"], viewport["northeast"]
    dlat = radians(ne["lat"] - center["lat"])
    dlng = radians(ne["lng"] - center["lng"])
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(center["lat"])) * cos(radians(ne["lat"])) * sin(dlng / 2) ** 2
    )
    return 2 * 6371000.0 * asin(sqrt(a))


def query_places_api(google_maps_client: Client, query, location=None):
    """Query the Google Places API to get list of results.

    ``location`` may be a free-text string (e.g. "Tel Aviv"), which is geocoded
    and used as a location bias with a radius sized to the geocoded area (capped
    at the Places 50 km limit), or a (lat, lng) pair used directly as a bias
    (e.g. from the KML importer). Note a country-sized area exceeds the 50 km
    cap, so scope to a city for an effective bias.
    """
    params = {}
    if isinstance(location, str) and location.strip():
        geometry = geocode_location(google_maps_client, location)
        if geometry:
            params["location"] = geometry["location"]
            radius = _viewport_radius_m(geometry)
            params["radius"] = int(min(radius, PLACES_RADIUS_MAX_M)) if radius else PLACES_RADIUS_MAX_M
    elif location is not None:
        params["location"] = location

    places_api_response = google_maps_client.places(query, **params)
    if places_api_response["status"] != "OK":
        logger.error(
            "Failed to query places API for {}\n{}", query, places_api_response
        )
        return []
    elif places_api_response["status"] == "ZERO_RESULTS":
        logger.warning("Zero results for query: {}", query)
        return []
    else:
        return places_api_response["results"]


def get_detailed_place_data(google_maps_client, place_id) -> GooglePlace:
    """Query the Google Place API to get detailed information on a place.

    Parameters
    ----------
    google_maps_client : googlemaps.Client
        Client object for the Google Maps API.
    place_id : str
        Place ID to query.
    """
    place_api_response = google_maps_client.place(
        place_id,
        fields=["name", "formatted_address", "website", "geometry", "place_id", "business_status"],
    )
    if place_api_response["status"] != "OK":
        logger.error(f"Failed to pull detailed record on {place_id}")
        return None
    result = place_api_response["result"]
    return GooglePlace(
        name=result["name"],
        address=result["formatted_address"],
        website=result.get("website", None),
        latitude=result["geometry"]["location"]["lat"],
        longitude=result["geometry"]["location"]["lng"],
        google_place_id=result["place_id"],
        google_json_data=json.dumps(result),
    )


############################################
# MySpots datastore
############################################


def _install_notion_rate_limit_retry(client, max_attempts: int = 6, max_delay: float = 30.0):
    """Make a notion_client retry on HTTP 429 with backoff (via tenacity).

    Notion caps API traffic (~3 req/s); when exceeded it returns a
    ``rate_limited`` error with a ``Retry-After`` header. Patching the
    client's low-level ``request`` makes every call resilient — including the
    per-page requests inside paginated queries and individual page inserts —
    without each caller needing its own retry loop.
    """
    from notion_client.errors import APIResponseError
    from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

    original_request = client.request
    exponential = wait_exponential(multiplier=1, max=max_delay)

    def _is_rate_limited(exc) -> bool:
        return isinstance(exc, APIResponseError) and getattr(exc, "code", None) == "rate_limited"

    def _wait(retry_state) -> float:
        # Honor Notion's Retry-After header when present, else back off.
        exc = retry_state.outcome.exception()
        headers = getattr(exc, "headers", None)
        retry_after = headers.get("Retry-After") if headers is not None else None
        return float(retry_after) if retry_after else exponential(retry_state)

    def _log(retry_state) -> None:
        logger.warning(
            "Notion rate limited; retrying in {:.0f}s (attempt {}/{})",
            retry_state.next_action.sleep, retry_state.attempt_number, max_attempts,
        )

    @retry(
        retry=retry_if_exception(_is_rate_limited),
        wait=_wait,
        stop=stop_after_attempt(max_attempts),
        before_sleep=_log,
        reraise=True,
    )
    @functools.wraps(original_request)
    def request_with_retry(*args, **kwargs):
        return original_request(*args, **kwargs)

    client.request = request_with_retry


class NotionMySpotsStore:
    def __init__(self, config: dict):
        from notion_client import Client

        # ultimate-notion's Session is a process-wide singleton that refuses a
        # second concurrent instance. Reuse the active one if present (e.g. when
        # deploying several instances in one run, or switching instances in the
        # TUI) — all instances share the same Notion token, so the connection is
        # interchangeable; only the database IDs differ.
        if uno.Session._active_session is not None:
            self.notion = uno.Session._active_session
        else:
            notion_client = Client(auth=config["notion_api_token"])
            _install_notion_rate_limit_retry(notion_client)
            self.notion = uno.Session(client=notion_client)
        self.notion_categories_database_id = config["notion_categories_database_id"]
        self.notion_places_database_id = config["notion_places_database_id"]

    def insert_spot(self, place: GooglePlace, notes=None, category_ids=None, tags=None, flags=None):
        places_db = self.notion.get_db(self.notion_places_database_id)
        kwargs = {
            "name": place.name,
            "address": place.address,
            "latitude": place.latitude,
            "longitude": place.longitude,
            "google_place_id": place.google_place_id,
            "google_json_data": place.google_json_data,
        }
        if place.website:
            kwargs["website"] = place.website
        if notes:
            kwargs["notes"] = notes
        page = places_db.create_page(**kwargs)

        # Set category relations (two-step: create page, then set props)
        if category_ids:
            cat_pages = [self.notion.get_page(cid) for cid in category_ids]
            page.props["primary_category"] = cat_pages

        if tags:
            page.props["tags"] = tags

        if flags:
            page.props["flags"] = flags

    def fetch_tag_options(self) -> list[str]:
        """Get available tag names from places DB schema."""
        places_db = self.notion.get_db(self.notion_places_database_id)
        tags_prop = places_db.schema.get_prop("tags")
        return [opt.name for opt in tags_prop.options]

    def fetch_flag_options(self) -> list[str]:
        """Get available flag names from places DB schema."""
        places_db = self.notion.get_db(self.notion_places_database_id)
        flags_prop = places_db.schema.get_prop("flags")
        return [opt.name for opt in flags_prop.options]

    def fetch_categories(self) -> list[dict]:
        """Get all categories with id, name, parent_id."""
        categories_db = self.notion.get_db(self.notion_categories_database_id)
        result = []
        for category in categories_db.query.execute():
            parent = category.props["parent"]
            parent_id = parent[0].id if parent else None
            result.append({
                "id": str(category.id),
                "name": str(category.props["category"]),
                "parent_id": str(parent_id) if parent_id else None,
            })
        return result

    def fetch_known_place_ids(self) -> set[str]:
        """Get all google_place_ids currently in the places DB."""
        result = set()
        for place in self.iter_places():
            pid = place.props.get("google_place_id")
            if pid:
                result.add(pid)
        return result

    def spot_exists(self, place_id: GooglePlaceID):
        places_db = self.notion.get_db(self.notion_places_database_id)
        pages = places_db.query.filter(
            uno.prop("google_place_id") == place_id
        ).execute()
        return len(list(pages)) > 0

    def category_graph(self) -> DiGraph:
        categories_db = self.notion.get_db(self.notion_categories_database_id)
        graph = DiGraph()
        for category in categories_db.query.execute():
            graph.add_node(
                category.id,
                name=category.props["category"],
                google_style_icon_code=category.props["google_style_icon_code"],
            )
            parent = category.props["parent"]
            if parent:
                graph.add_edge(parent[0].id, category.id)
        return graph

    def iter_places(self, sort_oldest_first=False):
        places_db = self.notion.get_db(self.notion_places_database_id)
        query = places_db.query
        if sort_oldest_first:
            query = query.sort(uno.prop("last_modified").asc())
        for place in query.execute():
            yield place


def get_root_categories(
    category_graph: DiGraph, place
) -> list[str]:
    primary_category = place.props["primary_category"]
    if not primary_category:
        return ["Uncategorized"]
    root_categories = []
    for category_ref in primary_category:
        root_category_id = category_ref.id
        while category_graph.in_degree(root_category_id) > 0:
            root_category_id = list(category_graph.predecessors(root_category_id))[0]
        root_categories.append(root_category_id)
    return list(set(root_categories))


############################################
# KML export utils
############################################

KML_NS = "http://www.opengis.net/kml/2.2"
ICON_BASE_URL = "https://www.gstatic.com/mapspro/images/stock"
# Flag → color mapping for icon styles
# see: https://github.com/kitchen/kml-icon-converter/blob/master/style_map.csv
FLAG_COLORS = [
    ("Favorite", "F9A825"),   # yellow
    ("Queued", "558B2F"),     # green
    ("Visited", "0288D1"),    # blue
]
DEFAULT_COLOR = "757575"      # gray
DEFAULT_ICON_CODE = "503"     # default pin


def _kml_sub(parent, tag, text=None, **attribs):
    el = lxml.etree.SubElement(parent, f"{{{KML_NS}}}{tag}", **attribs)
    if text is not None:
        el.text = str(text)
    return el


def _icon_color_for_flags(flags: set[str]) -> str:
    for flag, color in FLAG_COLORS:
        if flag in flags:
            return color
    return DEFAULT_COLOR


def _add_style_map(doc, icon_code: str, color: str):
    """Add a Style (normal), Style (highlight), and StyleMap to a Document."""
    style_id = f"icon-{icon_code}-{color}-nodesc"
    icon_url = f"{ICON_BASE_URL}/{icon_code}-{color}.png"

    for suffix, scale in [("normal", "1.0"), ("highlight", "1.1")]:
        style = _kml_sub(doc, "Style", id=f"{style_id}-{suffix}")
        icon_style = _kml_sub(style, "IconStyle")
        _kml_sub(icon_style, "scale", scale)
        icon = _kml_sub(icon_style, "Icon")
        _kml_sub(icon, "href", icon_url)

    style_map = _kml_sub(doc, "StyleMap", id=style_id)
    for key, suffix in [("normal", "normal"), ("highlight", "highlight")]:
        pair = _kml_sub(style_map, "Pair")
        _kml_sub(pair, "key", key)
        _kml_sub(pair, "styleUrl", f"#{style_id}-{suffix}")


def build_kml(store, category_graph: DiGraph, no_styles: bool, default_invisible: bool) -> str:
    """Build a KML string from the places store."""
    root = lxml.etree.Element(f"{{{KML_NS}}}kml", nsmap={None: KML_NS})
    doc = _kml_sub(root, "Document")
    _kml_sub(doc, "name", "MySpots")

    # Add styles
    _add_style_map(doc, DEFAULT_ICON_CODE, DEFAULT_COLOR)
    if not no_styles:
        for _, node in category_graph.nodes(data=True):
            icon_code = node.get("google_style_icon_code")
            if icon_code is None:
                continue
            for _, color in FLAG_COLORS:
                _add_style_map(doc, icon_code, color)
            _add_style_map(doc, icon_code, DEFAULT_COLOR)

    # Add placemarks grouped by root category
    folders = {}
    for place in store.iter_places():
        flags = set(f.name for f in (place.props["flags"] or []))
        tags = set(t.name for t in (place.props["tags"] or []))
        notes = place.props["notes"] or ""

        if "Permanently Closed" in flags or "Lame" in flags:
            continue

        for category in get_root_categories(category_graph, place):
            if category == "Uncategorized":
                cat_name = "Uncategorized"
                icon_code = DEFAULT_ICON_CODE
            else:
                cat_name = category_graph.nodes[category]["name"]
                icon_code = category_graph.nodes[category].get("google_style_icon_code", DEFAULT_ICON_CODE)

            if cat_name not in folders:
                folder = _kml_sub(doc, "Folder")
                _kml_sub(folder, "name", cat_name)
                _kml_sub(folder, "visibility", "0" if default_invisible else "1")
                folders[cat_name] = folder

            color = DEFAULT_COLOR if no_styles or category == "Uncategorized" else _icon_color_for_flags(flags)
            style_id = f"icon-{icon_code}-{color}-nodesc"
            description = f"{cat_name}\n{' | '.join(tags)}\n{notes}"

            pm = _kml_sub(folders[cat_name], "Placemark")
            _kml_sub(pm, "name", place.title)
            _kml_sub(pm, "description", description)
            _kml_sub(pm, "styleUrl", f"#{style_id}")
            point = _kml_sub(pm, "Point")
            _kml_sub(point, "coordinates", f"{place.props['longitude']},{place.props['latitude']},0")

    return lxml.etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode()
