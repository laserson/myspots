import sys
import json
from pathlib import Path

import yaml
import pandas as pd
from googlemaps import Client as GoogleMapsClient
from airtable import Airtable


def get_config(path=None):
    if path is None:
        path = Path.home() / ".config/myspots/cred.yaml"
    with open(path, "r") as ip:
        config = yaml.safe_load(ip)
    return config


def get_airtable(config, table="places"):
    return Airtable(
        config["airtable_base_id"], table, api_key=config["airtable_api_key"]
    )


def get_airtable_as_dataframe(config, table="places", view=None):
    airtable = get_airtable(config, table)
    record_list = airtable.get_all(view=view)
    df = pd.DataFrame([record["fields"] for record in record_list])
    return df


def get_google_maps_client(config):
    return GoogleMapsClient(config["google_api_key"])


def query_places(google_maps_client, query, location=None, latlng=None, radius=None):
    # first find the location where the search is performed
    location_coords = None
    if latlng is not None:
        location_coords = latlng
    elif location is not None:
        geocode_api_response = google_maps_client.geocode(location)
        if len(geocode_api_response) == 0:
            print("No results for location geocode; using None", file=sys.stderr)
        else:
            addr = geocode_api_response[0]["formatted_address"]
            print(f"Found location: {addr}", file=sys.stderr)
            location_coords = geocode_api_response[0]["geometry"]["location"]

    places_api_response = google_maps_client.places(
        query, location=location_coords, radius=radius
    )

    if places_api_response["status"] == "ZERO_RESULTS":
        return []
    elif places_api_response["status"] != "OK":
        print(places_api_response, file=sys.stderr)
        return None
    else:
        return places_api_response["results"]


def place_exists(airtable, google_place_id):
    results = airtable.search("google_place_id", google_place_id)
    if len(results) > 0:
        return True
    else:
        return False


def get_detailed_place_data(google_maps_client, place_id):
    place_api_response = google_maps_client.place(place_id)
    if place_api_response["status"] != "OK":
        print(f"Failed to pull detailed record on {place_id}", file=sys.stderr)
        return None
    return place_api_response["result"]


def add_place_ids(google_maps_client, airtable, place_ids):
    num_added = 0
    for place_id in place_ids:
        if place_exists(airtable, place_id):
            print(f"Already exists; skipping {place_id}")
            continue
        place_data = get_detailed_place_data(google_maps_client, place_id)
        if place_data is None:
            print(f"Skipping {place_id}")
            continue
        record = {
            "name": place_data["name"],
            "address": place_data["formatted_address"],
            "website": place_data.get("website", ""),
            "latitude": place_data["geometry"]["location"]["lat"],
            "longitude": place_data["geometry"]["location"]["lng"],
            "google_place_id": place_data["place_id"],
            "google_json_data": json.dumps(place_data),
        }
        airtable.insert(record)
        num_added += 1
    return num_added


def extract_kml_placemarks(kml_path):
    from fastkml import KML

    k = KML()
    results = []
    with open(kml_path, "rb") as ip:
        k.from_string(ip.read())
    for document in k.features():
        for folder in document.features():
            folder_name = folder.name
            for placemark in folder.features():
                name = placemark.name
                lat = placemark.geometry.y
                lng = placemark.geometry.x
                results.append((name, lat, lng, folder_name))
    return results
