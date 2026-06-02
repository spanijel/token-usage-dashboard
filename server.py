#!/usr/bin/env python3
import csv
import glob
import json
import os
import re
import shlex
import sqlite3
import subprocess
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parent
STATIC_DIR = APP_ROOT / "static"
DATA_DIR = APP_ROOT / "data"
HOME = Path.home()
VS_CODE_GLOBAL_STORAGE = HOME / "Library/Application Support/Code/User/globalStorage"
VS_CODE_STATE_DB = VS_CODE_GLOBAL_STORAGE / "state.vscdb"
DOWNLOADS_DIR = HOME / "Downloads"
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
MANUAL_PROVIDERS = {
    "chatgpt": "ChatGPT",
    "copilot": "GitHub Copilot",
}
OFFICIAL_CODEX_PRICING = {
    "kind": "official_gpt54_rough",
    "currency": "USD",
    "label": "OpenAI official GPT-5.4 rough estimate",
    "input_usd_per_million": 2.50,
    "cached_input_usd_per_million": 0.25,
    "output_usd_per_million": 15.00,
    "assumed_input_share": 0.85,
    "assumed_output_share": 0.15,
    "notes": (
        "Uses official GPT-5.4 API prices and a rough 85% input / 15% output mix. "
        "Local Codex thread totals do not expose exact input/output/cached token splits."
    ),
}
REMOTE_CODEX_HOSTS = [
    {
        "host": "e122378.arm.com",
        "label": "Ubuntu desktop",
    }
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def json_response(handler: BaseHTTPRequestHandler, payload, status=HTTPStatus.OK) -> None:
    data = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def text_response(
    handler: BaseHTTPRequestHandler,
    content: str,
    content_type: str,
    status=HTTPStatus.OK,
) -> None:
    data = content.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_request_json(handler: BaseHTTPRequestHandler):
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def format_local_timestamp(value):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return None


def coerce_number(value):
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return value
    cleaned = str(value).strip().replace(",", "")
    if not cleaned:
        return None
    number = float(cleaned)
    if number.is_integer():
        return int(number)
    return round(number, 4)


def fetch_all_dicts(cursor, query: str, params=()):
    return [dict(row) for row in cursor.execute(query, params).fetchall()]


def table_exists(cursor, table_name: str) -> bool:
    row = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    size = len(ordered)
    midpoint = size // 2
    if size % 2:
        return ordered[midpoint]
    return round((ordered[midpoint - 1] + ordered[midpoint]) / 2, 2)


def percentile(values, ratio: float):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def share_pct(part, whole):
    if not whole:
        return None
    return round(float(part) * 100.0 / float(whole), 2)


def abbreviate_path(path: str) -> str:
    if not path:
        return "(unknown)"
    text = str(path)
    home_text = str(HOME)
    if text.startswith(home_text):
        text = "~" + text[len(home_text) :]
    chunks = [chunk for chunk in text.split("/") if chunk]
    if text.startswith("~") and len(chunks) <= 3:
        return text
    if len(chunks) <= 4:
        return text
    prefix = "~/" if text.startswith("~") else "/"
    return prefix + "/".join(chunks[:2] + ["..."] + chunks[-2:])


def tidy_title(title: str, limit: int = 120) -> str:
    if not title:
        return "(untitled)"
    flattened = " ".join(str(title).split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1].rstrip() + "…"


def normalize_thread_source(row):
    source = row.get("source")
    if source == "cli":
        return "CLI"
    if source == "vscode":
        return "VS Code"
    if source == "exec":
        return "Exec"
    if isinstance(source, str) and source.startswith("{"):
        try:
            parsed = json.loads(source)
        except json.JSONDecodeError:
            parsed = None
        if parsed:
            spawn = (((parsed.get("subagent") or {}).get("thread_spawn")) or {})
            role = row.get("agent_role") or spawn.get("agent_role") or "subagent"
            return f"Subagent ({role})"
    return str(source or "(unknown)")


def summarize_sandbox(policy_text: str) -> str:
    if not policy_text:
        return "(unknown)"
    try:
        policy = json.loads(policy_text)
    except json.JSONDecodeError:
        return tidy_title(policy_text, limit=72)
    policy_type = policy.get("type", "unknown")
    network = "net:on" if policy.get("network_access") else "net:off"
    roots = policy.get("writable_roots") or []
    if roots:
        return f"{policy_type} ({len(roots)} roots, {network})"
    return f"{policy_type} ({network})"


def remote_snapshot_file(host: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", host)
    return DATA_DIR / f"remote_{safe}.json"


def run_remote_codex_snapshot(host_config):
    host = host_config["host"]
    label = host_config.get("label", host)
    cache_path = remote_snapshot_file(host)
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=8",
                host,
                f"python3 -c {shlex.quote(remote_query_script())}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(completed.stdout.strip() or "{}")
        totals = payload.get("totals") or {}
        snapshot = {
            "host": host,
            "label": label,
            "status": payload.get("status", "configured"),
            "db_path": payload.get("db_path"),
            "thread_count": totals.get("thread_count", 0),
            "total_tokens": totals.get("total_tokens", 0),
            "tokens_30d": totals.get("tokens_30d", 0),
            "tokens_7d": totals.get("tokens_7d", 0),
            "first_seen_local": format_local_timestamp(totals.get("first_seen")),
            "last_seen_local": format_local_timestamp(totals.get("last_seen")),
            "monthly": payload.get("monthly", []),
            "daily": payload.get("daily", []),
            "models": payload.get("models", []),
            "cwds": payload.get("cwds", []),
            "fetched_at": utc_now(),
        }
        cache_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return snapshot
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["status"] = "cached"
            cached["cache_notice"] = tidy_title(
                f"Live SSH refresh failed, showing cached snapshot instead. {stderr.strip() or str(exc)}",
                limit=220,
            )
            return cached
        return {
            "host": host,
            "label": label,
            "status": "error",
            "error": tidy_title(str(exc) if not stderr else stderr.strip(), limit=180),
            "thread_count": None,
            "total_tokens": None,
            "tokens_30d": None,
            "tokens_7d": None,
            "monthly": [],
            "daily": [],
            "models": [],
            "cwds": [],
        }


def remote_query_script() -> str:
    return r'''
import json, os, sqlite3
from pathlib import Path

dbs = sorted(Path.home().glob('.codex/state_*.sqlite'))
if not dbs:
    print(json.dumps({"status": "missing", "error": "No ~/.codex/state_*.sqlite found"}))
    raise SystemExit(0)

db_path = str(dbs[-1])
con = sqlite3.connect(db_path)
con.row_factory = sqlite3.Row
cur = con.cursor()
totals = dict(cur.execute("""
SELECT
  COUNT(*) AS thread_count,
  COALESCE(SUM(tokens_used), 0) AS total_tokens,
  COALESCE(SUM(CASE
    WHEN updated_at >= strftime('%s', 'now', 'localtime', '-30 days')
    THEN tokens_used ELSE 0 END), 0) AS tokens_30d,
  COALESCE(SUM(CASE
    WHEN updated_at >= strftime('%s', 'now', 'localtime', '-7 days')
    THEN tokens_used ELSE 0 END), 0) AS tokens_7d,
  MIN(updated_at) AS first_seen,
  MAX(updated_at) AS last_seen
FROM threads
""").fetchone())
monthly = [dict(r) for r in cur.execute("""
SELECT
  strftime('%Y-%m', updated_at, 'unixepoch', 'localtime') AS month,
  COUNT(*) AS thread_count,
  SUM(tokens_used) AS value
FROM threads
GROUP BY month
ORDER BY month DESC
LIMIT 12
""").fetchall()]
daily = [dict(r) for r in cur.execute("""
SELECT
  strftime('%Y-%m-%d', updated_at, 'unixepoch', 'localtime') AS day,
  COUNT(*) AS thread_count,
  SUM(tokens_used) AS value
FROM threads
GROUP BY day
ORDER BY day ASC
""").fetchall()]
models = [dict(r) for r in cur.execute("""
SELECT
  COALESCE(model, '(unknown)') AS model,
  COUNT(*) AS thread_count,
  SUM(tokens_used) AS total_tokens
FROM threads
GROUP BY model
ORDER BY total_tokens DESC
LIMIT 6
""").fetchall()]
cwds = [dict(r) for r in cur.execute("""
SELECT
  cwd,
  COUNT(*) AS thread_count,
  SUM(tokens_used) AS total_tokens
FROM threads
GROUP BY cwd
ORDER BY total_tokens DESC
LIMIT 6
""").fetchall()]
print(json.dumps({
  "status": "configured",
  "db_path": db_path,
  "totals": totals,
  "monthly": monthly,
  "daily": daily,
  "models": models,
  "cwds": cwds,
}))
'''


def month_key(row):
    return row["month"]


def day_key(row):
    return row["day"]


def aggregate_monthly_series(series_list, key_name="month", value_name="value"):
    merged = {}
    for rows in series_list:
        for row in rows or []:
            key = row[key_name]
            bucket = merged.setdefault(key, {"month": key, "value": 0, "thread_count": 0})
            bucket["value"] += row.get(value_name, 0) or 0
            bucket["thread_count"] += row.get("thread_count", 0) or 0
    return sorted(merged.values(), key=month_key, reverse=True)


def aggregate_daily_series(series_list, key_name="day", value_name="value"):
    merged = {}
    for rows in series_list:
        for row in rows or []:
            key = row[key_name]
            bucket = merged.setdefault(key, {"day": key, "value": 0, "thread_count": 0})
            bucket["value"] += row.get(value_name, 0) or 0
            bucket["thread_count"] += row.get("thread_count", 0) or 0
    return sorted(merged.values(), key=day_key)


def aggregate_named_rows(rows, label_key: str):
    merged = {}
    for row in rows:
        label = row.get(label_key)
        if not label:
            continue
        bucket = merged.setdefault(
            label,
            {"label": label, "thread_count": 0, "total_tokens": 0},
        )
        bucket["thread_count"] += row.get("thread_count", 0) or 0
        bucket["total_tokens"] += row.get("total_tokens", 0) or 0
    result = sorted(merged.values(), key=lambda row: row["total_tokens"], reverse=True)
    for row in result:
        row["avg_tokens"] = round(row["total_tokens"] / row["thread_count"], 0) if row["thread_count"] else 0
    return result


def normalize_monthly_rows(rows):
    normalized = []
    for raw in rows:
        month = str(
            raw.get("month")
            or raw.get("period")
            or raw.get("date")
            or raw.get("label")
            or ""
        ).strip()
        if not MONTH_PATTERN.match(month):
            raise ValueError(f"Invalid month '{month}'. Expected YYYY-MM.")
        value = coerce_number(
            raw.get("value")
            or raw.get("total")
            or raw.get("tokens")
            or raw.get("credits")
            or raw.get("usage")
            or raw.get("count")
        )
        if value is None:
            raise ValueError(f"Missing numeric value for month '{month}'.")
        normalized.append({"month": month, "value": value})
    normalized.sort(key=month_key, reverse=True)
    return normalized


def parse_csv_rows(content: str):
    reader = csv.DictReader(content.splitlines())
    if not reader.fieldnames:
        raise ValueError("CSV import requires a header row.")
    rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError("CSV import is empty.")
    return normalize_monthly_rows(rows)


def parse_json_rows(content: str):
    payload = json.loads(content)
    if isinstance(payload, dict):
        rows = payload.get("monthly") or payload.get("months") or payload.get("rows") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("JSON import must be an object or an array.")
    if not rows:
        raise ValueError("JSON import did not contain monthly rows.")
    return normalize_monthly_rows(rows), payload if isinstance(payload, dict) else {}


def normalize_pricing(payload, derived=None):
    derived = derived or {}
    usd_per_block = coerce_number(payload.get("usdPerBlock") or derived.get("usd_per_block"))
    units_per_block = coerce_number(payload.get("unitsPerBlock") or derived.get("units_per_block"))
    monthly_flat_usd = coerce_number(payload.get("monthlyFlatUsd") or derived.get("monthly_flat_usd"))
    currency = str(payload.get("currency") or derived.get("currency") or "USD").strip().upper()
    label = str(payload.get("pricingLabel") or derived.get("pricing_label") or "").strip()

    if (
        usd_per_block is None
        and units_per_block is None
        and monthly_flat_usd is None
        and not label
    ):
        return None

    pricing = {
        "kind": "custom_block",
        "currency": currency,
        "label": label or "Custom pricing assumption",
        "usd_per_block": usd_per_block,
        "units_per_block": units_per_block,
        "monthly_flat_usd": monthly_flat_usd,
    }
    if pricing["usd_per_block"] is not None and pricing["units_per_block"] in (None, 0):
        raise ValueError("unitsPerBlock must be set when usdPerBlock is provided.")
    return pricing


def build_cost_summary(total, monthly, pricing):
    if not pricing:
        return None

    if pricing.get("kind") == "official_gpt54_rough":
        input_rate = pricing["input_usd_per_million"]
        output_rate = pricing["output_usd_per_million"]
        cached_rate = pricing["cached_input_usd_per_million"]
        assumed_rate = (
            pricing["assumed_input_share"] * input_rate
            + pricing["assumed_output_share"] * output_rate
        )

        def project(token_total, rate):
            if token_total is None:
                return None
            return round(float(token_total) / 1_000_000.0 * float(rate), 2)

        monthly_rows = []
        for row in monthly or []:
            monthly_rows.append(
                {
                    "month": row["month"],
                    "rough_cost_usd": project(row["value"], assumed_rate),
                    "input_only_cost_usd": project(row["value"], input_rate),
                    "output_only_cost_usd": project(row["value"], output_rate),
                    "cached_input_cost_usd": project(row["value"], cached_rate),
                }
            )

        return {
            "currency": pricing["currency"],
            "label": pricing["label"],
            "kind": pricing["kind"],
            "notes": pricing.get("notes"),
            "assumed_rate_per_million": assumed_rate,
            "rough_cost_total_usd": project(total, assumed_rate),
            "input_only_cost_total_usd": project(total, input_rate),
            "output_only_cost_total_usd": project(total, output_rate),
            "cached_input_cost_total_usd": project(total, cached_rate),
            "latest_month_rough_cost_usd": monthly_rows[0]["rough_cost_usd"] if monthly_rows else None,
            "monthly_rows": monthly_rows,
        }

    usd_per_block = pricing.get("usd_per_block")
    units_per_block = pricing.get("units_per_block")
    monthly_flat_usd = pricing.get("monthly_flat_usd")
    usage_cost_total = None

    if (
        total is not None
        and usd_per_block is not None
        and units_per_block not in (None, 0)
    ):
        usage_cost_total = round(float(total) / float(units_per_block) * float(usd_per_block), 2)

    monthly_rows = []
    for row in monthly or []:
        usage_cost = None
        if usd_per_block is not None and units_per_block not in (None, 0):
            usage_cost = round(float(row["value"]) / float(units_per_block) * float(usd_per_block), 2)
        total_cost = None
        if usage_cost is not None or monthly_flat_usd is not None:
            total_cost = round((usage_cost or 0) + (monthly_flat_usd or 0), 2)
        monthly_rows.append(
            {
                "month": row["month"],
                "usage_cost_usd": usage_cost,
                "flat_cost_usd": monthly_flat_usd,
                "total_cost_usd": total_cost,
            }
        )

    latest_month_cost = next(
        (row["total_cost_usd"] for row in monthly_rows if row["total_cost_usd"] is not None),
        None,
    )

    return {
        "currency": pricing["currency"],
        "label": pricing["label"],
        "usage_cost_total_usd": usage_cost_total,
        "monthly_flat_usd": monthly_flat_usd,
        "latest_month_cost_usd": latest_month_cost,
        "monthly_rows": monthly_rows,
    }


def data_file(provider: str) -> Path:
    return DATA_DIR / f"{provider}.json"


def pricing_file(provider: str) -> Path:
    return DATA_DIR / f"{provider}_pricing.json"


def vscode_state_has_key(key: str) -> bool:
    if not VS_CODE_STATE_DB.exists():
        return False
    connection = sqlite3.connect(VS_CODE_STATE_DB)
    try:
        cursor = connection.cursor()
        row = cursor.execute(
            "SELECT 1 FROM ItemTable WHERE key=? LIMIT 1",
            (key,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def discover_download_candidates(provider: str):
    if not DOWNLOADS_DIR.exists():
        return []
    patterns = {
        "chatgpt": ["*chatgpt*", "*openai*export*", "*openai*usage*"],
        "copilot": ["*copilot*", "*github*copilot*", "*copilot*metrics*", "*copilot*usage*"],
    }[provider]
    matches = []
    for pattern in patterns:
        matches.extend(DOWNLOADS_DIR.glob(pattern))
    unique = []
    seen = set()
    for path in sorted(matches):
        text = str(path)
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return unique[:5]


def build_manual_placeholder(provider: str):
    if provider == "chatgpt":
        extension_detected = vscode_state_has_key("openai.chatgpt")
        downloads = discover_download_candidates("chatgpt")
        notes = (
            "No ChatGPT usage import is configured yet. "
            "VS Code ChatGPT extension state was detected, but it contains prompt/session history, "
            "not a reliable token or credit usage ledger."
            if extension_detected
            else "No ChatGPT usage import is configured yet."
        )
        steps = [
            "Open ChatGPT and get a usage export or dashboard report with totals or monthly numbers.",
            "Paste CSV or JSON into this card, or upload a matching file.",
            "Click Save ChatGPT Data.",
        ]
        return {
            "provider": provider,
            "display_name": MANUAL_PROVIDERS[provider],
            "status": "unavailable_in_this_environment",
            "status_label": "Needs ChatGPT export",
            "ingestion": "manual-import",
            "metric": None,
            "unit": None,
            "total": None,
            "monthly": [],
            "notes": notes,
            "setup_steps": steps,
            "local_signals": {
                "vscode_extension_state_detected": extension_detected,
                "download_candidates": downloads,
            },
        }

    extension_detected = vscode_state_has_key("GitHub.copilot-chat") or (VS_CODE_GLOBAL_STORAGE / "github.copilot-chat").exists()
    downloads = discover_download_candidates("copilot")
    notes = (
        "GitHub Copilot extension state was detected locally, but it does not expose a reliable local "
        "usage or token ledger. This source needs a GitHub Copilot metrics export or API-backed fetch."
        if extension_detected
        else "No GitHub Copilot usage import is configured yet."
    )
    steps = [
        "Export GitHub Copilot usage/metrics from GitHub, or provide an org/enterprise metrics API source.",
        "Paste CSV or JSON into this card, or upload a matching file.",
        "Click Save Copilot Data.",
    ]
    return {
        "provider": provider,
        "display_name": MANUAL_PROVIDERS[provider],
        "status": "unavailable_in_this_environment",
        "status_label": "Needs GitHub metrics access",
        "ingestion": "manual-import",
        "metric": None,
        "unit": None,
        "total": None,
        "monthly": [],
        "notes": notes,
        "setup_steps": steps,
        "local_signals": {
            "vscode_extension_state_detected": extension_detected,
            "download_candidates": downloads,
        },
    }


def load_manual_source(provider: str):
    path = data_file(provider)
    if not path.exists():
        return build_manual_placeholder(provider)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["display_name"] = MANUAL_PROVIDERS[provider]
    stored["cost"] = build_cost_summary(stored.get("total"), stored.get("monthly", []), stored.get("pricing"))
    return stored


def save_manual_source(provider: str, payload):
    content_format = payload.get("contentFormat", "none")
    content = payload.get("content", "").strip()
    monthly = []
    derived = {}
    if content_format == "csv" and content:
        monthly = parse_csv_rows(content)
    elif content_format == "json" and content:
        monthly, derived = parse_json_rows(content)
    elif content_format == "none" or not content:
        monthly = []
    else:
        raise ValueError(f"Unsupported content format '{content_format}'.")

    metric = str(payload.get("metric") or derived.get("metric") or "tokens").strip().lower()
    unit = str(payload.get("unit") or derived.get("unit") or metric).strip()
    label = str(payload.get("label") or derived.get("label") or MANUAL_PROVIDERS[provider]).strip()
    total = coerce_number(payload.get("total"))
    if total is None and monthly:
        total = sum(row["value"] for row in monthly)

    stored = {
        "provider": provider,
        "display_name": MANUAL_PROVIDERS[provider],
        "status": "configured",
        "ingestion": "manual-import",
        "label": label,
        "metric": metric,
        "unit": unit,
        "total": total,
        "monthly": monthly,
        "pricing": normalize_pricing(payload, derived),
        "notes": str(payload.get("notes") or derived.get("notes") or "").strip(),
        "updated_at": utc_now(),
    }
    stored["cost"] = build_cost_summary(stored["total"], stored["monthly"], stored["pricing"])
    data_file(provider).write_text(json.dumps(stored, indent=2), encoding="utf-8")
    return stored


def clear_manual_source(provider: str):
    path = data_file(provider)
    if path.exists():
        path.unlink()


def load_codex_pricing():
    path = pricing_file("codex")
    if not path.exists():
        return dict(OFFICIAL_CODEX_PRICING)
    return json.loads(path.read_text(encoding="utf-8"))


def save_codex_pricing(payload):
    pricing = normalize_pricing(payload)
    if pricing is None:
        raise ValueError("At least one pricing field is required.")
    pricing["updated_at"] = utc_now()
    pricing_file("codex").write_text(json.dumps(pricing, indent=2), encoding="utf-8")
    return pricing


def clear_codex_pricing():
    path = pricing_file("codex")
    if path.exists():
        path.unlink()


def load_codex_source():
    db_paths = sorted(glob.glob(str(HOME / ".codex" / "state_*.sqlite")))
    if not db_paths:
        return {
            "provider": "codex",
            "display_name": "Codex",
            "status": "missing",
            "ingestion": "automatic-local-sqlite",
            "metric": "tokens",
            "unit": "tokens",
            "notes": "No ~/.codex/state_*.sqlite database was found.",
            "monthly": [],
            "daily": [],
            "daily_30": [],
            "model_breakdown": [],
            "effort_breakdown": [],
            "cwd_breakdown": [],
            "source_breakdown": [],
            "approval_breakdown": [],
            "sandbox_breakdown": [],
            "top_threads": [],
            "recent_threads": [],
        }

    db_path = Path(db_paths[-1])
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()

    totals = cursor.execute(
        """
        SELECT
          COUNT(*) AS thread_count,
          COALESCE(SUM(tokens_used), 0) AS total_tokens,
          COALESCE(SUM(CASE
            WHEN updated_at >= strftime('%s', 'now', 'localtime', '-30 days')
            THEN tokens_used ELSE 0 END), 0) AS tokens_30d,
          COALESCE(SUM(CASE
            WHEN updated_at >= strftime('%s', 'now', 'localtime', '-7 days')
            THEN tokens_used ELSE 0 END), 0) AS tokens_7d,
          MIN(updated_at) AS first_seen,
          MAX(updated_at) AS last_seen,
          COUNT(DISTINCT strftime('%Y-%m-%d', updated_at, 'unixepoch', 'localtime')) AS active_days_total
        FROM threads
        """
    ).fetchone()

    monthly = fetch_all_dicts(
        cursor,
        """
        SELECT
          strftime('%Y-%m', updated_at, 'unixepoch', 'localtime') AS month,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS value,
          ROUND(AVG(tokens_used), 0) AS avg_tokens
        FROM threads
        GROUP BY month
        ORDER BY month DESC
        """,
    )

    daily = fetch_all_dicts(
        cursor,
        """
        SELECT
          strftime('%Y-%m-%d', updated_at, 'unixepoch', 'localtime') AS day,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS value
        FROM threads
        GROUP BY day
        ORDER BY day ASC
        """,
    )

    daily_30 = fetch_all_dicts(
        cursor,
        """
        SELECT
          strftime('%Y-%m-%d', updated_at, 'unixepoch', 'localtime') AS day,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS value
        FROM threads
        WHERE updated_at >= strftime('%s', 'now', 'localtime', '-30 days')
        GROUP BY day
        ORDER BY day ASC
        """,
    )

    model_breakdown = fetch_all_dicts(
        cursor,
        """
        SELECT
          COALESCE(model, '(unknown)') AS model,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS total_tokens,
          ROUND(AVG(tokens_used), 0) AS avg_tokens
        FROM threads
        GROUP BY model
        ORDER BY total_tokens DESC
        LIMIT 8
        """,
    )

    effort_breakdown = fetch_all_dicts(
        cursor,
        """
        SELECT
          COALESCE(reasoning_effort, '(unknown)') AS reasoning_effort,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS total_tokens
        FROM threads
        GROUP BY reasoning_effort
        ORDER BY total_tokens DESC
        """,
    )

    cwd_breakdown = fetch_all_dicts(
        cursor,
        """
        SELECT
          cwd,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS total_tokens,
          ROUND(AVG(tokens_used), 0) AS avg_tokens
        FROM threads
        GROUP BY cwd
        ORDER BY total_tokens DESC
        LIMIT 8
        """,
    )

    source_rows = fetch_all_dicts(
        cursor,
        """
        SELECT
          source,
          agent_role,
          agent_nickname,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS total_tokens
        FROM threads
        GROUP BY source, agent_role, agent_nickname
        ORDER BY total_tokens DESC
        """,
    )
    source_agg = {}
    for row in source_rows:
        label = normalize_thread_source(row)
        bucket = source_agg.setdefault(label, {"label": label, "thread_count": 0, "total_tokens": 0})
        bucket["thread_count"] += row["thread_count"]
        bucket["total_tokens"] += row["total_tokens"]
    source_breakdown = sorted(source_agg.values(), key=lambda row: row["total_tokens"], reverse=True)
    for row in source_breakdown:
        row["avg_tokens"] = round(row["total_tokens"] / row["thread_count"], 0) if row["thread_count"] else 0

    approval_breakdown = fetch_all_dicts(
        cursor,
        """
        SELECT
          approval_mode,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS total_tokens
        FROM threads
        GROUP BY approval_mode
        ORDER BY total_tokens DESC
        """,
    )

    sandbox_breakdown = fetch_all_dicts(
        cursor,
        """
        SELECT
          sandbox_policy,
          COUNT(*) AS thread_count,
          SUM(tokens_used) AS total_tokens
        FROM threads
        GROUP BY sandbox_policy
        ORDER BY total_tokens DESC
        LIMIT 6
        """,
    )

    top_threads = fetch_all_dicts(
        cursor,
        """
        SELECT
          title,
          cwd,
          model_provider,
          COALESCE(model, '(unknown)') AS model,
          COALESCE(reasoning_effort, '(unknown)') AS reasoning_effort,
          approval_mode,
          tokens_used,
          updated_at
        FROM threads
        ORDER BY tokens_used DESC
        LIMIT 8
        """,
    )

    recent_threads = fetch_all_dicts(
        cursor,
        """
        SELECT
          title,
          cwd,
          model_provider,
          COALESCE(model, '(unknown)') AS model,
          COALESCE(reasoning_effort, '(unknown)') AS reasoning_effort,
          approval_mode,
          tokens_used,
          updated_at
        FROM threads
        ORDER BY updated_at DESC
        LIMIT 8
        """,
    )

    thread_tokens = [row["tokens_used"] for row in fetch_all_dicts(cursor, "SELECT tokens_used FROM threads")]

    if table_exists(cursor, "logs"):
        response_events = cursor.execute(
            "SELECT COUNT(*) FROM logs WHERE message LIKE '%response.completed%'"
        ).fetchone()[0]
        log_coverage = cursor.execute(
            """
            SELECT
              COUNT(*) AS log_rows,
              MIN(ts) AS first_log,
              MAX(ts) AS last_log
            FROM logs
            """
        ).fetchone()
    else:
        response_events = 0
        log_coverage = {"log_rows": 0, "first_log": None, "last_log": None}

    connection.close()

    total_tokens = totals["total_tokens"]
    for row in monthly:
        row["share_pct"] = share_pct(row["value"], total_tokens)
    for row in daily_30:
        row["share_pct_30d"] = share_pct(row["value"], totals["tokens_30d"])
    for row in model_breakdown:
        row["share_pct"] = share_pct(row["total_tokens"], total_tokens)
    for row in effort_breakdown:
        row["share_pct"] = share_pct(row["total_tokens"], total_tokens)
    for row in cwd_breakdown:
        row["label"] = abbreviate_path(row["cwd"])
        row["share_pct"] = share_pct(row["total_tokens"], total_tokens)
    for row in source_breakdown:
        row["share_pct"] = share_pct(row["total_tokens"], total_tokens)
    for row in approval_breakdown:
        row["share_pct"] = share_pct(row["total_tokens"], total_tokens)
    for row in sandbox_breakdown:
        row["label"] = summarize_sandbox(row["sandbox_policy"])
        row["share_pct"] = share_pct(row["total_tokens"], total_tokens)
    for row in top_threads + recent_threads:
        row["title_short"] = tidy_title(row["title"])
        row["cwd_short"] = abbreviate_path(row["cwd"])
        row["updated_local"] = format_local_timestamp(row.get("updated_at"))
        row["share_pct"] = share_pct(row["tokens_used"], total_tokens)

    notes = [
        f"Reading newest local Codex database: {db_path}",
        f"Found {totals['thread_count']} recorded Codex threads.",
    ]
    if response_events == 0:
        notes.append(
            "Detailed response.completed usage logs were not present in this local state DB, "
            "so this dashboard uses thread-level token totals."
        )

    pricing = load_codex_pricing()
    busiest_day = max(daily_30, key=lambda row: row["value"], default=None)
    top_workspace = cwd_breakdown[0] if cwd_breakdown else None
    top_three_threads = sorted(thread_tokens, reverse=True)[:3]
    stats = {
        "avg_tokens_per_thread": round(total_tokens / totals["thread_count"], 0) if totals["thread_count"] else 0,
        "median_tokens_per_thread": median(thread_tokens),
        "p90_tokens_per_thread": percentile(thread_tokens, 0.9),
        "largest_thread_tokens": max(thread_tokens) if thread_tokens else 0,
        "zero_token_threads": sum(1 for value in thread_tokens if value == 0),
        "active_days_total": totals["active_days_total"],
        "active_days_30d": len(daily_30),
        "top_three_threads_share_pct": share_pct(sum(top_three_threads), total_tokens),
        "top_workspace_share_pct": share_pct(top_workspace["total_tokens"], total_tokens) if top_workspace else None,
    }

    if stats["zero_token_threads"]:
        notes.append(
            f"{stats['zero_token_threads']} threads recorded zero tokens, usually short exec-only activity."
        )

    highlights = []
    if top_workspace:
        highlights.append(
            f"Top workspace {top_workspace['label']} accounts for {stats['top_workspace_share_pct']}% of all tokens."
        )
    if stats["top_three_threads_share_pct"] is not None:
        highlights.append(
            f"The 3 heaviest threads account for {stats['top_three_threads_share_pct']}% of total token usage."
        )
    if busiest_day:
        highlights.append(
            f"Busiest day in the last 30 days was {busiest_day['day']} with {busiest_day['value']} tokens across {busiest_day['thread_count']} threads."
        )
    if effort_breakdown:
        top_effort = effort_breakdown[0]
        highlights.append(
            f"Most usage ran with reasoning effort {top_effort['reasoning_effort']} ({top_effort['share_pct']}% of tokens)."
        )

    return {
        "provider": "codex",
        "display_name": "Codex",
        "status": "configured",
        "ingestion": "automatic-local-sqlite",
        "label": "Local Codex SQLite",
        "metric": "tokens",
        "unit": "tokens",
        "total": total_tokens,
        "last_30_days": totals["tokens_30d"],
        "last_7_days": totals["tokens_7d"],
        "thread_count": totals["thread_count"],
        "db_path": str(db_path),
        "first_seen_local": format_local_timestamp(totals["first_seen"]),
        "last_seen_local": format_local_timestamp(totals["last_seen"]),
        "stats": stats,
        "monthly": monthly,
        "daily": daily,
        "daily_30": daily_30,
        "model_breakdown": model_breakdown,
        "effort_breakdown": effort_breakdown,
        "cwd_breakdown": cwd_breakdown,
        "source_breakdown": source_breakdown,
        "approval_breakdown": approval_breakdown,
        "sandbox_breakdown": sandbox_breakdown,
        "top_threads": top_threads,
        "recent_threads": recent_threads,
        "data_quality": {
            "log_rows": log_coverage["log_rows"],
            "first_log_local": format_local_timestamp(log_coverage["first_log"]),
            "last_log_local": format_local_timestamp(log_coverage["last_log"]),
            "response_events": response_events,
            "detailed_usage_available": response_events > 0,
        },
        "pricing": pricing,
        "cost": build_cost_summary(total_tokens, monthly, pricing),
        "highlights": highlights,
        "notes": " ".join(notes),
        "updated_at": utc_now(),
    }


def build_dashboard():
    codex = load_codex_source()
    remote_sources = [run_remote_codex_snapshot(config) for config in REMOTE_CODEX_HOSTS]
    pricing = codex.get("pricing") or load_codex_pricing()
    active_remote_sources = [
        source for source in remote_sources if source.get("status") in {"configured", "cached"}
    ]
    combined_total_tokens = codex.get("total", 0) + sum(source.get("total_tokens", 0) or 0 for source in active_remote_sources)
    combined_thread_count = codex.get("thread_count", 0) + sum(source.get("thread_count", 0) or 0 for source in active_remote_sources)
    combined_30d = codex.get("last_30_days", 0) + sum(source.get("tokens_30d", 0) or 0 for source in active_remote_sources)
    combined_7d = codex.get("last_7_days", 0) + sum(source.get("tokens_7d", 0) or 0 for source in active_remote_sources)
    combined_monthly = aggregate_monthly_series([codex.get("monthly", [])] + [source.get("monthly", []) for source in active_remote_sources])
    combined_daily = aggregate_daily_series([codex.get("daily", [])] + [source.get("daily", []) for source in active_remote_sources])
    aggregate_models = aggregate_named_rows(
        list(codex.get("model_breakdown", []))
        + [
            {
                "label": row.get("model"),
                "thread_count": row.get("thread_count", 0),
                "total_tokens": row.get("total_tokens", 0),
            }
            for source in active_remote_sources
            for row in source.get("models", [])
        ],
        "label",
    )
    for row in aggregate_models:
        row["share_pct"] = share_pct(row["total_tokens"], combined_total_tokens)
    aggregate_workspaces = [
        {
            "label": f"Local: {row.get('label') or abbreviate_path(row.get('cwd'))}",
            "thread_count": row.get("thread_count", 0),
            "total_tokens": row.get("total_tokens", 0),
            "machine": "local",
        }
        for row in codex.get("cwd_breakdown", [])
    ] + [
        {
            "label": f"{source.get('label')}: {abbreviate_path(row.get('cwd'))}",
            "thread_count": row.get("thread_count", 0),
            "total_tokens": row.get("total_tokens", 0),
            "machine": source.get("host"),
        }
        for source in active_remote_sources
        for row in source.get("cwds", [])
    ]
    aggregate_workspaces = sorted(aggregate_workspaces, key=lambda row: row["total_tokens"], reverse=True)
    for row in aggregate_workspaces:
        row["avg_tokens"] = round(row["total_tokens"] / row["thread_count"], 0) if row["thread_count"] else 0
        row["share_pct"] = share_pct(row["total_tokens"], combined_total_tokens)
    machine_breakdown = [
        {
            "label": "Local machine",
            "host": "local",
            "status": "configured",
            "thread_count": codex.get("thread_count", 0),
            "total_tokens": codex.get("total", 0),
            "tokens_30d": codex.get("last_30_days", 0),
            "tokens_7d": codex.get("last_7_days", 0),
            "monthly": codex.get("monthly", []),
            "daily": codex.get("daily", []),
        }
    ] + [
        {
            "label": source.get("label"),
            "host": source.get("host"),
            "status": source.get("status"),
            "thread_count": source.get("thread_count"),
            "total_tokens": source.get("total_tokens"),
            "tokens_30d": source.get("tokens_30d"),
            "tokens_7d": source.get("tokens_7d"),
            "monthly": source.get("monthly", []),
            "daily": source.get("daily", []),
            "cache_notice": source.get("cache_notice"),
            "error": source.get("error"),
        }
        for source in remote_sources
    ]
    fleet = {
        "machine_count": 1 + len(active_remote_sources),
        "reachable_remote_count": len(active_remote_sources),
        "configured_remote_count": len(remote_sources),
        "combined_total_tokens": combined_total_tokens,
        "combined_thread_count": combined_thread_count,
        "combined_30d_tokens": combined_30d,
        "combined_7d_tokens": combined_7d,
        "combined_monthly": combined_monthly,
        "combined_daily": combined_daily,
        "cost": build_cost_summary(combined_total_tokens, combined_monthly, pricing),
        "models": aggregate_models,
        "workspaces": aggregate_workspaces,
        "machines": machine_breakdown,
    }
    configured = sum(1 for source in (codex,) if source["status"] == "configured")
    return {
        "generated_at": utc_now(),
        "coverage": {
            "configured_sources": configured + len(active_remote_sources),
            "total_sources": 1 + len(remote_sources),
            "tokens_sources": ["codex"],
            "db_name": Path(codex["db_path"]).name if codex.get("db_path") else None,
            "detailed_usage_available": codex.get("data_quality", {}).get("detailed_usage_available"),
        },
        "source": codex,
        "remote_sources": remote_sources,
        "fleet": fleet,
    }


class TokenUsageHandler(BaseHTTPRequestHandler):
    server_version = "TokenUsageDashboard/0.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/":
            return self.serve_static("index.html", "text/html; charset=utf-8")
        if route == "/favicon.ico":
            return self.serve_static("favicon.svg", "image/svg+xml")
        if route.startswith("/static/"):
            name = route[len("/static/") :]
            content_type = "text/plain; charset=utf-8"
            if name.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            elif name.endswith(".js"):
                content_type = "application/javascript; charset=utf-8"
            elif name.endswith(".svg"):
                content_type = "image/svg+xml"
            return self.serve_static(name, content_type)
        if route == "/api/dashboard":
            return json_response(self, build_dashboard())
        return json_response(
            self,
            {"error": f"Route '{route}' was not found."},
            status=HTTPStatus.NOT_FOUND,
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route.startswith("/api/import/"):
                provider = route.rsplit("/", 1)[-1]
                if provider not in MANUAL_PROVIDERS:
                    raise ValueError(f"Unsupported provider '{provider}'.")
                payload = read_request_json(self)
                saved = save_manual_source(provider, payload)
                return json_response(self, {"ok": True, "source": saved})
            if route == "/api/pricing/codex":
                payload = read_request_json(self)
                saved = save_codex_pricing(payload)
                return json_response(self, {"ok": True, "pricing": saved})
            if route.startswith("/api/clear/"):
                provider = route.rsplit("/", 1)[-1]
                if provider == "codex-pricing":
                    clear_codex_pricing()
                    return json_response(self, {"ok": True})
                if provider not in MANUAL_PROVIDERS:
                    raise ValueError(f"Unsupported provider '{provider}'.")
                clear_manual_source(provider)
                return json_response(self, {"ok": True})
        except ValueError as exc:
            return json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError as exc:
            return json_response(
                self,
                {"error": f"Invalid JSON body: {exc.msg}"},
                status=HTTPStatus.BAD_REQUEST,
            )
        return json_response(
            self,
            {"error": f"Route '{route}' was not found."},
            status=HTTPStatus.NOT_FOUND,
        )

    def serve_static(self, name: str, content_type: str):
        path = (STATIC_DIR / name).resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return json_response(
                self,
                {"error": "Invalid static path."},
                status=HTTPStatus.BAD_REQUEST,
            )
        if not path.exists() or not path.is_file():
            return json_response(
                self,
                {"error": f"Static file '{name}' was not found."},
                status=HTTPStatus.NOT_FOUND,
            )
        return text_response(self, read_text(path), content_type)

    def log_message(self, fmt, *args):
        return


def main():
    DATA_DIR.mkdir(exist_ok=True)
    host = os.environ.get("TOKEN_USAGE_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("TOKEN_USAGE_DASHBOARD_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), TokenUsageHandler)
    print(f"Token Usage Dashboard listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
