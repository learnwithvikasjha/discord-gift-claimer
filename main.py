import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

import discord


LOGS_DIR = Path(__file__).with_name("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("claim-gift")

CONFIG_PATH = Path(__file__).with_name("config.json")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Config:
    token: str
    claim_button_texts: Set[str] = field(default_factory=lambda: {"Claim Gift"})
    allowed_guild_ids: Set[int] = field(default_factory=set)
    allowed_channel_ids: Set[int] = field(default_factory=set)


def _parse_id_list(values) -> Set[int]:
    """Convert a list of IDs from JSON into a set of ints."""
    parsed: Set[int] = set()
    for value in values or []:
        try:
            parsed.add(int(value))
        except (TypeError, ValueError):
            logger.warning("Skipping invalid ID value in config: %r", value)
    return parsed


def _parse_label_list(values, legacy_value: str = "") -> Set[str]:
    """Convert list/str labels into a set of normalized, non-empty strings."""
    parsed: Set[str] = set()

    # Accept list/tuple for multiple labels
    if isinstance(values, (list, tuple)):
        sources = values
    elif values:
        sources = [values]
    else:
        sources = []

    for raw in sources:
        text = str(raw).strip()
        if text:
            parsed.add(text)

    legacy = str(legacy_value or "").strip()
    if legacy:
        parsed.add(legacy)

    if not parsed:
        parsed.add("Claim Gift")

    return parsed


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config file at {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)

    token = str(raw.get("token", "")).strip()
    if not token or token == "YOUR_DISCORD_TOKEN":
        raise ValueError("Please set your account token in config.json (token).")

    claim_texts = _parse_label_list(raw.get("claim_button_texts"), raw.get("claim_button_text", ""))
    guild_ids = _parse_id_list(raw.get("allowed_guild_ids"))
    channel_ids = _parse_id_list(raw.get("allowed_channel_ids"))

    return Config(
        token=token,
        claim_button_texts=claim_texts,
        allowed_guild_ids=guild_ids,
        allowed_channel_ids=channel_ids,
    )


async def main() -> None:
    config = load_config()
    claim_labels = {text.lower() for text in config.claim_button_texts}
    processed_messages: Set[int] = set()

    client = discord.Client()

    def message_allowed(message: discord.Message) -> bool:
        if config.allowed_guild_ids:
            if message.guild is None or message.guild.id not in config.allowed_guild_ids:
                return False
        if config.allowed_channel_ids and message.channel.id not in config.allowed_channel_ids:
            return False
        return True

    async def click_claim_button(message: discord.Message, event_received_at: datetime) -> bool:
        for row in message.components:
            for component in getattr(row, "children", []):
                label = getattr(component, "label", "")
                normalized = label.strip().lower() if label else ""
                if not normalized or normalized not in claim_labels:
                    continue

                custom_id = getattr(component, "custom_id", None)
                is_url = bool(getattr(component, "url", None))
                disabled = bool(getattr(component, "disabled", False))

                if disabled:
                    logger.info(
                        'Skipping disabled button "%s" on message %s in %s',
                        label,
                        message.id,
                        message.channel,
                    )
                    continue

                if is_url or not custom_id:
                    logger.info(
                        'Skipping non-interactive button "%s" on message %s in %s (url=%s custom_id=%s)',
                        label,
                        message.id,
                        message.channel,
                        getattr(component, "url", None),
                        custom_id,
                    )
                    continue

                now = _utcnow()
                created_age_ms = None
                if getattr(message, "created_at", None):
                    created_age_ms = (now - message.created_at).total_seconds() * 1000
                since_event_ms = (now - event_received_at).total_seconds() * 1000

                logger.info(
                    'Attempting click "%s" on message %s in %s (age=%s since_event=%s custom_id=%s)',
                    label or next(iter(config.claim_button_texts)),
                    message.id,
                    message.channel,
                    f"{created_age_ms:.1f}ms" if created_age_ms is not None else "?",
                    f"{since_event_ms:.1f}ms",
                    custom_id,
                )
                try:
                    await component.click()
                    return True
                except Exception as exc:
                    logger.error(
                        'Failed to click button on message %s (custom_id=%s url=%s disabled=%s): %s',
                        message.id,
                        custom_id,
                        getattr(component, "url", None),
                        disabled,
                        exc,
                    )
                    return False
        return False

    async def handle_message(message: discord.Message, source: str) -> None:
        event_received_at = _utcnow()
        created_age_ms = None
        edited_age_ms = None
        if getattr(message, "created_at", None):
            created_age_ms = (event_received_at - message.created_at).total_seconds() * 1000
        if getattr(message, "edited_at", None):
            edited_age_ms = (event_received_at - message.edited_at).total_seconds() * 1000

        if message.author == client.user:
            return
        if not message.components:
            return
        if not message_allowed(message):
            return

        logger.info(
            "%s event for message %s in %s (created_age=%s%s)",
            source,
            message.id,
            message.channel,
            f"{created_age_ms:.1f}ms" if created_age_ms is not None else "?",
            f", edited_age={edited_age_ms:.1f}ms" if edited_age_ms is not None else "",
        )

        if message.id in processed_messages:
            return

        clicked = await click_claim_button(message, event_received_at)
        if clicked:
            processed_messages.add(message.id)

    @client.event
    async def on_ready():
        logger.info("Ready as %s (%s)", client.user, client.user.id)
        if config.allowed_guild_ids:
            logger.info("Guild allowlist: %s", ", ".join(str(g) for g in sorted(config.allowed_guild_ids)))
        if config.allowed_channel_ids:
            logger.info(
                "Channel allowlist: %s",
                ", ".join(str(c) for c in sorted(config.allowed_channel_ids)),
            )

    @client.event
    async def on_message(message: discord.Message):
        await handle_message(message, source="message")

    @client.event
    async def on_message_edit(_, after: discord.Message):
        await handle_message(after, source="edit")

    await client.start(config.token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except Exception as exc:  # pragma: no cover - unexpected crash guard
        logger.exception("Unexpected error: %s", exc)
        raise SystemExit(1) from exc
