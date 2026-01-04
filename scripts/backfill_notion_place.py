"""Backfill the notion_place column for all existing entries in the database."""

from loguru import logger
from tqdm import tqdm

from myspots import NotionMySpotsStore, get_config


def backfill_notion_place():
    """Backfill notion_place property for all existing places in the database."""
    config = get_config()
    store = NotionMySpotsStore(config)

    updated_count = 0
    skipped_count = 0
    error_count = 0
    already_set_count = 0

    for place in tqdm(store.iter_places(), desc="Backfilling notion_place"):
        # Skip if notion_place is already set
        try:
            if place.props.notion_place is not None:
                already_set_count += 1
                continue
        except (AttributeError, KeyError):
            pass  # notion_place doesn't exist yet, that's expected

        # Skip if latitude or longitude is missing or None
        if place.props.latitude is None or place.props.longitude is None:
            logger.warning(f"Skipping {place.id}: latitude or longitude is None")
            skipped_count += 1
            continue

        # Build the notion_place property
        notion_place_value = {
            "lat": place.props.latitude,
            "lon": place.props.longitude,
            "name": place.title,
            "address": place.props.address,
            "google_place_id": place.props.google_place_id,
        }

        # Update the page with the notion_place property (network operation)
        try:
            place.props.notion_place = notion_place_value
            updated_count += 1
        except Exception as e:
            logger.error(f"Error updating {place.id}: {e}")
            error_count += 1

    logger.info(f"Backfill complete!")
    logger.info(f"  Updated: {updated_count}")
    logger.info(f"  Already set: {already_set_count}")
    logger.info(f"  Skipped: {skipped_count}")
    logger.info(f"  Errors: {error_count}")


if __name__ == "__main__":
    backfill_notion_place()
