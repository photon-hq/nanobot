"""iMessage channel implementation using Node.js bridge.

Supports two modes via Photon's iMessage SDKs:
  - **Local**: macOS only, uses ``@photon-ai/imessage-kit`` to read the on-device
    iMessage database and send via AppleScript.  Zero external server needed.
  - **Remote**: Any platform, uses ``@photon-ai/advanced-imessage-kit`` to connect
    to a Photon-managed iMessage server over HTTP + Socket.IO.

Communication between Python and the TypeScript bridge uses the same localhost
WebSocket pattern as the WhatsApp channel.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

_BRIDGE_DIR_NAME = "bridge-imessage"
_DEFAULT_BRIDGE_PORT = 3002


class IMessageConfig(Base):
    """iMessage channel configuration."""

    enabled: bool = False
    local: bool = True
    server_url: str = ""
    api_key: str = ""
    bridge_url: str = f"ws://localhost:{_DEFAULT_BRIDGE_PORT}"
    bridge_port: int = _DEFAULT_BRIDGE_PORT
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention"] = "open"


class IMessageChannel(BaseChannel):
    """iMessage channel with dual-mode support.

    Local mode runs on macOS using ``@photon-ai/imessage-kit`` to read/send via
    the on-device iMessage database and AppleScript.

    Remote mode connects to a Photon iMessage server using
    ``@photon-ai/advanced-imessage-kit`` for full-featured operation on any
    platform.
    """

    name = "imessage"
    display_name = "iMessage"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return IMessageConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = IMessageConfig.model_validate(config)
        super().__init__(config, bus)
        self._ws = None
        self._connected = False
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()

    async def login(self, force: bool = False) -> bool:
        """Set up the iMessage bridge (install npm deps, build TypeScript)."""
        try:
            _ensure_imessage_bridge_setup()
        except RuntimeError as e:
            logger.error("{}", e)
            return False

        logger.info("iMessage bridge is ready.")
        if self.config.local:
            logger.info(
                "Local mode: ensure Full Disk Access is granted to your terminal "
                "and iMessage is signed in on this Mac."
            )
        else:
            if not self.config.server_url or not self.config.api_key:
                logger.warning(
                    "Remote mode requires serverUrl and apiKey in config. "
                    "Get credentials from your Photon dashboard."
                )
                return False
        return True

    async def start(self) -> None:
        """Start the iMessage channel by launching the bridge and connecting."""
        import websockets

        try:
            bridge_dir = _ensure_imessage_bridge_setup()
        except RuntimeError as e:
            logger.error("iMessage bridge setup failed: {}", e)
            return

        env = self._build_bridge_env()
        bridge_proc = subprocess.Popen(
            [shutil.which("node") or "node", "dist/index.js"],
            cwd=bridge_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._bridge_proc = bridge_proc

        bridge_url = self.config.bridge_url
        logger.info("Connecting to iMessage bridge at {}...", bridge_url)
        self._running = True

        await asyncio.sleep(2)

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws

                    await ws.send(json.dumps({
                        "type": "config",
                        "local": self.config.local,
                        "serverUrl": self.config.server_url,
                        "apiKey": self.config.api_key,
                    }))

                    self._connected = True
                    logger.info(
                        "Connected to iMessage bridge ({})",
                        "local" if self.config.local else "remote",
                    )

                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling iMessage bridge message: {}", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("iMessage bridge connection error: {}", e)

                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

        self._cleanup_bridge()

    async def stop(self) -> None:
        """Stop the iMessage channel."""
        self._running = False
        self._connected = False

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._cleanup_bridge()

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through iMessage."""
        if not self._ws or not self._connected:
            logger.warning("iMessage bridge not connected")
            return

        chat_id = msg.chat_id

        if msg.content:
            try:
                payload = {"type": "send", "to": chat_id, "text": msg.content}
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logger.error("Error sending iMessage: {}", e)
                raise

        for media_path in msg.media or []:
            try:
                mime, _ = mimetypes.guess_type(media_path)
                payload = {
                    "type": "send_media",
                    "to": chat_id,
                    "filePath": media_path,
                    "mimetype": mime or "application/octet-stream",
                    "fileName": media_path.rsplit("/", 1)[-1],
                }
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logger.error("Error sending iMessage media {}: {}", media_path, e)
                raise

    # ------------------------------------------------------------------
    # Bridge message handling
    # ------------------------------------------------------------------

    async def _handle_bridge_message(self, raw: str) -> None:
        """Process a JSON frame from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from iMessage bridge: {}", raw[:100])
            return

        msg_type = data.get("type")

        if msg_type == "message":
            await self._on_incoming_message(data)

        elif msg_type == "status":
            status = data.get("status")
            logger.info("iMessage status: {}", status)
            if status in ("connected", "ready"):
                self._connected = True
            elif status == "disconnected":
                self._connected = False

        elif msg_type == "error":
            logger.error("iMessage bridge error: {}", data.get("error"))

    async def _on_incoming_message(self, data: dict) -> None:
        sender = data.get("sender", "")
        content = data.get("content", "")
        message_id = data.get("id", "")
        chat_id = data.get("chatId", sender)

        if message_id:
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

        is_group = data.get("isGroup", False)
        if is_group and getattr(self.config, "group_policy", "open") == "mention":
            return

        media_paths = data.get("media") or []
        if media_paths:
            for p in media_paths:
                mime, _ = mimetypes.guess_type(p)
                tag = "image" if mime and mime.startswith("image/") else "file"
                media_tag = f"[{tag}: {p}]"
                content = f"{content}\n{media_tag}" if content else media_tag

        sender_id = sender.split("@")[0] if "@" in sender else sender

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata={
                "message_id": message_id,
                "timestamp": data.get("timestamp"),
                "is_group": is_group,
                "source": data.get("source", "unknown"),
            },
        )

    # ------------------------------------------------------------------
    # Bridge lifecycle helpers
    # ------------------------------------------------------------------

    def _build_bridge_env(self) -> dict[str, str]:
        env = {**os.environ}
        env["BRIDGE_PORT"] = str(self.config.bridge_port)
        env["IMESSAGE_LOCAL"] = "true" if self.config.local else "false"
        if not self.config.local:
            env["IMESSAGE_SERVER_URL"] = self.config.server_url
            env["IMESSAGE_API_KEY"] = self.config.api_key
        return env

    def _cleanup_bridge(self) -> None:
        proc = getattr(self, "_bridge_proc", None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ======================================================================
# Bridge setup
# ======================================================================

def _get_imessage_bridge_install_dir() -> Path:
    """Return ``~/.nanobot/bridge-imessage``."""
    return Path.home() / ".nanobot" / _BRIDGE_DIR_NAME


def _ensure_imessage_bridge_setup() -> Path:
    """Ensure the iMessage bridge is installed and built.

    Returns the bridge directory.  Raises ``RuntimeError`` on failure.
    """
    user_bridge = _get_imessage_bridge_install_dir()

    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    npm_path = shutil.which("npm")
    if not npm_path:
        raise RuntimeError("npm not found. Please install Node.js >= 18.")

    current_file = Path(__file__)
    pkg_bridge = current_file.parent.parent / _BRIDGE_DIR_NAME
    src_bridge = current_file.parent.parent.parent / _BRIDGE_DIR_NAME

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        raise RuntimeError(
            "iMessage bridge source not found. "
            "Try reinstalling: pip install --force-reinstall nanobot-ai"
        )

    logger.info("Setting up iMessage bridge...")
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    logger.info("  Installing dependencies...")
    subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

    logger.info("  Building...")
    subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)

    logger.info("iMessage bridge ready")
    return user_bridge
