from __future__ import annotations

import os
import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests

AGENTMAIL_BASE = "https://api.agentmail.to/v0"
HYDRADB_BASE = "https://api.hydradb.com"
MAX_ITEM_CHARS = 8000


@dataclass
class AgentMailConfig:
    api_key: str
    base_url: str = AGENTMAIL_BASE


@dataclass
class HydraDBConfig:
    api_key: str
    database: str
    collection: str = ""
    base_url: str = HYDRADB_BASE


def _agentmail_headers(cfg: AgentMailConfig) -> dict[str, str]:
    """
    Build the AgentMail request headers.

    arg : cfg (AgentMailConfig) - AgentMail credentials.
    return : headers (dict) - Authorization bearer header.
    """
    return {"Authorization": f"Bearer {cfg.api_key}"}


def _get_with_retry(
    http: requests.Session,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    retries: int = 4,
) -> requests.Response:
    """
    GET a URL, backing off on rate limits and transient server errors.

    arg : http (requests.Session) - Session used for the request.
    arg : url (string) - Absolute URL to fetch.
    arg : headers (dict) - Request headers.
    arg : params (dict or None) - Query parameters.
    arg : retries (int) - Maximum number of backoff retries.
    return : response (requests.Response) - The successful response.
    """
    for attempt in range(retries + 1):
        resp = http.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries:
                resp.raise_for_status()
            wait = float(resp.headers.get("Retry-After") or (2**attempt))
            time.sleep(min(wait, 30))
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")


def list_inboxes(
    cfg: AgentMailConfig,
    session: requests.Session | None = None,
) -> list[str]:
    """
    List every inbox id on the account.

    arg : cfg (AgentMailConfig) - AgentMail credentials.
    arg : session (requests.Session or None) - Optional reused session.
    return : inbox_ids (list of string) - Inbox identifiers.
    """
    http = session or requests.Session()
    url = f"{cfg.base_url}/inboxes"
    params: dict[str, Any] = {"limit": 100}
    inbox_ids: list[str] = []
    while True:
        resp = _get_with_retry(http, url, _agentmail_headers(cfg), params)
        body = resp.json()
        for ibx in body.get("inboxes", []):
            if ibx:
                inbox_ids.append(ibx.get("inbox_id") or ibx.get("id"))
        token = body.get("next_page_token")
        if not token:
            break
        params = {"limit": 100, "page_token": token}
    return inbox_ids


def fetch_messages(
    cfg: AgentMailConfig,
    inbox_id: str,
    after: str | None = None,
    session: requests.Session | None = None,
) -> Iterable[dict[str, Any]]:
    """
    Yield every message in an inbox, following page-token pagination.

    arg : cfg (AgentMailConfig) - AgentMail credentials.
    arg : inbox_id (string) - Inbox to read.
    arg : after (string or None) - ISO 8601 cutoff for an incremental pull.
    arg : session (requests.Session or None) - Optional reused session.
    return : messages (iterable of dict) - One message at a time.
    """
    http = session or requests.Session()
    url = f"{cfg.base_url}/inboxes/{inbox_id}/messages"
    params: dict[str, Any] = {"limit": 100}
    if after:
        params["after"] = after
    while True:
        resp = _get_with_retry(http, url, _agentmail_headers(cfg), params)
        body = resp.json()
        for msg in body.get("messages", []):
            yield msg
        token = body.get("next_page_token")
        if not token:
            break
        params = {"limit": 100, "page_token": token}
        if after:
            params["after"] = after


def get_message(
    cfg: AgentMailConfig,
    inbox_id: str,
    message_id: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Fetch one full message, including its extracted body.

    arg : cfg (AgentMailConfig) - AgentMail credentials.
    arg : inbox_id (string) - Inbox the message belongs to.
    arg : message_id (string) - Message to fetch.
    arg : session (requests.Session or None) - Optional reused session.
    return : message (dict) - The full message object.
    """
    http = session or requests.Session()
    resp = _get_with_retry(
        http,
        f"{cfg.base_url}/inboxes/{inbox_id}/messages/{message_id}",
        _agentmail_headers(cfg),
        None,
    )
    return resp.json()


def _as_text(value: Any) -> str:
    """
    Flatten an address field (string or list) into a comma-joined string.

    arg : value (any) - A string, a list of strings, or None.
    return : text (string) - The flattened value.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _strip_brackets(value: Any) -> str:
    """
    Normalize an RFC Message-ID by stripping angle brackets and whitespace.

    arg : value (any) - A value such as "<id@host>" or a list of them.
    return : message_id (string) - The bare id, or "" when absent.
    """
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip().strip("<>").strip()


def message_to_item(msg: dict[str, Any]) -> dict[str, Any]:
    """
    Map an AgentMail message to a HydraDB app-knowledge item.

    arg : msg (dict) - An AgentMail message object.
    return : item (dict) - An email app-source item with thread and reply relations.
    """
    external_id = _strip_brackets(msg.get("message_id") or msg.get("id"))
    inbox_id = str(msg.get("inbox_id") or "")
    thread_id = str(msg.get("thread_id") or "")
    subject = (msg.get("subject") or "").strip()
    body = msg.get("extracted_text") or msg.get("text") or msg.get("preview") or subject

    text = f"{subject}\n\n{body}".strip() if subject else str(body)
    names = [a.get("filename") for a in (msg.get("attachments") or []) if a.get("filename")]
    if names:
        text = f"{text}\n\nAttachments: {', '.join(names)}"
    if len(text) > MAX_ITEM_CHARS:
        text = text[: MAX_ITEM_CHARS - 1] + "…"

    relation_ids = [f"agentmail-thread-{thread_id}"] if thread_id else []

    parent_id = _strip_brackets(msg.get("in_reply_to"))
    if parent_id and parent_id != external_id:
        relation_ids.append(parent_id)

    return {
        "id": external_id,
        "type": "email",
        "title": (subject or f"Email {external_id}")[:120],
        "content": {"text": text},
        "timestamp": msg.get("timestamp") or msg.get("created_at"),
        "tenant_metadata": {"inbox_id": inbox_id, "thread_id": thread_id},
        "additional_metadata": {
            "provider": "agentmail",
            "message_id": external_id,
            "from": _as_text(msg.get("from")),
            "to": _as_text(msg.get("to")),
            "cc": _as_text(msg.get("cc")),
            "reply_to": _as_text(msg.get("reply_to")),
            "in_reply_to": _as_text(msg.get("in_reply_to")),
            "labels": msg.get("labels") or [],
            "attachments": [
                f"{a.get('filename')} ({a.get('content_type')}, {a.get('size')}b)"
                for a in (msg.get("attachments") or [])
            ],
        },
        "relations": {"ids": relation_ids},
        "infer": True,
    }


def ingest_items(
    cfg: HydraDBConfig,
    items: list[dict[str, Any]],
    batch_size: int = 50,
    session: requests.Session | None = None,
) -> int:
    """
    Ingest app-knowledge items into HydraDB in batches.

    arg : cfg (HydraDBConfig) - HydraDB credentials and scope.
    arg : items (list of dict) - App-source items to store.
    arg : batch_size (int) - Items per ingest request.
    arg : session (requests.Session or None) - Optional reused session.
    return : sent (int) - Number of items accepted for ingestion.
    """
    http = session or requests.Session()
    sent = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        resp = http.post(
            f"{cfg.base_url}/context/ingest",
            headers={"Authorization": f"Bearer {cfg.api_key}", "API-Version": "2"},
            files={
                "tenant_id": (None, cfg.database),
                "sub_tenant_id": (None, cfg.collection),
                "kind": (None, "knowledge"),
                "app_knowledge": (None, json.dumps(batch)),
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results")
        if isinstance(results, list) and results:
            failed = [r for r in results if r.get("error") or r.get("error_code")]
            sent += len(batch) - len(failed)
            for r in failed:
                print(
                    f"HydraDB ingest skipped {r.get('id')}: {r.get('error') or r.get('error_code')}",
                    file=sys.stderr,
                )
        elif body.get("success", False):
            sent += len(batch)
        else:
            raise RuntimeError(f"HydraDB ingest failed: {body.get('error')}")
    return sent


def sync(
    agentmail: AgentMailConfig,
    hydradb: HydraDBConfig,
    inbox_id: str | None = None,
    after: str | None = None,
) -> int:
    """
    Pull AgentMail messages and ingest them into HydraDB.

    arg : agentmail (AgentMailConfig) - AgentMail credentials.
    arg : hydradb (HydraDBConfig) - HydraDB credentials and scope.
    arg : inbox_id (string or None) - A single inbox, or None for every inbox.
    arg : after (string or None) - ISO 8601 cutoff for an incremental sync.
    return : synced (int) - Number of messages ingested.
    """
    http = requests.Session()
    inboxes = [inbox_id] if inbox_id else list_inboxes(agentmail, session=http)

    items: list[dict[str, Any]] = []
    for ibx in inboxes:
        if not ibx:
            continue
        for msg in fetch_messages(agentmail, ibx, after=after, session=http):
            if not (msg.get("extracted_text") or msg.get("text")):
                mid = msg.get("message_id") or msg.get("id")
                if mid:
                    msg = get_message(agentmail, ibx, mid, session=http)
            items.append(message_to_item(msg))

    return ingest_items(hydradb, items, session=http)


def main() -> None:
    """
    Run a sync from environment variables and command-line arguments.

    return : none (None) - Prints the number of messages synced.
    """
    import sys

    inbox_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENTMAIL_INBOX_ID") or None
    agentmail = AgentMailConfig(api_key=os.environ["AGENTMAIL_API_KEY"])
    hydradb = HydraDBConfig(
        api_key=os.environ["HYDRADB_API_KEY"],
        database=os.environ["HYDRADB_DATABASE"],
        collection=os.environ.get("HYDRADB_COLLECTION", ""),
    )
    count = sync(agentmail, hydradb, inbox_id, os.environ.get("AGENTMAIL_AFTER"))
    scope = inbox_id or "all inboxes"
    print(f"Synced {count} AgentMail messages from {scope} into HydraDB.")


if __name__ == "__main__":
    main()
