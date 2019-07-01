#! /usr/bin/env python

import os
import os.path as osp
import sys
import json
import random
import time

import yaml
from click import group, option, Path, pass_context, prompt
from fastkml import KML
from googlemaps import Client as GoogleMapsClient
from airtable.airtable import Airtable

# from geopy.distance import great_circle


def query_places(gmclient, query, location=None, latlng=None, radius=None):
    # first find the location where the search is performed
    location_coords = None
    if latlng is not None:
        location_coords = latlng
    elif location is not None:
        geocode_api_response = gmclient.geocode(location)
        if len(geocode_api_response) == 0:
            print("No results for location geocode; using None", file=sys.stderr)
        else:
            addr = geocode_api_response[0]["formatted_address"]
            print(f"Found location: {addr}", file=sys.stderr)
            location_coords = geocode_api_response[0]["geometry"]["location"]

    places_api_response = gmclient.places(
        query, location=location_coords, radius=radius
    )
    if places_api_response["status"] == "ZERO_RESULTS":
        return []
    elif places_api_response["status"] != "OK":
        print(places_api_response, file=sys.stderr)
        return None

    return places_api_response["results"]


def get_detailed_place_data(gmclient, place_id):
    place_api_response = gmclient.place(place_id)
    if place_api_response["status"] != "OK":
        print(f"Failed to pull detailed record on {place_id}", file=sys.stderr)
        return None
    return place_api_response["result"]


def place_exists(atclient, google_place_id):
    at_get_api_response = atclient.get(
        "places", filter_by_formula=f'{{google_place_id}} = "{google_place_id}"'
    )
    if len(at_get_api_response["records"]) > 0:
        return True
    else:
        return False


def add_place_ids(gmclient, atclient, place_ids, check_dup=False):
    num_ids = len(place_ids)
    num_added = 0
    for place_id in place_ids:
        if place_exists(atclient, place_id):
            print(f"Already exists; skipping {place_id}")
            continue
        place_data = get_detailed_place_data(gmclient, place_id)
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
        at_create_api_response = atclient.create("places", record)
        if "error" in at_create_api_response:
            print(f"Failed to add record for {place_id}")
            continue
        num_added += 1
    return num_added


def extract_kml_placemarks(kml_path):
    results = []
    k = KML()
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


@group(context_settings={"help_option_names": ["-h", "--help"]})
@option(
    "-c",
    "--config",
    "config_path",
    default=osp.expanduser("~/.config/myspots/cred.yaml"),
    type=Path(exists=True, dir_okay=False),
    help="myspots config (default ~/.config/myspots/cred.yaml)",
)
@pass_context
def cli(ctx, config_path):
    with open(config_path, "r") as ip:
        config = yaml.load(ip)
    ctx.obj["gmclient"] = GoogleMapsClient(config["google_api_key"])
    ctx.obj["atclient"] = Airtable(
        config["airtable_base_id"], config["airtable_api_key"]
    )


@cli.command(name="add-place")
@option("-q", "--query", prompt=True)
@option("-l", "--location", help="location to do search from (gets geocoded)")
@option("-r", "--radius", help="radius around location (meters)")
@pass_context
def add_place(ctx, query, location, radius):
    results = query_places(
        ctx.obj["gmclient"], query=query, location=location, radius=radius
    )
    if results is None:
        sys.exit("places API call failed")
    if len(results) == 0:
        sys.exit("query returned no results")

    for (i, result) in enumerate(results):
        print(
            "#{i}\nName: {name}\nID: {place_id}\nAddress: {formatted_address}\n".format(
                i=i + 1, **result
            )
        )

    selection = prompt("Please select option (0 = all, -1 = abort)", type=int)

    if selection == -1:
        sys.exit("Exit; no changes to Airtable.")
    elif selection == 0:
        selected_results = results
    elif selection > 0 and selection <= len(results):
        selected_results = [results[selection - 1]]
    else:
        sys.exit(f"Abort: did not understand selection: {selection}")

    num_added = add_place_ids(
        ctx.obj["gmclient"],
        ctx.obj["atclient"],
        [r["place_id"] for r in selected_results],
        check_dup=True,
    )

    print("Added {} out of {} attempted".format(num_added, len(selected_results)))


@cli.command(name="add-kml")
@option("-k", "--kml", "kml_path", type=Path(exists=True, dir_okay=False))
@option("--skip", default=0)
@pass_context
def add_kml(ctx, kml_path, skip):
    num_added = 0
    placemarks = extract_kml_placemarks(kml_path)
    exceptions = []
    try:
        for placemark in placemarks[skip:]:
            time.sleep(random.uniform(0.2, 1))
            print(placemark, file=sys.stderr)
            (name, lat, lng, folder) = placemark
            places = query_places(
                ctx.obj["gmclient"], name, latlng=(lat, lng), radius=500
            )
            if places is None:
                raise ValueError("places API query failed")
            elif len(places) == 0:
                print(f"failed to find {name}; skipping", file=sys.stderr)
                exceptions.append(placemark)
                continue
            elif len(places) > 1:
                print(f"found multiple {name}; skipping", file=sys.stderr)
                exceptions.append(placemark)
                continue
            place_id = places[0]["place_id"]

            # great_circle((lat, lng), (lat2, lng2)).km < 20
            num_added += add_place_ids(
                ctx.obj["gmclient"], ctx.obj["atclient"], [place_id], check_dup=True
            )
    finally:
        print("\n\nEXCEPTIONS\n\n")
        for placemark in exceptions:
            print(placemark)
        raise

    print("\nAdded {} out of {} attempted".format(num_added, len(placemarks)))


if __name__ == "__main__":
    cli(obj={})
