"""Keep MySpots DB v2 in step with Google Maps.

Each run re-checks the spots that are most overdue (never refreshed first, then
oldest `refreshed`), up to --max-checks. Open spots are due every OPEN_DAYS,
closed ones every CLOSED_DAYS. Per the MySpots Instructions page, only Google
fields, `closed` (set, never cleared) and `notes` (a new line at the top) are
changed; flags, name and added are never touched.

Things that need the user's decision are emailed (if SMTP is configured) and
everything is written to the run summary.

    uv run python -m myspots.refresh                 # dry run (no writes, no email)
    uv run python -m myspots.refresh --write --email

Env vars (or ~/.config/myspots/cred.yaml): NOTION_REFRESH_TOKEN (read + update
content), GOOGLE_API_KEY (Places API (New)); for --email: SMTP_USERNAME,
SMTP_PASSWORD (a Gmail app password), ALERT_EMAIL_TO.
"""

import dataclasses
import datetime as dt
import math
import os
import smtplib
import sys
import unicodedata
from email.message import EmailMessage

import click
from google.api_core import exceptions as gexc
from google.maps import places_v1
from loguru import logger
from notion_client import Client
from rapidfuzz import fuzz

from myspots.site import load_site_config

OPEN_DAYS = 30
CLOSED_DAYS = 180
MOVED_METERS = 100  # beyond this, update coordinates and log a note
NAME_SCORE_ALERT = 50  # below this, Google's name looks like a different business
# Pro-tier fields only: websiteUri would bill every call at the Enterprise tier.
FIELDS = ["id", "displayName", "formattedAddress", "location", "businessStatus"]
TODAY = dt.date.today().isoformat()


def _env(name: str, cred_key: str | None = None) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    if cred_key:
        from myspots import get_config

        try:
            return get_config().get(cred_key)
        except FileNotFoundError:
            return None
    return None


############################################
# Notion
############################################


def _text(prop: dict) -> str:
    key = "title" if prop["type"] == "title" else "rich_text"
    return "".join(x["plain_text"] for x in prop[key])


@dataclasses.dataclass
class Spot:
    page_id: str
    url: str
    name: str
    map: str
    flags: list[str]
    closed: bool
    address: str
    lat: float | None
    lon: float | None
    place_id: str
    refreshed: str | None
    added: str | None
    notes_rich_text: list[dict]

    @classmethod
    def from_page(cls, page: dict) -> "Spot":
        p = page["properties"]
        return cls(
            page_id=page["id"], url=page["url"], name=_text(p["name"]),
            map=(p["map"]["select"] or {}).get("name"),
            flags=[o["name"] for o in p["flags"]["multi_select"]], closed=p["closed"]["checkbox"],
            address=_text(p["address"]), lat=p["latitude"]["number"], lon=p["longitude"]["number"],
            place_id=_text(p["google_place_id"]),
            refreshed=(p["refreshed"]["date"] or {}).get("start"),
            added=(p["added"]["date"] or {}).get("start"),
            notes_rich_text=p["notes"]["rich_text"],
        )

    def due(self, today: dt.date) -> bool:
        if not self.refreshed:
            return True
        age = (today - dt.date.fromisoformat(self.refreshed[:10])).days
        return age >= (CLOSED_DAYS if self.closed else OPEN_DAYS)


def fetch_spots(notion: Client, data_source_id: str) -> list[Spot]:
    spots, cursor = [], None
    while True:
        r = notion.data_sources.query(data_source_id=data_source_id, page_size=100,
                                      **({"start_cursor": cursor} if cursor else {}))
        spots += [Spot.from_page(p) for p in r["results"]]
        if not r["has_more"]:
            return spots
        cursor = r["next_cursor"]


def _writable(rt: dict) -> dict:
    """A rich-text item from a read, reduced to what the API accepts on write."""
    return {"type": rt["type"], rt["type"]: rt[rt["type"]], "annotations": rt["annotations"]}


def prepend_notes(rich_text: list[dict], lines: list[str]) -> list[dict]:
    """Notes are newest-at-top: put new lines first, keep existing items (and their formatting) as is."""
    text = "\n".join(lines) + ("\n" if rich_text else "")
    return [{"type": "text", "text": {"content": text}}, *map(_writable, rich_text)]


############################################
# Compare with Google
############################################


def meters(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12_742_000 * math.asin(math.sqrt(a))


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


@dataclasses.dataclass
class Result:
    spot: Spot
    properties: dict = dataclasses.field(default_factory=dict)  # Notion property updates
    notes: list[str] = dataclasses.field(default_factory=list)  # new notes lines
    changes: list[str] = dataclasses.field(default_factory=list)  # routine, for the summary
    attention: list[str] = dataclasses.field(default_factory=list)  # needs the user


def compare(spot: Spot, place) -> Result:
    res = Result(spot)
    status = places_v1.Place.BusinessStatus(place.business_status).name
    name = place.display_name.text

    if place.id and place.id != spot.place_id:
        res.properties["google_place_id"] = {"rich_text": [{"type": "text", "text": {"content": place.id}}]}
        res.notes.append(f"{TODAY} · refresh: Google place ID changed ({spot.place_id} → {place.id})")
        res.changes.append("new place ID")

    if place.formatted_address and place.formatted_address != spot.address:
        res.properties["address"] = {"rich_text": [{"type": "text", "text": {"content": place.formatted_address}}]}
        res.changes.append(f"address: {spot.address!r} → {place.formatted_address!r}")
    if place.location.latitude or place.location.longitude:
        lat, lon = place.location.latitude, place.location.longitude
        dist = meters(spot.lat, spot.lon, lat, lon) if spot.lat is not None and spot.lon is not None else math.inf
        if dist > MOVED_METERS:
            res.properties["latitude"] = {"number": lat}
            res.properties["longitude"] = {"number": lon}
            if dist != math.inf:
                res.notes.append(f"{TODAY} · refresh: moved {dist:,.0f} m, now {place.formatted_address}")
                res.changes.append(f"moved {dist:,.0f} m")

    if status == "CLOSED_PERMANENTLY" and not spot.closed:
        res.properties["closed"] = {"checkbox": True}
        res.notes.append(f"{TODAY} · refresh: Google reports permanently closed")
        res.changes.append("now permanently closed")
        if {"queued", "favorite"} & set(spot.flags):
            res.attention.append(f"closed permanently (it was {', '.join(sorted({'queued', 'favorite'} & set(spot.flags)))})")
    elif status == "OPERATIONAL" and spot.closed:
        res.attention.append("marked closed, but Google says it's open: reopened? (closed is left as is)")
    elif status == "CLOSED_TEMPORARILY":
        res.changes.append("temporarily closed")

    score = max(fuzz.token_set_ratio(_norm(spot.name), _norm(name)),
                fuzz.partial_ratio(_norm(spot.name).replace(" ", ""), _norm(name).replace(" ", "")))
    if name and score < NAME_SCORE_ALERT:
        res.attention.append(f"Google now calls it {name!r}: renamed, or a different business?")

    res.properties["refreshed"] = {"date": {"start": TODAY}}
    return res


############################################
# Report
############################################


def render(results: list[Result], errors: list[str], checked: int, due: int, total: int) -> tuple[str, str]:
    """(markdown run summary, plain-text email body)."""
    attention = [r for r in results if r.attention]
    changed = [r for r in results if r.changes and not r.attention]
    md = [f"## MySpots refresh {TODAY}",
          f"Checked {checked} of {due} due ({total} spots in total). "
          f"{len(attention)} need attention, {len(changed)} updated, {len(errors)} errors."]
    txt = [f"MySpots refresh {TODAY}: {len(attention)} spot(s) need your attention.", ""]
    if attention:
        md.append("\n### Needs attention")
        for r in attention:
            s = r.spot
            md.append(f"- [{s.name}]({s.url}) ({s.map}): " + "; ".join(r.attention + r.changes))
            txt += [f"* {s.name} ({s.map})", *(f"    {a}" for a in r.attention + r.changes), f"    {s.url}", ""]
    if changed:
        md.append("\n### Updated")
        md += [f"- [{r.spot.name}]({r.spot.url}) ({r.spot.map}): " + "; ".join(r.changes) for r in changed]
    if errors:
        md.append("\n### Errors")
        md += [f"- {e}" for e in errors]
        txt += ["Errors:", *(f"* {e}" for e in errors)]
    return "\n".join(md) + "\n", "\n".join(txt) + "\n"


def send_email(subject: str, body: str):
    user, password, to = os.environ.get("SMTP_USERNAME"), os.environ.get("SMTP_PASSWORD"), os.environ.get("ALERT_EMAIL_TO")
    if not (user and password and to):
        logger.warning("SMTP_USERNAME / SMTP_PASSWORD / ALERT_EMAIL_TO not set; not emailing")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    logger.info("Emailed {} attention item(s)", body.count("\n* "))


############################################
# Main
############################################


@click.command()
@click.option("--write", is_flag=True, help="Update Notion (default: dry run).")
@click.option("--email", is_flag=True, help="Email items that need attention (needs SMTP_* env vars).")
@click.option("--max-checks", default=120, show_default=True, help="Google lookups per run.")
def main(write, email, max_checks):
    from myspots import _install_notion_rate_limit_retry

    notion_token = _env("NOTION_REFRESH_TOKEN", "notion_api_token")
    google_key = _env("GOOGLE_API_KEY", "google_api_key")
    if not (notion_token and google_key):
        raise SystemExit("Missing NOTION_REFRESH_TOKEN and/or GOOGLE_API_KEY")
    notion = Client(auth=notion_token)
    _install_notion_rate_limit_retry(notion)
    places = places_v1.PlacesClient(client_options={"api_key": google_key})

    database_id = load_site_config()["notion_database_id"]
    ds = notion.databases.retrieve(database_id=database_id)["data_sources"][0]["id"]
    spots = fetch_spots(notion, ds)
    today = dt.date.today()
    due = sorted((s for s in spots if s.due(today)), key=lambda s: (s.refreshed or "", s.added or "", s.name))
    batch = [s for s in due if s.place_id][:max_checks]
    errors = [f"{s.name} ({s.map}): no google_place_id" for s in due if not s.place_id]
    logger.info("{} spots, {} due, checking {}", len(spots), len(due), len(batch))

    results = []
    for s in batch:
        try:
            place = places.get_place(request={"name": f"places/{s.place_id}"},
                                     metadata=[("x-goog-fieldmask", ",".join(FIELDS))])
            res = compare(s, place)
        except gexc.NotFound:
            res = Result(s, properties={"refreshed": {"date": {"start": TODAY}}},
                         attention=["Google no longer finds this place ID: closed, or merged into another listing?"])
        except gexc.GoogleAPICallError as e:
            errors.append(f"{s.name} ({s.map}): Google error {e.code}: {e.message}")
            continue
        if res.notes:
            res.properties["notes"] = {"rich_text": prepend_notes(s.notes_rich_text, res.notes)}
        results.append(res)
        if write:
            notion.pages.update(page_id=s.page_id, properties=res.properties)

    md, txt = render(results, errors, len(batch), len(due), len(spots))
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    if not write:
        print("Dry run: nothing written to Notion, no email sent.")
        return
    if email and (any(r.attention for r in results) or errors):
        n = sum(bool(r.attention) for r in results)
        send_email(f"MySpots: {n} spot(s) need your attention" if n else "MySpots refresh: errors", txt)
    if len(errors) > len(batch) // 2:
        sys.exit("Most checks failed; see errors above")


if __name__ == "__main__":
    main()
