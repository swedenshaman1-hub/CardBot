import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

from supabase import create_client

BUCKET = "card-images"

CARD_EVENT_TYPES = {
    "spread_published",
    "card_button_clicked",
    "card_opened",
    "card_delivery_succeeded",
    "card_rejected_subscription",
    "card_rejected_bot_not_started",
    "card_rejected_duplicate",
    "card_rejected_limit",
    "card_delivery_failed",
    "voice_requested",
    "voice_sent",
    "reaction_added",
    "reaction_removed",
    "reflection_feedback",
    "reflection_question_shown",
    "reflection_answered",
    "reflection_completed",
}

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


def save_spread(
    card_ids: list[int],
    channel_message_id: int | None = None,
    question: str | None = None,
) -> int:
    client = get_client()
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "card_ids": card_ids,
        "channel_message_id": channel_message_id,
    }
    if question is not None:
        payload["question"] = question
    res = (
        client.table("spreads")
        .insert(payload)
        .execute()
    )
    return res.data[0]["id"]


def update_spread_message(spread_id: int, channel_message_id: int):
    client = get_client()
    client.table("spreads").update(
        {"channel_message_id": channel_message_id}
    ).eq("id", spread_id).execute()


def update_spread_question(spread_id: int, question: str | None):
    client = get_client()
    client.table("spreads").update(
        {"question": question}
    ).eq("id", spread_id).execute()


def get_latest_spread() -> dict | None:
    published = get_published_spreads()
    return published[0] if published else None


def get_published_spreads() -> list[dict]:
    """Return every spread that still has a channel message attached."""
    client = get_client()
    res = client.table("spreads").select("*").order("id", desc=True).execute()
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "card_ids": row["card_ids"],
            "channel_message_id": row["channel_message_id"],
            "question": row.get("question"),
        }
        for row in res.data
        if row.get("channel_message_id")
    ]


def clear_spread_message(spread_id: int):
    """Mark a channel post as removed so it is not retried unnecessarily."""
    client = get_client()
    client.table("spreads").update(
        {"channel_message_id": None}
    ).eq("id", spread_id).execute()


def get_spread(spread_id: int) -> dict | None:
    client = get_client()
    res = client.table("spreads").select("*").eq("id", spread_id).limit(1).execute()
    if not res.data:
        return None
    row = res.data[0]
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "card_ids": row["card_ids"],
        "channel_message_id": row["channel_message_id"],
        "question": row.get("question"),
    }


# ── settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str) -> str | None:
    client = get_client()
    res = client.table("settings").select("value").eq("key", key).execute()
    return res.data[0]["value"] if res.data else None


def set_setting(key: str, value: str):
    client = get_client()
    client.table("settings").upsert({"key": key, "value": value}).execute()


def get_settings_by_prefix(prefix: str) -> dict[str, str]:
    """Return settings whose keys start with the requested prefix."""
    client = get_client()
    res = client.table("settings").select("key,value").like("key", f"{prefix}%").execute()
    return {row["key"]: row["value"] for row in res.data or []}


def get_recent_settings(prefix: str, limit: int = 7) -> list[str]:
    """Return the newest saved values for a settings namespace."""
    client = get_client()
    res = (
        client.table("settings")
        .select("key,value")
        .like("key", f"{prefix}%")
        .order("key", desc=True)
        .limit(limit)
        .execute()
    )
    return [row["value"] for row in res.data or []]


# ── privacy-safe analytics ────────────────────────────────────────────────

def record_event(
    event_type: str,
    idempotency_key: str,
    *,
    spread_id: int | None = None,
    card_id: int | None = None,
    card_position: int | None = None,
    actor_hash: str | None = None,
    reaction_type: str | None = None,
    metadata: dict | None = None,
) -> bool:
    """Record one analytics event once; return False for an existing key.

    ``actor_hash`` must already be anonymised by the caller.  This function
    intentionally has no Telegram user-id argument, so a raw identifier cannot
    be persisted accidentally.
    """
    if event_type not in CARD_EVENT_TYPES:
        raise ValueError(f"Unsupported CardBot event type: {event_type}")
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key:
        raise ValueError("idempotency_key must not be empty")
    if card_position is not None and not 1 <= int(card_position) <= 6:
        raise ValueError("card_position must be between 1 and 6")

    row = {
        "event_type": event_type,
        "spread_id": spread_id,
        "card_id": card_id,
        "card_position": card_position,
        "actor_hash": actor_hash,
        "reaction_type": reaction_type,
        "metadata": metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    event_key = "analytics_event:" + hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()
    if get_setting(event_key) is not None:
        return False
    get_client().table("settings").upsert(
        {"key": event_key, "value": json.dumps(row, ensure_ascii=False)}
    ).execute()
    return True


def set_card_reaction(
    spread_id: int,
    card_id: int,
    card_position: int,
    actor_hash: str,
    reaction_type: str | None,
    idempotency_key: str,
) -> bool:
    """Set one current reaction per spread/card/anonymous actor.

    Passing ``None`` or an empty reaction removes the current reaction.  The
    return value is True when a reaction remains and False when it was removed.
    """
    actor_hash = (actor_hash or "").strip()
    if not actor_hash:
        raise ValueError("actor_hash must not be empty")
    if reaction_type and reaction_type not in {"close", "reflect", "not_now"}:
        raise ValueError("Unsupported card reaction")

    reaction_key = f"analytics_reaction:{spread_id}:{card_id}:{actor_hash}"
    if not reaction_type:
        get_client().table("settings").delete().eq("key", reaction_key).execute()
        return False

    reaction_row = {
        "spread_id": spread_id,
        "card_id": card_id,
        "actor_hash": actor_hash,
        "reaction_type": reaction_type,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    get_client().table("settings").upsert(
        {"key": reaction_key, "value": json.dumps(reaction_row, ensure_ascii=False)}
    ).execute()
    record_event(
        "reaction_added",
        idempotency_key,
        spread_id=spread_id,
        card_id=card_id,
        card_position=card_position,
        actor_hash=actor_hash,
        reaction_type=reaction_type,
    )
    return True


def _analytics_rows(days: int, spread_id: int | None = None) -> list[dict]:
    if days < 1:
        raise ValueError("days must be at least 1")
    cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat()
    rows: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        page = (
            get_client()
            .table("settings")
            .select("key,value")
            .like("key", "analytics_event:%")
            .order("key", desc=True)
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        for item in page:
            try:
                row = json.loads(item["value"])
                if row.get("created_at", "") < cutoff:
                    continue
                if spread_id is not None and row.get("spread_id") != spread_id:
                    continue
                rows.append(row)
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
        if len(page) < page_size:
            return rows
        offset += page_size


def _summarise_events(rows: list[dict]) -> dict:
    event_counts = Counter(row["event_type"] for row in rows)
    unique_actors = {
        row["actor_hash"] for row in rows if row.get("actor_hash")
    }
    opened_actors = {
        row["actor_hash"]
        for row in rows
        if row["event_type"] == "card_opened" and row.get("actor_hash")
    }
    position_counts = Counter(
        int(row["card_position"])
        for row in rows
        if row["event_type"] == "card_opened" and row.get("card_position") is not None
    )
    card_counts = Counter(
        int(row["card_id"])
        for row in rows
        if row["event_type"] == "card_opened" and row.get("card_id") is not None
    )
    reaction_counts = Counter(
        row["reaction_type"]
        for row in rows
        if row["event_type"] == "reaction_added" and row.get("reaction_type")
    )
    reflection_feedback_counts = Counter(
        row["reaction_type"]
        for row in rows
        if row["event_type"] == "reflection_feedback" and row.get("reaction_type")
    )
    reflection_feedback_by_card: dict[int, Counter] = {}
    for row in rows:
        if row["event_type"] != "reflection_feedback" or not row.get("card_id"):
            continue
        card_id = int(row["card_id"])
        reflection_feedback_by_card.setdefault(card_id, Counter())[
            row.get("reaction_type") or "unknown"
        ] += 1
    actor_openings: dict[str, set[tuple[int | None, int | None]]] = {}
    for row in rows:
        if row["event_type"] != "card_opened" or not row.get("actor_hash"):
            continue
        actor_openings.setdefault(row["actor_hash"], set()).add(
            (row.get("spread_id"), row.get("card_position"))
        )
    button_attempt_ids = {
        str(row.get("metadata", {}).get("attempt_id"))
        for row in rows
        if row["event_type"] == "card_button_clicked"
        and row.get("metadata", {}).get("attempt_id") is not None
    }
    legacy_button_clicks = sum(
        1
        for row in rows
        if row["event_type"] == "card_button_clicked"
        and row.get("metadata", {}).get("attempt_id") is None
    )
    outcomes_by_attempt: dict[str, set[str]] = {}
    tracked_outcome_types = {
        "card_delivery_succeeded",
        "card_rejected_subscription",
        "card_rejected_bot_not_started",
        "card_rejected_duplicate",
        "card_rejected_limit",
        "card_delivery_failed",
    }
    for row in rows:
        attempt_id = row.get("metadata", {}).get("attempt_id")
        if attempt_id is None or row["event_type"] not in tracked_outcome_types:
            continue
        outcomes_by_attempt.setdefault(str(attempt_id), set()).add(row["event_type"])

    button_outcomes = Counter()
    for attempt_id in button_attempt_ids:
        outcomes = outcomes_by_attempt.get(attempt_id, set())
        if "card_delivery_succeeded" in outcomes:
            button_outcomes["delivered"] += 1
        elif "card_delivery_failed" in outcomes:
            button_outcomes["delivery_failed"] += 1
        elif "card_rejected_limit" in outcomes:
            button_outcomes["limit"] += 1
        elif "card_rejected_duplicate" in outcomes:
            button_outcomes["duplicate"] += 1
        elif "card_rejected_subscription" in outcomes:
            button_outcomes["subscription"] += 1
        elif "card_rejected_bot_not_started" in outcomes:
            button_outcomes["waiting_for_start"] += 1
        else:
            button_outcomes["unclassified"] += 1
    return {
        "events_total": len(rows),
        "event_counts": dict(event_counts),
        "unique_actors": len(unique_actors),
        "unique_card_openers": len(opened_actors),
        "card_opened_by_position": dict(sorted(position_counts.items())),
        "card_opened_by_card": dict(card_counts.most_common()),
        "reaction_counts": dict(reaction_counts),
        "reflection_feedback_counts": dict(reflection_feedback_counts),
        "reflection_feedback_by_card": {
            card_id: dict(counts)
            for card_id, counts in reflection_feedback_by_card.items()
        },
        "users_opened_one": sum(len(values) == 1 for values in actor_openings.values()),
        "users_opened_two_or_more": sum(
            len(values) >= 2 for values in actor_openings.values()
        ),
        "button_outcome_counts": dict(button_outcomes),
        "tracked_button_attempts": len(button_attempt_ids),
        "legacy_button_clicks": legacy_button_clicks,
    }


def get_stats(days: int = 7) -> dict:
    """Return aggregate, anonymous CardBot analytics for the last N days."""
    rows = _analytics_rows(days)
    result = _summarise_events(rows)
    result.update({"days": days, "spread_id": None})
    return result


def get_spread_stats(spread_id: int) -> dict:
    """Return lifetime aggregate analytics for one spread."""
    # A very wide bounded window keeps the query simple while avoiding an
    # unfiltered table scan in Supabase/PostgREST.
    rows = _analytics_rows(36500, spread_id=spread_id)
    result = _summarise_events(rows)
    result.update({"days": None, "spread_id": spread_id})
    return result


def _spread_selection_key(spread_id: int, user_id: int) -> str:
    return f"spread_selection:{spread_id}:{user_id}"


def get_spread_selections(spread_id: int, user_id: int) -> list[int]:
    """Return positions already opened by a user for one published spread."""
    raw = get_setting(_spread_selection_key(spread_id, user_id))
    if not raw:
        return []
    try:
        values = json.loads(raw)
        return sorted({int(value) for value in values if 1 <= int(value) <= 6})
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def claim_spread_selection(
    spread_id: int,
    user_id: int,
    position: int,
    max_selections: int = 2,
) -> dict:
    """Persist one card choice and report whether it may be delivered."""
    selections = get_spread_selections(spread_id, user_id)
    if position in selections:
        return {"allowed": True, "is_new": False, "selections": selections}
    if len(selections) >= max_selections:
        return {"allowed": False, "is_new": False, "selections": selections}

    selections.append(position)
    selections.sort()
    set_setting(
        _spread_selection_key(spread_id, user_id),
        json.dumps(selections, separators=(",", ":")),
    )
    return {"allowed": True, "is_new": True, "selections": selections}


def release_spread_selection(spread_id: int, user_id: int, position: int) -> bool:
    """Release a reserved choice after card delivery failed.

    Returns True when the position was present and removed.  A failed delivery
    must not consume one of the user's two choices for the spread.
    """
    selections = get_spread_selections(spread_id, user_id)
    if position not in selections:
        return False
    selections.remove(position)
    set_setting(
        _spread_selection_key(spread_id, user_id),
        json.dumps(selections, separators=(",", ":")),
    )
    return True


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
