import sys
import pathlib

from click import group, option, Path, pass_context, prompt
from loguru import logger

from myspots import (
    get_config,
    get_google_maps_client,
    query_places_api,
    get_detailed_place_data,
    NotionMySpotsStore,
    get_root_categories,
    get_placemark_style,
    get_placemark_description,
    kml_add_styles,
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
@pass_context
def add_place(ctx, query, location):
    config = ctx.obj["config"]

    # search google maps for places
    google_maps_client = get_google_maps_client(config)
    results = query_places_api(google_maps_client, query=query, location=location)
    if len(results) == 0:
        sys.exit(f"Query returned no results:\nquery: {query}\nlocation: {location}")
    for i, result in enumerate(results):
        pp_res = "Name: {name}\nAddress: {formatted_address}".format(**result)
        print(f"---\n{i+1}\n{pp_res}\n")

    # make selection
    selection = prompt("Please select option (0 = all, -1 = abort)", type=int)
    if selection == -1:
        sys.exit("Exit; no changes to MySpots store.")
    elif selection == 0:
        selected_results = results
    elif selection > 0 and selection <= len(results):
        selected_results = [results[selection - 1]]
    else:
        sys.exit(f"Abort: did not understand selection: {selection}")

    # add selected places to MySpots store
    myspots_store = NotionMySpotsStore(config)
    for result in selected_results:
        if myspots_store.spot_exists(result["place_id"]):
            logger.info("Already exists; skipping {}", result["place_id"])
            continue
        place = get_detailed_place_data(google_maps_client, result["place_id"])
        myspots_store.insert_spot(place)
        logger.info("Added {}", result["place_id"])


@cli.command(name="write-kml")
@option("--no-styles", is_flag=True)
@option("--default-invisible", is_flag=True)
@option("--hierarchical", is_flag=True)
@pass_context
def write_kml(ctx, no_styles, default_invisible, hierarchical):
    from fastkml import kml
    from shapely.geometry import Point

    store = NotionMySpotsStore(ctx.obj["config"])
    category_graph = store.category_graph()

    # construct KML containers - works bc of mutability
    ns = "{http://www.opengis.net/kml/2.2}"
    k = kml.KML()
    root_doc = kml.Document(
        ns=ns,
        id="myspots-document-id",
        name="myspots-document-name",
        description="myspots-document-description",
    )
    k.append(root_doc)

    folders = {}
    for place in store.iter_places():
        flags = set(f.name for f in place.flags)
        tags = set(t.name for t in place.tags)

        # skip certain places
        if "Permanently Closed" in flags:
            continue
        if "Lame" in flags:
            continue

        # each place may have multiple categories; process for each
        for category in get_root_categories(category_graph, place):
            category_name = (
                category_graph.nodes[category]["name"]
                if category != "Uncategorized"
                else "Uncategorized"
            )
            if category_name not in folders:
                folders[category_name] = kml.Folder(
                    ns=ns, id=category_name, name=category_name
                )
                root_doc.append(folders[category_name])
            style = (
                get_placemark_style(
                    flags, category_graph.nodes[category]["google_style_icon_code"]
                )
                if not no_styles and category != "Uncategorized"
                else "#icon-1899-757575-nodesc"
            )
            description = get_placemark_description(category_name, tags)
            p = kml.Placemark(
                ns=ns,
                id=str(place.id),
                name=place.name,
                styleUrl=style,
                description=description,
            )
            p.geometry = Point(place.longitude, place.latitude)
            folders[category_name].append(p)

    visibility = 0 if default_invisible else 1
    for container in folders.values():
        container.visibility = visibility
    root_doc.visibility = 1

    kml_add_styles(root_doc, category_graph, no_styles)

    print(k.to_string(prettyprint=True))
