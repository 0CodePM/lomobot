"""MQTT channel implementation — connects to a mosquitto broker.

Uses paho-mqtt in a background thread with auto-reconnect, so the connection
stays alive long-term. Anonymous connection (no username/password) when
``username`` is empty.

Agent identity: the MQTT name is always ``ag_<agent_id>`` (prefix auto-added
unless agent_id already starts with it). Set ``username`` only to override
the MQTT auth account explicitly.

Topics (mesh spec, see im_api payload docs):
  - Subscribe: im/ag_<name>/inbox    (legacy/human path, no countersign)
               am/ag_<name>/inbox    (agent path, countersign REQUIRED)
  - Publish:   im/<sender>/inbox     (reply to a human — no countersign)
               am/<ag_target>/inbox  (reply to an agent — countersign sent)

An inbound am/ message is dropped unless its ``countersign`` field matches
``config.countersign`` exactly (16-char code issued at agent creation).
If no countersign is configured, all am/ messages are rejected.

Payload format matches webim (Fireside Chat):
  {"from": ..., "display_name": ..., "msg_type": "im",
   "content_type": "text", "text": ..., "ts": ...}

Images/files follow the Fireside webim media spec:
  - JSON inline:  content_type "image" + base64 ``image_data`` (< 70 KB)
  - Binary frame: ``1B ver | 1B type | 2B meta_len (BE) | meta JSON | data``
    type 0x01=TEXT, 0x02=IMAGE (<= 2 MB), 0x03=FILE (<= 10 MB), 0x04=SYSTEM
  - Detection: buf[0]==0x01 -> binary frame, 0x7B ('{') -> JSON, else drop.
Received media is saved under ~/.lomobot/media/mqtt/ and passed to the
agent loop via InboundMessage.media.
"""

import asyncio
import base64
import json
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from lomobot.bus.events import OutboundMessage
from lomobot.bus.queue import MessageBus
from lomobot.channels.base import BaseChannel
from lomobot.config.schema import MQTTConfig

# Fireside binary protocol v1 frame types
FRAME_TEXT = 0x01
FRAME_IMAGE = 0x02
FRAME_FILE = 0x03
FRAME_SYSTEM = 0x04

# Client-side size limits the spec says clients MUST enforce
INLINE_IMAGE_LIMIT = 70 * 1024  # base64 JSON threshold
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024

_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class MQTTChannel(BaseChannel):
    """
    MQTT channel that connects to a mosquitto broker.

    paho-mqtt runs in a background thread (loop_start) with
    reconnect_on_failure, so the connection is maintained long-term without
    blocking the agent's main loop.
    """

    name = "mqtt"

    def __init__(self, config: MQTTConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: MQTTConfig = config
        self._client = None
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def _sender_name(self) -> str:
        return self.config.display_name or self.identity

    @property
    def identity(self) -> str:
        """MQTT-visible agent name: always ag_<agent_id> (prefix auto-added)."""
        aid = self.config.agent_id
        return aid if aid.startswith("ag_") else f"ag_{aid}"

    @staticmethod
    def _is_agent_name(name: str) -> bool:
        return str(name).startswith("ag_")

    # ---- paho callbacks (run in paho's background thread) ----

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            logger.info(f"MQTT connected to {self.config.broker_host}:{self.config.broker_port}")
            # Subscribe to both namespaces (im/ = legacy human path, am/ = agent path)
            client.subscribe(f"im/{self.identity}/inbox", qos=1)
            client.subscribe(f"am/{self.identity}/inbox", qos=1)
            logger.info(f"MQTT subscribed to im/{self.identity}/inbox + am/{self.identity}/inbox")
        else:
            logger.error(f"MQTT connect rejected (rc={rc})")

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            logger.warning(f"MQTT unexpected disconnect (rc={rc}) — auto-reconnect will retry")
        else:
            logger.info("MQTT disconnected")

    # ---- payload parsing (Fireside webim: JSON + binary frame v1) ----

    @staticmethod
    def _build_frame(ftype: int, meta: dict[str, Any], data: bytes | None = None) -> bytes:
        """Build a Fireside binary frame: 1B ver | 1B type | 2B meta_len | meta | data."""
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        return struct.pack(">BBH", 1, ftype, len(meta_bytes)) + meta_bytes + (data or b"")

    def _parse_payload(self, raw: bytes, topic: str) -> dict[str, Any] | None:
        """Normalize any Fireside payload into a dict, or None to drop."""
        if not raw:
            logger.warning(f"MQTT empty payload on {topic}")
            return None
        if raw[0] == 0x01:  # binary frame (ver=1)
            return self._parse_binary_frame(raw, topic)
        if raw[0] == 0x7B:  # '{' — legacy JSON envelope
            return self._parse_json_payload(raw, topic)
        logger.warning(f"MQTT unsupported raw payload on {topic}: {raw[:40]!r}")
        return None

    def _parse_json_payload(self, raw: bytes, topic: str) -> dict[str, Any] | None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"MQTT invalid JSON on {topic}: {raw[:80]!r}")
            return None
        if not isinstance(data, dict):
            logger.warning(f"MQTT ignoring non-object payload on {topic}")
            return None
        sender = str(data.get("from", "")).strip()
        if not sender:
            logger.warning(f"MQTT message missing 'from' on {topic}")
            return None

        content_type = str(data.get("content_type", "text"))
        parsed: dict[str, Any] = {
            "from": sender,
            "display_name": data.get("display_name", sender),
            "text": str(data.get("text", "")),
            "content_type": content_type,
            "ts": data.get("ts"),
            "countersign": str(data.get("countersign", "")),
            "media_bytes": None,
            "media_name": None,
        }
        if content_type == "image" and data.get("image_data"):
            try:
                parsed["media_bytes"] = base64.b64decode(str(data["image_data"]), validate=True)
            except Exception:
                logger.warning(f"MQTT bad base64 image_data on {topic} — ignoring media")
            mime = str(data.get("mime", "image/jpeg"))
            ext = _EXT_BY_MIME.get(mime, ".jpg")
            parsed["media_name"] = f"image{ext}"
            parsed["mime"] = mime
        elif content_type in ("image", "file") and data.get("url"):
            logger.warning(
                f"MQTT url-based {content_type} on {topic} not implemented — text only"
            )
        return parsed

    def _parse_binary_frame(self, raw: bytes, topic: str) -> dict[str, Any] | None:
        if len(raw) < 4:
            logger.warning(f"MQTT truncated binary frame on {topic}")
            return None
        _ver, ftype = raw[0], raw[1]
        (meta_len,) = struct.unpack(">H", raw[2:4])
        if len(raw) < 4 + meta_len:
            logger.warning(f"MQTT truncated meta in binary frame on {topic}")
            return None
        try:
            meta = json.loads(raw[4 : 4 + meta_len].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"MQTT invalid meta JSON in binary frame on {topic}")
            return None
        if not isinstance(meta, dict):
            logger.warning(f"MQTT ignoring non-object frame meta on {topic}")
            return None
        sender = str(meta.get("from", "")).strip()
        if not sender:
            logger.warning(f"MQTT binary frame missing 'from' on {topic}")
            return None

        data = raw[4 + meta_len :]
        parsed: dict[str, Any] = {
            "from": sender,
            "display_name": meta.get("display_name", sender),
            "text": str(meta.get("text", "")),
            "content_type": "text",
            "ts": meta.get("ts"),
            "countersign": str(meta.get("countersign", "")),
            "media_bytes": None,
            "media_name": None,
        }
        if ftype == FRAME_IMAGE:
            if len(data) > MAX_IMAGE_BYTES:
                logger.warning(f"MQTT image frame too large ({len(data)} B) on {topic}")
                return None
            mime = str(meta.get("mime", "image/jpeg"))
            ext = _EXT_BY_MIME.get(mime, ".jpg")
            parsed.update(content_type="image", media_bytes=data, media_name=f"image{ext}", mime=mime)
        elif ftype == FRAME_FILE:
            if len(data) > MAX_FILE_BYTES:
                logger.warning(f"MQTT file frame too large ({len(data)} B) on {topic}")
                return None
            filename = str(meta.get("filename", "file.bin"))
            parsed.update(content_type="file", media_bytes=data, media_name=filename)
        elif ftype == FRAME_SYSTEM:
            parsed["content_type"] = "system"
        elif ftype != FRAME_TEXT:
            logger.warning(f"MQTT unknown frame type {ftype:#04x} on {topic}")
            return None
        return parsed

    def _save_media(self, parsed: dict[str, Any]) -> str | None:
        """Persist received media bytes under ~/.lomobot/media/mqtt (blocking; run in thread)."""
        try:
            media_dir = Path.home() / ".lomobot" / "media" / "mqtt"
            media_dir.mkdir(parents=True, exist_ok=True)
            safe_name = _SAFE_NAME_RE.sub("_", str(parsed.get("media_name") or "media"))[:80]
            # uuid suffix: ms-timestamp alone can collide when bursts land in
            # the same millisecond and to_thread completion order is undefined.
            path = media_dir / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{safe_name}"
            path.write_bytes(parsed["media_bytes"])
            return str(path)
        except OSError as exc:
            logger.warning(f"MQTT failed to save media: {exc}")
            return None

    async def _handle_inbound(self, parsed: dict[str, Any], topic: str) -> None:
        """Async side of inbound handling: save media, then hand to BaseChannel."""
        if self._loop is None or self._loop.is_closed():
            logger.warning("MQTT event loop not available — dropping message")
            return

        media_paths: list[str] = []
        if parsed.get("media_bytes"):
            path = await asyncio.to_thread(self._save_media, parsed)
            if path:
                media_paths.append(path)

        content = parsed["text"]
        if not content and media_paths:
            label = "image" if parsed["content_type"] == "image" else "file"
            content = f"[{label} received]"

        await self._handle_message(
            sender_id=parsed["from"],
            chat_id=parsed["from"],
            content=content,
            media=media_paths or None,
            metadata={
                "topic": topic,
                "sender_name": parsed["display_name"],
                "content_type": parsed["content_type"],
                "ts": parsed["ts"],
            },
        )

    def _on_message(self, client, userdata, msg):
        """Receive a message addressed to this agent."""
        parsed = self._parse_payload(msg.payload, msg.topic)
        if parsed is None:
            return

        # Ignore our own echoes (e.g. shared wildcard subscriptions).
        if parsed["from"] == self.identity:
            return

        # am/ namespace: countersign is mandatory (mesh agent-to-agent spec).
        if msg.topic.startswith("am/"):
            expected = self.config.countersign
            got = parsed.get("countersign", "")
            if not expected or got != expected:
                logger.warning(
                    f"MQTT am/ message from {parsed['from']} rejected: "
                    f"countersign {'missing' if not got else 'mismatch'}"
                )
                return

        # Schedule the async handler on the agent's event loop
        if self._loop is None or self._loop.is_closed():
            logger.warning("MQTT event loop not available — dropping message")
            return

        coro = self._handle_inbound(parsed, msg.topic)
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ---- BaseChannel interface ----

    async def start(self) -> None:
        """Connect to the broker and keep the connection alive."""
        import paho.mqtt.client as mqtt

        self._running = True
        self._loop = asyncio.get_running_loop()

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"agent_{self.config.agent_id}",
            transport=self.config.transport,
            reconnect_on_failure=True,
        )
        if self.config.tls:
            import ssl as _ssl

            self._client.tls_set(cert_reqs=_ssl.CERT_REQUIRED)
        if self.config.username:
            self._client.username_pw_set(self.config.username, self.config.password or None)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        logger.info(
            f"MQTT connecting to {self.config.broker_host}:{self.config.broker_port} "
            f"(agent={self.config.agent_id})..."
        )
        self._client.connect(self.config.broker_host, self.config.broker_port, 60)
        self._client.loop_start()  # background thread — keeps connection alive

        # Long-running: keep alive until stop() is called
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Disconnect and clean up."""
        self._running = False
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as e:
                logger.warning(f"MQTT disconnect error: {e}")
            self._client = None
        self._connected = False
        logger.info("MQTT channel stopped")

    def _build_media_payload(self, media_path: str, to_agent: bool = False) -> tuple[bytes, str] | None:
        """Build a Fireside payload for one media file. Returns (payload, kind)."""
        path = Path(media_path)
        if not path.is_file():
            logger.warning(f"MQTT media file not found: {media_path}")
            return None
        data = path.read_bytes()
        mime = _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
        meta = {
            "from": self.identity,
            "display_name": self._sender_name,
            "msg_type": "im",
            "ts": int(time.time() * 1000),
        }
        if to_agent and self.config.countersign:
            meta["countersign"] = self.config.countersign
        if mime.startswith("image/"):
            if len(data) > MAX_IMAGE_BYTES:
                logger.warning(f"MQTT refusing to send image over 2 MB: {media_path}")
                return None
            if len(data) < INLINE_IMAGE_LIMIT:
                payload = json.dumps(
                    {
                        **meta,
                        "content_type": "image",
                        "image_data": base64.b64encode(data).decode("ascii"),
                        "mime": mime,
                        "size": len(data),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                return payload, "image(json)"
            frame_meta = {**meta, "mime": mime, "size": len(data)}
            return self._build_frame(FRAME_IMAGE, frame_meta, data), "image(frame)"
        if len(data) > MAX_FILE_BYTES:
            logger.warning(f"MQTT refusing to send file over 10 MB: {media_path}")
            return None
        frame_meta = {**meta, "filename": path.name, "size": len(data)}
        return self._build_frame(FRAME_FILE, frame_meta, data), "file(frame)"

    async def send(self, msg: OutboundMessage) -> None:
        """Send a reply (text + optional media) back to the sender via MQTT.

        Routing: replies to agents (ag_*) go to am/<target>/inbox with our
        countersign attached; replies to humans keep im/<target>/inbox.
        """
        if self._client is None or not self._connected:
            logger.warning("MQTT not connected — cannot send")
            return

        to_agent = self._is_agent_name(msg.chat_id)
        topic = f"{'am' if to_agent else 'im'}/{msg.chat_id}/inbox"
        if msg.content:
            body: dict[str, Any] = {
                "from": self.identity,
                "display_name": self._sender_name,
                "msg_type": "im",
                "content_type": "text",
                "text": msg.content,
                "ts": int(time.time() * 1000),
            }
            if to_agent and self.config.countersign:
                body["countersign"] = self.config.countersign
            payload = json.dumps(body, ensure_ascii=False)
            try:
                self._client.publish(topic, payload, qos=1)
                logger.debug(f"MQTT sent text to {topic}")
            except Exception as e:
                logger.error(f"MQTT send error: {e}")

        for media_path in msg.media or []:
            try:
                built = await asyncio.to_thread(self._build_media_payload, media_path, to_agent)
                if built is None:
                    continue
                payload, kind = built
                self._client.publish(topic, payload, qos=1)
                logger.info(f"MQTT sent {kind} ({len(payload)} B) to {topic}")
            except Exception as e:
                logger.error(f"MQTT media send error: {e}")

    def is_allowed(self, sender_id: str) -> bool:
        """Allow-list check (empty = allow everyone)."""
        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            return True
        return str(sender_id) in allow_list
