import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import ultimate_notion as uno
import yaml
from fastkml import styles
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


def location_to_latlng(google_maps_client, location: str):
    geocode_api_response = google_maps_client.geocode(location)
    if len(geocode_api_response) == 0:
        logger.warning("No results for location geocode: {}", location)
        return None
    addr = geocode_api_response[0]["formatted_address"]
    logger.debug("Found location: {}", addr)
    return geocode_api_response[0]["geometry"]["location"]


def query_places_api(google_maps_client: Client, query, location=None):
    """Query the Google Places API to get list of results.

    Parameters
    ----------
    google_maps_client : googlemaps.Client
        Client object for the Google Maps API.
    query : str
        Search query to use.
    location : str, optional
        Location to append to query for geographic context, by default None

    Returns
    -------
    list
        List of results from the Google Places API.
    """
    places_api_response = google_maps_client.places(query, location=location)
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
    place_api_response = google_maps_client.place(place_id)
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


class NotionMySpotsStore:
    def __init__(self, config: dict):
        from notion_client import Client
        # Create notion client directly with token, then pass to Session
        notion_client = Client(auth=config["notion_api_token"])
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
            "notion_place": {
                "lat": place.latitude,
                "lon": place.longitude,
                "name": place.name,
                "address": place.address,
                "google_place_id": place.google_place_id,
            },
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


def get_placemark_style(flags: set[str], google_style_icon_code: str):
    if "Favorite" in flags:
        icon_color = "F9A825"  # yellow
    elif "Queued" in flags:
        icon_color = "558B2F"  # green
    elif "Visited" in flags:
        icon_color = "0288D1"  # blue
    else:
        icon_color = "757575"  # gray
    return f"#icon-{google_style_icon_code}-{icon_color}-nodesc"


def get_placemark_description(category: str, tags: set[str], notes: str):
    tags = " | ".join(tags)
    return f"{category}\n{tags}\n{notes}"


def kml_add_styles(root_doc, category_graph: DiGraph, no_styles: bool):
    # define styles for placemarks using Google icon ids
    # see: https://github.com/kitchen/kml-icon-converter/blob/master/style_map.csv
    
    ns = "{http://www.opengis.net/kml/2.2}"

    def create_style_map(icon_code: str, color: str) -> styles.StyleMap:
        style_id = f"icon-{icon_code}-{color}-nodesc"
        normal_style = styles.Style(
            ns=ns,
            id=f"{style_id}-normal",
            styles=[
                styles.IconStyle(
                    ns=ns,
                    icon_href=f"https://www.gstatic.com/mapspro/images/stock/{icon_code}-{color}.png",
                    scale=1.0
                )
            ]
        )
        highlight_style = styles.Style(
            ns=ns,
            id=f"{style_id}-highlight",
            styles=[
                styles.IconStyle(
                    ns=ns,
                    icon_href=f"https://www.gstatic.com/mapspro/images/stock/{icon_code}-{color}.png",
                    scale=1.1
                )
            ]
        )
        root_doc.append(normal_style)
        root_doc.append(highlight_style)
        
        style_map = styles.StyleMap(
            ns=ns,
            id=style_id,
            pairs=[
                styles.Pair(
                    ns=ns,
                    key=0,  # 0 = normal
                    style_url=styles.StyleUrl(ns=ns, url=f"#{style_id}-normal")
                ),
                styles.Pair(
                    ns=ns,
                    key=1,  # 1 = highlight
                    style_url=styles.StyleUrl(ns=ns, url=f"#{style_id}-highlight")
                )
            ]
        )
        return style_map

    # Default style for uncategorized places
    root_doc.append(create_style_map("503", "757575"))  # 503 is the default pin icon
    
    if not no_styles:
        for _, node in category_graph.nodes(data=True):
            if node.get("google_style_icon_code") is None:
                continue
            icon_code = node["google_style_icon_code"]
            # Create style maps for each state (Favorite, Queued, Visited, default)
            for icon_color in ["0288D1", "F9A825", "558B2F", "757575"]:
                style_map = create_style_map(icon_code, icon_color)
                root_doc.append(style_map)
