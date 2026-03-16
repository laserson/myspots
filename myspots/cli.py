import json
import pathlib
import sys
from time import sleep

import ultimate_notion as uno
from click import Path, group, option, pass_context, prompt
from googlemaps.exceptions import ApiError
from loguru import logger

from myspots import (
    NotionMySpotsStore,
    build_kml,
    get_config,
    get_detailed_place_data,
    get_google_maps_client,
    query_places_api,
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
    """Manage your favorite places with Google Maps and Notion."""
    logger.add(sys.stderr, level="INFO")
    ctx.ensure_object(dict)
    ctx.obj["config"] = get_config(config_path)


@cli.command(name="add")
@option("--refresh-cache", is_flag=True, help="Force refresh of cached categories/tags/flags")
@pass_context
def add_tui(ctx, refresh_cache):
    """Interactive TUI for searching, selecting, and annotating places.

    Search Google Maps, multi-select results, assign categories/tags/flags,
    and push to Notion — all in one screen.
    """
    from myspots.tui import MySpotsApp

    app = MySpotsApp(config=ctx.obj["config"], refresh_cache=refresh_cache)
    app.run()


@cli.command(name="add-place")
@option("-q", "--query", prompt=True)
@option("-l", "--location", help="location to do search from (gets geocoded)")
@pass_context
def add_place(ctx, query, location):
    """Search Google Maps and add places to Notion (simple CLI prompt flow)."""
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

    # ask for any notes
    notes = prompt(
        "Enter any notes for these places (optional; return to skip)", default=""
    )
    notes = None if notes.strip() == "" else notes

    # add selected places to MySpots store
    myspots_store = NotionMySpotsStore(config)
    for result in selected_results:
        if myspots_store.spot_exists(result["place_id"]):
            logger.info("Already exists; skipping {}", result["place_id"])
            continue
        place = get_detailed_place_data(google_maps_client, result["place_id"])
        myspots_store.insert_spot(place, notes)
        logger.info("Added {}", result["place_id"])


@cli.command(name="write-kml")
@option("--no-styles", is_flag=True, help="Use default pin style for all places")
@option("--default-invisible", is_flag=True, help="Set folder visibility to off by default")
@pass_context
def write_kml(ctx, no_styles, default_invisible):
    """Export all places to KML (stdout) for Google Earth / My Maps."""
    store = NotionMySpotsStore(ctx.obj["config"])
    category_graph = store.category_graph()
    print(build_kml(store, category_graph, no_styles, default_invisible))


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _build(config, output_dir=None, mapbox_token=None) -> pathlib.Path:
    import os

    from myspots.site import build_site_data, render_site, write_site

    token = mapbox_token or config.get("mapbox_access_token") or os.environ.get("MAPBOX_ACCESS_TOKEN")
    if not token:
        sys.exit("Mapbox access token required. Use --mapbox-token, set mapbox_access_token in cred.yaml, or set MAPBOX_ACCESS_TOKEN env var.")

    store = NotionMySpotsStore(config)
    category_graph = store.category_graph()
    data = build_site_data(store, category_graph)
    html = render_site(data, token)
    out = pathlib.Path(output_dir) if output_dir else _repo_root() / "docs"
    write_site(html, out)
    logger.info("Wrote site to {}", out / "index.html")
    return out


@cli.command(name="build-site")
@option("--output-dir", default=None, type=Path(), help="Output directory (default: docs/ in repo root)")
@option("--mapbox-token", default=None, help="Mapbox access token")
@pass_context
def build_site(ctx, output_dir, mapbox_token):
    """Build the static map site from Notion data."""
    _build(ctx.obj["config"], output_dir, mapbox_token)


@cli.command(name="deploy")
@option("--mapbox-token", default=None, help="Mapbox access token")
@pass_context
def deploy(ctx, mapbox_token):
    """Build the site, commit, and push to GitHub Pages. No-ops if nothing changed."""
    import subprocess

    out = _build(ctx.obj["config"], mapbox_token=mapbox_token)
    repo_root = _repo_root()

    result = subprocess.run(
        ["git", "diff", "--quiet", str(out)],
        cwd=repo_root,
    )
    if result.returncode == 0:
        logger.info("No changes to deploy")
        return

    subprocess.run(["git", "add", str(out)], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Rebuild site"],
        cwd=repo_root, check=True,
    )
    subprocess.run(["git", "push"], cwd=repo_root, check=True)
    logger.info("Deployed")


@cli.command(name="refresh-store")
@option("--dry-run", is_flag=True)
@pass_context
def refresh_store(ctx, dry_run):
    """Sync Notion places with Google Maps (updated place IDs, closures)."""
    google_maps_client = get_google_maps_client(ctx.obj["config"])
    store = NotionMySpotsStore(ctx.obj["config"])

    # get the database to access schema information
    places_db = store.notion.get_db(store.notion_places_database_id)

    for place in store.iter_places(sort_oldest_first=True):
        sleep(0.1)
        logger.debug("Processing {}", place.title)
        try:
            data = json.loads(
                get_detailed_place_data(
                    google_maps_client, place.props["google_place_id"]
                ).google_json_data
            )
        except ApiError as e:
            if e.status == "NOT_FOUND":
                logger.warning(f"{place.title} place id NOT_FOUND")
                continue

        # refresh Place ID and JSON data
        if data["place_id"] != place.props["google_place_id"]:
            logger.info(f"{place.title}: PLACE DATA needs update")
            if not dry_run:
                action = prompt("Select action: [s]kip, [u]pdate, [a]bort", default="s")
                if action == "s":
                    pass
                elif action == "u":
                    place.props["google_place_id"] = data["place_id"]
                    place.props["google_json_data"] = json.dumps(data)
                    logger.info("Updated {}", place.title)
                elif action == "a":
                    sys.exit("Abort")
                else:
                    sys.exit(f"Abort: did not understand action: {action}")

        # refresh Permanently Closed flag
        google_status = (
            "CLOSED" if data.get("business_status") == "CLOSED_PERMANENTLY" else "OPEN"
        )
        myspots_status = (
            "CLOSED"
            if "Permanently Closed" in set(f.name for f in (place.props["flags"] or []))
            else "OPEN"
        )
        if google_status != myspots_status:
            logger.info(
                f"{place.title}: we say {myspots_status} but Google says {google_status}"
            )
            if not dry_run:
                action = prompt("Select action: [s]kip, [u]pdate, [a]bort", default="s")
                if action == "s":
                    pass
                elif action == "u":
                    logger.info("Updating {} bc diff in closure", place.title)
                    # In ultimate-notion, we can work with flags more directly
                    # Get current flag names and add "Permanently Closed" if not present
                    current_flag_names = set(f.name for f in (place.props["flags"] or []))
                    current_flag_names.add("Permanently Closed")
                    place.props["flags"] = list(current_flag_names)
                    place.props["google_json_data"] = json.dumps(data)
                elif action == "a":
                    sys.exit("Abort")
                else:
                    sys.exit(f"Abort: did not understand action: {action}")
