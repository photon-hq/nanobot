"""iMessage channel implementation.

Supports two modes via Photon's iMessage platform:

  **Local** — macOS only.  Reads the on-device iMessage database
  (``~/Library/Messages/chat.db``) via ``sqlite3`` and sends through
  AppleScript.  No external server required.

  **Remote** — Any platform.  Connects to a Photon-managed iMessage
  server over pure HTTP using ``httpx``.  Polls for new messages and
  sends via REST API.  Supports HTTP proxy for all traffic.
"""

from __future__ import annotations

import asyncio
import mimetypes
import platform
import sqlite3
import subprocess
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

_DEFAULT_DB_PATH = str(Path.home() / "Library" / "Messages" / "chat.db")
_LOCAL_POLL_INTERVAL = 2.0
_REMOTE_POLL_INTERVAL = 2.0

_AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".wav", ".aac", ".ogg", ".caf", ".opus"})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class IMessageConfig(Base):
    """iMessage channel configuration."""

    enabled: bool = False
    local: bool = True
    server_url: str = ""
    api_key: str = ""
    proxy: str | None = None
    poll_interval: float = _REMOTE_POLL_INTERVAL
    database_path: str = _DEFAULT_DB_PATH
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention"] = "open"
    reply_to_message: bool = False


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class IMessageChannel(BaseChannel):
    """iMessage channel with local (macOS) and remote (Photon) modes.

    Local mode reads from the native iMessage SQLite database and sends
    via AppleScript — pure Python, no external dependencies.

    Remote mode uses pure HTTP to talk to a Photon iMessage server,
    polling for new messages via the REST API.  All traffic goes through
    ``httpx`` so the optional ``proxy`` config is respected everywhere.
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
        self._last_rowid: int = 0
        self._last_message_date: int = 0

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.config.local:
            await self._start_local()
        else:
            await self._start_remote()

    async def stop(self) -> None:
        self._running = False
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
                c.style AS chat_style,
                a.ROWID AS att_rowid,
                a.filename AS att_filename,
                a.mime_type AS att_mime,
                a.transfer_name AS att_transfer_name
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN message_attachment_join maj ON m.ROWID = maj.message_id
            LEFT JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
            """,
            (self._last_rowid,),
        )
        msg_map: dict[int, dict[str, Any]] = {}
        for row in cur:
            d = dict(row)
            rowid = d["ROWID"]
            if rowid not in msg_map:
                msg_map[rowid] = {**d, "attachments": []}
            if d.get("att_rowid"):
                raw_path = d.get("att_filename") or ""
                resolved = raw_path.replace("~", str(Path.home()), 1) if raw_path.startswith("~") else raw_path
                msg_map[rowid]["attachments"].append({
                    "filename": resolved,
                    "mime_type": d.get("att_mime") or "",
                    "transfer_name": d.get("att_transfer_name") or "",
                })
            self._last_rowid = max(self._last_rowid, rowid)
        conn.close()
        return list(msg_map.values())

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

        media_paths: list[str] = []
        for att in row.get("attachments") or []:
            file_path = att.get("filename", "")
            if not file_path or not Path(file_path).exists():
                continue

            mime = att.get("mime_type") or ""
            ext = Path(file_path).suffix.lower()

            if ext in _AUDIO_EXTENSIONS or mime.startswith("audio/"):
                transcription = await self.transcribe_audio(file_path)
                if transcription:
                    voice_tag = f"[Voice Message: {transcription}]"
                    content = f"{content}\n{voice_tag}" if content else voice_tag
                    continue

            media_paths.append(file_path)
            tag = "image" if mime.startswith("image/") else "file"
            media_tag = f"[{tag}: {file_path}]"
            content = f"{content}\n{media_tag}" if content else media_tag

        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=content,
            media=media_paths,
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
    # REMOTE MODE  (Photon server — pure HTTP polling)
    # ======================================================================

    def _build_http_client(self) -> httpx.AsyncClient:
        proxy = self.config.proxy or None
        return httpx.AsyncClient(
            base_url=self.config.server_url.rstrip("/"),
            headers={
                "X-API-Key": self.config.api_key,
                "Content-Type": "application/json",
            },
            proxy=proxy,
            timeout=30.0,
        )

    async def _start_remote(self) -> None:
        if not self.config.server_url:
            logger.error("iMessage remote mode requires serverUrl")
            return
        if not self.config.api_key:
            logger.error("iMessage remote mode requires apiKey")
            return

        self._running = True
        self._http = self._build_http_client()

        self._last_message_date = await self._get_server_latest_timestamp()
        poll_interval = max(0.5, self.config.poll_interval)
        logger.info(
            "iMessage remote polling started ({}s interval, proxy={})",
            poll_interval,
            self.config.proxy or "none",
        )

        while self._running:
            try:
                await self._poll_remote()
            except Exception as e:
                logger.warning("iMessage remote poll error: {}", e)
            await asyncio.sleep(poll_interval)

    async def _get_server_latest_timestamp(self) -> int:
        """Fetch the most recent message timestamp so we only poll forward."""
        try:
            resp = await self._http.get(  # type: ignore[union-attr]
                "/api/v1/message",
                params={"limit": 1, "sort": "DESC"},
            )
            if resp.is_success:
                data = resp.json()
                messages = data.get("data") or data if isinstance(data, list) else []
                if isinstance(data, dict):
                    messages = data.get("data") or data.get("messages") or []
                if messages and isinstance(messages, list):
                    return messages[0].get("dateCreated", 0)
        except Exception as e:
            logger.debug("Could not seed latest timestamp: {}", e)
        return 0

    async def _poll_remote(self) -> None:
        if not self._http:
            return

        params: dict[str, Any] = {"sort": "DESC", "limit": 50}
        if self._last_message_date:
            params["after"] = self._last_message_date

        try:
            resp = await self._http.get("/api/v1/message", params=params)
        except httpx.HTTPError as e:
            logger.warning("iMessage poll request failed: {}", e)
            return

        if not resp.is_success:
            logger.warning("iMessage poll HTTP {}: {}", resp.status_code, resp.text[:200])
            return

        data = resp.json()
        messages: list[dict[str, Any]] = []
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            messages = data.get("data") or data.get("messages") or []

        for msg in reversed(messages):
            ts = msg.get("dateCreated", 0)
            if isinstance(ts, int) and ts > self._last_message_date:
                self._last_message_date = ts
            await self._handle_remote_message(msg)

    async def _handle_remote_message(self, data: dict[str, Any]) -> None:
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
            att_guid = att.get("guid", "")
            name = att.get("transferName") or att.get("filename") or ""
            if not att_guid or not self._http:
                continue

            local_path = await self._download_attachment(att_guid, name)
            if not local_path:
                continue

            mime, _ = mimetypes.guess_type(local_path)
            ext = Path(local_path).suffix.lower()

            if ext in _AUDIO_EXTENSIONS or (mime and mime.startswith("audio/")):
                transcription = await self.transcribe_audio(local_path)
                if transcription:
                    voice_tag = f"[Voice Message: {transcription}]"
                    content = f"{content}\n{voice_tag}" if content else voice_tag
                    continue

            media_paths.append(local_path)
            tag = "image" if mime and mime.startswith("image/") else "file"
            media_tag = f"[{tag}: {local_path}]"
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

    async def _download_attachment(self, att_guid: str, filename: str) -> str | None:
        """Download an attachment from the Photon server to local media dir."""
        if not self._http:
            return None
        try:
            resp = await self._http.get(f"/api/v1/attachment/{att_guid}/download")
            if not resp.is_success:
                return None
            media_dir = get_media_dir("imessage")
            safe_name = filename or f"{att_guid}.bin"
            dest = media_dir / safe_name
            dest.write_bytes(resp.content)
            return str(dest)
        except Exception as e:
            logger.warning("Failed to download iMessage attachment {}: {}", att_guid, e)
            return None

    async def _send_remote(self, msg: OutboundMessage) -> None:
        if not self._http:
            logger.warning("iMessage remote HTTP client not initialised")
            return

        chat_guid = msg.chat_id

        if msg.content:
            body: dict[str, Any] = {"chatGuid": chat_guid, "message": msg.content}
            if self.config.reply_to_message and msg.reply_to:
                body["selectedMessageGuid"] = msg.reply_to
            try:
                await self._http.post("/api/v1/message/text", json=body)
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
