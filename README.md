<p align="center">
  <img src="assets/icon.png" width="88" alt="HydraDB" />
</p>

<h1 align="center">HydraDB for AgentMail</h1>

<p align="center">
  Sync <a href="https://agentmail.to">AgentMail</a> inboxes into a
  <a href="https://hydradb.com">HydraDB</a> knowledge graph.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-FF5719" alt="MIT license" />
  <img src="https://img.shields.io/badge/python-3.9%2B-FF5719" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/HydraDB-app%20source-111111" alt="HydraDB" />
</p>

---

AgentMail gives AI agents their own inboxes. This connector ingests that email
into HydraDB as an app source, so an agent can recall past conversations before
it replies.

Each message is stored as a HydraDB `app_knowledge` item of type `email` and
linked to its thread node, so recall returns the whole conversation. AgentMail
provides a native `thread_id`, so threads need no reconstruction.

## Quick start

```bash
pip install -e sync/

export AGENTMAIL_API_KEY=...       # AgentMail API key
export HYDRADB_API_KEY=...         # HydraDB API key
export HYDRADB_DATABASE=...        # target database (tenant)

python sync/agentmail.py             # every inbox
python sync/agentmail.py <inbox_id>  # one inbox
```

## Behavior

- With no argument, lists all inboxes and syncs each. Pass an inbox id to scope
  to one.
- Uses AgentMail's reply-stripped `extracted_text`, falling back to `text` then
  `preview`. Caps body length at 8000 characters.
- Links each message to an `agentmail-thread-<thread_id>` node and to its parent
  via `in_reply_to`, so threads and reply chains come back connected.
- Set `AGENTMAIL_AFTER` (ISO 8601) to pull only messages after a cutoff.
- Query with `query_apps: true` (the default) to search this email alongside
  other app sources.

## Configuration

| Variable | Required | Description |
| - | - | - |
| `AGENTMAIL_API_KEY` | yes | AgentMail API key (bearer token). |
| `HYDRADB_API_KEY` | yes | HydraDB API key. |
| `HYDRADB_DATABASE` | yes | Target HydraDB database (tenant). |
| `HYDRADB_COLLECTION` | no | Sub-tenant scope within the database. |
| `AGENTMAIL_INBOX_ID` | no | Default inbox when no argument is given. |
| `AGENTMAIL_AFTER` | no | ISO 8601 cutoff for an incremental pull. |

## Field mapping

AgentMail's `message_id` is the RFC Message-ID, so `in_reply_to` already equals
the parent's node id once angle brackets are stripped. The parent upserts to the
same id on its own sync, so reply edges connect across incremental runs.

| AgentMail field | HydraDB `app_knowledge` |
| - | - |
| `message_id` (RFC Message-ID) | `id` (upsert key, brackets stripped) |
| (constant) | `type: "email"` |
| `subject` | `title` and first line of `content.text` |
| `extracted_text` / `text` / `preview` | `content.text` |
| `timestamp` | `timestamp` |
| `inbox_id`, `thread_id` | `tenant_metadata` |
| `from`, `to`, `cc`, `reply_to`, `in_reply_to`, `labels`, attachments | `additional_metadata` |
| `thread_id` | `relations.ids`: `agentmail-thread-<thread_id>` |
| `in_reply_to` | `relations.ids`: parent message node |

## Live ingest

Run on a schedule for a backfill, or call it from an AgentMail
`message.received` / `message.sent` webhook to ingest each email on arrival.
`message_to_item` and `ingest_items` are importable for the webhook path.

## License

MIT. See [LICENSE](LICENSE).
