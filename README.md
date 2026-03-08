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

Create a configuration file at `~/.config/myspots/cred.yaml`:

```yaml
google_api_key: YOUR_GOOGLE_MAPS_API_KEY
notion_api_token: YOUR_NOTION_API_TOKEN
notion_places_database_id: YOUR_NOTION_PLACES_DATABASE_ID
notion_categories_database_id: YOUR_NOTION_CATEGORIES_DATABASE_ID
mapbox_access_token: YOUR_MAPBOX_TOKEN
```

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

### Add a Place

Search for places on Google Maps and add them to your Notion database:

```bash
myspots add-place

# With location context
myspots add-place --location "Brooklyn, NY"

# Non-interactive mode
myspots add-place --query "Joe's Pizza"
```

The command will:
1. Search Google Maps for your query
2. Display results for selection
3. Prompt for optional notes
4. Add selected places to Notion

### Refresh Place Data

Keep your Notion database synchronized with Google Maps. This maintenance command iterates through all places in your database (oldest first) and checks for changes:

```bash
myspots refresh-store

# Dry run to see what would change without making updates
myspots refresh-store --dry-run
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
│   └── cli.py           # CLI commands
├── scripts/             # Utility scripts
├── pyproject.toml       # Project metadata and dependencies
└── README.md
```

### Dependencies

- `googlemaps` - Google Maps API client
- `ultimate-notion` - Notion API wrapper
- `fastkml` - KML file generation
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
