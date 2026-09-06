# clickup-api-toolkit

A small, dependency-free Python client for ClickUp's REST API v2, plus a
weekly-digest script built on top of it. Written while automating reporting
workflows for a marketing team — no `pip install` required, everything's
standard library.

## Why this exists

ClickUp's official MCP connector (as exposed in tools like Claude Code) is
read/update on task values but has **no create/update-custom-field or
create-view endpoints**. The real REST API supports field management, so
`client.py` fills that gap and gives any digest/aggregation workflow a
reusable way to pull data instead of one-off calls.

One confirmed platform limitation worth knowing: creating a Form *view* has
no REST API endpoint at all — that step stays manual in the ClickUp UI.

## Files

- **`client.py`** — `ClickUpClient`: read + write (`get_tasks`,
  `get_all_tasks`, `get_task`, `get_task_comments`, `get_filtered_team_tasks`,
  `get_custom_fields`, `find_custom_field`, `find_option_id`,
  `create_custom_field`, `delete_custom_field`, `create_task`, `update_task`,
  `set_custom_field_value`, `create_comment`, ...). Reads the API key from
  `.env` in this folder automatically. Retries on `429` with the API's own
  `Retry-After` header instead of failing a whole run over one rate-limited
  call — needed once you're pulling per-task detail + comments for a
  thousand-plus tasks in one go.
- **`weekly_digest.py`** — pulls a week's worth of Form submissions plus a
  roster's task activity as structured JSON, with **no classification
  applied** — deciding what's actually report-worthy is left as a separate
  step (human or LLM) rather than baked into the script. Demonstrates a
  couple of non-obvious ClickUp gotchas along the way (see below).

## Setup

```
cp .env.example .env
# fill in CLICKUP_API_KEY — a personal API token from ClickUp settings
```

The key is sent raw in the `Authorization` header, with no `Bearer` prefix
(ClickUp's own convention, not OAuth-style).

## Quick usage

```python
from client import ClickUpClient, DEFAULT_WEEKLY_LIST_ID

cu = ClickUpClient()

fields = cu.get_custom_fields(DEFAULT_WEEKLY_LIST_ID)
tasks = cu.get_all_tasks(DEFAULT_WEEKLY_LIST_ID)  # paginates automatically

field = cu.find_custom_field(DEFAULT_WEEKLY_LIST_ID, "Orders Driven")
cu.set_custom_field_value(task_id="...", field_id=field["id"], value=42)
```

## Notes worth keeping in mind

- Money/currency custom fields are type `currency` (not `money`), with
  `type_config={"currency_type": "SAR"}` (or your own currency).
- A custom field literally named `Summary` collides with a ClickUp-reserved
  field name — pick something else.
- ClickUp's cross-list task search (`GET /team/{id}/task`, wrapped here as
  `get_filtered_team_tasks`) supports a real `date_updated_gt`/`_lt` filter
  that some higher-level search tools don't expose — worth reaching for
  directly whenever you need to *trust* that a date-windowed pull is
  complete.
- A recurring "container" task can hold a team's real activity as
  *comments* on a subtask with **zero assignees** — a pure assignee-filtered
  search will silently miss it. `weekly_digest.py`'s second pass scans one
  known high-signal list directly, with no assignee filter, specifically to
  catch that case.
- ClickUp's rich-comment `table-embed` blocks collapse to the literal string
  `"undefined"` in the plain `comment_text` field — the real table content
  only exists in the raw `comment` blocks array. `weekly_digest.py` renders
  it back to markdown properly (`_render_table_embed`).
