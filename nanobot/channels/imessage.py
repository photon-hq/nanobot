"""iMessage channel implementation.

Supports two modes via Photon's iMessage platform:

  **Local** — macOS only.  Reads the on-device iMessage database
  (``~/Library/Messages/chat.db``) via ``sqlite3`` and sends through
  AppleScript.  No external server required.

  **Remote** — Any platform.  Connects to a Photon
  `advanced-imessage-http-proxy <https://github.com/photon-hq/advanced-imessage-http-proxy>`_
  over pure HTTP.  Polls ``GET /messages`` for inbound, sends via
  ``POST /send``, and supports the full proxy feature set:

    - ``POST /send`` — text with optional effects and inline replies
    - ``POST /send/file`` — images, files, audio messages
    - ``POST /send/sticker`` — standalone or reply stickers
    - ``DELETE /messages/:id`` — unsend / retract a message
    - ``POST /messages/:id/react`` — add tapback
    - ``DELETE /messages/:id/react`` — remove tapback
    - ``GET /messages`` — poll for new messages
    - ``GET /messages/search`` — search by text
    - ``GET /messages/:id`` — get single message
    - ``GET /chats`` — list conversations
    - ``GET /chats/:id`` — chat details
    - ``GET /chats/:id/messages`` — chat history
    - ``GET /chats/:id/participants`` — group participants
    - ``POST /chats/:id/read`` — mark as read
    - ``POST /chats/:id/typing`` — start typing indicator
    - ``DELETE /chats/:id/typing`` — stop typing indicator
    - ``POST /groups`` — create group chat
    - ``PATCH /groups/:id`` — rename group
    - ``POST /polls`` — create poll
    - ``GET /polls/:id`` — get poll
    - ``POST /polls/:id/vote`` — vote on poll
    - ``POST /polls/:id/unvote`` — remove vote
    - ``POST /polls/:id/options`` — add poll option
    - ``GET /attachments/:id`` — download attachment
    - ``GET /attachments/:id/info`` — attachment metadata
    - ``GET /check/:address`` — check iMessage availability
    - ``GET /contacts`` — list contacts
    - ``GET /handles`` — list handles
    - ``GET /server`` — server info
    - ``GET /health`` — health check

  All traffic goes through ``httpx`` so the optional ``proxy`` config
  is respected everywhere.
"""

from __future__ import annotations

import asyncio
import base64
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
    react_tapback: str = "love"
    done_tapback: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bearer_token(server_url: str, api_key: str) -> str:
    """Build the Bearer token expected by advanced-imessage-http-proxy.

    If the key already decodes to a ``url|key`` pair it is used as-is.
    """
    try:
        decoded = base64.b64decode(api_key, validate=True).decode()
        if "|" in decoded:
            return api_key
    except Exception:
        pass
    raw = f"{server_url}|{api_key}"
    return base64.b64encode(raw.encode()).decode()


def _extract_address(chat_id: str) -> str:
    """Convert a chatGuid to the proxy's address format.

    ``iMessage;-;+1234567890`` → ``+1234567890``
    ``iMessage;+;chat123``     → ``group:chat123``
    ``+1234567890``            → ``+1234567890`` (passthrough)
    """
    if ";-;" in chat_id:
        return chat_id.split(";-;", 1)[1]
    if ";+;" in chat_id:
        return "group:" + chat_id.split(";+;", 1)[1]
    return chat_id


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class IMessageChannel(BaseChannel):
    """iMessage channel with local (macOS) and remote (Photon) modes.

    Local mode reads from the native iMessage SQLite database and sends
    via AppleScript — pure Python, no external dependencies.

    Remote mode talks to a Photon ``advanced-imessage-http-proxy`` server.
    See https://github.com/photon-hq/advanced-imessage-http-proxy for the
    full API reference.
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
    # REMOTE MODE  (advanced-imessage-http-proxy)
    # https://github.com/photon-hq/advanced-imessage-http-proxy
    # ======================================================================

    def _build_http_client(self) -> httpx.AsyncClient:
        token = _make_bearer_token(self.config.server_url, self.config.api_key)
        return httpx.AsyncClient(
            base_url=self.config.server_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            proxy=self.config.proxy or None,
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

        if not await self._api_health():
            logger.error("iMessage server health check failed — will retry in poll loop")

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
        try:
            resp = await self._api_get_messages(limit=1)
            if resp and isinstance(resp, list) and resp:
                return resp[0].get("dateCreated", 0)
        except Exception as e:
            logger.debug("Could not seed latest timestamp: {}", e)
        return 0

    # ---- inbound -----------------------------------------------------------

    async def _poll_remote(self) -> None:
        if not self._http:
            return

        messages = await self._api_get_messages(
            limit=50,
            after=self._last_message_date if self._last_message_date else None,
        )
        if not messages:
            return

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

        address = _extract_address(chat_guid) or sender
        content = data.get("text") or ""
        is_group = ";+;" in chat_guid or address.startswith("group:")

        if is_group and self.config.group_policy == "mention":
            return

        await self._api_react(address, message_id, self.config.react_tapback)
        await self._api_mark_read(address)

        media_paths: list[str] = []
        for att in data.get("attachments") or []:
            att_guid = att.get("guid", "")
            name = att.get("transferName") or att.get("filename") or ""
            if not att_guid or not self._http:
                continue

            local_path = await self._api_download_attachment(att_guid, name)
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
            chat_id=address,
            content=content,
            media=media_paths,
            metadata={
                "message_id": message_id,
                "chat_guid": chat_guid,
                "is_group": is_group,
                "source": "remote",
                "timestamp": data.get("dateCreated"),
            },
        )

    # ---- outbound ----------------------------------------------------------

    async def _send_remote(self, msg: OutboundMessage) -> None:
        if not self._http:
            logger.warning("iMessage remote HTTP client not initialised")
            return

        to = msg.chat_id
        is_progress = (msg.metadata or {}).get("_progress", False)

        if not is_progress:
            await self._api_start_typing(to)

        if msg.content:
            body: dict[str, Any] = {"to": to, "text": msg.content}
            if self.config.reply_to_message and msg.reply_to:
                body["replyTo"] = msg.reply_to
            try:
                await self._api_send(body)
            except Exception as e:
                logger.error("iMessage remote send failed: {}", e)
                raise
            finally:
                if not is_progress:
                    await self._api_stop_typing(to)

        for media_path in msg.media or []:
            try:
                await self._api_send_file(to, media_path)
            except Exception as e:
                logger.error("iMessage remote media send failed: {}", e)
                raise

        if not is_progress:
            message_id = (msg.metadata or {}).get("message_id")
            if message_id and self.config.react_tapback:
                await self._api_remove_react(to, message_id, self.config.react_tapback)
                if self.config.done_tapback:
                    await self._api_react(to, message_id, self.config.done_tapback)

    # ======================================================================
    # PROXY API METHODS
    # https://github.com/photon-hq/advanced-imessage-http-proxy
    # ======================================================================

    # ---- messaging ---------------------------------------------------------

    async def _api_send(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """``POST /send`` — send text message with optional effect / reply."""
        return await self._post("/send", body)

    async def _api_send_file(self, to: str, file_path: str, audio: bool | None = None) -> dict[str, Any] | None:
        """``POST /send/file`` — send attachment (image, file, audio message)."""
        if not self._http:
            return None
        mime, _ = mimetypes.guess_type(file_path)
        ext = Path(file_path).suffix.lower()
        is_audio = audio if audio is not None else (ext in _AUDIO_EXTENSIONS or (mime or "").startswith("audio/"))
        data: dict[str, str] = {"to": to}
        if is_audio:
            data["audio"] = "true"
        with open(file_path, "rb") as f:
            resp = await self._http.post(
                "/send/file",
                data=data,
                files={"file": (Path(file_path).name, f, mime or "application/octet-stream")},
            )
        return self._unwrap(resp)

    async def _api_send_sticker(
        self, to: str, file_path: str, reply_to: str | None = None, **kwargs: Any,
    ) -> dict[str, Any] | None:
        """``POST /send/sticker`` — send standalone or reply sticker."""
        if not self._http:
            return None
        data: dict[str, str] = {"to": to}
        if reply_to:
            data["replyTo"] = reply_to
        for k in ("stickerX", "stickerY", "stickerScale", "stickerRotation", "stickerWidth"):
            if k in kwargs:
                data[k] = str(kwargs[k])
        with open(file_path, "rb") as f:
            resp = await self._http.post(
                "/send/sticker",
                data=data,
                files={"file": (Path(file_path).name, f, "image/png")},
            )
        return self._unwrap(resp)

    async def _api_unsend(self, message_id: str) -> dict[str, Any] | None:
        """``DELETE /messages/:id`` — retract a sent message."""
        return await self._delete(f"/messages/{message_id}")

    # ---- reactions ---------------------------------------------------------

    async def _api_react(self, chat: str, message_id: str, tapback: str) -> None:
        """``POST /messages/:id/react`` — add tapback (best-effort)."""
        if not self._http or not tapback:
            return
        try:
            await self._http.post(
                f"/messages/{message_id}/react",
                json={"chat": chat, "type": tapback},
            )
        except Exception as e:
            logger.debug("iMessage tapback failed: {}", e)

    async def _api_remove_react(self, chat: str, message_id: str, tapback: str) -> None:
        """``DELETE /messages/:id/react`` — remove tapback (best-effort)."""
        if not self._http or not tapback:
            return
        try:
            await self._http.request(
                "DELETE", f"/messages/{message_id}/react",
                json={"chat": chat, "type": tapback},
            )
        except Exception as e:
            logger.debug("iMessage remove tapback failed: {}", e)

    # ---- messages ----------------------------------------------------------

    async def _api_get_messages(
        self, limit: int = 50, chat: str | None = None, after: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /messages`` — query messages."""
        params: dict[str, Any] = {"limit": limit}
        if chat:
            params["chat"] = chat
        if after:
            params["after"] = after
        data = await self._get("/messages", params=params)
        return data if isinstance(data, list) else []

    async def _api_search_messages(self, query: str, chat: str | None = None) -> list[dict[str, Any]]:
        """``GET /messages/search`` — search messages by text."""
        params: dict[str, Any] = {"q": query}
        if chat:
            params["chat"] = chat
        data = await self._get("/messages/search", params=params)
        return data if isinstance(data, list) else []

    async def _api_get_message(self, message_id: str) -> dict[str, Any] | None:
        """``GET /messages/:id`` — get single message details."""
        data = await self._get(f"/messages/{message_id}")
        return data if isinstance(data, dict) else None

    # ---- chats -------------------------------------------------------------

    async def _api_get_chats(self) -> list[dict[str, Any]]:
        """``GET /chats`` — list all conversations."""
        data = await self._get("/chats")
        return data if isinstance(data, list) else []

    async def _api_get_chat(self, address: str) -> dict[str, Any] | None:
        """``GET /chats/:id`` — get chat details."""
        data = await self._get(f"/chats/{address}")
        return data if isinstance(data, dict) else None

    async def _api_get_chat_messages(
        self, address: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """``GET /chats/:id/messages`` — get message history for a chat."""
        data = await self._get(f"/chats/{address}/messages", params={"limit": limit})
        return data if isinstance(data, list) else []

    async def _api_get_chat_participants(self, address: str) -> list[dict[str, Any]]:
        """``GET /chats/:id/participants`` — get group participants."""
        data = await self._get(f"/chats/{address}/participants")
        return data if isinstance(data, list) else []

    async def _api_mark_read(self, address: str) -> None:
        """``POST /chats/:id/read`` — clear unread badge."""
        if not self._http:
            return
        try:
            await self._http.post(f"/chats/{address}/read")
        except Exception as e:
            logger.debug("iMessage mark-read failed: {}", e)

    async def _api_start_typing(self, address: str) -> None:
        """``POST /chats/:id/typing`` — show typing indicator."""
        if not self._http:
            return
        try:
            await self._http.post(f"/chats/{address}/typing")
        except Exception as e:
            logger.debug("iMessage typing start failed: {}", e)

    async def _api_stop_typing(self, address: str) -> None:
        """``DELETE /chats/:id/typing`` — stop typing indicator."""
        if not self._http:
            return
        try:
            await self._http.request("DELETE", f"/chats/{address}/typing")
        except Exception as e:
            logger.debug("iMessage typing stop failed: {}", e)

    # ---- groups ------------------------------------------------------------

    async def _api_create_group(self, members: list[str], name: str | None = None) -> dict[str, Any] | None:
        """``POST /groups`` — create a group chat."""
        body: dict[str, Any] = {"members": members}
        if name:
            body["name"] = name
        return await self._post("/groups", body)

    async def _api_update_group(self, group_id: str, name: str) -> dict[str, Any] | None:
        """``PATCH /groups/:id`` — rename a group."""
        if not self._http:
            return None
        resp = await self._http.patch(f"/groups/{group_id}", json={"name": name})
        return self._unwrap(resp)

    # ---- polls -------------------------------------------------------------

    async def _api_create_poll(
        self, to: str, question: str, options: list[str],
    ) -> dict[str, Any] | None:
        """``POST /polls`` — create an interactive poll."""
        return await self._post("/polls", {"to": to, "question": question, "options": options})

    async def _api_get_poll(self, poll_id: str) -> dict[str, Any] | None:
        """``GET /polls/:id`` — get poll details."""
        data = await self._get(f"/polls/{poll_id}")
        return data if isinstance(data, dict) else None

    async def _api_vote_poll(self, poll_id: str, chat: str, option_id: str) -> dict[str, Any] | None:
        """``POST /polls/:id/vote`` — vote on a poll option."""
        return await self._post(f"/polls/{poll_id}/vote", {"chat": chat, "optionId": option_id})

    async def _api_unvote_poll(self, poll_id: str, chat: str, option_id: str) -> dict[str, Any] | None:
        """``POST /polls/:id/unvote`` — remove vote from poll."""
        return await self._post(f"/polls/{poll_id}/unvote", {"chat": chat, "optionId": option_id})

    async def _api_add_poll_option(self, poll_id: str, chat: str, text: str) -> dict[str, Any] | None:
        """``POST /polls/:id/options`` — add option to existing poll."""
        return await self._post(f"/polls/{poll_id}/options", {"chat": chat, "text": text})

    # ---- attachments -------------------------------------------------------

    async def _api_download_attachment(self, att_guid: str, filename: str) -> str | None:
        """``GET /attachments/:id`` — download to local media dir."""
        if not self._http:
            return None
        try:
            resp = await self._http.get(f"/attachments/{att_guid}")
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

    async def _api_attachment_info(self, att_guid: str) -> dict[str, Any] | None:
        """``GET /attachments/:id/info`` — get attachment metadata."""
        data = await self._get(f"/attachments/{att_guid}/info")
        return data if isinstance(data, dict) else None

    # ---- contacts & handles ------------------------------------------------

    async def _api_check_imessage(self, address: str) -> bool:
        """``GET /check/:address`` — check if address uses iMessage."""
        data = await self._get(f"/check/{address}")
        if isinstance(data, dict):
            return bool(data.get("available") or data.get("imessage"))
        return False

    async def _api_get_contacts(self) -> list[dict[str, Any]]:
        """``GET /contacts`` — list device contacts."""
        data = await self._get("/contacts")
        return data if isinstance(data, list) else []

    async def _api_get_handles(self) -> list[dict[str, Any]]:
        """``GET /handles`` — list known handles."""
        data = await self._get("/handles")
        return data if isinstance(data, list) else []

    # ---- server ------------------------------------------------------------

    async def _api_server_info(self) -> dict[str, Any] | None:
        """``GET /server`` — get server info."""
        data = await self._get("/server")
        return data if isinstance(data, dict) else None

    async def _api_health(self) -> bool:
        """``GET /health`` — basic health check."""
        if not self._http:
            return False
        try:
            resp = await self._http.get("/health")
            if resp.is_success:
                logger.info("iMessage server health check passed")
                return True
            logger.warning("iMessage health check HTTP {}", resp.status_code)
        except Exception as e:
            logger.warning("iMessage health check failed: {}", e)
        return False

    # ---- HTTP helpers ------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._http:
            return None
        try:
            resp = await self._http.get(path, params=params)
            return self._unwrap(resp)
        except Exception as e:
            logger.warning("iMessage GET {} failed: {}", path, e)
            return None

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self._http:
            return None
        resp = await self._http.post(path, json=body)
        return self._unwrap(resp)

    async def _delete(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self._http:
            return None
        resp = await self._http.request("DELETE", path, json=body)
        return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> Any:
        """Unwrap the proxy's ``{"ok": true, "data": ...}`` envelope."""
        if not resp.is_success:
            return None
        body = resp.json()
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

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
