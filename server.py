#!/usr/bin/env python3
import csv
import glob
import json
import os
import re
import shlex
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from usage_accounting import (
    aggregate_events as aggregate_logical_events,
    discover_rollouts,
    group_events as group_logical_events,
    scan_rollouts,
)


APP_ROOT = Path(__file__).resolve().parent
STATIC_DIR = APP_ROOT / "static"
DATA_DIR = APP_ROOT / "data"
HOME = Path.home()
VS_CODE_GLOBAL_STORAGE = HOME / "Library/Application Support/Code/User/globalStorage"
VS_CODE_STATE_DB = VS_CODE_GLOBAL_STORAGE / "state.vscdb"
DOWNLOADS_DIR = HOME / "Downloads"
CODEX_SESSIONS_DIR = HOME / ".codex" / "sessions"
CODEX_APP_SNAPSHOT_FILE = DATA_DIR / "codex_app_sessions.json"
TOKEN_TYPE_FIELDS = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "non_reasoning_output_tokens",
    "unclassified_tokens",
)
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
MANUAL_PROVIDERS = {
    "chatgpt": "ChatGPT",
    "copilot": "GitHub Copilot",
}
OFFICIAL_MODEL_PRICING = {
    "gpt-5.6-sol": (5.00, 0.50, 30.00),
    "gpt-5.6-terra": (2.50, 0.25, 15.00),
    "gpt-5.6-luna": (1.00, 0.10, 6.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
    "gpt-5.2-codex": (1.75, 0.175, 14.00),
    "gpt-5.1-codex-max": (1.25, 0.125, 10.00),
    "gpt-5.1-codex": (1.25, 0.125, 10.00),
    "gpt-5.1-codex-mini": (0.25, 0.025, 2.00),
    "gpt-5-codex": (1.25, 0.125, 10.00),
}
OFFICIAL_CODEX_PRICING = {
    "kind": "official_api_equivalent",
    "currency": "USD",
    "label": "OpenAI API-equivalent estimate from measured token types",
    "rates_updated_at": "2026-07-27",
    "notes": (
        "Uses official per-model API input, cached-input, and output prices. "
        "This is not a Codex subscription invoice. Long-context uplifts, cache-write charges, "
        "regional processing, tool fees, and tokens without a public model price are excluded."
    ),
}
REMOTE_CODEX_HOSTS = [
    {
        "host": "e122378.arm.com",
        "label": "Ubuntu desktop",
    },
    {
        "host": "vscode-login3.hpc01.eu03.arm.com",
        "fallback_hosts": ["vscode-login4.hpc01.eu03.arm.com"],
        "display_host": "vscode-login3 + vscode-login4",
        "label": "EU03 VS Code cluster (shared)",
    },
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
    handler.send_header("Cache-Control", "no-cache")
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
    display_host = host_config.get("display_host", host)
    query_hosts = [host] + list(host_config.get("fallback_hosts") or [])
    cache_path = remote_snapshot_file(host)
    errors = []
    payload = None
    queried_host = None
    for candidate in query_hosts:
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
                    candidate,
                    f"python3 -c {shlex.quote(remote_query_script())}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
            payload = json.loads(completed.stdout.strip() or "{}")
            queried_host = candidate
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            errors.append(f"{candidate}: {stderr.strip() or str(exc)}")

    if payload is not None:
        totals = payload.get("totals") or {}
        snapshot = {
            "host": display_host,
            "queried_host": queried_host,
            "configured_hosts": query_hosts,
            "label": label,
            "status": payload.get("status", "configured"),
            "db_path": payload.get("db_path"),
            "thread_count": totals.get("thread_count", 0),
            "raw_thread_count": totals.get("raw_thread_count", totals.get("thread_count", 0)),
            "deduplicated_subagent_count": totals.get("deduplicated_subagent_count", 0),
            "raw_usage_event_count": totals.get("raw_usage_event_count", 0),
            "unique_usage_event_count": totals.get("unique_usage_event_count", 0),
            "replayed_usage_event_count": totals.get("replayed_usage_event_count", 0),
            "missing_last_usage_count": totals.get("missing_last_usage_count", 0),
            "total_tokens": totals.get("total_tokens", 0),
            "tokens_30d": totals.get("tokens_30d", 0),
            "tokens_7d": totals.get("tokens_7d", 0),
            "first_seen_local": format_local_timestamp(totals.get("first_seen")),
            "last_seen_local": format_local_timestamp(totals.get("last_seen")),
            "token_usage": payload.get("token_usage", {}),
            "monthly": payload.get("monthly", []),
            "daily": payload.get("daily", []),
            "models": payload.get("models", []),
            "model_months": payload.get("model_months", []),
            "cwds": payload.get("cwds", []),
            "fetched_at": utc_now(),
        }
        cache_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return snapshot

    error_text = " | ".join(errors)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        cached["status"] = "cached"
        cached["cache_notice"] = tidy_title(
            f"Live SSH refresh failed, showing cached snapshot instead. {error_text}",
            limit=220,
        )
        return cached
    return {
        "host": display_host,
        "queried_host": None,
        "configured_hosts": query_hosts,
        "label": label,
        "status": "error",
        "error": tidy_title(error_text, limit=180),
        "thread_count": None,
        "total_tokens": None,
        "tokens_30d": None,
        "tokens_7d": None,
        "monthly": [],
        "daily": [],
        "models": [],
        "model_months": [],
        "cwds": [],
    }


def remote_query_script() -> str:
    return r'''
import json, os, re, sqlite3, time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

FIELDS = (
  "total_tokens", "input_tokens", "cached_input_tokens", "uncached_input_tokens",
  "output_tokens", "reasoning_output_tokens", "non_reasoning_output_tokens",
  "unclassified_tokens",
)

def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

def normalize(raw=None, fallback=0):
    raw = raw or {}
    input_tokens = integer(raw.get("input_tokens"))
    cached_tokens = min(integer(raw.get("cached_input_tokens")), input_tokens)
    output_tokens = integer(raw.get("output_tokens"))
    reasoning_tokens = min(integer(raw.get("reasoning_output_tokens")), output_tokens)
    total_tokens = integer(raw.get("total_tokens")) or integer(fallback)
    typed = any(key in raw for key in (
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"
    ))
    classified = input_tokens + output_tokens if typed else 0
    return {
      "total_tokens": total_tokens,
      "input_tokens": input_tokens,
      "cached_input_tokens": cached_tokens,
      "uncached_input_tokens": max(input_tokens - cached_tokens, 0),
      "output_tokens": output_tokens,
      "reasoning_output_tokens": reasoning_tokens,
      "non_reasoning_output_tokens": max(output_tokens - reasoning_tokens, 0),
      "unclassified_tokens": max(total_tokens - classified, 0),
      "has_token_types": typed,
    }

def scale(raw, target):
    target = max(integer(target), 0)
    usage = normalize(raw, target)
    if not usage["has_token_types"] or not usage["total_tokens"]:
        return normalize(None, target)
    ratio = target / usage["total_tokens"]
    input_tokens = round(usage["input_tokens"] * ratio)
    output_tokens = round(usage["output_tokens"] * ratio)
    overflow = max(input_tokens + output_tokens - target, 0)
    if overflow:
        reduction = min(output_tokens, overflow)
        output_tokens -= reduction
        input_tokens -= overflow - reduction
    return normalize({
      "total_tokens": target,
      "input_tokens": input_tokens,
      "cached_input_tokens": min(round(usage["cached_input_tokens"] * ratio), input_tokens),
      "output_tokens": output_tokens,
      "reasoning_output_tokens": min(round(usage["reasoning_output_tokens"] * ratio), output_tokens),
    })

def latest(path):
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            remainder = b""
            while position > 0:
                size = min(65536, position)
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + remainder).split(b"\n")
                remainder = lines[0] if position else b""
                for line in reversed(lines[1:] if position else lines):
                    if b'"token_count"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    payload = event.get("payload") or {}
                    if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                        usage = (payload.get("info") or {}).get("total_token_usage")
                        if isinstance(usage, dict):
                            return normalize(usage)
    except OSError:
        return None
    return None

def local_day(value):
    if not value:
        return None
    text = str(value).replace("+00:00", "Z")
    for pattern in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            return parsed.astimezone().strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

def counter_events(path):
    events = []
    if not path:
        return events
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                payload = event.get("payload") or {}
                usage = (payload.get("info") or {}).get("total_token_usage")
                day = local_day(event.get("timestamp"))
                if event.get("type") == "event_msg" and payload.get("type") == "token_count" and isinstance(usage, dict) and integer(usage.get("total_tokens")) > 0 and day:
                    events.append({"day": day, "usage": normalize(usage)})
    except OSError:
        return []
    return events

def allocate_daily(events, target, fallback_day):
    target = max(integer(target), 0)
    if not target:
        return []
    segment_start = 0
    previous_total = 0
    for index, event in enumerate(events):
        current_total = event["usage"]["total_tokens"]
        if current_total < previous_total:
            segment_start = index
        previous_total = current_total
    events = events[segment_start:]
    previous = normalize(None, 0)
    buckets = {}
    for event in events:
        current = event["usage"]
        current_total = min(current["total_tokens"], target)
        previous_total = min(previous["total_tokens"], target)
        eligible = max(current_total - previous_total, 0)
        if eligible:
            interval = {
              "total_tokens": max(current["total_tokens"] - previous["total_tokens"], 0),
              "input_tokens": max(current["input_tokens"] - previous["input_tokens"], 0),
              "cached_input_tokens": max(current["cached_input_tokens"] - previous["cached_input_tokens"], 0),
              "output_tokens": max(current["output_tokens"] - previous["output_tokens"], 0),
              "reasoning_output_tokens": max(current["reasoning_output_tokens"] - previous["reasoning_output_tokens"], 0),
            }
            usage = scale(interval, eligible)
            bucket = buckets.setdefault(event["day"], {key: 0 for key in (
              "total_tokens", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"
            )})
            for key in bucket:
                bucket[key] += usage[key]
        previous = current
    if not buckets:
        return [{"day": fallback_day, "usage": scale(events[-1]["usage"] if events else None, target)}]
    raw_total = sum(bucket["total_tokens"] for bucket in buckets.values())
    allocations = {}
    fractions = []
    allocated = 0
    for day, bucket in buckets.items():
        exact = target * bucket["total_tokens"] / raw_total
        whole = int(exact)
        allocations[day] = whole
        allocated += whole
        fractions.append((exact - whole, day))
    for _, day in sorted(fractions, key=lambda item: (-item[0], item[1]))[:target - allocated]:
        allocations[day] += 1
    return [
      {"day": day, "usage": scale(buckets[day], allocations[day])}
      for day in sorted(buckets) if allocations[day] > 0
    ]

def rollout_daily(path, target, stamp):
    fallback_day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    match = re.search(r"[/\\]sessions[/\\](\d{4})[/\\](\d{2})[/\\](\d{2})[/\\]", str(path or ""))
    start_day = "-".join(match.groups()) if match else None
    if start_day == fallback_day:
        return [{"day": fallback_day, "usage": scale(latest(path), target)}]
    return allocate_daily(counter_events(path), target, fallback_day)

def aggregate(records):
    totals = {key: 0 for key in FIELDS}
    session_ids = set()
    typed_session_ids = set()
    for record in records:
        usage = record["usage"]
        for key in FIELDS:
            totals[key] += integer(usage.get(key))
        session_id = record.get("session_id")
        if session_id:
            session_ids.add(session_id)
            if usage.get("has_token_types"):
                typed_session_ids.add(session_id)
    totals["thread_count"] = len(session_ids) if session_ids else len(records)
    totals["typed_thread_count"] = len(typed_session_ids) if session_ids else sum(
        int(bool(record["usage"].get("has_token_types"))) for record in records
    )
    totals["detail_coverage_pct"] = (
        100.0 * totals["typed_thread_count"] / totals["thread_count"]
        if totals["thread_count"] else 0.0
    )
    return totals

def grouped(records, key):
    groups = {}
    for record in records:
        groups.setdefault(record[key], []).append(record)
    result = []
    for label, values in groups.items():
        row = {key: label}
        row.update(aggregate(values))
        row["value"] = row["total_tokens"]
        result.append(row)
    return result

def attach(base_rows, detail_rows, key):
    details = {row[key]: row for row in detail_rows}
    result = []
    for base in base_rows:
        row = dict(base)
        detail = details.get(row.get(key))
        if detail is None:
            detail = normalize(None, row.get("value", 0))
            detail.update(
              thread_count=integer(row.get("thread_count")),
              typed_thread_count=0,
              detail_coverage_pct=0.0,
            )
        for field in FIELDS + ("typed_thread_count", "detail_coverage_pct"):
            row[field] = detail.get(field, 0)
        result.append(row)
    return result

def parent_id(row):
    try:
        source = json.loads(row.get("source") or "")
    except (TypeError, ValueError):
        return None
    return (((source.get("subagent") or {}).get("thread_spawn") or {}).get("parent_thread_id"))

def accounting_rows(rows):
    by_id = {row["id"]: row for row in rows}
    groups = {}
    for row in rows:
        current = row
        seen = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            parent = by_id.get(parent_id(current))
            if not parent:
                break
            current = parent
        groups.setdefault(current["id"], []).append(row)
    result = []
    for root_id, members in groups.items():
        root = by_id[root_id]
        counter = max(members, key=lambda row: integer(row.get("tokens_used")))
        representative = dict(root)
        for key in ("tokens_used", "rollout_path", "updated_at"):
            representative[key] = counter.get(key)
        representative["tree_member_count"] = len(members)
        result.append(representative)
    return result

def grouped_many(records, keys):
    groups = {}
    for record in records:
        label = tuple(record.get(key) for key in keys)
        groups.setdefault(label, []).append(record)
    result = []
    for label, values in groups.items():
        row = dict(zip(keys, label))
        row.update(aggregate(values))
        result.append(row)
    return result

def monthly_records(records):
    groups = {}
    for record in records:
        groups.setdefault((record["session_id"], record["day"][:7]), []).append(record)
    result = []
    for (_, month), values in groups.items():
        result.append({
          "month": month,
          "model": values[0]["model"],
          "cwd": values[0]["cwd"],
          "usage": aggregate(values),
        })
    return result

def rollout_paths(rows):
    paths = set()
    for directory in (Path.home() / ".codex" / "sessions", Path.home() / ".codex" / "archived_sessions"):
        if directory.exists():
            paths.update(directory.glob("**/rollout-*.jsonl"))
    paths.update(Path(row["rollout_path"]) for row in rows if row.get("rollout_path"))
    return sorted(paths)

def event_epoch(value):
    if not value:
        return None
    text = str(value).replace("+00:00", "Z")
    for pattern in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None

def fingerprint(raw):
    return tuple(integer((raw or {}).get(key)) for key in (
      "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
      "output_tokens", "reasoning_output_tokens", "total_tokens",
    ))

TOKEN_USAGE_PATTERN = re.compile(
  r'"total_token_usage"\s*:\s*(\{[^{}]*\}).*?'
  r'"last_token_usage"\s*:\s*(\{[^{}]*\})'
)
TIMESTAMP_PATTERN = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')

def scan_logical_events_chunk(paths, metadata):
    unique = {}
    sessions = set()
    raw_count = 0
    missing_last = 0
    local_timezone = datetime.now().astimezone().tzinfo
    for path in paths:
        meta = {}
        context = {}
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not any(marker in line for marker in ('"session_meta"', '"turn_context"', '"token_count"')):
                    continue
                if '"token_count"' in line:
                    usage_match = TOKEN_USAGE_PATTERN.search(line)
                    timestamp_match = TIMESTAMP_PATTERN.search(line)
                    if not usage_match or not timestamp_match:
                        continue
                    try:
                        cumulative = json.loads(usage_match.group(1))
                        increment = json.loads(usage_match.group(2))
                    except Exception:
                        continue
                    if integer(cumulative.get("total_tokens")) <= 0:
                        continue
                    raw_count += 1
                    if integer(increment.get("total_tokens")) <= 0:
                        missing_last += 1
                        continue
                    session_id = str(meta.get("id") or Path(path).stem)
                    key = (session_id, fingerprint(cumulative))
                    previous = unique.get(key)
                    timestamp_text = timestamp_match.group(1)
                    if previous is not None and timestamp_text >= previous["timestamp_text"]:
                        continue
                    stamp = event_epoch(timestamp_text)
                    if stamp is None:
                        continue
                    sqlite_meta = metadata.get(session_id) or {}
                    local = datetime.fromtimestamp(stamp, local_timezone)
                    unique[key] = {
                      "event_key": key,
                      "session_id": session_id,
                      "day": local.strftime("%Y-%m-%d"),
                      "month": local.strftime("%Y-%m"),
                      "timestamp": stamp,
                      "timestamp_text": timestamp_text,
                      "model": context.get("model") or sqlite_meta.get("model") or "(unknown)",
                      "cwd": context.get("cwd") or meta.get("cwd") or sqlite_meta.get("cwd") or "(unknown)",
                      "usage": normalize(increment),
                    }
                    sessions.add(session_id)
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "session_meta":
                    meta.update(payload)
                    continue
                if event.get("type") == "turn_context":
                    for field in ("model", "cwd", "effort"):
                        if payload.get(field) is not None:
                            context[field] = payload.get(field)
                    continue
    events = sorted(unique.values(), key=lambda row: row["timestamp"])
    return events, sessions, raw_count, missing_last

def scan_logical_events(paths, metadata):
    worker_count = min(4, len(paths))
    if worker_count < 2:
        results = [scan_logical_events_chunk(paths, metadata)]
    else:
        chunks = [paths[index::worker_count] for index in range(worker_count)]
        try:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(scan_logical_events_chunk, chunks, [metadata] * worker_count))
        except Exception:
            results = [scan_logical_events_chunk(paths, metadata)]
    unique = {}
    sessions = set()
    raw_count = 0
    missing_last = 0
    for events, chunk_sessions, chunk_raw, chunk_missing in results:
        raw_count += chunk_raw
        missing_last += chunk_missing
        sessions.update(chunk_sessions)
        for event in events:
            key = tuple(event["event_key"])
            previous = unique.get(key)
            if previous is None or event["timestamp_text"] < previous["timestamp_text"]:
                unique[key] = event
    events = sorted(unique.values(), key=lambda row: row["timestamp"])
    for event in events:
        event.pop("event_key", None)
        event.pop("timestamp_text", None)
    return events, sessions, raw_count, missing_last

dbs = sorted(Path.home().glob('.codex/state_*.sqlite'))
if not dbs:
    print(json.dumps({"status": "missing", "error": "No ~/.codex/state_*.sqlite found"}))
    raise SystemExit(0)

db_path = str(dbs[-1])
db_uri = Path(db_path).resolve().as_uri() + '?mode=ro&immutable=1'
con = sqlite3.connect(db_uri, uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()
raw_rows = [dict(r) for r in cur.execute("""
SELECT id, source, COALESCE(model, '(unknown)') AS model, cwd,
       tokens_used, rollout_path, updated_at
FROM threads
""").fetchall()]
metadata = {
  str(row["id"]): {
    "model": row.get("model") or "(unknown)",
    "cwd": row.get("cwd") or "(unknown)",
  }
  for row in raw_rows
}
paths = rollout_paths(raw_rows)
events, session_ids, raw_event_count, missing_last_count = scan_logical_events(paths, metadata)
now = time.time()
totals = {
  "thread_count": len(session_ids),
  "raw_thread_count": len(raw_rows),
  "deduplicated_subagent_count": max(len(raw_rows) - len(session_ids), 0),
  "raw_usage_event_count": raw_event_count,
  "unique_usage_event_count": len(events),
  "replayed_usage_event_count": raw_event_count - missing_last_count - len(events),
  "missing_last_usage_count": missing_last_count,
  "total_tokens": sum(record["usage"]["total_tokens"] for record in events),
  "tokens_30d": 0,
  "tokens_7d": 0,
  "first_seen": min((record["timestamp"] for record in events), default=None),
  "last_seen": max((record["timestamp"] for record in events), default=None),
}
records = events
daily_records = events
month_records = events
cutoff_30 = time.strftime("%Y-%m-%d", time.localtime(now - 30 * 86400))
cutoff_7 = time.strftime("%Y-%m-%d", time.localtime(now - 7 * 86400))
totals["tokens_30d"] = sum(record["usage"]["total_tokens"] for record in daily_records if record["day"] >= cutoff_30)
totals["tokens_7d"] = sum(record["usage"]["total_tokens"] for record in daily_records if record["day"] >= cutoff_7)
token_usage = aggregate(records)
monthly = sorted(grouped(month_records, "month"), key=lambda row: row["month"], reverse=True)[:12]
daily = sorted(grouped(daily_records, "day"), key=lambda row: row["day"])
models = sorted(grouped(records, "model"), key=lambda row: row["total_tokens"], reverse=True)
cwds = sorted(grouped(records, "cwd"), key=lambda row: row["total_tokens"], reverse=True)[:6]
model_months = grouped_many(month_records, ("month", "model"))
print(json.dumps({
  "status": "configured",
  "db_path": db_path,
  "totals": totals,
  "token_usage": token_usage,
  "monthly": monthly,
  "daily": daily,
  "models": models,
  "model_months": model_months,
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
            usage = normalize_token_usage(row, row.get(value_name, 0))
            for field in TOKEN_TYPE_FIELDS:
                bucket[field] = bucket.get(field, 0) + usage[field]
            bucket["typed_thread_count"] = bucket.get("typed_thread_count", 0) + int(
                row.get("typed_thread_count", 0) or 0
            )
    result = sorted(merged.values(), key=month_key, reverse=True)
    for row in result:
        row["detail_coverage_pct"] = (
            round(100.0 * row["typed_thread_count"] / row["thread_count"], 2)
            if row["thread_count"]
            else 0.0
        )
    return result


def aggregate_daily_series(series_list, key_name="day", value_name="value"):
    merged = {}
    for rows in series_list:
        for row in rows or []:
            key = row[key_name]
            bucket = merged.setdefault(key, {"day": key, "value": 0, "thread_count": 0})
            bucket["value"] += row.get(value_name, 0) or 0
            bucket["thread_count"] += row.get("thread_count", 0) or 0
            usage = normalize_token_usage(row, row.get(value_name, 0))
            for field in TOKEN_TYPE_FIELDS:
                bucket[field] = bucket.get(field, 0) + usage[field]
            bucket["typed_thread_count"] = bucket.get("typed_thread_count", 0) + int(
                row.get("typed_thread_count", 0) or 0
            )
    result = sorted(merged.values(), key=day_key)
    for row in result:
        row["detail_coverage_pct"] = (
            round(100.0 * row["typed_thread_count"] / row["thread_count"], 2)
            if row["thread_count"]
            else 0.0
        )
    return result


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
        for field in TOKEN_TYPE_FIELDS[1:]:
            bucket[field] = bucket.get(field, 0) + int(row.get(field, 0) or 0)
        bucket["typed_thread_count"] = bucket.get("typed_thread_count", 0) + int(
            row.get("typed_thread_count", 0) or 0
        )
    result = sorted(merged.values(), key=lambda row: row["total_tokens"], reverse=True)
    for row in result:
        row["avg_tokens"] = round(row["total_tokens"] / row["thread_count"], 0) if row["thread_count"] else 0
        row["detail_coverage_pct"] = (
            round(100.0 * row["typed_thread_count"] / row["thread_count"], 2)
            if row["thread_count"]
            else 0.0
        )
    return result


def merge_token_breakdown(rows, extra_rows, key_name: str, limit=None):
    merged = {}
    for row in list(rows or []) + list(extra_rows or []):
        label = row.get(key_name)
        if not label:
            continue
        bucket = merged.setdefault(label, {key_name: label, "thread_count": 0, "total_tokens": 0})
        bucket["thread_count"] += row.get("thread_count", 0) or 0
        bucket["total_tokens"] += row.get("total_tokens", 0) or 0
    result = sorted(merged.values(), key=lambda row: row["total_tokens"], reverse=True)
    for row in result:
        row["avg_tokens"] = round(row["total_tokens"] / row["thread_count"], 0) if row["thread_count"] else 0
    if limit:
        return result[:limit]
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


def build_cost_summary(total, monthly, pricing, token_usage=None, models=None, model_months=None):
    if not pricing:
        return None

    if pricing.get("kind") == "official_api_equivalent":
        def price_row(row):
            model = row.get("model") or row.get("label") or "(unknown)"
            usage = normalize_token_usage(row, row.get("total_tokens", 0))
            rates = OFFICIAL_MODEL_PRICING.get(model)
            if not rates:
                return {
                    "model": model,
                    "total_tokens": usage["total_tokens"],
                    "priced_tokens": 0,
                    "unpriced_tokens": usage["total_tokens"],
                    "cost_usd": 0.0,
                    "uncached_input_cost_usd": 0.0,
                    "cached_input_cost_usd": 0.0,
                    "output_cost_usd": 0.0,
                    "rates": None,
                }
            input_rate, cached_rate, output_rate = rates
            uncached_cost = usage["uncached_input_tokens"] / 1_000_000.0 * input_rate
            cached_cost = usage["cached_input_tokens"] / 1_000_000.0 * cached_rate
            output_cost = usage["output_tokens"] / 1_000_000.0 * output_rate
            priced_tokens = (
                usage["uncached_input_tokens"]
                + usage["cached_input_tokens"]
                + usage["output_tokens"]
            )
            return {
                "model": model,
                "total_tokens": usage["total_tokens"],
                "priced_tokens": priced_tokens,
                "unpriced_tokens": max(usage["total_tokens"] - priced_tokens, 0),
                "cost_usd": uncached_cost + cached_cost + output_cost,
                "uncached_input_cost_usd": uncached_cost,
                "cached_input_cost_usd": cached_cost,
                "output_cost_usd": output_cost,
                "rates": {
                    "input_usd_per_million": input_rate,
                    "cached_input_usd_per_million": cached_rate,
                    "output_usd_per_million": output_rate,
                },
            }

        priced_models = [price_row(row) for row in (models or [])]
        priced_tokens = sum(row["priced_tokens"] for row in priced_models)
        unpriced_tokens = max(int(total or 0) - priced_tokens, 0)
        total_cost = sum(row["cost_usd"] for row in priced_models)
        cost_parts = {
            "uncached_input_cost_usd": sum(row["uncached_input_cost_usd"] for row in priced_models),
            "cached_input_cost_usd": sum(row["cached_input_cost_usd"] for row in priced_models),
            "output_cost_usd": sum(row["output_cost_usd"] for row in priced_models),
        }
        monthly_costs = {}
        for row in model_months or []:
            priced = price_row(row)
            month = row.get("month")
            bucket = monthly_costs.setdefault(
                month,
                {"month": month, "api_equivalent_cost_usd": 0.0, "priced_tokens": 0, "total_tokens": 0},
            )
            bucket["api_equivalent_cost_usd"] += priced["cost_usd"]
            bucket["priced_tokens"] += priced["priced_tokens"]
            bucket["total_tokens"] += priced["total_tokens"]
        monthly_rows = sorted(monthly_costs.values(), key=lambda row: row["month"], reverse=True)
        for row in monthly_rows:
            row["api_equivalent_cost_usd"] = round(row["api_equivalent_cost_usd"], 2)
            row["pricing_coverage_pct"] = (
                round(100.0 * row["priced_tokens"] / row["total_tokens"], 2)
                if row["total_tokens"]
                else 0.0
            )

        for row in priced_models:
            for key in (
                "cost_usd",
                "uncached_input_cost_usd",
                "cached_input_cost_usd",
                "output_cost_usd",
            ):
                row[key] = round(row[key], 2)
        priced_models.sort(key=lambda row: row["cost_usd"], reverse=True)
        return {
            "currency": pricing["currency"],
            "label": pricing["label"],
            "kind": pricing["kind"],
            "notes": pricing.get("notes"),
            "rates_updated_at": pricing.get("rates_updated_at"),
            "api_equivalent_cost_total_usd": round(total_cost, 2),
            "latest_month_api_equivalent_cost_usd": (
                monthly_rows[0]["api_equivalent_cost_usd"] if monthly_rows else None
            ),
            "priced_tokens": priced_tokens,
            "unpriced_tokens": unpriced_tokens,
            "pricing_coverage_pct": (
                round(100.0 * priced_tokens / int(total or 0), 2) if total else 0.0
            ),
            "effective_rate_per_million": (
                round(total_cost * 1_000_000.0 / priced_tokens, 4) if priced_tokens else None
            ),
            **{key: round(value, 2) for key, value in cost_parts.items()},
            "model_rows": priced_models,
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


def parse_iso_datetime(value: str):
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def iso_to_local_timestamp(value: str):
    parsed = parse_iso_datetime(value)
    if not parsed:
        return None
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def iso_to_local_epoch(value: str):
    parsed = parse_iso_datetime(value)
    if not parsed:
        return None
    return int(parsed.timestamp())


def iso_to_local_day(value: str):
    parsed = parse_iso_datetime(value)
    if not parsed:
        return None
    return parsed.astimezone().strftime("%Y-%m-%d")


def counter_int(counter, key: str) -> int:
    try:
        return int((counter or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def normalize_token_usage(raw=None, fallback_total=0):
    raw = raw or {}
    input_tokens = counter_int(raw, "input_tokens")
    cached_input_tokens = counter_int(raw, "cached_input_tokens")
    output_tokens = counter_int(raw, "output_tokens")
    reasoning_output_tokens = counter_int(raw, "reasoning_output_tokens")
    total_tokens = counter_int(raw, "total_tokens") or int(fallback_total or 0)
    has_token_types = any(
        key in raw
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
    )
    classified_tokens = input_tokens + output_tokens if has_token_types else 0
    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_input_tokens": min(cached_input_tokens, input_tokens),
        "uncached_input_tokens": max(input_tokens - cached_input_tokens, 0),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": min(reasoning_output_tokens, output_tokens),
        "non_reasoning_output_tokens": max(output_tokens - reasoning_output_tokens, 0),
        "unclassified_tokens": max(total_tokens - classified_tokens, 0),
        "has_token_types": has_token_types,
    }


def scale_token_usage(raw, target_total):
    target_total = max(int(target_total or 0), 0)
    usage = normalize_token_usage(raw, target_total)
    if not usage["has_token_types"] or not usage["total_tokens"]:
        return normalize_token_usage(None, target_total)

    ratio = target_total / usage["total_tokens"]
    input_tokens = round(usage["input_tokens"] * ratio)
    output_tokens = round(usage["output_tokens"] * ratio)
    overflow = max(input_tokens + output_tokens - target_total, 0)
    if overflow:
        output_reduction = min(output_tokens, overflow)
        output_tokens -= output_reduction
        input_tokens -= overflow - output_reduction
    cached_input_tokens = min(round(usage["cached_input_tokens"] * ratio), input_tokens)
    reasoning_output_tokens = min(round(usage["reasoning_output_tokens"] * ratio), output_tokens)
    return normalize_token_usage(
        {
            "total_tokens": target_total,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
        }
    )


def latest_rollout_usage(path: str):
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            remainder = b""
            while position > 0:
                size = min(64 * 1024, position)
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + remainder).split(b"\n")
                remainder = lines[0] if position else b""
                candidates = lines[1:] if position else lines
                for line in reversed(candidates):
                    if b'"token_count"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    payload = event.get("payload") or {}
                    if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                        continue
                    usage = (payload.get("info") or {}).get("total_token_usage")
                    if isinstance(usage, dict):
                        return normalize_token_usage(usage)
    except (OSError, ValueError):
        return None
    return None


def rollout_counter_events(path: str):
    events = []
    if not path:
        return events
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                usage = (payload.get("info") or {}).get("total_token_usage")
                timestamp = event.get("timestamp")
                if isinstance(usage, dict) and counter_int(usage, "total_tokens") > 0 and timestamp:
                    events.append({"timestamp": timestamp, "usage": normalize_token_usage(usage)})
    except OSError:
        return []
    return events


def allocate_daily_counter_usage(events, target_total, baseline_total=0, fallback_day=None):
    target_total = max(int(target_total or 0), 0)
    baseline_total = max(int(baseline_total or 0), 0)
    if target_total <= 0:
        return []

    normalized_events = []
    for event in events or []:
        day = iso_to_local_day(event.get("timestamp"))
        usage = normalize_token_usage(event.get("usage"))
        if day and usage["total_tokens"] > 0:
            normalized_events.append({"day": day, "usage": usage})

    # A decreasing absolute counter starts a new counter segment. The final
    # segment is authoritative because SQLite and snapshot totals track it.
    segment_start = 0
    previous_total = 0
    for index, event in enumerate(normalized_events):
        current_total = event["usage"]["total_tokens"]
        if current_total < previous_total:
            segment_start = index
        previous_total = current_total
    normalized_events = normalized_events[segment_start:]

    upper_total = baseline_total + target_total
    previous = normalize_token_usage(None, 0)
    buckets = {}
    for event in normalized_events:
        current = event["usage"]
        previous_total = previous["total_tokens"]
        current_total = current["total_tokens"]
        if current_total < previous_total:
            previous = normalize_token_usage(None, 0)
            previous_total = 0
        overlap_start = max(previous_total, baseline_total)
        overlap_end = min(current_total, upper_total)
        eligible_total = max(overlap_end - overlap_start, 0)
        if eligible_total:
            interval_total = max(current_total - previous_total, 0)
            interval_usage = {
                "total_tokens": interval_total,
                "input_tokens": max(current["input_tokens"] - previous["input_tokens"], 0),
                "cached_input_tokens": max(
                    current["cached_input_tokens"] - previous["cached_input_tokens"], 0
                ),
                "output_tokens": max(current["output_tokens"] - previous["output_tokens"], 0),
                "reasoning_output_tokens": max(
                    current["reasoning_output_tokens"] - previous["reasoning_output_tokens"], 0
                ),
            }
            usage = scale_token_usage(interval_usage, eligible_total)
            bucket = buckets.setdefault(
                event["day"],
                {
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            )
            for field in bucket:
                bucket[field] += usage[field]
        previous = current

    if not buckets:
        if not fallback_day:
            return []
        latest = normalized_events[-1]["usage"] if normalized_events else None
        return [{"day": fallback_day, "usage": scale_token_usage(latest, target_total)}]

    raw_total = sum(bucket["total_tokens"] for bucket in buckets.values())
    allocations = {}
    fractions = []
    allocated = 0
    for day, bucket in buckets.items():
        exact = target_total * bucket["total_tokens"] / raw_total
        whole = int(exact)
        allocations[day] = whole
        allocated += whole
        fractions.append((exact - whole, day))
    for _, day in sorted(fractions, key=lambda item: (-item[0], item[1]))[: target_total - allocated]:
        allocations[day] += 1

    return [
        {"day": day, "usage": scale_token_usage(buckets[day], allocations[day])}
        for day in sorted(buckets)
        if allocations[day] > 0
    ]


def rollout_start_day(path: str):
    match = re.search(r"[/\\]sessions[/\\](\d{4})[/\\](\d{2})[/\\](\d{2})[/\\]", str(path or ""))
    return "-".join(match.groups()) if match else None


def rollout_daily_usage(path: str, target_total, baseline_total=0, fallback_epoch=None):
    fallback_day = None
    if fallback_epoch:
        fallback_day = datetime.fromtimestamp(int(fallback_epoch)).astimezone().strftime("%Y-%m-%d")
    if rollout_start_day(path) == fallback_day and baseline_total == 0:
        return [
            {
                "day": fallback_day,
                "usage": scale_token_usage(latest_rollout_usage(path), target_total),
            }
        ]
    return allocate_daily_counter_usage(
        rollout_counter_events(path),
        target_total,
        baseline_total=baseline_total,
        fallback_day=fallback_day,
    )


def monthly_usage_records(daily_records):
    monthly = []
    grouped = {}
    for record in daily_records:
        month = record["day"][:7]
        grouped.setdefault(month, []).append(record)
    for month, records in grouped.items():
        monthly.append(
            {
                "month": month,
                "model": records[0].get("model") or "(unknown)",
                "thread_count": records[0].get("thread_count", 1),
                "usage": aggregate_token_usage(records),
            }
        )
    return monthly


def aggregate_token_usage(records):
    totals = {key: 0 for key in TOKEN_TYPE_FIELDS}
    thread_count = 0
    typed_thread_count = 0
    for record in records:
        usage = record.get("usage") or normalize_token_usage(None, record.get("tokens_used", 0))
        for key in TOKEN_TYPE_FIELDS:
            totals[key] += counter_int(usage, key)
        record_threads = int(record.get("thread_count", 1) or 0)
        thread_count += record_threads
        if usage.get("has_token_types"):
            typed_thread_count += record_threads
    totals["thread_count"] = thread_count
    totals["typed_thread_count"] = typed_thread_count
    totals["detail_coverage_pct"] = (
        round(100.0 * typed_thread_count / thread_count, 2) if thread_count else 0.0
    )
    return totals


def grouped_token_usage(records, key: str, reverse=False):
    grouped = {}
    for record in records:
        label = record.get(key)
        if not label:
            continue
        grouped.setdefault(label, []).append(record)
    result = []
    for label, values in grouped.items():
        row = {key: label}
        row.update(aggregate_token_usage(values))
        row["value"] = row["total_tokens"]
        result.append(row)
    return sorted(result, key=lambda row: row[key], reverse=reverse)


def grouped_model_month_usage(records):
    grouped = {}
    for record in records:
        model = record.get("model") or "(unknown)"
        month = record.get("month")
        if not month:
            continue
        grouped.setdefault((month, model), []).append(record)
    result = []
    for (month, model), values in grouped.items():
        row = {"month": month, "model": model}
        row.update(aggregate_token_usage(values))
        result.append(row)
    return sorted(result, key=lambda row: (row["month"], row["model"]), reverse=True)


def attach_token_usage(base_rows, detail_rows, key: str):
    details = {row[key]: row for row in detail_rows}
    attached = []
    for base in base_rows:
        row = dict(base)
        detail = details.get(row.get(key))
        if detail is None:
            detail = aggregate_token_usage(
                [
                    {
                        "thread_count": row.get("thread_count", 0),
                        "usage": normalize_token_usage(None, row.get("value", 0)),
                    }
                ]
            )
        for field in TOKEN_TYPE_FIELDS + ("typed_thread_count", "detail_coverage_pct"):
            row[field] = detail.get(field, 0)
        attached.append(row)
    return attached


def combine_token_usage_summaries(summaries):
    totals = {key: 0 for key in TOKEN_TYPE_FIELDS}
    thread_count = 0
    typed_thread_count = 0
    for summary in summaries:
        summary = summary or {}
        usage = normalize_token_usage(summary, summary.get("total_tokens", 0))
        for key in TOKEN_TYPE_FIELDS:
            totals[key] += counter_int(usage, key)
        thread_count += int(summary.get("thread_count", 0) or 0)
        typed_thread_count += int(summary.get("typed_thread_count", 0) or 0)
    totals["thread_count"] = thread_count
    totals["typed_thread_count"] = typed_thread_count
    totals["detail_coverage_pct"] = (
        round(100.0 * typed_thread_count / thread_count, 2) if thread_count else 0.0
    )
    return totals


def token_usage_or_unclassified(summary, total_tokens, thread_count):
    if summary:
        return summary
    usage = normalize_token_usage(None, total_tokens)
    usage["thread_count"] = int(thread_count or 0)
    usage["typed_thread_count"] = 0
    usage["detail_coverage_pct"] = 0.0
    return usage


def app_session_from_rollout(path: Path):
    meta = {}
    last_counter = None
    last_counter_at = None
    token_event_count = 0
    counter_events = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"session_meta"' not in line and '"token_count"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "session_meta":
                    payload = event.get("payload") or {}
                    meta = {
                        "id": payload.get("id"),
                        "cwd": payload.get("cwd"),
                        "originator": payload.get("originator"),
                        "source": payload.get("source"),
                        "thread_source": payload.get("thread_source"),
                        "cli_version": payload.get("cli_version"),
                        "model_provider": payload.get("model_provider"),
                        "created_at": payload.get("timestamp") or event.get("timestamp"),
                    }
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                usage = ((payload.get("info") or {}).get("total_token_usage")) or {}
                total = counter_int(usage, "total_tokens")
                if total <= 0:
                    continue
                token_event_count += 1
                last_counter = {
                    "input_tokens": counter_int(usage, "input_tokens"),
                    "cached_input_tokens": counter_int(usage, "cached_input_tokens"),
                    "output_tokens": counter_int(usage, "output_tokens"),
                    "reasoning_output_tokens": counter_int(usage, "reasoning_output_tokens"),
                    "total_tokens": total,
                }
                last_counter_at = event.get("timestamp")
                counter_events.append({"timestamp": last_counter_at, "usage": last_counter})
    except OSError:
        return None

    if not last_counter:
        return None

    originator = str(meta.get("originator") or "")
    source = str(meta.get("source") or "")
    if originator != "Codex Desktop" and not (not originator and source == "vscode"):
        return None

    captured_at = last_counter_at or meta.get("created_at")
    day = iso_to_local_day(captured_at)
    if not day:
        return None

    session_id = meta.get("id") or path.stem
    cwd = meta.get("cwd") or "(unknown)"
    return {
        "session_id": session_id,
        "rollout_path": str(path),
        "cwd": cwd,
        "originator": originator or "Codex Desktop",
        "source": source or "vscode",
        "thread_source": meta.get("thread_source") or "(unknown)",
        "model_provider": meta.get("model_provider") or "openai",
        "cli_version": meta.get("cli_version") or "",
        "created_at": meta.get("created_at"),
        "captured_at": captured_at,
        "captured_local": iso_to_local_timestamp(captured_at),
        "captured_epoch": iso_to_local_epoch(captured_at),
        "day": day,
        "month": day[:7],
        "counter": last_counter,
        "counter_events": counter_events,
        "token_event_count": token_event_count,
    }


def load_codex_app_source(indexed_rollouts=None):
    indexed_rollouts = indexed_rollouts or {}
    if not CODEX_SESSIONS_DIR.exists():
        return {
            "status": "missing",
            "ingestion": "automatic-codex-app-jsonl",
            "notes": "No ~/.codex/sessions directory was found.",
            "total": 0,
            "last_30_days": 0,
            "last_7_days": 0,
            "thread_count": 0,
            "monthly": [],
            "daily": [],
            "daily_30": [],
            "model_breakdown": [],
            "cwd_breakdown": [],
            "source_breakdown": [],
            "top_threads": [],
            "recent_threads": [],
            "thread_tokens": [],
            "all_session_count": 0,
            "included_session_count": 0,
            "indexed_session_count": 0,
            "_detail_records": [],
        }

    cutoff_30 = datetime.now().astimezone() - timedelta(days=30)
    cutoff_7 = datetime.now().astimezone() - timedelta(days=7)
    discovered = []
    included_rows = []
    indexed_session_count = 0
    indexed_delta_session_count = 0

    for path in sorted(CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl")):
        session = app_session_from_rollout(path)
        if not session:
            continue
        discovered.append(session)
        existing_tokens = indexed_rollouts.get(session["rollout_path"])
        total_tokens = session["counter"]["total_tokens"]
        tokens_to_add = total_tokens if existing_tokens is None else max(0, total_tokens - existing_tokens)
        if existing_tokens is not None:
            indexed_session_count += 1
            if tokens_to_add:
                indexed_delta_session_count += 1
        if tokens_to_add <= 0:
            continue
        counted_as_thread = existing_tokens is None
        included_rows.append(
            {
                **session,
                "tokens_used": tokens_to_add,
                "counted_as_thread": counted_as_thread,
                "thread_count": 1 if counted_as_thread else 0,
                "indexed_tokens": existing_tokens or 0,
                "daily_usage": allocate_daily_counter_usage(
                    session["counter_events"],
                    tokens_to_add,
                    baseline_total=existing_tokens or 0,
                    fallback_day=session["day"],
                ),
            }
        )

    daily_detail_records = []
    monthly_detail_records = []
    for row in included_rows:
        session_daily_records = [
            {
                "day": item["day"],
                "month": item["day"][:7],
                "model": "(codex app counter)",
                "thread_count": row["thread_count"],
                "usage": item["usage"],
            }
            for item in row["daily_usage"]
        ]
        daily_detail_records.extend(session_daily_records)
        monthly_detail_records.extend(monthly_usage_records(session_daily_records))

    monthly = grouped_token_usage(monthly_detail_records, "month", reverse=True)
    for row in monthly:
        row["avg_tokens"] = round(row["value"] / row["thread_count"], 0) if row["thread_count"] else 0
    daily = grouped_token_usage(daily_detail_records, "day")
    daily_30_cutoff = cutoff_30.strftime("%Y-%m-%d")
    daily_30 = [row for row in daily if row["day"] >= daily_30_cutoff]
    app_thread_count = sum(row["thread_count"] for row in included_rows)
    app_total = sum(row["tokens_used"] for row in included_rows)
    model_breakdown = [
        {
            "model": "(codex app counter)",
            "thread_count": app_thread_count,
            "total_tokens": app_total,
            "avg_tokens": round(
                app_total
                / app_thread_count,
                0,
            )
            if app_thread_count
            else 0,
        }
    ] if included_rows else []
    effort_breakdown = [
        {
            "reasoning_effort": "(codex app counter)",
            "thread_count": app_thread_count,
            "total_tokens": app_total,
        }
    ] if included_rows else []
    approval_breakdown = [
        {
            "approval_mode": "(codex app counter)",
            "thread_count": app_thread_count,
            "total_tokens": app_total,
        }
    ] if included_rows else []
    sandbox_breakdown = [
        {
            "sandbox_policy": "(codex app counter)",
            "thread_count": app_thread_count,
            "total_tokens": app_total,
        }
    ] if included_rows else []
    cwd_breakdown = summarize_app_group(included_rows, "cwd", "cwd")
    source_breakdown = summarize_app_group(included_rows, "Codex App", "label")

    app_threads = [
        {
            "title": "Codex App session",
            "cwd": row["cwd"],
            "model_provider": row["model_provider"],
            "model": "(codex app counter)",
            "reasoning_effort": "(counter snapshot)",
            "approval_mode": row["thread_source"],
            "tokens_used": row["tokens_used"],
            "updated_at": row["captured_epoch"] or 0,
            "rollout_path": row["rollout_path"],
            "counted_as_thread": row["counted_as_thread"],
        }
        for row in included_rows
    ]
    app_threads.sort(key=lambda row: row["tokens_used"], reverse=True)
    recent_threads = sorted(app_threads, key=lambda row: row["updated_at"], reverse=True)
    thread_tokens = [row["tokens_used"] for row in included_rows if row["counted_as_thread"]]
    total = app_total
    first_seen = min((row["captured_epoch"] for row in included_rows if row["captured_epoch"]), default=None)
    last_seen = max((row["captured_epoch"] for row in included_rows if row["captured_epoch"]), default=None)

    cutoff_7_day = cutoff_7.strftime("%Y-%m-%d")
    last_30_days = sum(row["value"] for row in daily if row["day"] >= daily_30_cutoff)
    last_7_days = sum(row["value"] for row in daily if row["day"] >= cutoff_7_day)

    snapshot_sessions = []
    for row in discovered:
        existing_tokens = indexed_rollouts.get(row["rollout_path"])
        total_tokens = row["counter"]["total_tokens"]
        tokens_included = total_tokens if existing_tokens is None else max(0, total_tokens - existing_tokens)
        snapshot_sessions.append(
            {
                "session_id": row["session_id"],
                "rollout_path": row["rollout_path"],
                "captured_at": row["captured_at"],
                "cwd": row["cwd"],
                "counter": row["counter"],
                "tokens_included": tokens_included,
                "indexed_tokens": existing_tokens or 0,
                "counted_as_thread": existing_tokens is None and tokens_included > 0,
            }
        )

    snapshot = {
        "captured_at": utc_now(),
        "sessions_dir": str(CODEX_SESSIONS_DIR),
        "all_session_count": len(discovered),
        "included_session_count": len(included_rows),
        "indexed_session_count": indexed_session_count,
        "indexed_delta_session_count": indexed_delta_session_count,
        "included_total_tokens": total,
        "sessions": snapshot_sessions,
    }
    snapshot_error = None
    try:
        DATA_DIR.mkdir(exist_ok=True)
        CODEX_APP_SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    except OSError as exc:
        snapshot_error = str(exc)

    notes = [
        f"Read Codex App token_count counters from {CODEX_SESSIONS_DIR}.",
        f"Added {len(included_rows)} sessions not fully represented in local SQLite.",
    ]
    if indexed_session_count:
        notes.append(f"Skipped or delta-adjusted {indexed_session_count} App sessions already indexed by threads.rollout_path.")
    if snapshot_error:
        notes.append(f"Could not write generated app snapshot: {snapshot_error}.")

    detail_records = [
        {
            "month": row["month"],
            "day": row["day"],
            "model": row.get("model") or "(unknown)",
            "thread_count": row["thread_count"],
            "usage": scale_token_usage(row["counter"], row["tokens_used"]),
        }
        for row in included_rows
    ]

    return {
        "status": "configured",
        "ingestion": "automatic-codex-app-jsonl",
        "label": "Codex App rollout counters",
        "metric": "tokens",
        "unit": "tokens",
        "total": total,
        "last_30_days": last_30_days,
        "last_7_days": last_7_days,
        "thread_count": sum(row["thread_count"] for row in included_rows),
        "first_seen": first_seen,
        "last_seen": last_seen,
        "monthly": monthly,
        "daily": daily,
        "daily_30": daily_30,
        "model_breakdown": model_breakdown,
        "effort_breakdown": effort_breakdown,
        "cwd_breakdown": cwd_breakdown,
        "source_breakdown": source_breakdown,
        "approval_breakdown": approval_breakdown,
        "sandbox_breakdown": sandbox_breakdown,
        "top_threads": app_threads[:8],
        "recent_threads": recent_threads[:8],
        "thread_tokens": thread_tokens,
        "all_session_count": len(discovered),
        "included_session_count": len(included_rows),
        "indexed_session_count": indexed_session_count,
        "indexed_delta_session_count": indexed_delta_session_count,
        "snapshot_path": str(CODEX_APP_SNAPSHOT_FILE),
        "snapshot_error": snapshot_error,
        "_detail_records": detail_records,
        "_daily_detail_records": daily_detail_records,
        "_monthly_detail_records": monthly_detail_records,
        "notes": " ".join(notes),
    }


def aggregate_daily_or_monthly(rows, key: str):
    merged = {}
    for row in rows:
        label = row.get(key)
        if not label:
            continue
        bucket = merged.setdefault(label, {key: label, "value": 0, "thread_count": 0})
        bucket["value"] += row.get("tokens_used", 0) or 0
        bucket["thread_count"] += row.get("thread_count", 0) or 0
    sort_key = month_key if key == "month" else day_key
    normalized = sorted(merged.values(), key=sort_key, reverse=(key == "month"))
    if key == "month":
        for row in normalized:
            row["avg_tokens"] = round(row["value"] / row["thread_count"], 0) if row["thread_count"] else 0
    return normalized


def summarize_app_group(rows, value, output_key: str):
    if not rows:
        return []
    merged = {}
    for row in rows:
        label = row.get(value) if output_key != "label" else value
        if not label:
            continue
        bucket = merged.setdefault(label, {output_key: label, "thread_count": 0, "total_tokens": 0})
        bucket["thread_count"] += row.get("thread_count", 0) or 0
        bucket["total_tokens"] += row.get("tokens_used", 0) or 0
    result = sorted(merged.values(), key=lambda row: row["total_tokens"], reverse=True)
    for row in result:
        row["avg_tokens"] = round(row["total_tokens"] / row["thread_count"], 0) if row["thread_count"] else 0
    return result[:8]


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

    totals = dict(cursor.execute(
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
    ).fetchone())

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
    detail_thread_rows = fetch_all_dicts(
        cursor,
        """
        SELECT tokens_used, rollout_path, updated_at, COALESCE(model, '(unknown)') AS model
        FROM threads
        """,
    )
    indexed_rollouts = {
        row["rollout_path"]: int(row["tokens_used"] or 0)
        for row in fetch_all_dicts(
            cursor,
            "SELECT rollout_path, tokens_used FROM threads WHERE rollout_path IS NOT NULL AND rollout_path <> ''",
        )
    }

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

    detail_records = []
    daily_detail_records = []
    monthly_detail_records = []
    for row in detail_thread_rows:
        updated_at = int(row.get("updated_at") or 0)
        local_time = datetime.fromtimestamp(updated_at).astimezone()
        tokens_used = int(row.get("tokens_used") or 0)
        rollout_usage = latest_rollout_usage(row.get("rollout_path"))
        detail_records.append(
            {
                "month": local_time.strftime("%Y-%m"),
                "day": local_time.strftime("%Y-%m-%d"),
                "model": row.get("model") or "(unknown)",
                "thread_count": 1,
                "usage": scale_token_usage(rollout_usage, tokens_used),
            }
        )
        thread_daily_records = [
            {
                "month": item["day"][:7],
                "day": item["day"],
                "model": row.get("model") or "(unknown)",
                "thread_count": 1,
                "usage": item["usage"],
            }
            for item in rollout_daily_usage(
                row.get("rollout_path"),
                tokens_used,
                fallback_epoch=updated_at,
            )
        ]
        daily_detail_records.extend(thread_daily_records)
        monthly_detail_records.extend(monthly_usage_records(thread_daily_records))

    app_source = load_codex_app_source(indexed_rollouts)
    detail_records.extend(app_source.pop("_detail_records", []))
    daily_detail_records.extend(app_source.pop("_daily_detail_records", []))
    monthly_detail_records.extend(app_source.pop("_monthly_detail_records", []))
    app_total = app_source.get("total", 0) or 0
    if app_total:
        totals["total_tokens"] += app_total
        totals["tokens_30d"] += app_source.get("last_30_days", 0) or 0
        totals["tokens_7d"] += app_source.get("last_7_days", 0) or 0
        totals["thread_count"] += app_source.get("thread_count", 0) or 0
        app_first_seen = app_source.get("first_seen")
        app_last_seen = app_source.get("last_seen")
        if app_first_seen is not None:
            totals["first_seen"] = min(
                value for value in [totals.get("first_seen"), app_first_seen] if value is not None
            )
        if app_last_seen is not None:
            totals["last_seen"] = max(
                value for value in [totals.get("last_seen"), app_last_seen] if value is not None
            )
        model_breakdown = merge_token_breakdown(
            model_breakdown,
            app_source.get("model_breakdown", []),
            "model",
            limit=8,
        )
        effort_breakdown = merge_token_breakdown(
            effort_breakdown,
            app_source.get("effort_breakdown", []),
            "reasoning_effort",
        )
        cwd_breakdown = merge_token_breakdown(
            cwd_breakdown,
            app_source.get("cwd_breakdown", []),
            "cwd",
            limit=8,
        )
        source_breakdown = merge_token_breakdown(
            source_breakdown,
            app_source.get("source_breakdown", []),
            "label",
        )
        approval_breakdown = merge_token_breakdown(
            approval_breakdown,
            app_source.get("approval_breakdown", []),
            "approval_mode",
        )
        sandbox_breakdown = merge_token_breakdown(
            sandbox_breakdown,
            app_source.get("sandbox_breakdown", []),
            "sandbox_policy",
            limit=6,
        )
        top_threads = sorted(
            top_threads + app_source.get("top_threads", []),
            key=lambda row: row.get("tokens_used", 0) or 0,
            reverse=True,
        )[:8]
        recent_threads = sorted(
            recent_threads + app_source.get("recent_threads", []),
            key=lambda row: row.get("updated_at", 0) or 0,
            reverse=True,
        )[:8]
        thread_tokens.extend(app_source.get("thread_tokens", []))

    token_usage = aggregate_token_usage(detail_records)
    model_breakdown = grouped_token_usage(detail_records, "model")
    for row in model_breakdown:
        row["avg_tokens"] = (
            round(row["total_tokens"] / row["thread_count"], 0) if row["thread_count"] else 0
        )
    model_breakdown.sort(key=lambda row: row["total_tokens"], reverse=True)
    model_months = grouped_model_month_usage(monthly_detail_records)
    monthly = grouped_token_usage(monthly_detail_records, "month", reverse=True)
    for row in monthly:
        row["avg_tokens"] = round(row["value"] / row["thread_count"], 0) if row["thread_count"] else 0
    daily = grouped_token_usage(daily_detail_records, "day")
    cutoff_30_day = (datetime.now().astimezone() - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_7_day = (datetime.now().astimezone() - timedelta(days=7)).strftime("%Y-%m-%d")
    daily_30 = [row for row in daily if row["day"] >= cutoff_30_day]
    totals["tokens_30d"] = sum(row["value"] for row in daily_30)
    totals["tokens_7d"] = sum(row["value"] for row in daily if row["day"] >= cutoff_7_day)
    totals["active_days_total"] = len([row for row in daily if row.get("value", 0)])

    total_tokens = totals["total_tokens"]
    for row in monthly:
        row["share_pct"] = share_pct(row["value"], total_tokens)
    for row in daily_30:
        row["share_pct_30d"] = share_pct(row["value"], totals["tokens_30d"])
    for row in model_breakdown:
        row["label"] = row.get("model")
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
    if app_source.get("status") == "configured":
        notes.append(app_source.get("notes", ""))
        if app_source.get("included_session_count"):
            notes.append(
                f"Codex App counters added {app_source['total']} tokens from "
                f"{app_source['included_session_count']} unindexed or delta-adjusted sessions."
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
    if app_source.get("included_session_count"):
        highlights.append(
            f"Codex App counters added {app_source['total']} local tokens not yet fully indexed by SQLite."
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
        "token_usage": token_usage,
        "db_path": str(db_path),
        "first_seen_local": format_local_timestamp(totals["first_seen"]),
        "last_seen_local": format_local_timestamp(totals["last_seen"]),
        "stats": stats,
        "monthly": monthly,
        "daily": daily,
        "daily_30": daily_30,
        "model_breakdown": model_breakdown,
        "model_months": model_months,
        "effort_breakdown": effort_breakdown,
        "cwd_breakdown": cwd_breakdown,
        "source_breakdown": source_breakdown,
        "approval_breakdown": approval_breakdown,
        "sandbox_breakdown": sandbox_breakdown,
        "top_threads": top_threads,
        "recent_threads": recent_threads,
        "codex_app": app_source,
        "data_quality": {
            "log_rows": log_coverage["log_rows"],
            "first_log_local": format_local_timestamp(log_coverage["first_log"]),
            "last_log_local": format_local_timestamp(log_coverage["last_log"]),
            "response_events": response_events,
            "detailed_usage_available": response_events > 0,
            "codex_app_sessions_seen": app_source.get("all_session_count", 0),
            "codex_app_sessions_included": app_source.get("included_session_count", 0),
            "codex_app_sessions_already_indexed": app_source.get("indexed_session_count", 0),
            "codex_app_snapshot_path": app_source.get("snapshot_path"),
            "typed_thread_count": token_usage.get("typed_thread_count", 0),
            "token_type_coverage_pct": token_usage.get("detail_coverage_pct", 0),
        },
        "pricing": pricing,
        "cost": build_cost_summary(
            total_tokens,
            monthly,
            pricing,
            token_usage=token_usage,
            models=model_breakdown,
            model_months=model_months,
        ),
        "highlights": highlights,
        "notes": " ".join(notes),
        "updated_at": utc_now(),
    }


def load_codex_source_event_based():
    db_paths = sorted(glob.glob(str(HOME / ".codex" / "state_*.sqlite")))
    if not db_paths:
        return {
            "provider": "codex",
            "display_name": "Codex",
            "status": "missing",
            "notes": "No ~/.codex/state_*.sqlite database was found.",
            "monthly": [],
            "daily": [],
            "model_breakdown": [],
            "cwd_breakdown": [],
        }

    db_path = Path(db_paths[-1])
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    raw_rows = fetch_all_dicts(
        cursor,
        """
        SELECT id, title, cwd, source, thread_source, model, reasoning_effort,
               approval_mode, sandbox_policy, tokens_used, rollout_path, updated_at
        FROM threads
        """,
    )
    log_coverage = {"log_rows": 0, "first_log": None, "last_log": None}
    response_events = 0
    if table_exists(cursor, "logs"):
        response_events = cursor.execute(
            "SELECT COUNT(*) FROM logs WHERE message LIKE '%response.completed%'"
        ).fetchone()[0]
        log_coverage = dict(cursor.execute(
            "SELECT COUNT(*) AS log_rows, MIN(ts) AS first_log, MAX(ts) AS last_log FROM logs"
        ).fetchone())
    connection.close()

    metadata = {}
    titles = {}
    rollout_paths = set(discover_rollouts(HOME / ".codex"))
    for row in raw_rows:
        session_id = str(row.get("id") or "")
        if session_id:
            metadata[session_id] = {
                "model": row.get("model") or "(unknown)",
                "cwd": row.get("cwd") or "(unknown)",
                "reasoning": row.get("reasoning_effort") or "(unknown)",
                "approval": row.get("approval_mode") or "(unknown)",
                "entry_point": normalize_thread_source(row),
            }
            titles[session_id] = row
        if row.get("rollout_path"):
            rollout_paths.add(Path(row["rollout_path"]))

    scan = scan_rollouts(sorted(rollout_paths), metadata)
    events = scan["events"]
    sessions = scan["sessions"]
    token_usage = aggregate_logical_events(events)
    total_tokens = token_usage["total_tokens"]

    daily = sorted(group_logical_events(events, "day"), key=lambda row: row["day"])
    monthly = sorted(group_logical_events(events, "month"), key=lambda row: row["month"], reverse=True)
    model_breakdown = sorted(
        group_logical_events(events, "model"), key=lambda row: row["total_tokens"], reverse=True
    )
    model_months = group_logical_events(events, ("month", "model"))
    cwd_breakdown = sorted(
        group_logical_events(events, "cwd"), key=lambda row: row["total_tokens"], reverse=True
    )[:8]
    effort_breakdown = sorted(
        group_logical_events(events, "reasoning"), key=lambda row: row["total_tokens"], reverse=True
    )
    source_breakdown = sorted(
        group_logical_events(events, "entry_point"), key=lambda row: row["total_tokens"], reverse=True
    )
    approval_breakdown = sorted(
        group_logical_events(events, "approval"), key=lambda row: row["total_tokens"], reverse=True
    )

    for row in monthly:
        row["avg_tokens"] = round(row["value"] / row["thread_count"], 0) if row["thread_count"] else 0
        row["share_pct"] = share_pct(row["value"], total_tokens)
    for row in model_breakdown:
        row["label"] = row["model"]
        row["avg_tokens"] = round(row["value"] / row["thread_count"], 0) if row["thread_count"] else 0
        row["share_pct"] = share_pct(row["value"], total_tokens)
    for row in cwd_breakdown:
        row["label"] = abbreviate_path(row["cwd"])
        row["avg_tokens"] = round(row["value"] / row["thread_count"], 0) if row["thread_count"] else 0
        row["share_pct"] = share_pct(row["value"], total_tokens)
    for row in effort_breakdown:
        row["reasoning_effort"] = row.pop("reasoning")
        row["share_pct"] = share_pct(row["value"], total_tokens)
    for row in source_breakdown:
        row["label"] = row.pop("entry_point")
        row["share_pct"] = share_pct(row["value"], total_tokens)
    for row in approval_breakdown:
        row["approval_mode"] = row.pop("approval")
        row["share_pct"] = share_pct(row["value"], total_tokens)

    now = datetime.now().astimezone()
    cutoff_30_day = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    cutoff_7_day = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    daily_30 = [row for row in daily if row["day"] >= cutoff_30_day]
    tokens_30d = sum(row["value"] for row in daily_30)
    tokens_7d = sum(row["value"] for row in daily if row["day"] >= cutoff_7_day)
    for row in daily_30:
        row["share_pct_30d"] = share_pct(row["value"], tokens_30d)

    session_rows = []
    for session in sessions:
        usage = session.get("usage") or {}
        sqlite_row = titles.get(session["session_id"], {})
        session_rows.append(
            {
                "title": sqlite_row.get("title") or session["session_id"],
                "cwd": session.get("cwd") or sqlite_row.get("cwd") or "(unknown)",
                "model_provider": "openai",
                "model": session.get("model") or sqlite_row.get("model") or "(unknown)",
                "reasoning_effort": session.get("reasoning") or "(unknown)",
                "approval_mode": session.get("approval") or "(unknown)",
                "tokens_used": usage.get("total_tokens", 0),
                "updated_at": session.get("last_seen", 0),
            }
        )
    top_threads = sorted(session_rows, key=lambda row: row["tokens_used"], reverse=True)[:8]
    recent_threads = sorted(session_rows, key=lambda row: row["updated_at"], reverse=True)[:8]
    for row in top_threads + recent_threads:
        row["title_short"] = tidy_title(row["title"])
        row["cwd_short"] = abbreviate_path(row["cwd"])
        row["updated_local"] = format_local_timestamp(row["updated_at"])
        row["share_pct"] = share_pct(row["tokens_used"], total_tokens)

    thread_tokens = [row["tokens_used"] for row in session_rows]
    top_workspace = cwd_breakdown[0] if cwd_breakdown else None
    busiest_day = max(daily_30, key=lambda row: row["value"], default=None)
    stats = {
        "avg_tokens_per_thread": round(total_tokens / len(sessions), 0) if sessions else 0,
        "median_tokens_per_thread": median(thread_tokens),
        "p90_tokens_per_thread": percentile(thread_tokens, 0.9),
        "largest_thread_tokens": max(thread_tokens) if thread_tokens else 0,
        "zero_token_threads": sum(1 for value in thread_tokens if value == 0),
        "active_days_total": len(daily),
        "active_days_30d": len(daily_30),
        "top_three_threads_share_pct": share_pct(sum(sorted(thread_tokens, reverse=True)[:3]), total_tokens),
        "top_workspace_share_pct": share_pct(top_workspace["total_tokens"], total_tokens) if top_workspace else None,
    }
    highlights = []
    if top_workspace:
        highlights.append(
            f"Top workspace {top_workspace['label']} accounts for {stats['top_workspace_share_pct']}% of all tokens."
        )
    if busiest_day:
        highlights.append(
            f"Busiest day in the last 30 days was {busiest_day['day']} with {busiest_day['value']} tokens."
        )

    pricing = load_codex_pricing()
    notes = (
        f"Reading {scan['file_count']} rollout files referenced by {db_path.name}. "
        f"Counted {scan['unique_event_count']} unique API usage increments across "
        f"{scan['logical_session_count']} logical sessions; removed "
        f"{scan['duplicate_event_count']} replayed increments. Periods use each increment's event time."
    )
    first_seen = min((event["timestamp"] for event in events), default=None)
    last_seen = max((event["timestamp"] for event in events), default=None)
    return {
        "provider": "codex",
        "display_name": "Codex",
        "status": "configured",
        "ingestion": "automatic-logical-rollout-events",
        "label": "Local Codex logical sessions",
        "metric": "tokens",
        "unit": "tokens",
        "total": total_tokens,
        "last_30_days": tokens_30d,
        "last_7_days": tokens_7d,
        "thread_count": len(sessions),
        "raw_thread_count": len(raw_rows),
        "deduplicated_event_count": scan["duplicate_event_count"],
        "token_usage": token_usage,
        "db_path": str(db_path),
        "first_seen_local": format_local_timestamp(first_seen),
        "last_seen_local": format_local_timestamp(last_seen),
        "stats": stats,
        "monthly": monthly,
        "daily": daily,
        "daily_30": daily_30,
        "model_breakdown": model_breakdown,
        "model_months": model_months,
        "effort_breakdown": effort_breakdown,
        "cwd_breakdown": cwd_breakdown,
        "source_breakdown": source_breakdown,
        "approval_breakdown": approval_breakdown,
        "sandbox_breakdown": [],
        "top_threads": top_threads,
        "recent_threads": recent_threads,
        "codex_app": {"status": "absorbed", "notes": "Codex App rollout events are included by logical session."},
        "data_quality": {
            "log_rows": log_coverage["log_rows"],
            "first_log_local": format_local_timestamp(log_coverage["first_log"]),
            "last_log_local": format_local_timestamp(log_coverage["last_log"]),
            "response_events": response_events,
            "detailed_usage_available": bool(events),
            "rollout_file_count": scan["file_count"],
            "logical_session_count": scan["logical_session_count"],
            "raw_usage_event_count": scan["raw_event_count"],
            "unique_usage_event_count": scan["unique_event_count"],
            "replayed_usage_event_count": scan["duplicate_event_count"],
            "missing_last_usage_count": scan["missing_last_usage_count"],
            "typed_thread_count": token_usage["typed_thread_count"],
            "token_type_coverage_pct": token_usage["detail_coverage_pct"],
        },
        "pricing": pricing,
        "cost": build_cost_summary(
            total_tokens,
            monthly,
            pricing,
            token_usage=token_usage,
            models=model_breakdown,
            model_months=model_months,
        ),
        "highlights": highlights,
        "notes": notes,
        "updated_at": utc_now(),
    }


load_codex_source_legacy = load_codex_source
load_codex_source = load_codex_source_event_based


def build_dashboard():
    with ThreadPoolExecutor(max_workers=1 + len(REMOTE_CODEX_HOSTS)) as executor:
        local_future = executor.submit(load_codex_source)
        remote_futures = [
            executor.submit(run_remote_codex_snapshot, config) for config in REMOTE_CODEX_HOSTS
        ]
        codex = local_future.result()
        remote_sources = [future.result() for future in remote_futures]
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
    combined_token_usage = combine_token_usage_summaries(
        [
            token_usage_or_unclassified(
                codex.get("token_usage"), codex.get("total", 0), codex.get("thread_count", 0)
            )
        ]
        + [
            token_usage_or_unclassified(
                source.get("token_usage"),
                source.get("total_tokens", 0),
                source.get("thread_count", 0),
            )
            for source in active_remote_sources
        ]
    )
    aggregate_models = aggregate_named_rows(
        list(codex.get("model_breakdown", []))
        + [
            {
                **row,
                "label": row.get("model"),
            }
            for source in active_remote_sources
            for row in source.get("models", [])
        ],
        "label",
    )
    for row in aggregate_models:
        row["share_pct"] = share_pct(row["total_tokens"], combined_total_tokens)
    combined_model_months = list(codex.get("model_months", [])) + [
        row
        for source in active_remote_sources
        for row in source.get("model_months", [])
    ]
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
            "token_usage": token_usage_or_unclassified(
                codex.get("token_usage"), codex.get("total", 0), codex.get("thread_count", 0)
            ),
            "monthly": codex.get("monthly", []),
            "daily": codex.get("daily", []),
        }
    ] + [
        {
            "label": source.get("label"),
            "host": source.get("host"),
            "status": source.get("status"),
            "thread_count": source.get("thread_count"),
            "raw_thread_count": source.get("raw_thread_count"),
            "deduplicated_subagent_count": source.get("deduplicated_subagent_count", 0),
            "total_tokens": source.get("total_tokens"),
            "tokens_30d": source.get("tokens_30d"),
            "tokens_7d": source.get("tokens_7d"),
            "token_usage": token_usage_or_unclassified(
                source.get("token_usage"),
                source.get("total_tokens", 0),
                source.get("thread_count", 0),
            ),
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
        "combined_token_usage": combined_token_usage,
        "combined_monthly": combined_monthly,
        "combined_daily": combined_daily,
        "pricing": pricing,
        "cost": build_cost_summary(
            combined_total_tokens,
            combined_monthly,
            pricing,
            token_usage=combined_token_usage,
            models=aggregate_models,
            model_months=combined_model_months,
        ),
        "models": aggregate_models,
        "model_months": combined_model_months,
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
