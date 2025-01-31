import json
from typing import TypeAlias
from dataclasses import dataclass, asdict
from pathlib import Path

import notional
import yaml
from networkx import DiGraph
from googlemaps import Client
from fastkml import styles
from loguru import logger


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
        Location to search around, by default None

    Returns
    -------
    list
        List of results from the Google Places API.
    """
    latlng = location_to_latlng(google_maps_client, location) if location else None
    places_api_response = google_maps_client.places(query, location=latlng)
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
        self.notion = notional.connect(auth=config["notion_api_token"])
        self.notion_categories_database_id = config["notion_categories_database_id"]
        self.notion_places_database_id = config["notion_places_database_id"]

    def insert_spot(self, place: GooglePlace, notes: str = None):
        places_db = self.notion.databases.retrieve(self.notion_places_database_id)
        NotionPlace = notional.orm.connected_page(
            session=self.notion, source_db=places_db
        )
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
        NotionPlace.create(**kwargs)

    def spot_exists(self, place_id: GooglePlaceID):
        query = self.notion.databases.query(self.notion_places_database_id).filter(
            property="google_place_id",
            rich_text=notional.query.TextCondition(equals=place_id),
        )
        return query.first() is not None

    def category_graph(self) -> DiGraph:
        categories_db = self.notion.databases.retrieve(
            self.notion_categories_database_id
        )
        NotionCategory = notional.orm.connected_page(
            session=self.notion, source_db=categories_db
        )
        graph = DiGraph()
        for category in NotionCategory.query().execute():
            graph.add_node(
                category.id,
                name=category.category,
                google_style_icon_code=category.google_style_icon_code,
            )
            if len(category.parent.relation) > 0:
                graph.add_edge(category.parent.relation[0].id, category.id)
        return graph

    def iter_places(self, sort_oldest_first=False):
        places_db = self.notion.databases.retrieve(self.notion_places_database_id)
        NotionPlace = notional.orm.connected_page(
            session=self.notion, source_db=places_db
        )
        query = NotionPlace.query()
        if sort_oldest_first:
            query = query.sort(
                property="last_modified",
                direction=notional.query.SortDirection.ASCENDING,
            )
        for place in query.execute():
            yield place


def get_root_categories(
    category_graph: DiGraph, place: notional.orm.ConnectedPage
) -> list[str]:
    if len(place.primary_category.relation) == 0:
        return ["Uncategorized"]
    root_categories = []
    for category_ref in place.primary_category.relation:
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
    root_doc.append(
        styles.Style(
            ns=ns,
            id="icon-1899-757575-nodesc",
            styles=[
                styles.IconStyle(
                    icon_href="https://www.gstatic.com/mapspro/images/stock/503-wht-blank_maps.png"
                )
            ],
        )
    )
    if not no_styles:
        for _, node in category_graph.nodes(data=True):
            if node.get("google_style_icon_code") is None:
                continue
            for icon_color in ["0288D1", "F9A825", "558B2F", "757575"]:
                style = styles.Style(
                    ns=ns,
                    id=f"icon-{node['google_style_icon_code']}-{icon_color}-nodesc",
                    styles=[
                        styles.IconStyle(
                            icon_href="https://www.gstatic.com/mapspro/images/stock/503-wht-blank_maps.png"
                        )
                    ],
                )
                root_doc.append(style)
