"""Migrate the v1 per-city Notion Places DBs into "MySpots DB v2".

Decisions (category/tag/flag mappings, merges, repoints) live in
scripts/v2_migration.yaml. v1 is only read, never modified.

    uv run python scripts/migrate_v2.py            # dry run: report only
    uv run python scripts/migrate_v2.py --write    # create rows in v2

--write is idempotent: rows whose google_place_id already exists in v2 are
skipped, so an interrupted run can simply be restarted.
"""

import collections
import datetime as dt
import json
import pathlib

import click
import yaml
from loguru import logger
from notion_client import Client
from tqdm import tqdm

from myspots import (
    NotionMySpotsStore,
    _install_notion_rate_limit_retry,
    get_config,
    get_detailed_place_data,
    get_google_maps_client,
    resolve_instance_config,
)

V2_DATABASE_ID = "4185ebf8-0ae5-81f1-95eb-c5f143cf4035"
PLAN_PATH = pathlib.Path(__file__).with_name("v2_migration.yaml")
TODAY = dt.date.today().isoformat()
RICH_TEXT_CHUNK = 2000  # Notion's per-text-object limit


def _uniq(xs):
    return list(dict.fromkeys(x for x in xs if x))


def load_v1(config) -> list[dict]:
    rows = []
    for inst in config["instances"]:
        rc = resolve_instance_config(config, inst)
        store = NotionMySpotsStore(rc)
        cats = {c["id"]: c["name"] for c in store.fetch_categories()}
        for pl in tqdm(store.iter_places(), desc=f"v1 {inst}"):
            p = pl.props
            orig = p.get("orig_date_added")
            rows.append({
                "page_id": str(pl.id),
                "map": inst,
                "name": str(pl.title),
                "added": str(orig)[:10] if orig else pl.created_time.date().isoformat(),
                "v1_categories": [cats[str(c.id)] for c in (p["primary_category"] or [])],
                "v1_flags": [f.name for f in (p["flags"] or [])],
                "v1_tags": [t.name for t in (p["tags"] or [])],
                "v1_notes": str(p.get("notes") or "").strip(),
                "address": str(p.get("address") or ""),
                "latitude": p.get("latitude"),
                "longitude": p.get("longitude"),
                "website": p.get("website") or None,
                "google_place_id": str(p.get("google_place_id") or "").strip(),
            })
    return rows


def transform(r: dict, plan: dict) -> dict:
    categories, tags = [], []
    for c in r["v1_categories"]:
        v2, extra = plan["category_map"][c]
        categories.append(v2)
        tags.append(extra)
    for t in r["v1_tags"]:
        tags.append(plan["tag_map"].get(t, t))
    categories = _uniq(categories)
    tags = _uniq(tags)
    if plan["drop_tags_matching_category"]:
        tags = [t for t in tags if t not in categories]

    flags = _uniq(plan["flag_map"][f] for f in r["v1_flags"]
                  if plan["flag_map"].get(f) and not plan["flag_map"][f].startswith("closed"))
    closed = "Permanently Closed" in r["v1_flags"]

    notes = []
    if r["v1_notes"]:
        source = "import" if r["v1_notes"].startswith("Imported from KML") else "me"
        notes.append(f"{r['added']} · {source}: {r['v1_notes']}")

    return {**r, "category": categories, "tags": tags, "flags": flags, "closed": closed, "notes": notes}


def fix_flags(s: dict, fixes: collections.Counter):
    f = s["flags"]
    if "favorite" in f and "visited" not in f:
        f.append("visited"); fixes["favorite → +visited"] += 1
    if "queued" in f and ({"visited", "favorite"} & set(f)):
        f.remove("queued"); fixes["queued dropped (visited)"] += 1
    if "lame" in f and "queued" in f:
        f.remove("queued"); fixes["queued dropped (lame)"] += 1
    if "favorite" in f and "lame" in f:
        logger.warning("favorite + lame on {!r}; keeping both for manual review", s["name"])


def apply_merges(spots: dict, plan: dict, gmaps) -> list[str]:
    log = []
    for m in plan["merges"]:
        keep, absorb = spots[m["keep"]], spots.pop(m["absorb"])
        keep["category"] = _uniq(keep["category"] + absorb["category"])
        keep["tags"] = [t for t in _uniq(keep["tags"] + absorb["tags"]) if t not in keep["category"]]
        keep["flags"] = _uniq(keep["flags"] + absorb["flags"])
        keep["added"] = min(keep["added"], absorb["added"])
        keep["notes"] = sorted(keep["notes"] + absorb["notes"])
        if m.get("note"):
            keep["notes"].append(f"{TODAY} · migration: merged “{absorb['name']}” into this entry; {m['note']}")
        if m.get("refetch_from_google"):
            g = get_detailed_place_data(gmaps, keep["google_place_id"])
            keep.update(address=g.address, latitude=g.latitude, longitude=g.longitude, website=g.website or keep["website"])
        log.append(f"merged {absorb['name']!r} ({absorb['address']}) → {keep['name']!r} ({keep['address']})")
    for rp in plan["repoint"]:
        s = spots[rp["page"]]
        old = s["google_place_id"]
        s["google_place_id"] = rp["new_google_place_id"]
        if rp.get("refetch_from_google"):
            g = get_detailed_place_data(gmaps, rp["new_google_place_id"])
            s.update(address=g.address, latitude=g.latitude, longitude=g.longitude, website=g.website or s["website"])
        if rp.get("clear_closed"):
            s["closed"] = False
        s["notes"].append(f"{TODAY} · migration: {rp['note']}")
        log.append(f"repointed {s['name']!r}: {old} → {s['google_place_id']} ({s['address']})")
    return log


def _rich_text(s: str) -> list[dict]:
    return [{"type": "text", "text": {"content": s[i:i + RICH_TEXT_CHUNK]}}
            for i in range(0, len(s), RICH_TEXT_CHUNK)]


def to_properties(s: dict) -> dict:
    props = {
        "name": {"title": _rich_text(s["name"])},
        "map": {"select": {"name": s["map"]}},
        "category": {"multi_select": [{"name": c} for c in s["category"]]},
        "flags": {"multi_select": [{"name": f} for f in s["flags"]]},
        "closed": {"checkbox": s["closed"]},
        "tags": {"multi_select": [{"name": t} for t in s["tags"]]},
        "notes": {"rich_text": _rich_text("\n".join(s["notes"]))},
        "added": {"date": {"start": s["added"]}},
        "address": {"rich_text": _rich_text(s["address"])},
        "latitude": {"number": s["latitude"]},
        "longitude": {"number": s["longitude"]},
        "google_place_id": {"rich_text": _rich_text(s["google_place_id"])},
    }
    if s["website"]:
        props["website"] = {"url": s["website"]}
    return props


def report(rows, spots, merge_log, fixes):
    out = []
    p = out.append
    p(f"v1 rows read: {len(rows)}   →   v2 rows to create: {len(spots)}")
    p("per map: " + ", ".join(f"{k}={v}" for k, v in collections.Counter(s["map"] for s in spots).most_common()))
    p("\ncategories (spots, multi-category counted in each):")
    for c, k in collections.Counter(c for s in spots for c in s["category"]).most_common():
        p(f"  {c:<12} {k}")
    p("\nflags: " + ", ".join(f"{k}={v}" for k, v in collections.Counter(f for s in spots for f in s["flags"]).most_common())
      + f", none={sum(not s['flags'] for s in spots)}, closed={sum(s['closed'] for s in spots)}")
    p("flag fixes: " + (", ".join(f"{k}={v}" for k, v in fixes.items()) or "none"))
    tags = collections.Counter(t for s in spots for t in s["tags"])
    p(f"\ntags: {len(tags)} distinct")
    p("  " + ", ".join(f"{t} ({k})" for t, k in sorted(tags.items())))
    p(f"\nadded range: {min(s['added'] for s in spots)} … {max(s['added'] for s in spots)}")
    p(f"spots with notes: {sum(bool(s['notes']) for s in spots)}; longest notes: {max(len(chr(10).join(s['notes'])) for s in spots)} chars")
    p("\nmerges / repoints:")
    out.extend("  " + x for x in merge_log)
    ids = collections.Counter(s["google_place_id"] for s in spots)
    problems = [f"missing google_place_id: {s['name']!r}" for s in spots if not s["google_place_id"]]
    problems += [f"duplicate google_place_id still present: {k}" for k, v in ids.items() if v > 1]
    problems += [f"no category: {s['name']!r}" for s in spots if not s["category"]]
    p("\nproblems: " + ("none" if not problems else ""))
    out.extend("  " + x for x in problems)
    return "\n".join(out), problems


def v2_data_source(notion: Client) -> str:
    return notion.databases.retrieve(database_id=V2_DATABASE_ID)["data_sources"][0]["id"]


def existing_v2_place_ids(notion: Client, ds: str) -> set[str]:
    ids, cursor = set(), None
    while True:
        r = notion.data_sources.query(data_source_id=ds, page_size=100, **({"start_cursor": cursor} if cursor else {}))
        for page in r["results"]:
            rt = page["properties"]["google_place_id"]["rich_text"]
            ids.add("".join(x["plain_text"] for x in rt))
        if not r["has_more"]:
            return ids
        cursor = r["next_cursor"]


@click.command()
@click.option("--write", is_flag=True, help="Create the rows in v2 (default: dry run).")
@click.option("--report-path", type=click.Path(path_type=pathlib.Path), default=None,
              help="Also save the report and transformed rows (JSON) next to this path.")
def main(write, report_path):
    config = get_config()
    plan = yaml.safe_load(PLAN_PATH.read_text())
    rows = load_v1(config)

    fixes = collections.Counter()
    spots = {}
    for r in rows:
        s = transform(r, plan)
        fix_flags(s, fixes)
        spots[s["page_id"]] = s
    merge_log = apply_merges(spots, plan, get_google_maps_client(config))
    spots = sorted(spots.values(), key=lambda s: (s["map"], s["added"], s["name"]))

    text, problems = report(rows, spots, merge_log, fixes)
    print(text)
    if report_path:
        report_path.write_text(text)
        report_path.with_suffix(".json").write_text(json.dumps(spots, indent=1, ensure_ascii=False, default=str))
    if not write:
        print("\nDry run: nothing written. Re-run with --write.")
        return
    if problems:
        raise click.ClickException("Refusing to write while problems are reported.")

    notion = Client(auth=config["notion_api_token"])
    _install_notion_rate_limit_retry(notion)
    ds = v2_data_source(notion)
    done = existing_v2_place_ids(notion, ds)
    todo = [s for s in spots if s["google_place_id"] not in done]
    print(f"\nv2 already has {len(done)} rows; creating {len(todo)}")
    for s in tqdm(todo, desc="writing v2"):
        notion.pages.create(parent={"type": "data_source_id", "data_source_id": ds}, properties=to_properties(s))

    final = collections.Counter()
    cursor = None
    while True:
        r = notion.data_sources.query(data_source_id=ds, page_size=100, **({"start_cursor": cursor} if cursor else {}))
        for page in r["results"]:
            sel = page["properties"]["map"]["select"]
            final[sel["name"] if sel else None] += 1
        if not r["has_more"]:
            break
        cursor = r["next_cursor"]
    expected = collections.Counter(s["map"] for s in spots)
    print("v2 per map:", dict(final), "| expected:", dict(expected), "| OK" if final == expected else "| MISMATCH")


if __name__ == "__main__":
    main()
