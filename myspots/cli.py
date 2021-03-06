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
        google_maps_client, airtable, [r["place_id"] for r in selected_results],
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

    df1 = get_airtable_as_dataframe(ctx.obj["config"], "categories", view="Grid view")
    df2 = df1.drop("places", axis=1).explode("parent")
    # TODO: need to repeatedly self-join until reach the root
    df3 = pd.merge(
        df2,
        df2,
        how="left",
        left_on="parent",
        right_on="airtable_record_id",
        suffixes=("", "_join"),
    )
    categories_df = df3
    df1 = get_airtable_as_dataframe(ctx.obj["config"], "places")
    df2 = df1.explode("primary_category")
    df3 = pd.merge(
        df2,
        categories_df,
        how="left",
        left_on="primary_category",
        right_on="airtable_record_id",
    )
    places_df = df3

    ns = "{http://www.opengis.net/kml/2.2}"
    k = kml.KML()

    # construct all nodes in hierarchy of categories
    folders = {
        "_root": kml.Document(
            ns=ns,
            id="myspots-document",
            name="myspots-document",
            description="myspots-document",
        ),
        "_uncat": kml.Folder(ns=ns, id="_uncat", name="uncategorized"),
    }
    for tup in categories_df.itertuples(index=False):
        folders[tup.category] = kml.Folder(ns=ns, id=tup.category, name=tup.category)

    # append folders into each other; works bc of mutability
    k.append(folders["_root"])
    folders["_root"].append(folders["_uncat"])
    for tup in categories_df.itertuples(index=False):
        parent = "_root" if pd.isna(tup.category_join) else tup.category_join
        container = parent if hierarchical else "_root"
        folders[container].append(folders[tup.category])

    # add places to approp folders
    for tup in places_df.itertuples(index=False):
        category = "_uncat" if pd.isna(tup.category) else tup.category
        style = None if no_styles else f"#style-{category}"
        p = kml.Placemark(ns=ns, id=str(tup.id), name=tup.name, styleUrl=style)
        p.geometry = Point(tup.longitude, tup.latitude)
        folders[category].append(p)

    # set default visibility
    visibility = 0 if default_invisible else 1
    for container in folders.values():
        container.visibility = visibility
    folders["_root"].visibility = 1

    # define icon styles for placemarks
    if not no_styles:
        doc = folders["_root"]
        doc.append_style(
            styles.Style(
                ns=ns,
                id="style-_uncat",
                styles=[
                    styles.IconStyle(
                        icon_href="https://raw.githubusercontent.com/google/material-design-icons/master/maps/1x_web/ic_place_black_48dp.png"
                    )
                ],
            )
        )
        for tup in categories_df.itertuples(index=False):
            if pd.isna(tup.icon_href):
                continue
            style = styles.Style(
                ns=ns,
                id=f"style-{tup.category}",
                styles=[styles.IconStyle(icon_href=tup.icon_href)],
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


@cli.command()
def streamlit():
    from streamlit.cli import _main_run
    from myspots import app

    filename = app.__file__
    _main_run(filename)
