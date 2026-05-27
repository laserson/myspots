# myspots

A Python CLI tool for managing your favorite places using Google Maps and Notion.

## Overview

`myspots` helps you discover, organize, and publish places of interest. Search for places using the Google Maps API, store them in a Notion database with custom categories and flags, and publish them as interactive web maps (Mapbox GL) on GitHub Pages. You can run multiple **instances** — one per city/map — from a single install, import an existing Google My Maps KML export to seed a new instance, and export back to KML for Google Earth or Google My Maps.

## Installation

This project uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
# Clone the repository
git clone https://github.com/laserson/myspots.git
cd myspots

# Install dependencies (keeps local .venv in sync for development)
uv sync

# Install the CLI tool globally (editable so source changes are reflected immediately)
uv tool install --editable .
```

The editable install reflects source-code edits live, but **not** dependency
changes — after pulling changes that add or bump a dependency, refresh the
tool's environment with `uv tool upgrade myspots`.

## Configuration

Create a configuration file at `~/.config/myspots/cred.yaml`. Shared
credentials live at the top level; each city/map is an entry under
`instances`:

```yaml
google_api_key: YOUR_GOOGLE_MAPS_API_KEY
notion_api_token: YOUR_NOTION_API_TOKEN
mapbox_access_token: YOUR_MAPBOX_TOKEN

# Categories can be shared across all instances (recommended), so the
# taxonomy ("Restaurant", "Museum", …) is consistent everywhere.
notion_categories_database_id: YOUR_NOTION_CATEGORIES_DATABASE_ID

instances:
  nyc:
    title: "New York"
    notion_places_database_id: NYC_PLACES_DATABASE_ID
  sf:
    title: "San Francisco"
    notion_places_database_id: SF_PLACES_DATABASE_ID
    # Optionally override the shared categories DB for a city-specific taxonomy:
    # notion_categories_database_id: SF_CATEGORIES_DATABASE_ID
```

Each instance needs its own **Places** database. The **Categories** database
is shared by default (multiple Places DBs can relate to one Categories DB), but
any instance may override it.

All data commands require `-i/--instance` (there is no default), e.g.
`myspots -i nyc build-site`. The `add` TUI is the exception — it pre-loads the
most recently used instance and lets you switch between instances from a
dropdown.

### Notion Database Setup

You'll need two Notion databases:

**Places Database** with properties:
- `name` (Title)
- `address` (Text)
- `latitude` (Number)
- `longitude` (Number)
- `google_place_id` (Text)
- `google_json_data` (Text)
- `website` (URL)
- `notes` (Text)
- `primary_category` (Relation to Categories database)
- `tags` (Multi-select)
- `flags` (Multi-select: Favorite, Queued, Visited, Permanently Closed, Lame)

**Categories Database** with properties:
- `category` (Title)
- `parent` (Relation to Categories database, for hierarchical categories)
- `google_style_icon_code` (Text) - Google Maps icon codes for KML styling

### Adding a New Instance

An "instance" is one city/map. Each instance has its own Places database but
shares the Categories database, so adding one is mostly a Notion + config step:

1. **Create the Places database in Notion.** The easiest path is to *duplicate*
   an existing Places database so you inherit the exact schema (see the
   property list above), then empty it. Make sure its `primary_category`
   relation points at your **shared** Categories database — not a new one — so
   the taxonomy stays consistent across cities.

2. **Share it with your integration.** In Notion, open the new database's
   `•••` menu → *Connections* and add the same integration whose token is in
   `notion_api_token`. Without this the API can't see the database.

3. **Get the database ID.** It's the 32-character hex string in the database
   URL: `https://notion.so/<workspace>/<DATABASE_ID>?v=...`.

4. **Add a block to `cred.yaml`** under `instances`. Pick a short lowercase
   `slug` (it becomes the CLI selector and the site path `/<slug>/`):

   ```yaml
   instances:
     nyc:
       title: "New York"
       notion_places_database_id: NYC_PLACES_DATABASE_ID
     paris:                                  # ← new instance
       title: "Paris"
       notion_places_database_id: PARIS_PLACES_DATABASE_ID
   ```

5. **Add places and publish.** The new instance is immediately usable:

   ```bash
   myspots -i paris add          # search & add places (TUI)
   myspots -i paris deploy       # build to docs/paris/ and push
   ```

   `deploy` publishes the map to `/paris/` and regenerates the root landing
   page so it links to the new instance. The TUI's instance dropdown also picks
   up the new entry automatically.

No code changes are required to add an instance.

## Usage

### Add Places (TUI)

Interactive terminal UI for searching, selecting, and annotating places:

```bash
myspots add

# Force refresh cached categories/tags/flags
myspots add --refresh-cache
```

Two-column layout: search and results on the left, annotation (categories, tags, flags, notes) on the right. Key bindings:

- **Enter** — search / toggle result selection / select category or tag
- **Tab** — move to annotation fields
- **Shift+Tab** — back to results
- **Space** — toggle flag checkboxes
- **Ctrl+S** — submit selected places to Notion
- **Ctrl+R** — reset form
- **Escape** — quit

Categories, tags, and flags are cached locally per instance (`~/.config/myspots/cache-<instance>.json`, 24h TTL) for instant startup. The most recently used instance is remembered (`state.json`) and pre-loaded; switch instances from the dropdown at the top of the left panel. Location is remembered across sessions. Existing places in Notion are marked with a yellow star — if you delete a place in Notion, run `myspots -i <instance> add --refresh-cache` so the local cache forgets it. Right-to-left names (e.g. Hebrew) are reordered for correct display in the results list and header.

### Add a Place (CLI)

Simpler CLI prompt flow (no annotation):

```bash
myspots -i nyc add-place

# With location context
myspots -i nyc add-place --location "Brooklyn, NY"

# Non-interactive mode
myspots -i nyc add-place --query "Joe's Pizza"
```

### Import from a Google My Maps KML

Seed an instance from a Google My Maps export. My Maps placemarks carry only a
name, a point, and an optional description, so each is re-resolved against the
Google Places API — searched by name, biased to the KML coordinates, and
matched to the closest result within `--max-distance` (250m by default) — to
recover the address, `google_place_id`, and website.

```bash
# Always preview first: prints one line per placemark, writes nothing
myspots -i tlv import-kml ~/Downloads/MyMap.kml --dry-run

# Import for real
myspots -i tlv import-kml ~/Downloads/MyMap.kml

# Widen the match radius if pins are placed loosely
myspots -i tlv import-kml ~/Downloads/MyMap.kml --max-distance 500
```

Each imported place is tagged `imported` plus `imported-<folder>` for the My
Maps layer it came from, so you can find or bulk-remove the batch later. The KML
name and description are stored in the notes field (`Imported from KML as: "…"`).
Because the match is filtered by *location*, a nearby place with a different
name can be picked; when the matched Google name differs from the KML name
(same-script only), the place is marked `[OK?]` in the preview and tagged
`imported-review` so you can eyeball it in Notion. Placemarks with no result, or
whose nearest match is beyond `--max-distance`, are flagged and skipped — add
those by hand. Re-running is safe: existing places are de-duplicated by
`google_place_id`.

### Refresh Place Data

Keep your Notion database synchronized with Google Maps. This maintenance command iterates through all places in your database (oldest first) and checks for changes:

```bash
myspots -i nyc refresh-store

# Dry run to see what would change without making updates
myspots -i nyc refresh-store --dry-run
```

For each place, the command:

1. **Checks for Place ID updates** - Google occasionally changes place IDs for businesses
   - If detected, you'll be prompted to skip, update, or abort
   - Updating refreshes both the `google_place_id` and `google_json_data` fields

2. **Syncs business closure status** - Compares Google's current status with your Notion flags
   - If a business is marked as permanently closed on Google but not in your database (or vice versa), you'll be prompted to update
   - Updates the "Permanently Closed" flag and refreshes the cached JSON data

3. **Handles errors gracefully** - If a place ID is not found on Google Maps, logs a warning and continues

The `--dry-run` flag reports what would change without prompting or making any actual updates, useful for seeing what's out of sync before committing to changes. The command includes rate limiting (0.1s between API calls) to avoid hitting Google Maps API limits. Notion API calls — here and in every command — automatically retry with exponential backoff (honoring `Retry-After`) when Notion returns a rate-limit error (HTTP 429).

### Build & Deploy Site

```bash
# Build only (writes to docs/<instance>/)
myspots -i nyc build-site

# Deploy one instance: build, commit, and push to GitHub Pages
myspots -i nyc deploy

# Deploy ALL instances (omit -i): rebuilds every city in one commit
myspots deploy
```

Each instance builds to `docs/<instance>/index.html`, served at
`/<instance>/` on GitHub Pages (e.g. `/nyc/`). The site root (`docs/index.html`)
is a minimal landing page linking to each instance, regenerated from config on
every build. `deploy` is cron-friendly: with `-i` it deploys that one instance,
without `-i` it deploys all of them, and either way it skips the commit/push if
nothing changed — so a single `myspots deploy` cron keeps every map current.

The published map has a geolocation button (drops a tracking dot at your current
location on click) and renders right-to-left labels (Hebrew, Arabic) correctly
via Mapbox's RTL text plugin.

### Custom Config Path

All commands support a custom config file location (note `--config` and
`--instance` are global options, so they come before the subcommand):

```bash
myspots --config /path/to/config.yaml -i nyc add-place
```

## Development

### Project Structure

```
myspots/
├── myspots/
│   ├── __init__.py        # Core functions, data models, Notion store (with 429 retry)
│   ├── cache.py           # Per-instance JSON cache + last-used instance state
│   ├── cli.py             # CLI commands
│   ├── kml_import.py      # KML parsing + name/geo matching helpers
│   ├── site.py            # Static site + landing page builder
│   ├── tui.py             # Textual TUI for `myspots add`
│   └── templates/
│       └── map.html       # Mapbox GL map template
├── scripts/               # Utility scripts
├── docs/                  # Built site, one folder per instance (GitHub Pages)
├── pyproject.toml         # Project metadata and dependencies
└── README.md
```

### Dependencies

- `googlemaps` - Google Maps API client
- `ultimate-notion` - Notion API wrapper
- `textual` - Terminal UI framework
- `lxml` - KML generation and parsing
- `click` - CLI framework
- `tenacity` - Retry/backoff for Notion rate limits
- `python-bidi` - Right-to-left text display in the TUI
- `loguru` - Logging
- `tqdm` - Progress bars

## License

See the repository for license information.

## Author

Uri Laserson (uri.laserson@gmail.com)

## Links

- [GitHub Repository](https://github.com/laserson/myspots)
- [Google Maps Places API](https://developers.google.com/maps/documentation/places/web-service)
- [Notion API](https://developers.notion.com/)
