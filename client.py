"""Minimal ClickUp REST API v2 client.

Pure standard library — no `pip install` needed. Reads credentials from
`.env` in this same folder (CLICKUP_API_KEY).

Built because ClickUp's own MCP connector (as exposed in Claude Code) has no
create/update-custom-field or create-view endpoints (read-only on fields,
read/update on task values) — but the real REST API supports field
management. This client fills that gap and gives any weekly-digest /
monthly-aggregation workflow a reusable way to pull data instead of one-off
MCP calls.

Note: creating a Form *view* has no REST API endpoint at all (confirmed
ClickUp platform limitation, not a gap in this client) — that step stays
manual in the ClickUp UI.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.clickup.com/api/v2"

# Defaults for your workspace — find these with get_workspace_hierarchy /
# get_lists_in_folder, or from the ClickUp UI (they're in the URL).
DEFAULT_TEAM_ID = "0000000"                # your workspace's team id
DEFAULT_WEEKLY_LIST_ID = "000000000000"    # the List you pull weekly data from


def _load_env():
    env_path = Path(__file__).parent / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


_ENV = _load_env()
CLICKUP_API_KEY = os.environ.get("CLICKUP_API_KEY") or _ENV.get("CLICKUP_API_KEY")


class ClickUpError(RuntimeError):
    pass


class ClickUpClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or CLICKUP_API_KEY
        if not self.api_key:
            raise ClickUpError("Missing CLICKUP_API_KEY — put it in clickup/.env")

    def _request(self, method, path, body=None, params=None, _retries=5):
        url = f"{API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.api_key)  # raw pk_... key, no "Bearer"
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for attempt in range(_retries):
            try:
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < _retries - 1:
                    # ClickUp rate limit — back off and retry rather than
                    # fail a whole script run over one throttled call. This
                    # was a real failure mode: pulling ~1300 tasks' worth of
                    # per-task detail + comments (Team Sprint Timeline with
                    # subtasks) hit the limit mid-run (2026-08-19).
                    wait = float(e.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(wait)
                    continue
                detail = e.read().decode(errors="replace")
                raise ClickUpError(f"{method} {path} -> HTTP {e.code}: {detail}") from None

    # ---------- hierarchy / read ----------

    def get_spaces(self, team_id=DEFAULT_TEAM_ID):
        return self._request("GET", f"/team/{team_id}/space")["spaces"]

    def get_folders(self, space_id):
        return self._request("GET", f"/space/{space_id}/folder")["folders"]

    def get_lists_in_folder(self, folder_id):
        return self._request("GET", f"/folder/{folder_id}/list")["lists"]

    def get_list(self, list_id):
        return self._request("GET", f"/list/{list_id}")

    def get_custom_fields(self, list_id):
        return self._request("GET", f"/list/{list_id}/field")["fields"]

    def find_custom_field(self, list_id, field_name):
        for f in self.get_custom_fields(list_id):
            if f.get("name") == field_name:
                return f
        raise ClickUpError(f"No custom field named {field_name!r} on list {list_id}")

    def find_option_id(self, field, option_name):
        """For a dropdown/label field dict (as returned by get_custom_fields),
        look up an option's id by its visible name."""
        for opt in field.get("type_config", {}).get("options", []):
            if opt.get("name") == option_name:
                return opt["id"]
        raise ClickUpError(f"No option named {option_name!r} on field {field.get('name')!r}")

    def get_tasks(self, list_id, include_closed=True, page=0, **extra_params):
        """One page of tasks on a list. Does NOT include custom field values
        by default for list responses — use get_task() per task if you need
        those. Paginate by calling again with page+1 until fewer than 100
        tasks come back."""
        params = {"include_closed": str(include_closed).lower(), "page": page, **extra_params}
        return self._request("GET", f"/list/{list_id}/task", params=params)["tasks"]

    def get_all_tasks(self, list_id, include_closed=True, **extra_params):
        """Convenience: pages through get_tasks() until exhausted."""
        page = 0
        out = []
        while True:
            batch = self.get_tasks(list_id, include_closed=include_closed, page=page, **extra_params)
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    def get_task(self, task_id, include_subtasks=False):
        params = {"include_subtasks": str(include_subtasks).lower()}
        return self._request("GET", f"/task/{task_id}", params=params)

    def get_task_comments(self, task_id):
        return self._request("GET", f"/task/{task_id}/comment")["comments"]

    def get_filtered_team_tasks(self, team_id=DEFAULT_TEAM_ID, **params):
        """GET /team/{team_id}/task — the broad cross-list search. Useful
        params: `list_ids[]`, `assignees[]`, `date_updated_gt` /
        `date_updated_lt` (epoch ms), `include_closed`. This is what powers
        `weekly_digest.py`'s roster-wide pull — read + classify results with
        judgment, don't treat the raw list as the digest itself."""
        return self._request("GET", f"/team/{team_id}/task", params=params)["tasks"]

    # ---------- custom fields (write) — the actual gap the MCP tool has ----------

    def create_custom_field(self, list_id, name, field_type, type_config=None):
        """`field_type` is ClickUp's raw type string, e.g. "drop_down",
        "short_text", "text", "number", "currency". Gotchas learned the hard
        way: money fields are type "currency" with
        type_config={"currency_type": "SAR"}, not "money"; a field literally
        named "Summary" collides with ClickUp's own reserved field."""
        body = {"name": name, "type": field_type}
        if type_config is not None:
            body["type_config"] = type_config
        return self._request("POST", f"/list/{list_id}/field", body)

    def delete_custom_field(self, field_id):
        return self._request("DELETE", f"/field/{field_id}")

    # ---------- tasks (write) ----------

    def create_task(self, list_id, name, description=None, assignees=None,
                     custom_fields=None, **extra_fields):
        body = {"name": name, **extra_fields}
        if description is not None:
            body["description"] = description
        if assignees is not None:
            body["assignees"] = assignees
        if custom_fields is not None:
            body["custom_fields"] = custom_fields
        return self._request("POST", f"/list/{list_id}/task", body)

    def update_task(self, task_id, **fields):
        """Partial update of task-level fields (name, description, status,
        assignees, ...). To set a custom field's value use
        set_custom_field_value() instead — ClickUp handles those via a
        separate endpoint, not PUT /task."""
        return self._request("PUT", f"/task/{task_id}", fields)

    def set_custom_field_value(self, task_id, field_id, value):
        return self._request("POST", f"/task/{task_id}/field/{field_id}", {"value": value})

    def create_comment(self, task_id, comment_text):
        return self._request("POST", f"/task/{task_id}/comment", {"comment_text": comment_text})

    def task_url(self, task_id):
        return f"https://app.clickup.com/t/{task_id}"
