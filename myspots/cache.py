import json
from datetime import datetime, timezone
from pathlib import Path

CACHE_TTL_HOURS = 24
MAX_RECENT_LOCATIONS = 20

STATE_PATH = Path.home() / ".config" / "myspots" / "state.json"


def cache_path(instance: str) -> Path:
    """Per-instance cache file so cities don't cross-contaminate."""
    return Path.home() / ".config" / "myspots" / f"cache-{instance}.json"


def get_last_instance() -> str | None:
    """Return the instance most recently used in the TUI, if any."""
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text()).get("last_instance")
    except (json.JSONDecodeError, OSError):
        return None


def set_last_instance(instance: str) -> None:
    """Remember the instance most recently used in the TUI."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state["last_instance"] = instance
    STATE_PATH.write_text(json.dumps(state, indent=2))


class MySpotsCache:
    def __init__(self, instance: str):
        self.instance = instance
        self.path = cache_path(instance)
        self.categories: list[dict] = []
        self.tags: list[str] = []
        self.flags: list[str] = []
        self.recent_locations: list[str] = []
        self.last_location: str = ""
        self.known_place_ids: set[str] = set()
        self.cached_at: str | None = None

    def load(self) -> bool:
        """Load cache from disk. Returns True if cache was loaded and is fresh."""
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text())
            self.categories = data.get("categories", [])
            self.tags = data.get("tags", [])
            self.flags = data.get("flags", [])
            self.recent_locations = data.get("recent_locations", [])
            self.last_location = data.get("last_location", "")
            self.known_place_ids = set(data.get("known_place_ids", []))
            self.cached_at = data.get("cached_at")
            return self._is_fresh()
        except (json.JSONDecodeError, KeyError):
            return False

    def _is_fresh(self) -> bool:
        if not self.cached_at:
            return False
        cached_time = datetime.fromisoformat(self.cached_at)
        age = datetime.now(timezone.utc) - cached_time
        return age.total_seconds() < CACHE_TTL_HOURS * 3600

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "categories": self.categories,
            "tags": self.tags,
            "flags": self.flags,
            "recent_locations": self.recent_locations,
            "last_location": self.last_location,
            "known_place_ids": sorted(self.known_place_ids),
            "cached_at": self.cached_at,
        }
        self.path.write_text(json.dumps(data, indent=2))

    def refresh(self, store):
        """Refresh cache data from Notion."""
        self.categories = store.fetch_categories()
        self.tags = store.fetch_tag_options()
        self.flags = store.fetch_flag_options()
        self.known_place_ids = store.fetch_known_place_ids()
        self.cached_at = datetime.now(timezone.utc).isoformat()
        self.save()

    def add_location(self, location: str):
        """Add a location to recent locations list."""
        location = location.strip()
        if not location:
            return
        self.recent_locations = [loc for loc in self.recent_locations if loc != location]
        self.recent_locations.insert(0, location)
        self.recent_locations = self.recent_locations[:MAX_RECENT_LOCATIONS]
        self.last_location = location
        self.save()

    def add_known_place_id(self, place_id: str):
        """Track a newly added place."""
        self.known_place_ids.add(place_id)
        self.save()
