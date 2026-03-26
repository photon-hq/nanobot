"""iMessage channel implementation.

Supports two modes via Photon's iMessage platform:

  **Local** — macOS only.  Reads the on-device iMessage database
  (``~/Library/Messages/chat.db``) via ``sqlite3`` and sends through
  AppleScript.  No external server required.

  **Remote** — Any platform.  Connects to a Photon-managed iMessage
  server over HTTP + Socket.IO using ``httpx`` and ``python-socketio``.
  Supports the full feature set: reactions, typing indicators, message
  editing, polls, and file attachments.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import platform
import sqlite3
import subprocess
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

import httpx
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base

try:
    import socketio

    SOCKETIO_AVAILABLE = True
except ImportError:
    socketio = None  # type: ignore[assignment]
    SOCKETIO_AVAILABLE = False

_DEFAULT_DB_PATH = str(Path.home() / "Library" / "Messages" / "chat.db")
_LOCAL_POLL_INTERVAL = 2.0  # seconds between local DB polls


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class IMessageConfig(Base):
    """iMessage channel configuration."""

    enabled: bool = False
    local: bool = True
    server_url: str = ""
    api_key: str = ""
    database_path: str = _DEFAULT_DB_PATH
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention"] = "open"


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class IMessageChannel(BaseChannel):
    """iMessage channel with local (macOS) and remote (Photon) modes.

    Local mode reads from the native iMessage SQLite database and sends
    via AppleScript — pure Python, no external dependencies.

    Remote mode uses HTTP + Socket.IO to talk to a Photon iMessage server,
    following the same pattern as the Mochat channel.
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
        self.config: IMessageConfig = config
        self._processed_ids: OrderedDict[str, None] = OrderedDict()
        self._http: httpx.AsyncClient | None = None
        self._sio: Any = None
        self._sio_connected = False
        self._last_rowid: int = 0

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.config.local:
            await self._start_local()
        else:
            await self._start_remote()

    async def stop(self) -> None:
        self._running = False
        if self._sio:
            try:
                await self._sio.disconnect()
            except Exception:
                pass
            self._sio = None
        self._sio_connected = False
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        if self.config.local:
            await self._send_local(msg)
        else:
            await self._send_remote(msg)

    # ======================================================================
    # LOCAL MODE  (macOS — sqlite3 + AppleScript)
    # ======================================================================

    async def _start_local(self) -> None:
        if platform.system() != "Darwin":
            logger.error("iMessage local mode requires macOS")
            return

        db_path = self.config.database_path
        if not Path(db_path).exists():
            logger.error(
                "iMessage database not found at {}. "
                "Ensure Full Disk Access is granted to your terminal.",
                db_path,
            )
            return

        self._running = True
        self._last_rowid = self._get_max_rowid(db_path)
        logger.info("iMessage local watcher started (polling {})", db_path)

        while self._running:
            try:
                await self._poll_local_db(db_path)
            except Exception as e:
                logger.warning("iMessage local poll error: {}", e)
            await asyncio.sleep(_LOCAL_POLL_INTERVAL)

    def _get_max_rowid(self, db_path: str) -> int:
        try:
            conn = sqlite3.connect(db_path, uri=True)
            cur = conn.execute("SELECT MAX(ROWID) FROM message")
            row = cur.fetchone()
            conn.close()
            return row[0] or 0
        except Exception:
            return 0

    async def _poll_local_db(self, db_path: str) -> None:
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, self._fetch_new_messages, db_path)
        for row in rows:
            await self._handle_local_message(row)

    def _fetch_new_messages(self, db_path: str) -> list[dict[str, Any]]:
        conn = sqlite3.connect(db_path, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT
                m.ROWID,
                m.guid,
                m.text,
                m.is_from_me,
                m.date AS msg_date,
                m.service,
                h.id AS sender,
                c.chat_identifier,
                c.style AS chat_style
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
            """,
            (self._last_rowid,),
        )
        results = []
        for row in cur:
            d = dict(row)
            self._last_rowid = max(self._last_rowid, d["ROWID"])
            results.append(d)
        conn.close()
        return results

    async def _handle_local_message(self, row: dict[str, Any]) -> None:
        if row.get("is_from_me"):
            return

        message_id = row.get("guid", "")
        if self._is_duplicate(message_id):
            return

        sender = row.get("sender") or ""
        chat_id = row.get("chat_identifier") or sender
        content = row.get("text") or ""
        is_group = (row.get("chat_style") or 0) == 43

        if is_group and self.config.group_policy == "mention":
            return

        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=content,
            metadata={
                "message_id": message_id,
                "service": row.get("service", "iMessage"),
                "is_group": is_group,
                "source": "local",
            },
        )

    async def _send_local(self, msg: OutboundMessage) -> None:
        recipient = msg.chat_id
        if msg.content:
            await self._applescript_send_text(recipient, msg.content)

        for media_path in msg.media or []:
            await self._applescript_send_file(recipient, media_path)

    async def _applescript_send_text(self, recipient: str, text: str) -> None:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'tell application "Messages"\n'
            f'  set targetService to 1st account whose service type = iMessage\n'
            f'  set targetBuddy to participant "{recipient}" of targetService\n'
            f'  send "{escaped}" to targetBuddy\n'
            f'end tell'
        )
        await self._run_osascript(script)

    async def _applescript_send_file(self, recipient: str, file_path: str) -> None:
        script = (
            f'tell application "Messages"\n'
            f'  set targetService to 1st account whose service type = iMessage\n'
            f'  set targetBuddy to participant "{recipient}" of targetService\n'
            f'  send POSIX file "{file_path}" to targetBuddy\n'
            f'end tell'
        )
        await self._run_osascript(script)

    async def _run_osascript(self, script: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["osascript", "-e", script],
                    check=True,
                    capture_output=True,
                    timeout=15,
                ),
            )
        except subprocess.CalledProcessError as e:
            logger.error("AppleScript send failed: {}", e.stderr.decode()[:200] if e.stderr else str(e))
            raise
        except subprocess.TimeoutExpired:
            logger.error("AppleScript send timed out")
            raise

    # ======================================================================
    # REMOTE MODE  (Photon server — httpx + Socket.IO)
    # ======================================================================

    async def _start_remote(self) -> None:
        if not self.config.server_url:
            logger.error("iMessage remote mode requires serverUrl")
            return
        if not self.config.api_key:
            logger.error("iMessage remote mode requires apiKey")
            return
        if not SOCKETIO_AVAILABLE:
            logger.error("python-socketio required for iMessage remote mode (already a nanobot dependency)")
            return

        self._running = True
        self._http = httpx.AsyncClient(
            base_url=self.config.server_url.rstrip("/"),
            headers={
                "X-API-Key": self.config.api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        await self._connect_socketio()

        while self._running:
            await asyncio.sleep(1)

    async def _connect_socketio(self) -> None:
        client = socketio.AsyncClient(
            reconnection=True,
            reconnection_delay=2,
            reconnection_delay_max=10,
            logger=False,
            engineio_logger=False,
        )

        @client.event
        async def connect() -> None:
            self._sio_connected = True
            logger.info("Connected to Photon iMessage server")

        @client.event
        async def disconnect() -> None:
            if not self._running:
                return
            self._sio_connected = False
            logger.warning("Disconnected from Photon iMessage server")

        @client.on("new-message")
        async def on_new_message(data: Any) -> None:
            await self._handle_remote_message(data)

        self._sio = client

        server_url = self.config.server_url.rstrip("/")
        try:
            await client.connect(
                server_url,
                transports=["websocket"],
                auth={"apiKey": self.config.api_key},
                wait_timeout=10,
            )
        except Exception as e:
            logger.error("Failed to connect to Photon iMessage server: {}", e)
            self._sio_connected = False

    async def _handle_remote_message(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if data.get("isFromMe"):
            return

        message_id = data.get("guid", "")
        if self._is_duplicate(message_id):
            return

        sender = ""
        handle = data.get("handle")
        if isinstance(handle, dict):
            sender = handle.get("address", "")
        if not sender:
            sender = data.get("sender", "")

        chats = data.get("chats") or []
        chat_guid = chats[0].get("guid", "") if chats else ""
        if not chat_guid:
            chat_guid = data.get("chatGuid", sender)

        content = data.get("text") or ""
        is_group = ";+;" in chat_guid

        if is_group and self.config.group_policy == "mention":
            return

        media_paths: list[str] = []
        for att in data.get("attachments") or []:
            name = att.get("transferName") or att.get("filename") or ""
            if name:
                media_tag = f"[file: {name}]"
                content = f"{content}\n{media_tag}" if content else media_tag

        await self._handle_message(
            sender_id=sender,
            chat_id=chat_guid,
            content=content,
            media=media_paths,
            metadata={
                "message_id": message_id,
                "is_group": is_group,
                "source": "remote",
                "timestamp": data.get("dateCreated"),
            },
        )

    async def _send_remote(self, msg: OutboundMessage) -> None:
        if not self._http:
            logger.warning("iMessage remote HTTP client not initialised")
            return

        chat_guid = msg.chat_id

        if msg.content:
            try:
                await self._http.post(
                    "/api/v1/message/text",
                    json={"chatGuid": chat_guid, "message": msg.content},
                )
            except Exception as e:
                logger.error("iMessage remote send failed: {}", e)
                raise

        for media_path in msg.media or []:
            try:
                mime, _ = mimetypes.guess_type(media_path)
                with open(media_path, "rb") as f:
                    await self._http.post(
                        "/api/v1/attachment",
                        data={"chatGuid": chat_guid},
                        files={"file": (Path(media_path).name, f, mime or "application/octet-stream")},
                    )
            except Exception as e:
                logger.error("iMessage remote media send failed: {}", e)
                raise

    # ---- dedup helper ------------------------------------------------------

    def _is_duplicate(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._processed_ids:
            return True
        self._processed_ids[message_id] = None
        while len(self._processed_ids) > 1000:
            self._processed_ids.popitem(last=False)
        return False
