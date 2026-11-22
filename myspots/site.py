"""Build the static map site from "MySpots DB v2" in Notion.

One Notion query fetches every spot; spots are split by their `map` property
and each map is rendered from templates/map.html into <out>/<map>/index.html,
plus a landing page and a build-hash.txt used by CI to skip no-op deploys.
"""

import hashlib
import importlib.resources
import json
import os
import re
from pathlib import Path

import yaml
from loguru import logger
from notion_client import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_CONFIG_PATH = REPO_ROOT / "maps.yaml"


def load_site_config(path: Path = SITE_CONFIG_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def _secret(env_var: str, cred_key: str, creds: dict) -> str:
    value = os.environ.get(env_var) or creds.get(cred_key)
    if not value:
        raise SystemExit(f"Missing {env_var} (env var) / {cred_key} (cred.yaml)")
    return value


############################################
# Credential guard
############################################

# Patterns for secrets that must never be published: Mapbox secret tokens,
# Notion integration tokens (old and new formats), Google API keys.
SECRET_PATTERNS = {
    "Mapbox secret token": re.compile(r"\bsk\.[A-Za-z0-9_-]{20,}"),
    "Notion token": re.compile(r"\b(?:ntn|secret)_[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
}


def check_mapbox_token(token: str):
    """The Mapbox token is embedded in every page, so it must be a public token."""
    if not token.startswith("pk."):
        raise SystemExit("Refusing to build: the Mapbox token must be a public 'pk.' token "
                         "(it is embedded in the published pages).")


def check_no_secrets(files: dict[str, str], secrets: list[str]):
    """Fail the build if any known secret value or secret-shaped string appears in the output."""
    problems = []
    for rel, content in files.items():
        for secret in secrets:
            if secret and secret in content:
                problems.append(f"{rel}: contains a configured secret value")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                problems.append(f"{rel}: contains something that looks like a {label}")
    if problems:
        raise SystemExit("Refusing to write site, possible credential leak:\n  " + "\n  ".join(problems))


############################################
# Fetch
############################################


def _text(prop: dict) -> str:
    key = "title" if prop["type"] == "title" else "rich_text"
    return "".join(x["plain_text"] for x in prop[key])


def _names(prop: dict) -> list[str]:
    return [o["name"] for o in prop["multi_select"]]


def fetch_spots(notion: Client, database_id: str) -> list[dict]:
    from myspots import _install_notion_rate_limit_retry

    _install_notion_rate_limit_retry(notion)
    ds = notion.databases.retrieve(database_id=database_id)["data_sources"][0]["id"]
    spots, cursor = [], None
    while True:
        r = notion.data_sources.query(
            data_source_id=ds, page_size=100, **({"start_cursor": cursor} if cursor else {})
        )
        for page in r["results"]:
            p = page["properties"]
            spots.append({
                "name": _text(p["name"]),
                "map": (p["map"]["select"] or {}).get("name"),
                "categories": _names(p["category"]),
                "flags": _names(p["flags"]),
                "closed": p["closed"]["checkbox"],
                "tags": _names(p["tags"]),
                "notes": _text(p["notes"]),
                "address": _text(p["address"]),
                "lat": p["latitude"]["number"],
                "lon": p["longitude"]["number"],
                "website": p["website"]["url"],
                "google_place_id": _text(p["google_place_id"]),
                "notion_url": page["url"],
            })
        if not r["has_more"]:
            return spots
        cursor = r["next_cursor"]


############################################
# Shape + render
############################################


def build_map_data(spots: list[dict], slug: str, cfg: dict) -> dict:
    order = list(cfg["categories"])
    rank = {c: i for i, c in enumerate(order)}
    hidden = set(cfg["hidden_flags"])
    places = []
    for s in spots:
        if s["map"] != slug or s["closed"] or hidden & set(s["flags"]):
            continue
        if s["lat"] is None or s["lon"] is None:
            logger.warning("Skipping {!r}: no coordinates", s["name"])
            continue
        cats = sorted(s["categories"], key=lambda c: rank.get(c, len(order))) or ["Uncategorized"]
        places.append({
            "name": s["name"], "address": s["address"], "lat": s["lat"], "lon": s["lon"],
            "website": s["website"], "notes": s["notes"], "google_place_id": s["google_place_id"],
            "notion_url": s["notion_url"],
            "flags": [f for f in cfg["flags"] if f in s["flags"]],
            "tags": sorted(s["tags"]),
            "categories": cats,
        })
    places.sort(key=lambda p: (p["name"].lower(), p["google_place_id"]))

    if places:
        lats, lons = [p["lat"] for p in places], [p["lon"] for p in places]
        bounds = [min(lons), min(lats), max(lons), max(lats)]
    else:
        bounds = [-74.05, 40.68, -73.90, 40.82]
    present = {c for p in places for c in p["categories"]}
    return {
        "title": cfg["maps"][slug]["title"],
        "places": places,
        "categories": sorted(present),
        "category_icons": {**cfg["categories"], "Uncategorized": "marker"},
        "primary_categories": cfg["primary_categories"],
        "flag_names": cfg["flags"],
        "all_tags": sorted({t for p in places for t in p["tags"]}),
        "initial_bounds": bounds,
    }


def render_map(data: dict, mapbox_token: str) -> str:
    template = importlib.resources.files("myspots.templates").joinpath("map.html").read_text()
    html = template.replace("__MAPBOX_TOKEN__", mapbox_token)
    html = html.replace("__TITLE__", data["title"])
    html = html.replace("__SPOTS_DATA__", json.dumps(data, ensure_ascii=False))
    html = html.replace("__INITIAL_BOUNDS__", json.dumps(data["initial_bounds"]))
    return html


def render_landing(slugs: list[str]) -> str:
    """Root landing page: one link per map, in config order."""
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


def build_site(out_dir: Path, creds: dict | None = None, only: list[str] | None = None) -> str:
    """Build every map (or just `only`) into out_dir. Returns the build hash."""
    creds = creds or {}
    cfg = load_site_config()
    notion_token = _secret("NOTION_TOKEN", "notion_api_token", creds)
    notion = Client(auth=notion_token)
    token = _secret("MAPBOX_ACCESS_TOKEN", "mapbox_access_token", creds)
    check_mapbox_token(token)
    other_secrets = [notion_token, os.environ.get("GOOGLE_API_KEY") or creds.get("google_api_key")]

    spots = fetch_spots(notion, cfg["notion_database_id"])
    unknown = sorted({s["map"] for s in spots} - set(cfg["maps"]), key=str)
    if unknown:
        logger.warning("Spots with a map not in maps.yaml (not published): {}", unknown)

    slugs = only or list(cfg["maps"])
    files = {"index.html": render_landing(list(cfg["maps"]))}
    for slug in slugs:
        data = build_map_data(spots, slug, cfg)
        files[f"{slug}/index.html"] = render_map(data, token)
        logger.info("{}: {} spots", slug, len(data["places"]))

    check_no_secrets(files, other_secrets)

    digest = hashlib.sha256()
    for rel in sorted(files):
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files[rel])
        digest.update(rel.encode() + b"\0" + files[rel].encode())
    build_hash = digest.hexdigest()
    (out_dir / "build-hash.txt").write_text(build_hash + "\n")
    logger.info("Wrote {} files to {} (hash {})", len(files), out_dir, build_hash[:12])
    return build_hash
