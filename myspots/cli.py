import sys
import random
import time
import json
import pathlib

from click import group, option, Path, pass_context, prompt

from myspots.utils import (
    get_config,
    get_airtable,
    get_google_maps_client,
    query_places,
    add_place_ids,
    get_airtable_as_dataframe,
    get_root_category_mapping,
    get_places,
)


@group(context_settings={"help_option_names": ["-h", "--help"]})
@option(
    "-c",
    "--config",
    "config_path",
    default=pathlib.Path.home() / ".config/myspots/cred.yaml",
    type=Path(exists=True, dir_okay=False),
    help="myspots config (default ~/.config/myspots/cred.yaml)",
)
@pass_context
def cli(ctx, config_path):
    ctx.ensure_object(dict)
    ctx.obj["config"] = get_config(config_path)


@cli.command(name="add-place")
@option("-q", "--query", prompt=True)
@option("-l", "--location", help="location to do search from (gets geocoded)")
@option("-r", "--radius", help="radius around location (meters)")
@pass_context
def add_place(ctx, query, location, radius):
    google_maps_client = get_google_maps_client(ctx.obj["config"])
    airtable = get_airtable(ctx.obj["config"])

    results = query_places(
        google_maps_client, query=query, location=location, radius=radius
    )
    if results is None:
        sys.exit("places API call failed")
    if len(results) == 0:
        sys.exit("query returned no results")

    for (i, result) in enumerate(results):
        print(i + 1)
        print("  Name: {name}".format(**result))
        print("  ID: {place_id}".format(**result))
        print("  Address: {formatted_address}".format(**result))
        print()

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
        google_maps_client,
        airtable,
        [r["place_id"] for r in selected_results],
    )

    print("Added {} out of {} attempted".format(num_added, len(selected_results)))


@cli.command(name="write-kml")
@option("--no-styles", is_flag=True)
@option("--default-invisible", is_flag=True)
@option("--hierarchical", is_flag=True)
@pass_context
def write_kml(ctx, no_styles, default_invisible, hierarchical):
    from fastkml import kml, styles
    from shapely.geometry import Point
    import pandas as pd

    config = ctx.obj["config"]
    categories_df = get_root_category_mapping(config)
    places_df = get_places(config)

    # construct KML containers - works bc of mutability
    ns = "{http://www.opengis.net/kml/2.2}"
    k = kml.KML()
    root_doc = kml.Document(
        ns=ns,
        id="myspots-document",
        name="myspots-document",
        description="myspots-document",
    )
    k.append(root_doc)
    folders = {}
    folders["uncategorized"] = kml.Folder(
        ns=ns, id="uncategorized", name="uncategorized"
    )
    root_doc.append(folders["uncategorized"])
    for category_name in categories_df["category_root_name"].unique():
        folders[category_name] = kml.Folder(ns=ns, id=category_name, name=category_name)
        root_doc.append(folders[category_name])

    # add places to approp folders
    for tup in places_df.itertuples(index=False):
        if tup.is_perm_closed == True or tup.is_lame == True:
            continue
        category = (
            "uncategorized"
            if pd.isna(tup.category_root_name)
            else tup.category_root_name
        )
        if no_styles or pd.isna(tup.google_style_icon_code):
            style_url = "#icon-1899-757575-nodesc"
        else:
            if tup.is_favorite == True:
                icon_color = "F9A825"  # yellow
            elif tup.is_queued == True:
                icon_color = "558B2F"  # green
            elif tup.is_visited == True:
                icon_color = "0288D1"  # blue
            else:
                icon_color = "757575"  # grey
            style_url = f"#icon-{tup.google_style_icon_code}-{icon_color}-nodesc"
        p = kml.Placemark(ns=ns, id=str(tup.id), name=tup.name, styleUrl=style_url)
        p.geometry = Point(tup.longitude, tup.latitude)
        folders[category].append(p)

    # set default visibility
    visibility = 0 if default_invisible else 1
    for container in folders.values():
        container.visibility = visibility
    root_doc.visibility = 1

    # define styles for placemarks using Google icon ids
    # see: https://github.com/kitchen/kml-icon-converter/blob/master/style_map.csv
    doc = root_doc
    doc.append_style(
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
        for tup in categories_df.itertuples():
            if pd.isna(tup.google_style_icon_code):
                continue
            for icon_color in ["0288D1", "F9A825", "558B2F", "757575"]:
                style = styles.Style(
                    ns=ns,
                    id=f"icon-{tup.google_style_icon_code}-{icon_color}-nodesc",
                    styles=[
                        styles.IconStyle(
                            icon_href="https://www.gstatic.com/mapspro/images/stock/503-wht-blank_maps.png"
                        )
                    ],
                )
                doc.append_style(style)

    print(k.to_string(prettyprint=True))


@cli.command(name="add-kml")
@option("-k", "--kml", "kml_path", type=Path(exists=True, dir_okay=False))
@option("--skip", default=0)
@pass_context
def add_kml(ctx, kml_path, skip):
    from myspots.utils import extract_kml_placemarks

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


@cli.command(name="export-geojson")
@pass_context
def export_geojson(ctx):
    import geojson
    from zipfile import ZipFile

    config = ctx.obj["config"]

    airtable = get_airtable(config, "categories")
    record_list = airtable.get_all(view=None)
    categories = {}
    for record in record_list:
        id_ = record["id"]
        name = record["fields"]["category"]
        parent = record["fields"].get("parent", [None])[0]
        top_level = None
        categories[id_] = dict(id=id_, name=name, parent=parent)
    for id_ in categories.keys():
        top_level = id_
        while categories[top_level]["parent"] is not None:
            top_level = categories[top_level]["parent"]
        categories[id_]["top_level"] = categories[top_level]["name"]
    categories["UNCATEGORIZED"] = {
        "name": "UNCATEGORIZED",
        "top_level": "UNCATEGORIZED",
    }

    airtable = get_airtable(config, "places")
    record_list = airtable.get_all(view=None)
    features = []
    for record in record_list:
        id_ = record["fields"]["id"]
        geometry = geojson.Point(
            coordinates=(record["fields"]["longitude"], record["fields"]["latitude"])
        )
        tags = record["fields"].get("tags", [])
        cat_list = record["fields"].get("primary_category", ["UNCATEGORIZED"])
        for cat in cat_list:
            properties = {
                "name": record["fields"]["name"],
                "address": record["fields"]["address"],
                "primary_category": categories[cat]["name"],
                "top_level_category": categories[cat]["top_level"],
                "website": record["fields"].get("website", ""),
                "is_reviewed": record["fields"].get("is_reviewed", False),
                "is_visited": record["fields"].get("is_visited", False),
                "is_perm_closed": record["fields"].get("is_perm_closed", False),
                "is_lame": record["fields"].get("is_lame", False),
                "is_queued": record["fields"].get("is_queued", False),
                "tags": tags,
            }
            feature = geojson.Feature(id=id_, geometry=geometry, properties=properties)
            features.append(feature)
    feature_collection = geojson.FeatureCollection(features)

    with open("myspots.geojson", "w") as op:
        print(geojson.dumps(feature_collection), file=op)


@cli.command()
def streamlit():
    from streamlit.cli import _main_run
    from myspots import app

    filename = app.__file__
    _main_run(filename)
