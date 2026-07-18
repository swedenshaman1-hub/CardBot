import os
import time
from datetime import datetime, timezone

from supabase import create_client

BUCKET = "card-images"

_client = None


def get_client():
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client


def init_db():
    # Таблицы "cards" и "spreads" и бакет "card-images" создаются один раз
    # вручную в Supabase (см. README) — здесь ничего создавать не нужно.
    pass


def upload_card_image(card_id: int, file_bytes: bytes) -> str:
    client = get_client()
    path = f"{card_id}_{int(time.time())}.jpg"
    client.storage.from_(BUCKET).upload(
        path,
        file_bytes,
        {"content-type": "image/jpeg"},
    )
    return client.storage.from_(BUCKET).get_public_url(path)


def list_all_cards() -> list[dict]:
    client = get_client()
    res = client.table("cards").select("id, name, meaning").order("id").execute()
    return res.data


def delete_card(card_id: int):
    client = get_client()
    client.table("cards").delete().eq("id", card_id).execute()


def update_card_meaning(card_id: int, meaning: str):
    client = get_client()
    client.table("cards").update({"meaning": meaning}).eq("id", card_id).execute()


def delete_all_cards():
    client = get_client()
    client.table("cards").delete().neq("id", 0).execute()


def add_card(card_id: int, name: str, meaning: str, image_url: str):
    client = get_client()
    client.table("cards").upsert(
        {"id": card_id, "name": name, "meaning": meaning, "image_url": image_url}
    ).execute()


def get_card(card_id: int) -> dict | None:
    client = get_client()
    res = client.table("cards").select("*").eq("id", card_id).execute()
    return res.data[0] if res.data else None


def get_cards(card_ids: list[int]):
    client = get_client()
    res = client.table("cards").select("*").in_("id", card_ids).execute()
    by_id = {row["id"]: row for row in res.data}
    missing = [cid for cid in card_ids if cid not in by_id]
    ordered = [by_id[cid] for cid in card_ids if cid in by_id]
    return ordered, missing


def save_spread(card_ids: list[int], channel_message_id: int | None = None) -> int:
    client = get_client()
    res = (
        client.table("spreads")
        .insert(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "card_ids": card_ids,
                "channel_message_id": channel_message_id,
            }
        )
        .execute()
    )
    return res.data[0]["id"]


def update_spread_message(spread_id: int, channel_message_id: int):
    client = get_client()
    client.table("spreads").update(
        {"channel_message_id": channel_message_id}
    ).eq("id", spread_id).execute()


def get_latest_spread() -> dict | None:
    client = get_client()
    res = client.table("spreads").select("*").order("id", desc=True).limit(1).execute()
    if not res.data:
        return None
    row = res.data[0]
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "card_ids": row["card_ids"],
        "channel_message_id": row["channel_message_id"],
    }


# ── settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str) -> str | None:
    client = get_client()
    res = client.table("settings").select("value").eq("key", key).execute()
    return res.data[0]["value"] if res.data else None


def set_setting(key: str, value: str):
    client = get_client()
    client.table("settings").upsert({"key": key, "value": value}).execute()


def get_card_back_url() -> str | None:
    env_url = os.getenv("CARD_BACK_URL", "").strip()
    if env_url:
        return env_url
    return get_setting("card_back_url")


def upload_back_image(file_bytes: bytes) -> str:
    client = get_client()
    path = "back.jpg"
    client.storage.from_(BUCKET).upload(
        path,
        file_bytes,
        {"content-type": "image/jpeg", "upsert": "true"},
    )
    url = client.storage.from_(BUCKET).get_public_url(path)
    set_setting("card_back_url", url)
    return url
