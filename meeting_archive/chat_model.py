"""Lossless message text and conservative structured relationships; no source HTML executes."""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def positive(value):
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        return 0


def stamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sequence(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def safe_metadata(value):
    """Keep structural API data but exclude credentials and ephemeral file URLs."""
    if isinstance(value, dict):
        return {key: safe_metadata(item) for key, item in value.items()
                if str(key).replace("_", "").lower() not in {
                    "auth", "accesstoken", "refreshtoken", "clientsecret", "token", "downloadurl",
                    "url", "urlpreview", "urlshow", "previewurl", "avatar", "avatarhr"}}
    if isinstance(value, list):
        return [safe_metadata(item) for item in value]
    return value


def category(file):
    extension = Path(str(file.get("name") or file.get("NAME") or "")).suffix.lower()
    kind = str(file.get("type") or file.get("mimeType") or file.get("contentType") or "").lower()
    if file.get("isVoiceNote") or file.get("isVoice") or file.get("isAudio") or kind.startswith("audio") or extension in {
            ".mp3", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wma"}:
        return "audio"
    if file.get("isVideoNote") or file.get("isVideo") or kind.startswith("video") or extension in {
            ".mp4", ".webm", ".mkv", ".mov", ".avi", ".wmv"}:
        return "video"
    if kind in {"image", "picture"} or kind.startswith("image/") or extension in {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic", ".tif", ".tiff"}:
        return "images"
    if extension in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md",
                      ".rtf", ".csv", ".odt", ".ods", ".odp", ".json", ".xml"}:
        return "documents"
    return "other"


def file_record(raw):
    return {"id": positive(raw.get("id") or raw.get("fileId") or raw.get("ID")),
            "name": str(raw.get("name") or raw.get("NAME") or "Файл"),
            "size": positive(raw.get("size") or raw.get("SIZE")), "category": category(raw),
            "source_size": positive(raw.get("size") or raw.get("SIZE")),
            "source": safe_metadata(raw)}


def relation(raw, kind, chat_id):
    if isinstance(raw, (str, int)):
        raw = {"id": positive(raw)}
    if not isinstance(raw, dict):
        return None
    id = positive(raw.get("id") or raw.get("messageId") or raw.get("message_id"))
    if not id:
        return None
    return {"kind": kind, "message_id": id,
            "chat_id": positive(raw.get("chatId") or raw.get("chat_id")) or chat_id,
            "author_id": positive(raw.get("userId") or raw.get("authorId") or raw.get("author_id")),
            "date": raw.get("date") or "", "excerpt": str(raw.get("text") or raw.get("excerpt") or ""),
            "evidence": "structured_api"}


def normalize(raw, chat_id, users=None, reactions=None):
    id = positive(raw.get("id") or raw.get("ID"))
    actual = positive(raw.get("chatId") or raw.get("chat_id")) or chat_id
    if not id or actual != chat_id:
        raise ValueError("Сообщение без ID или относится к другому чату")
    params = raw.get("params") or {}
    if isinstance(params, list):
        params = {str(p.get("name") or p.get("key")): p.get("value") for p in params if isinstance(p, dict)}
    if not isinstance(params, dict):
        params = {}
    author = positive(raw.get("authorId") or raw.get("author_id"))
    links = []
    forwards = raw.get("forward") or raw.get("forwards") or []
    if isinstance(forwards, dict):
        forwards = [forwards] if any(k in forwards for k in ("id", "messageId")) else sequence(forwards)
    for item in forwards if isinstance(forwards, list) else []:
        link = relation(item, "forward", chat_id)
        if link:
            links.append(link)
    # Only explicit IDs constitute a reply. Never match quote text to an author.
    reply = raw.get("reply") or raw.get("replyId") or raw.get("reply_id") or params.get("REPLY_ID")
    link = relation(reply, "reply", chat_id)
    if link:
        links.append(link)
    quote = relation(raw.get("quote"), "quote", chat_id)
    if quote:
        links.append(quote)
    for link in links:
        link["author"] = (users or {}).get(link["author_id"], "")
    ids = params.get("FILE_ID") or params.get("fileId") or raw.get("fileIds") or []
    if not isinstance(ids, list):
        ids = [ids]
    else:
        ids = list(ids)
    ids += [f.get("id") or f.get("fileId") if isinstance(f, dict) else f for f in sequence(raw.get("files"))]
    date = raw.get("date") or raw.get("dateCreate") or ""
    if isinstance(date, (int, float)):
        date = datetime.fromtimestamp(date, timezone.utc).isoformat()
    if not isinstance(date, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", date):
        date = ""  # Do not fabricate a creation time from observation time.
    text = str(raw.get("text") or "")
    return {"id": id, "chat_id": chat_id, "author_id": author,
            "author": (users or {}).get(author, ""), "date": date, "text": text,
            "system": bool(raw.get("isSystem") or author == 0),
            "deleted": bool(raw.get("deleted") or raw.get("isDeleted")), "relations": links,
            "quote": bool(quote or re.search(r"\[quote(?:=[^\]]*)?\]|^\s*>>|^-{6}", text, re.I | re.M)),
            "file_ids": list(dict.fromkeys(positive(i) for i in ids if positive(i))),
            "reactions": safe_metadata(reactions if reactions is not None else raw.get("reactions") or []),
            "source": safe_metadata({k: raw[k] for k in ("uuid", "params", "replaces", "disappearing_date",
                                                          "attach", "sticker") if k in raw})}


def message_hash(message):
    # A name lookup, read status and observation timestamp are not text revisions.
    value = {k: v for k, v in message.items() if k not in {"author", "observed_at", "hash", "versions"}}
    value["relations"] = [{k: v for k, v in link.items() if k != "author"} for link in message.get("relations", [])]
    return fingerprint(value)


def text_html(text):
    result = html.escape(str(text))
    for tag, element in (("b", "strong"), ("i", "em"), ("u", "u"), ("s", "s"), ("code", "code")):
        result = re.sub(rf"\[{tag}\](.*?)\[/{tag}\]", rf"<{element}>\1</{element}>", result, flags=re.I | re.S)
    result = re.sub(r"\[quote(?:=[^\]]*)?\](.*?)\[/quote\]", r"<blockquote>\1</blockquote>", result, flags=re.I | re.S)
    result = re.sub(r"(?m)^&gt;&gt;\s?(.*)$", r"<blockquote>\1</blockquote>", result)
    def link(match):
        address = html.unescape(match[1])
        try:
            parsed = urlsplit(address)
        except ValueError:
            return match[0]
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return match[0]
        return f'<a href="{html.escape(address, quote=True)}" target="_blank" rel="noopener noreferrer">{match[2]}</a>'
    result = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", link, result, flags=re.I | re.S)
    return result.replace("\n", "<br>")


def text_markdown(text):
    # Escape raw HTML; preserve unsupported BBCode, so no data silently vanishes.
    text = str(text).replace("<", "&lt;").replace(">", "&gt;")
    for tag, token in (("b", "**"), ("i", "*"), ("s", "~~"), ("code", "`")):
        text = re.sub(rf"\[{tag}\](.*?)\[/{tag}\]", lambda m: token + m[1] + token, text, flags=re.I | re.S)
    text = re.sub(r"\[quote(?:=[^\]]*)?\](.*?)\[/quote\]", lambda m: "\n" + "\n".join("> " + s for s in m[1].splitlines()) + "\n", text, flags=re.I | re.S)
    text = re.sub(r"(?m)^&gt;&gt;\s?", "> ", text)
    return text


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(". ")[:130] or "file"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                       *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    return name
