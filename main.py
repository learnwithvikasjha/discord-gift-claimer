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


def _format_ms(value) -> str:
    return f"{value:.1f}ms" if value is not None else "?"


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

    async def click_claim_button(message: discord.Message, event_received_at: datetime, source: str) -> bool:
        """Attempt to click the first allowed button as fast as possible."""
        seen_labels = []
        saw_claim_label = False
        now = _utcnow()
        created_age_ms = (now - message.created_at).total_seconds() * 1000 if getattr(message, "created_at", None) else None
        edited_age_ms = (now - message.edited_at).total_seconds() * 1000 if getattr(message, "edited_at", None) else None

        for row in message.components:
            for component in getattr(row, "children", []):
                label = getattr(component, "label", "")
                seen_labels.append(label)
                normalized = label.strip().lower() if label else ""
                if not normalized or normalized not in claim_labels:
                    continue

                saw_claim_label = True
                custom_id = getattr(component, "custom_id", None)
                is_url = bool(getattr(component, "url", None))
                disabled = bool(getattr(component, "disabled", False))

                if disabled:
                    logger.info('Skipping disabled button "%s" on message %s in %s', label, message.id, message.channel)
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

                processed_messages.add(message.id)  # prevent duplicate attempts on rapid edits
                before_click = _utcnow()
                since_event_ms = (before_click - event_received_at).total_seconds() * 1000
                try:
                    await component.click()
                    after_click = _utcnow()
                    total_since_event_ms = (after_click - event_received_at).total_seconds() * 1000
                    logger.info(
                        'Clicked "%s" on message %s in %s via %s (age=%s edited_age=%s since_event=%s after_click=%s custom_id=%s)',
                        label or next(iter(config.claim_button_texts)),
                        message.id,
                        message.channel,
                        source,
                        _format_ms(created_age_ms),
                        _format_ms(edited_age_ms),
                        _format_ms(since_event_ms),
                        _format_ms(total_since_event_ms),
                        custom_id,
                    )
                    return True
                except Exception as exc:
                    logger.error(
                        'Failed to click button on message %s (custom_id=%s url=%s disabled=%s since_event=%s): %s',
                        message.id,
                        custom_id,
                        getattr(component, "url", None),
                        disabled,
                        _format_ms(since_event_ms),
                        exc,
                    )
                    return False

        if saw_claim_label:
            logger.info(
                "No clickable claim buttons on message %s (labels=%s)",
                message.id,
                ", ".join(repr(label) for label in seen_labels) if seen_labels else "none",
            )
        else:
            logger.info(
                "No claim labels matched on message %s (labels=%s)",
                message.id,
                ", ".join(repr(label) for label in seen_labels) if seen_labels else "none",
            )
        return False

    async def handle_message(message: discord.Message, source: str) -> None:
        event_received_at = _utcnow()
        created_age_ms = None
        edited_age_ms = None
        if getattr(message, "created_at", None):
            created_age_ms = (event_received_at - message.created_at).total_seconds() * 1000
        if getattr(message, "edited_at", None):
            edited_age_ms = (event_received_at - message.edited_at).total_seconds() * 1000
        components = getattr(message, "components", None) or []
        component_children = []
        for row in components:
            component_children.extend(getattr(row, "children", []))

        logger.info(
            "%s event for message %s in %s (created_age=%s edited_age=%s components=%s)",
            source,
            message.id,
            message.channel,
            _format_ms(created_age_ms),
            _format_ms(edited_age_ms),
            len(component_children),
        )

        if message.author == client.user:
            logger.info("Skipping message %s (self message)", message.id)
            return
        if not message_allowed(message):
            logger.info("Skipping message %s (not in allowlists)", message.id)
            return
        if not component_children:
            logger.info("Skipping message %s (no components with children)", message.id)
            return
        if message.id in processed_messages:
            logger.info("Skipping message %s (already processed/attempted)", message.id)
            return

        await click_claim_button(message, event_received_at, source)

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
