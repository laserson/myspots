# myspots

A Python CLI tool for managing your favorite places using Google Maps and Notion.

## Overview

`myspots` helps you discover, organize, and export places of interest. Search for places using the Google Maps API, store them in a Notion database with custom categories and flags, and export them to KML format for use in Google Earth or Google My Maps.

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
- `notion_place` (Place) - Notion's native place field
- `website` (URL)
- `notes` (Text)
- `primary_category` (Relation to Categories database)
- `tags` (Multi-select)
- `flags` (Multi-select: Favorite, Queued, Visited, Permanently Closed, Lame)

**Categories Database** with properties:
- `category` (Title)
- `parent` (Relation to Categories database, for hierarchical categories)
- `google_style_icon_code` (Text) - Google Maps icon codes for KML styling

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

Categories, tags, and flags are cached locally per instance (`~/.config/myspots/cache-<instance>.json`, 24h TTL) for instant startup. The most recently used instance is remembered (`state.json`) and pre-loaded; switch instances from the dropdown at the top of the left panel. Location is remembered across sessions. Existing places in Notion are marked with a yellow star.

### Add a Place (CLI)

Simpler CLI prompt flow (no annotation):

```bash
myspots -i nyc add-place

# With location context
myspots -i nyc add-place --location "Brooklyn, NY"

# Non-interactive mode
myspots -i nyc add-place --query "Joe's Pizza"
```

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

The `--dry-run` flag reports what would change without prompting or making any actual updates, useful for seeing what's out of sync before committing to changes. The command includes rate limiting (0.1s between API calls) to avoid hitting Google Maps API limits.

### Build & Deploy Site

```bash
# Build only (writes to docs/<instance>/)
myspots -i nyc build-site

# Build, commit, and push to GitHub Pages in one step
myspots -i nyc deploy
```

Each instance builds to `docs/<instance>/index.html`, served at
`/<instance>/` on GitHub Pages (e.g. `/nyc/`). The site root (`docs/index.html`)
is a minimal landing page linking to each instance, regenerated from config on
every build. `deploy` is cron-friendly: it commits that instance's output plus
the landing page, and skips the commit/push if nothing changed.

### Custom Config Path

All commands support a custom config file location:

```bash
myspots --config /path/to/config.yaml add-place
```

## Development

### Project Structure

```
myspots/
├── myspots/
│   ├── __init__.py      # Core functions and data models
│   ├── cache.py         # Local JSON cache for categories, tags, flags
│   ├── cli.py           # CLI commands
│   └── tui.py           # Textual TUI for `myspots add`
├── scripts/             # Utility scripts
├── pyproject.toml       # Project metadata and dependencies
└── README.md
```

### Dependencies

- `googlemaps` - Google Maps API client
- `ultimate-notion` - Notion API wrapper
- `textual` - Terminal UI framework
- `lxml` - KML generation
- `click` - CLI framework
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
