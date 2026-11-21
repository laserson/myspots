import json
import pathlib
import sys
from time import sleep

import ultimate_notion as uno
from click import Path, UsageError, argument, group, option, pass_context, prompt
from googlemaps.exceptions import ApiError
from loguru import logger

from myspots import (
    NotionMySpotsStore,
    build_kml,
    get_config,
    get_detailed_place_data,
    get_google_maps_client,
    query_places_api,
    resolve_instance_config,
)


@group(context_settings={"help_option_names": ["-h", "--help"]})
@option(
    "-c",
    "--config",
    "config_path",
    default=pathlib.Path.home() / ".config/myspots/cred.yaml",
    type=Path(dir_okay=False),
    help="myspots credentials (default ~/.config/myspots/cred.yaml; optional "
    "for build-site, which can read NOTION_TOKEN / MAPBOX_ACCESS_TOKEN from the env)",
)
@option(
    "-i",
    "--instance",
    "instance",
    default=None,
    help="Which instance/city to operate on (see 'instances' in config)",
)
@pass_context
def cli(ctx, config_path, instance):
    """Manage your favorite places with Google Maps and Notion."""
    logger.add(sys.stderr, level="INFO")
    ctx.ensure_object(dict)
    ctx.obj["full_config"] = get_config(config_path) if pathlib.Path(config_path).exists() else {}
    ctx.obj["instance"] = instance


def _resolved(ctx):
    """Resolve the selected instance for data commands; error if none given.

    Returns ``(config, instance)``. Data commands require an explicit
    ``-i/--instance`` — there is no default.
    """
    instance = ctx.obj["instance"]
    if not instance:
        instances = ctx.obj["full_config"].get("instances") or {}
        available = ", ".join(sorted(instances)) or "(none configured)"
        raise UsageError(
            "This command requires -i/--instance (no default).\n"
            f"Available instances: {available}\n"
            "Example: myspots -i nyc <command>"
        )
    return resolve_instance_config(ctx.obj["full_config"], instance), instance


@cli.command(name="add")
@option("--refresh-cache", is_flag=True, help="Force refresh of cached categories/tags/flags")
@pass_context
def add_tui(ctx, refresh_cache):
    """Interactive TUI for searching, selecting, and annotating places.

    Search Google Maps, multi-select results, assign categories/tags/flags,
    and push to Notion — all in one screen. Pre-loads the most recently used
    instance; switch between instances with the dropdown at the top.
    """
    from myspots.cache import get_last_instance
    from myspots.tui import MySpotsApp

    full_config = ctx.obj["full_config"]
    instances = full_config.get("instances") or {}
    if not instances:
        sys.exit("No 'instances' configured. Add an 'instances:' map to your config.")

    # Initial selection: --instance > most recently used > first available.
    initial = ctx.obj["instance"] or get_last_instance()
    if initial not in instances:
        initial = sorted(instances)[0]

    instance_options = sorted(
        ((cfg.get("title", slug), slug) for slug, cfg in instances.items()),
        key=lambda opt: opt[0].lower(),
    )

    app = MySpotsApp(
        full_config=full_config,
        instance_options=instance_options,
        instance=initial,
        refresh_cache=refresh_cache,
    )
    app.run()


@cli.command(name="add-place")
@option("-q", "--query", prompt=True)
@option("-l", "--location", help="location to do search from (gets geocoded)")
@pass_context
def add_place(ctx, query, location):
    """Search Google Maps and add places to Notion (simple CLI prompt flow)."""
    config, _ = _resolved(ctx)

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


@cli.command(name="import-kml")
@argument("kml_file", type=Path(exists=True, dir_okay=False))
@option("--dry-run", is_flag=True, help="Preview matches without writing to Notion")
@option(
    "--max-distance",
    default=250.0,
    type=float,
    help="Max meters between the KML pin and its matched Google place (default 250)",
)
@pass_context
def import_kml(ctx, kml_file, dry_run, max_distance):
    """Seed an instance from a Google My Maps KML export.

    Each placemark is re-resolved against the Google Places API (search by
    name, biased to the KML coordinates, picking the closest match within
    --max-distance). Imported places are tagged 'imported' plus
    'imported-<folder>' for the layer they came from. When the matched Google
    name differs from the KML name (same-script only), the place is marked
    '[OK?]' and tagged 'imported-review' so you can eyeball it in Notion. Run
    with --dry-run first to review matches before writing.
    """
    from myspots.kml_import import haversine_m, name_mismatch, parse_kml

    config, instance = _resolved(ctx)
    client = get_google_maps_client(config)
    store = NotionMySpotsStore(config)
    known_place_ids = store.fetch_known_place_ids()

    placemarks = parse_kml(kml_file)
    logger.info("Parsed {} placemarks from {}", len(placemarks), kml_file)

    added = skipped_dup = flagged = 0
    for pm in placemarks:
        results = query_places_api(client, query=pm.name, location=(pm.latitude, pm.longitude))
        if not results:
            print(f"[FLAG] {pm.name}: no Google results")
            flagged += 1
            continue

        # Pick the result whose geometry is closest to the KML pin.
        def _dist(r):
            loc = r["geometry"]["location"]
            return haversine_m(pm.latitude, pm.longitude, loc["lat"], loc["lng"])

        best = min(results, key=_dist)
        dist = _dist(best)
        if dist > max_distance:
            print(
                f"[FLAG] {pm.name}: nearest match '{best['name']}' is {dist:.0f}m away "
                f"(> {max_distance:.0f}m); skipping"
            )
            flagged += 1
            continue

        place_id = best["place_id"]
        if place_id in known_place_ids:
            print(f"[DUP]  {pm.name} → {best['name']} (already in instance)")
            skipped_dup += 1
            continue

        tags = ["imported"]
        if pm.folder:
            tags.append(f"imported-{pm.folder}")

        # The page title becomes the matched Google name, so a same-script name
        # mismatch (e.g. Onza→Akbar) means the proximity pick is probably wrong.
        # Tag it 'imported-review' so it's filterable in Notion afterwards.
        mismatch = name_mismatch(pm.name, best["name"])
        if mismatch:
            tags.append("imported-review")

        # Preserve the original KML name (so you can spot mismatches) and the
        # original KML description in the notes field.
        note_lines = [f'Imported from KML as: "{pm.name}"']
        if pm.description:
            note_lines.append(pm.description)
        notes = "\n".join(note_lines)

        print(
            f"{'[OK?]' if mismatch else '[OK] '}  {pm.name} → {best['name']}  ({dist:.0f}m)  "
            f"{best.get('formatted_address', '')}  tags={tags}"
        )

        if not dry_run:
            place = get_detailed_place_data(client, place_id)
            if place is None:
                print(f"[FLAG] {pm.name}: failed to fetch details for {place_id}")
                flagged += 1
                continue
            store.insert_spot(place, notes=notes, tags=tags)
            known_place_ids.add(place_id)
            added += 1
        sleep(0.1)  # be gentle with the Places API

    verb = "Would add" if dry_run else "Added"
    logger.info(
        "{} {} place(s); {} duplicate(s) skipped; {} flagged for review.",
        verb,
        len(placemarks) - skipped_dup - flagged if dry_run else added,
        skipped_dup,
        flagged,
    )
    if dry_run:
        logger.info("Dry run — nothing written. Re-run without --dry-run to import.")


@cli.command(name="write-kml")
@option("--no-styles", is_flag=True, help="Use default pin style for all places")
@option("--default-invisible", is_flag=True, help="Set folder visibility to off by default")
@pass_context
def write_kml(ctx, no_styles, default_invisible):
    """Export all places to KML (stdout) for Google Earth / My Maps."""
    config, _ = _resolved(ctx)
    store = NotionMySpotsStore(config)
    category_graph = store.category_graph()
    print(build_kml(store, category_graph, no_styles, default_invisible))


@cli.command(name="build-site")
@option("--output-dir", default="_site", type=Path(file_okay=False), show_default=True,
        help="Where to write the site")
@pass_context
def build_site(ctx, output_dir):
    """Build the static map site from MySpots DB v2.

    Builds every map in maps.yaml, or just one with -i/--instance <map>.
    """
    from myspots.site import build_site as _build_site

    only = [ctx.obj["instance"]] if ctx.obj["instance"] else None
    _build_site(pathlib.Path(output_dir), creds=ctx.obj["full_config"], only=only)


@cli.command(name="deploy")
def deploy():
    """Trigger the GitHub Actions workflow that builds and publishes the site."""
    import subprocess

    subprocess.run(["gh", "workflow", "run", "site.yml", "-f", "force=true"], check=True)
    logger.info("Triggered site build; follow it with: gh run watch")


@cli.command(name="refresh-store")
@option("--dry-run", is_flag=True)
@pass_context
def refresh_store(ctx, dry_run):
    """Sync Notion places with Google Maps (updated place IDs, closures)."""
    config, _ = _resolved(ctx)
    google_maps_client = get_google_maps_client(config)
    store = NotionMySpotsStore(config)

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
