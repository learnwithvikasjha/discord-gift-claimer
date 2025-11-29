import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Set, Optional
from collections import deque
from time import perf_counter

import orjson
import discord

# ---------------------------
# Basic setup
# ---------------------------
LOGS_DIR = Path(__file__).with_name("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)
APP_LOG = LOGS_DIR / "app.log"
if APP_LOG.exists():
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rotated = LOGS_DIR / f"app_{timestamp}.log"
    try:
        APP_LOG.rename(rotated)
    except OSError as exc:
        # Fall back to continuing with the existing log if rotation fails
        print(f"Warning: could not rotate existing app.log: {exc}", file=sys.stderr)
LOG_FILE = APP_LOG

# Minimal logging configuration: INFO for important events, DEBUG if you explicitly enable it.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("claim-gift")
# suppress very verbose discord library logs by default
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)

CONFIG_PATH = Path(__file__).with_name("config.json")


# ---------------------------
# Utilities
# ---------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_ms(value: Optional[float]) -> str:
    return f"{value:.1f}ms" if value is not None else "?"

LAT_WINDOW = 200
metrics = {
    "enqueue_latencies": deque(maxlen=LAT_WINDOW),
    "queue_wait": deque(maxlen=LAT_WINDOW),
    "handler_time": deque(maxlen=LAT_WINDOW),
}


def _sample_stats(bucket: deque, value: float) -> None:
    bucket.append(value)


def _pctile(values: deque, p: float) -> Optional[float]:
    if not values:
        return None
    data = sorted(values)
    idx = int((len(data) - 1) * (p / 100))
    return data[idx]


# ---------------------------
# Config dataclass + loaders
# ---------------------------
@dataclass
class Config:
    token: str
    claim_button_texts: Set[str] = field(default_factory=lambda: {"claim gift"})
    allowed_guild_ids: Set[int] = field(default_factory=set)
    allowed_channel_ids: Set[int] = field(default_factory=set)
    worker_count: int = 2  # number of concurrent click workers
    processed_ttl_seconds: int = 300  # how long to remember processed message IDs


def _parse_id_list(values) -> Set[int]:
    parsed: Set[int] = set()
    for value in values or []:
        try:
            parsed.add(int(value))
        except (TypeError, ValueError):
            logger.warning("Skipping invalid ID value in config: %r", value)
    return parsed


def _parse_label_list(values, legacy_value: str = "") -> Set[str]:
    parsed: Set[str] = set()
    if isinstance(values, (list, tuple)):
        sources = values
    elif values:
        sources = [values]
    else:
        sources = []

    for raw in sources:
        text = str(raw).strip()
        if text:
            parsed.add(text.lower())

    legacy = str(legacy_value or "").strip()
    if legacy:
        parsed.add(legacy.lower())

    if not parsed:
        parsed.add("claim gift")
    return parsed


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config file at {CONFIG_PATH}")

    with CONFIG_PATH.open("rb") as fp:
        raw = orjson.loads(fp.read())

    token = str(raw.get("token", "")).strip()
    if not token or token == "YOUR_DISCORD_TOKEN":
        raise ValueError("Please set your account token in config.json (token).")

    claim_texts = _parse_label_list(raw.get("claim_button_texts"), raw.get("claim_button_text", ""))
    guild_ids = _parse_id_list(raw.get("allowed_guild_ids"))
    channel_ids = _parse_id_list(raw.get("allowed_channel_ids"))
    worker_count = int(raw.get("worker_count", 2)) if raw.get("worker_count") else 2
    ttl = int(raw.get("processed_ttl_seconds", 300)) if raw.get("processed_ttl_seconds") else 300

    return Config(
        token=token,
        claim_button_texts=claim_texts,
        allowed_guild_ids=guild_ids,
        allowed_channel_ids=channel_ids,
        worker_count=max(1, min(8, worker_count)),  # clamp to a sane range
        processed_ttl_seconds=max(30, ttl),
    )


# ---------------------------
# Main runtime
# ---------------------------
async def main() -> None:
    config = load_config()
    logger.info("Loaded configuration: workers=%d claim_texts=%s", config.worker_count, sorted(config.claim_button_texts))

    # Intents: only enable what's required (minimizes gateway event noise)
    intents = None
    if hasattr(discord, "Intents"):
        intents = discord.Intents.none()
        intents.messages = True
        intents.message_content = True  # required to read message content/components in many discord.py builds

    client_kwargs = {"bot": False}
    if intents is not None:
        client_kwargs["intents"] = intents

    try:
        client = discord.Client(**client_kwargs)
    except TypeError:
        client_kwargs.pop("intents", None)
        client = discord.Client(**client_kwargs)

    # A small bounded queue for click tasks. Bounded so memory won't explode during bursts.
    click_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    # processed_messages maps message_id -> timestamp when it was processed
    processed_messages: Dict[int, datetime] = {}
    processed_lock = asyncio.Lock()  # protect processed_messages
    stats = {
        "messages_seen": 0,
        "edits_seen": 0,
        "allowed": 0,
        "enqueued": 0,
        "clicked": 0,
        "click_fail": 0,
    }

    # Provide a periodic cleanup to remove stale entries
    async def processed_cleanup_task():
        while True:
            await asyncio.sleep(max(10, config.processed_ttl_seconds // 4))
            cutoff = _utcnow() - timedelta(seconds=config.processed_ttl_seconds)
            async with processed_lock:
                removed = [mid for mid, ts in processed_messages.items() if ts < cutoff]
                for mid in removed:
                    del processed_messages[mid]
            if removed:
                logger.debug("Cleaned up %d processed message ids", len(removed))

    async def stats_reporter():
        while True:
            await asyncio.sleep(30)
            async with processed_lock:
                processed_count = len(processed_messages)
            enqueue_p50 = _pctile(metrics["enqueue_latencies"], 50)
            enqueue_p95 = _pctile(metrics["enqueue_latencies"], 95)
            wait_p50 = _pctile(metrics["queue_wait"], 50)
            wait_p95 = _pctile(metrics["queue_wait"], 95)
            handler_p50 = _pctile(metrics["handler_time"], 50)
            handler_p95 = _pctile(metrics["handler_time"], 95)
            logger.info(
                "Heartbeat: msg=%d edit=%d allowed=%d enqueued=%d clicked=%d failed=%d queue=%d processed=%d "
                "enqueue_p50=%s enqueue_p95=%s wait_p50=%s wait_p95=%s handler_p50=%s handler_p95=%s",
                stats["messages_seen"],
                stats["edits_seen"],
                stats["allowed"],
                stats["enqueued"],
                stats["clicked"],
                stats["click_fail"],
                click_queue.qsize(),
                processed_count,
                _format_ms(enqueue_p50) if enqueue_p50 is not None else "?",
                _format_ms(enqueue_p95) if enqueue_p95 is not None else "?",
                _format_ms(wait_p50) if wait_p50 is not None else "?",
                _format_ms(wait_p95) if wait_p95 is not None else "?",
                _format_ms(handler_p50) if handler_p50 is not None else "?",
                _format_ms(handler_p95) if handler_p95 is not None else "?",
            )

    # Worker that performs clicks
    async def worker(worker_id: int):
        logger.info("Worker-%d started", worker_id)
        while True:
            item = await click_queue.get()
            message = item["message"]
            event_received_at = item["event_received_at"]
            source = item["source"]
            enqueue_time = item.get("enqueue_time")
            try:
                if enqueue_time is not None:
                    _sample_stats(metrics["queue_wait"], (perf_counter() - enqueue_time) * 1000.0)
                handler_start = perf_counter()
                await _attempt_click(message, event_received_at, source)
                handler_end = perf_counter()
                _sample_stats(metrics["handler_time"], (handler_end - handler_start) * 1000.0)
            except Exception:
                logger.exception("Unhandled exception in worker-%d while clicking message %s", worker_id, getattr(message, "id", "unknown"))
            finally:
                click_queue.task_done()

    # the core click routine (keeps same click semantics as your original code)
    async def _attempt_click(message: discord.Message, event_received_at: datetime, source: str) -> bool:
        # compute a few cheap things early
        msg_id = message.id
        # iterate over components *once* and try to click the first matching interactive button
        saw_claim_label = False
        seen_labels = []
        now = _utcnow()
        created_age_ms = (now - message.created_at).total_seconds() * 1000 if getattr(message, "created_at", None) else None
        edited_age_ms = (now - message.edited_at).total_seconds() * 1000 if getattr(message, "edited_at", None) else None

        # For speed, access components directly and avoid allocations where possible
        for row in getattr(message, "components", []) or []:
            for comp in getattr(row, "children", []) or []:
                label = getattr(comp, "label", "") or ""
                seen_labels.append(label)
                normalized = label.strip().lower()
                if not normalized or normalized not in config.claim_button_texts:
                    continue

                saw_claim_label = True
                custom_id = getattr(comp, "custom_id", None)
                is_url = bool(getattr(comp, "url", None))
                disabled = bool(getattr(comp, "disabled", False))

                if disabled:
                    # cheap log at debug level
                    logger.debug('Skipping disabled button "%s" on message %s in %s', label, msg_id, message.channel)
                    continue

                if is_url or not custom_id:
                    logger.debug('Skipping non-interactive button "%s" on message %s (url=%s custom_id=%s)', label, msg_id, getattr(comp, "url", None), custom_id)
                    continue

                # mark processed early (guard against quick duplicate edits)
                async with processed_lock:
                    if msg_id in processed_messages:
                        logger.debug("Already processed message %s", msg_id)
                        return False
                    processed_messages[msg_id] = _utcnow()

                before_click = _utcnow()
                since_event_ms = (before_click - event_received_at).total_seconds() * 1000
                try:
                    # this is the actual click call you used previously
                    await comp.click()
                    after_click = _utcnow()
                    total_since_event_ms = (after_click - event_received_at).total_seconds() * 1000
                    stats["clicked"] += 1
                    logger.info(
                        'Clicked "%s" on message %s in %s via %s (age=%s edited_age=%s since_event=%s after_click=%s custom_id=%s)',
                        label or next(iter(config.claim_button_texts)),
                        msg_id,
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
                    stats["click_fail"] += 1
                    logger.error("Failed to click button on message %s (custom_id=%s since_event=%s): %s", msg_id, custom_id, _format_ms(since_event_ms), exc)
                    return False

        # If we get here, no clickable button found
        if saw_claim_label:
            logger.debug("No clickable claim buttons on message %s (labels=%s)", getattr(message, "id", None), ", ".join(repr(l) for l in seen_labels) or "none")
        else:
            logger.debug("No claim labels matched on message %s (labels=%s)", getattr(message, "id", None), ", ".join(repr(l) for l in seen_labels) or "none")
        return False

    # Fast allowlist check (cheap early return)
    def message_allowed(message: discord.Message) -> bool:
        if config.allowed_guild_ids:
            g = message.guild
            if g is None or g.id not in config.allowed_guild_ids:
                return False
        if config.allowed_channel_ids and message.channel.id not in config.allowed_channel_ids:
            return False
        return True

    # Enqueue a message for worker processing (non-blocking)
    async def enqueue_click(message: discord.Message, event_received_at: datetime, source: str) -> None:
        try:
            click_queue.put_nowait(
                {"message": message, "event_received_at": event_received_at, "source": source, "enqueue_time": perf_counter()}
            )
        except asyncio.QueueFull:
            logger.warning("Click queue is full; dropping message %s", message.id)

    # Event handlers: minimal and early-exit fast
    @client.event
    async def on_ready():
        logger.info("Ready as %s (%s)", client.user, client.user.id)
        if config.allowed_guild_ids:
            logger.info("Guild allowlist: %s", ", ".join(str(g) for g in sorted(config.allowed_guild_ids)))
        if config.allowed_channel_ids:
            logger.info("Channel allowlist: %s", ", ".join(str(c) for c in sorted(config.allowed_channel_ids)))

    async def _handle_incoming(message: discord.Message, source: str):
        if source == "message":
            stats["messages_seen"] += 1
            # We only act on edits; ignore new message events
            return
        else:
            stats["edits_seen"] += 1
        t_receive = perf_counter()

        # very cheap early checks
        if message.author == client.user:
            return
        if not message_allowed(message):
            return
        stats["allowed"] += 1

        # quickly test whether there are any component children; avoid building lists
        comps = getattr(message, "components", None) or []
        has_children = False
        for r in comps:
            if getattr(r, "children", None):
                has_children = True
                break
        if not has_children:
            return

        # ensure we don't enqueue duplicates
        async with processed_lock:
            if message.id in processed_messages:
                return

        # small metadata for metrics; cheap timestamp capture
        event_received_at = _utcnow()
        # enqueue for background workers (fast)
        await enqueue_click(message, event_received_at, source)
        stats["enqueued"] += 1
        _sample_stats(metrics["enqueue_latencies"], (perf_counter() - t_receive) * 1000.0)

    @client.event
    async def on_message(message: discord.Message):
        # don't await heavy work here; just call the fast handler
        await _handle_incoming(message, source="message")

    @client.event
    async def on_message_edit(before, after: discord.Message):
        await _handle_incoming(after, source="edit")

    # start background tasks and workers
    # - processed cleanup
    asyncio.create_task(processed_cleanup_task())
    asyncio.create_task(stats_reporter())

    # - worker pool
    for i in range(config.worker_count):
        asyncio.create_task(worker(i + 1))

    # start client (this call blocks until disconnect)
    await client.start(config.token)


# ---------------------------
# Entrypoint
# ---------------------------
if __name__ == "__main__":
    if sys.platform != "win32":
        try:
            import uvloop

            uvloop.install()
            logger.info("uvloop event loop installed for improved performance.")
        except Exception:
            logger.info("uvloop not installed; using default event loop.")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt).")
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        raise SystemExit(1) from exc
