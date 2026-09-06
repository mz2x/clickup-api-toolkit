"""Weekly team-activity digest — mechanical data-gathering only.

Pulls raw candidate data (a Form's submissions + a roster's task activity)
for a given week and prints structured JSON. This script deliberately never
decides what's "report-worthy", which category something belongs to, or
whether it matters — that classification step is left to a human (or an LLM
reading this script's output). Mechanical retrieval gets scripted;
judgment/classification never does.

Usage:
    python weekly_digest.py                        # this week (Sun-Thu, Asia/Riyadh)
    python weekly_digest.py --start 2026-08-09 --end 2026-08-13

Config (edit for your own workspace):
- DEFAULT_WEEKLY_LIST_ID / TEAM_SPRINT_TIMELINE_LIST_ID (in client.py / below)
  are ClickUp List ids — find yours with `get_lists_in_folder` or the
  ClickUp UI.
- ROSTER maps ClickUp user ids to display names for whoever's activity you
  want swept — get real ids from `GET /v2/team`.
"""
import argparse
import json
import re
from datetime import datetime, timedelta, timezone

from client import ClickUpClient, DEFAULT_TEAM_ID, DEFAULT_WEEKLY_LIST_ID

# A second, high-signal list to scan directly (with NO assignee filter) in
# addition to the general roster-wide search below. Worth doing when a team
# keeps its real day-to-day work as subtasks under a recurring container
# task rather than as top-level assigned tasks — a pure assignee-filtered
# search can silently miss tasks that have zero assignees but still carry
# the team's real weekly numbers as comments. Replace with your own list id.
TEAM_SPRINT_TIMELINE_LIST_ID = "000000000000"

# Example roster — replace with your own team's ClickUp user ids
# (`GET /v2/team`, matched by email). Keep this in sync with wherever else
# your workflow documents the roster.
ROSTER = {
    "10000001": "Alice",
    "10000002": "Bob",
    "10000003": "Carol",
    "10000004": "Dana",
}

RIYADH_TZ = timezone(timedelta(hours=3))
IMAGE_URL_RE = re.compile(
    r"https://t\d+\.p\.clickup-attachments\.com/\S+?\.(?:jpe?g|png|gif|webp)",
    re.IGNORECASE,
)
# Stable document links worth surfacing alongside images — only stable links
# found as plain text in description/comments qualify. A
# `clickup_download_task_attachment`-style single-use/expiring URL should
# never be captured here.
DOCUMENT_URL_RE = re.compile(
    r"https://(?:docs|drive)\.google\.com/\S+"
    r"|https://app\.clickup\.com/\S*?/(?:v/dc|docs)/\S+"
    r"|https://t\d+\.p\.clickup-attachments\.com/\S+?\.pdf",
    re.IGNORECASE,
)


def riyadh_now():
    return datetime.now(RIYADH_TZ)


def this_week_bounds():
    """Sunday through today, as Riyadh calendar dates (YYYY-MM-DD strings).
    Adjust days_since_sunday's base if your week starts on a different day."""
    now = riyadh_now()
    days_since_sunday = now.isoweekday() % 7  # Mon=1..Sat=6, Sun=7->0
    start = (now - timedelta(days=days_since_sunday)).date()
    return start.isoformat(), now.date().isoformat()


def _to_epoch_ms(date_str, end_of_day=False):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=RIYADH_TZ)
    if end_of_day:
        dt += timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _extract_images(*texts):
    urls = set()
    for t in texts:
        if t:
            urls.update(IMAGE_URL_RE.findall(t))
    return sorted(urls)


def _extract_documents(*texts):
    urls = set()
    for t in texts:
        if t:
            urls.update(DOCUMENT_URL_RE.findall(t))
    return sorted(urls)


def _render_table_embed(block):
    """Render a ClickUp rich-comment 'table-embed' block as a markdown table.
    Needed because the plain `comment_text` field collapses any table to the
    literal string "undefined" — the real content only exists in the raw
    `comment` blocks array (raw REST only; ClickUp's MCP tools don't expose
    this)."""
    te = block.get("table-embed", {})
    cells = te.get("cells", {})
    n_cols = len(te.get("columns", [])) or max(
        (int(k.split(":")[1]) for k in cells), default=0
    )
    n_rows = len(te.get("rows", [])) or max(
        (int(k.split(":")[0]) for k in cells), default=0
    )
    if not n_rows or not n_cols:
        return ""

    def cell_text(r, c):
        cell = cells.get(f"{r}:{c}", {})
        parts = [seg.get("insert", "") for seg in cell.get("content", [])]
        return "".join(parts).replace("\n", " ").strip()

    lines = []
    for r in range(1, n_rows + 1):
        row = [cell_text(r, c) for c in range(1, n_cols + 1)]
        lines.append("| " + " | ".join(row) + " |")
        if r == 1:
            lines.append("|" + "|".join([" --- "] * n_cols) + "|")
    return "\n".join(lines)


def _render_comment(comment):
    """Render a raw ClickUp comment (its `comment` rich-block array) to
    plain/markdown text, expanding any table-embed blocks properly instead
    of leaving "undefined" (what plain `comment_text` gives for tables)."""
    blocks = comment.get("comment")
    if not blocks:
        return comment.get("comment_text", "")
    parts = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "table-embed":
            parts.append("\n" + _render_table_embed(b) + "\n")
        elif isinstance(b, dict) and "text" in b:
            parts.append(b["text"])
        elif isinstance(b, dict) and b.get("type") == "tag":
            parts.append(b.get("text", ""))
    return "".join(parts).strip()


def _shape_task(cu, task_or_id, hint=False):
    """Fetch full task + comments and shape it into the digest's candidate format."""
    t = cu.get_task(task_or_id) if isinstance(task_or_id, str) else task_or_id
    tid = t["id"]
    name = t.get("name", "")
    description = t.get("markdown_description") or t.get("description") or ""
    comments = cu.get_task_comments(tid)
    rendered_comments = [_render_comment(c) for c in comments]

    out = {
        "id": tid,
        "url": f"https://app.clickup.com/t/{tid}",
        "name": name,
        "assignees": [a.get("username") for a in t.get("assignees", [])],
        "creator": (t.get("creator") or {}).get("username"),
        "list": (t.get("list") or {}).get("name"),
        "folder": (t.get("folder") or {}).get("name"),
        "status": (t.get("status") or {}).get("status") if isinstance(t.get("status"), dict) else t.get("status"),
        "description": description,
        "custom_fields": {
            f["name"]: f.get("value")
            for f in t.get("custom_fields", [])
            if f.get("value") not in (None, "", [])
        },
        "comments": [
            {"user": c.get("user", {}).get("username"), "text": rendered}
            for c, rendered in zip(comments, rendered_comments)
        ],
        "attachment_count": len(t.get("attachments") or []),
        "inline_images": _extract_images(description, *rendered_comments),
        "inline_documents": _extract_documents(description, *rendered_comments),
    }
    if hint:
        lname = name.lower()
        out["hints"] = {
            # Example bilingual keyword hint — a real workspace's task names
            # often mix languages, so a signal keyword is worth checking in
            # more than one language rather than assuming English only.
            "offer_keyword": ("offer" in lname) or ("عرض" in name) or ("عروض" in name),
        }
    return out


def gather(start, end, cu=None):
    cu = cu or ClickUpClient()
    window_start_ms = _to_epoch_ms(start)
    window_end_ms = _to_epoch_ms(end, end_of_day=True)

    # --- source 1: Form submissions created this week ---
    form_tasks = []
    for t in cu.get_all_tasks(DEFAULT_WEEKLY_LIST_ID, include_closed=True):
        created = int(t.get("date_created", 0))
        if window_start_ms <= created < window_end_ms:
            form_tasks.append(_shape_task(cu, t["id"]))

    # --- source 2: roster activity, workspace-wide, true date_updated filter ---
    # (the raw REST GET /team/{id}/task endpoint supports this properly —
    # some higher-level search tools have no direct updated-date filter and
    # can under-count for a busy week, so prefer the raw endpoint for
    # anything you need to trust completely.)
    raw = cu.get_filtered_team_tasks(
        team_id=DEFAULT_TEAM_ID,
        **{
            "assignees[]": list(ROSTER.keys()),
            "date_updated_gt": window_start_ms,
            "date_updated_lt": window_end_ms,
            "include_closed": "true",
        },
    )
    activity_tasks = [_shape_task(cu, t["id"], hint=True) for t in raw]

    # --- priority scan: a known high-signal list, explicitly, INCLUDING
    # SUBTASKS, with NO assignee filter ---
    # Worth doing separately: a recurring container task can hold the
    # team's real numbers as *comments* on a subtask that has zero
    # assignees — an assignee-filtered search (source 2 above) will
    # silently miss it. Since this list is small/curated, scanning it in
    # full (subtasks=true, no person filter, just the date window) is cheap
    # insurance.
    seen_ids = {t["id"] for t in activity_tasks}
    for t in cu.get_all_tasks(TEAM_SPRINT_TIMELINE_LIST_ID, include_closed=True, subtasks="true"):
        if t["id"] in seen_ids:
            continue
        updated = int(t.get("date_updated", 0))
        if window_start_ms <= updated < window_end_ms:
            activity_tasks.append(_shape_task(cu, t["id"], hint=True))
            seen_ids.add(t["id"])

    return {
        "window": {"start": start, "end": end},
        "roster": ROSTER,
        "form_submissions": form_tasks,
        "team_activity": activity_tasks,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", help="YYYY-MM-DD (Sunday). Default: this week's Sunday.")
    parser.add_argument("--end", help="YYYY-MM-DD (Thursday). Default: today.")
    args = parser.parse_args()

    if args.start and args.end:
        window_start, window_end = args.start, args.end
    elif args.start or args.end:
        parser.error("--start and --end must be given together")
    else:
        window_start, window_end = this_week_bounds()

    print(json.dumps(gather(window_start, window_end), indent=2, ensure_ascii=False))
