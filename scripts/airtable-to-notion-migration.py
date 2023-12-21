import sys

import pandas as pd
import notional
from loguru import logger
from tqdm import tqdm

from myspots.utils import get_config


logger.remove()
logger.add(sys.stderr, level="INFO")


config = get_config()
notion = notional.connect(auth=config["notion_api_token"])
categories_db = notion.databases.retrieve(config["notion_categories_database_id"])
places_db = notion.databases.retrieve(config["notion_places_database_id"])
NotionCategory = notional.orm.connected_page(session=notion, source_db=categories_db)
NotionPlace = notional.orm.connected_page(session=notion, source_db=places_db)


# these files were manually downloaded from the Airtable views
categories_df = pd.read_csv(
    "~/Downloads/categories.csv", dtype={"google_style_icon_code": str}
)
places_df = pd.read_csv("~/Downloads/places.csv")


categories = {}
for _, row in tqdm(categories_df.iterrows(), total=len(categories_df)):
    logger.debug(row)
    logger.info("Creating category {}", row.loc["category"])
    category_page = (
        NotionCategory.query()
        .filter(
            property="category",
            rich_text=notional.query.TextCondition(equals=row.loc["category"]),
        )
        .first()
    )
    if category_page is not None:
        logger.info("Category {} already exists, skipping", row.loc["category"])
        categories[row.loc["category"]] = category_page
        continue
    kwargs = {
        "category": row.loc["category"],
        "google_style_icon_code": row.loc["google_style_icon_code"],
    }
    if pd.notna(row.loc["parent"]):
        kwargs["parent"] = notional.types.Relation[categories[row.loc["parent"]].id]
    categories[row.loc["category"]] = NotionCategory.create(**kwargs)


for _, row in tqdm(places_df.iterrows(), total=len(places_df)):
    logger.debug(row)
    logger.info("Creating place {}", row.loc["name"])
    place_page = (
        NotionPlace.query()
        .filter(
            property="google_place_id",
            rich_text=notional.query.TextCondition(equals=row.loc["google_place_id"]),
        )
        .first()
    )
    if place_page is not None:
        logger.info("Place {} already exists, skipping", row.loc["name"])
        continue
    kwargs = {
        "name": row.loc["name"],
        "orig_date_added": row.loc["date_added"],
        # these all derived from places API and are always filled
        "latitude": row.loc["latitude"],
        "longitude": row.loc["longitude"],
        "google_place_id": row.loc["google_place_id"],
        "google_json_data": row.loc["google_json_data"],
    }

    # may be null
    if pd.notna(row.loc["address"]):
        kwargs["address"] = row.loc["address"]
    if pd.notna(row.loc["website"]):
        kwargs["website"] = row.loc["website"]
    if pd.notna(row.loc["primary_category"]):
        category_strings = row.loc["primary_category"].split(",")
        kwargs["primary_category"] = notional.types.Relation[
            *[categories[c].id for c in category_strings]
        ]

    flags = []
    if pd.notna(row.loc["is_reviewed"]):
        flags.append("Reviewed")
    if pd.notna(row.loc["is_visited"]):
        flags.append("Visited")
    if pd.notna(row.loc["is_queued"]):
        flags.append("Queued")
    if pd.notna(row.loc["is_favorite"]):
        flags.append("Favorite")
    if pd.notna(row.loc["is_lame"]):
        flags.append("Lame")
    if pd.notna(row.loc["is_perm_closed"]):
        flags.append("Permanently Closed")
    if len(flags) > 0:
        kwargs["flags"] = notional.types.MultiSelect[*flags]

    if pd.notna(row.loc["tags"]):
        tags = row.loc["tags"].split(",")
        kwargs["tags"] = notional.types.MultiSelect[*tags]

    NotionPlace.create(**kwargs)
