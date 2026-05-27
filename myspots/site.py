import importlib.resources
import json
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

from myspots import NotionMySpotsStore, get_root_categories


def build_site_data(store: NotionMySpotsStore, category_graph, title: str = "MySpots") -> dict:
    places = []
    root_category_names = set()
    all_tags = set()

    for place in tqdm(store.iter_places(), desc="Fetching places"):
        flags = set(f.name for f in (place.props["flags"] or []))
        if "Permanently Closed" in flags or "Lame" in flags:
            continue

        tags = [t.name for t in (place.props["tags"] or [])]
        all_tags.update(tags)
        roots = get_root_categories(category_graph, place)
        root_names = []
        for r in roots:
            name = (
                category_graph.nodes[r]["name"]
                if r != "Uncategorized"
                else "Uncategorized"
            )
            root_names.append(name)
            root_category_names.add(name)

        places.append({
            "name": place.title,
            "address": place.props["address"],
            "lat": place.props["latitude"],
            "lon": place.props["longitude"],
            "website": place.props.get("website"),
            "notes": place.props.get("notes"),
            "flags": sorted(flags - {"Permanently Closed", "Lame"}),
            "tags": tags,
            "root_categories": root_names,
        })

    # Compute initial bounds from all places
    if places:
        lats = [p["lat"] for p in places]
        lons = [p["lon"] for p in places]
        initial_bounds = [min(lons), min(lats), max(lons), max(lats)]
    else:
        initial_bounds = [-74.05, 40.68, -73.90, 40.82]

    return {
        "title": title,
        "places": places,
        "root_categories": sorted(root_category_names),
        "all_tags": sorted(all_tags),
        "initial_bounds": initial_bounds,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def render_site(data: dict, mapbox_token: str) -> str:
    template = importlib.resources.files("myspots.templates").joinpath("map.html").read_text()
    html = template.replace("__MAPBOX_TOKEN__", mapbox_token)
    html = html.replace("__TITLE__", data.get("title", "MySpots"))
    html = html.replace("__SPOTS_DATA__", json.dumps(data))
    html = html.replace("__INITIAL_BOUNDS__", json.dumps(data["initial_bounds"]))
    return html


def write_site(html: str, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(html)


def render_landing(slugs: list[str]) -> str:
    """Render the root landing page: one link per instance, in config order."""
    links = "\n".join(f'<a href="{slug}/">{slug}</a>' for slug in slugs)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MySpots</title>
<style>
  body {{ font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
         "Liberation Mono", monospace;
         margin: 2rem; line-height: 2; }}
  a {{ display: block; font-size: 1.75rem; }}
</style>
</head>
<body>
{links}
</body>
</html>
"""


def write_landing(html: str, docs_dir: Path):
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "index.html").write_text(html)
