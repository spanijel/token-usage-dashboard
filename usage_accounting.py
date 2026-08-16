#!/usr/bin/env python3
"""Logical-session accounting for Codex rollout token events."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


TOKEN_FIELDS = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "non_reasoning_output_tokens",
    "unclassified_tokens",
)

CUMULATIVE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

TOKEN_USAGE_PATTERN = re.compile(
    r'"total_token_usage"\s*:\s*(\{[^{}]*\}).*?'
    r'"last_token_usage"\s*:\s*(\{[^{}]*\})'
)
TIMESTAMP_PATTERN = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
MODEL_PATTERN = re.compile(r'"model"\s*:\s*"([^"]+)"')
CWD_PATTERN = re.compile(r'"cwd"\s*:\s*"((?:\\.|[^"])*)"')
EFFORT_PATTERN = re.compile(r'"effort"\s*:\s*"([^"]+)"')
_ROLLOUT_FILE_CACHE = {}


def integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_usage(raw=None, fallback_total=0):
    raw = raw or {}
    input_tokens = integer(raw.get("input_tokens"))
    cached_input_tokens = min(integer(raw.get("cached_input_tokens")), input_tokens)
    output_tokens = integer(raw.get("output_tokens"))
    reasoning_output_tokens = min(integer(raw.get("reasoning_output_tokens")), output_tokens)
    total_tokens = integer(raw.get("total_tokens")) or integer(fallback_total)
    has_types = any(
        key in raw
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    )
    classified = input_tokens + output_tokens if has_types else 0
    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "uncached_input_tokens": max(input_tokens - cached_input_tokens, 0),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "non_reasoning_output_tokens": max(output_tokens - reasoning_output_tokens, 0),
        "unclassified_tokens": max(total_tokens - classified, 0),
        "has_token_types": has_types,
    }


def usage_fingerprint(raw):
    return tuple(integer((raw or {}).get(field)) for field in CUMULATIVE_FIELDS)


def parse_timestamp(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def discover_rollouts(codex_home):
    root = Path(codex_home)
    paths = set()
    for directory in (root / "sessions", root / "archived_sessions"):
        if directory.exists():
            paths.update(directory.glob("**/rollout-*.jsonl"))
    return sorted(paths)


def _prefilter_rollouts(paths):
    existing_paths = [Path(path) for path in paths if Path(path).is_file()]
    if not existing_paths:
        return {}
    command = [
        "rg",
        "--with-filename",
        "--no-heading",
        "-e",
        r'^\{"timestamp"\s*:\s*"[^"]+"\s*(?:,\s*"ordinal"\s*:\s*\d+)?\s*,\s*"type"\s*:\s*"session_meta"',
        "-e",
        r'^\{"timestamp"\s*:\s*"[^"]+"\s*(?:,\s*"ordinal"\s*:\s*\d+)?\s*,\s*"type"\s*:\s*"turn_context"',
        "-e",
        r'^\{"timestamp"\s*:\s*"[^"]+"\s*(?:,\s*"ordinal"\s*:\s*\d+)?\s*,\s*"type"\s*:\s*"event_msg"\s*,\s*"payload"\s*:\s*\{"type"\s*:\s*"token_count"',
        *[str(path) for path in existing_paths],
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    filtered = {}
    for output_line in completed.stdout.splitlines():
        path_text, separator, line = output_line.partition(":")
        if separator:
            filtered.setdefault(path_text, []).append(line)
    return filtered


def _entry_point(meta):
    originator = meta.get("originator")
    source = meta.get("source")
    if originator:
        return str(originator)
    if isinstance(source, dict) and source:
        family, detail = next(iter(source.items()))
        if isinstance(detail, dict) and detail:
            return f"{family}/{next(iter(detail))}"
        return str(family)
    return str(source or "(unknown)")


def scan_rollouts(paths, metadata_by_session=None):
    """Return unique API usage increments from all logical Codex sessions.

    Rollout copies can replay the full cumulative history of one logical
    session. The cumulative tuple identifies the same event across those
    copies; the associated last_token_usage is the increment to count.
    """

    metadata_by_session = metadata_by_session or {}
    unique_events = {}
    paths_by_session = {}
    raw_event_count = 0
    missing_last_usage_count = 0
    unreadable_file_count = 0
    local_timezone = datetime.now().astimezone().tzinfo
    path_signatures = {}
    changed_paths = []
    for path_value in paths:
        path = Path(path_value)
        try:
            stat = path.stat()
        except OSError:
            unreadable_file_count += 1
            continue
        signature = (stat.st_size, stat.st_mtime_ns)
        path_signatures[str(path)] = signature
        cached = _ROLLOUT_FILE_CACHE.get(str(path))
        if cached is None or cached["signature"] != signature:
            changed_paths.append(path)
    filtered_lines = _prefilter_rollouts(changed_paths)

    def merge_file_result(path, result):
        nonlocal raw_event_count, missing_last_usage_count
        raw_event_count += result["raw_event_count"]
        missing_last_usage_count += result["missing_last_usage_count"]
        for key, record in result["events"].items():
            previous = unique_events.get(key)
            if previous is None or record["timestamp_text"] < previous["timestamp_text"]:
                unique_events[key] = record
        for session_id in result["session_ids"]:
            paths_by_session.setdefault(session_id, set()).add(str(path))

    for path_value in paths:
        path = Path(path_value)
        signature = path_signatures.get(str(path))
        if signature is None:
            continue
        cached = _ROLLOUT_FILE_CACHE.get(str(path))
        if cached is not None and cached["signature"] == signature:
            merge_file_result(path, cached)
            continue

        meta = {}
        context = {}
        file_events = {}
        file_session_ids = set()
        file_raw_event_count = 0
        file_missing_last_usage_count = 0
        if filtered_lines is None:
            try:
                handle = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                unreadable_file_count += 1
                continue
            lines = handle
        else:
            handle = None
            lines = filtered_lines.get(str(path), ())
        try:
            for line in lines:
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
                    except json.JSONDecodeError:
                        continue
                    file_raw_event_count += 1
                    if integer(cumulative.get("total_tokens")) <= 0:
                        continue
                    if integer(increment.get("total_tokens")) <= 0:
                        file_missing_last_usage_count += 1
                        continue

                    session_id = str(meta.get("id") or path.stem)
                    key = (session_id, usage_fingerprint(cumulative))
                    previous = file_events.get(key)
                    timestamp_text = timestamp_match.group(1)
                    if previous is not None and timestamp_text >= previous["timestamp_text"]:
                        continue
                    sqlite_meta = metadata_by_session.get(session_id) or {}
                    timestamp = parse_timestamp(timestamp_text)
                    if timestamp is None:
                        continue
                    model = context.get("model") or sqlite_meta.get("model") or "(unknown)"
                    cwd = context.get("cwd") or meta.get("cwd") or sqlite_meta.get("cwd") or "(unknown)"
                    reasoning = context.get("effort") or sqlite_meta.get("reasoning") or "(unknown)"
                    approval = context.get("approval_policy") or sqlite_meta.get("approval") or "(unknown)"
                    source = _entry_point(meta) if meta else sqlite_meta.get("entry_point", "(unknown)")
                    usage = normalize_usage(increment)
                    local_time = datetime.fromtimestamp(timestamp, local_timezone)
                    record = {
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "timestamp_text": timestamp_text,
                        "day": local_time.strftime("%Y-%m-%d"),
                        "month": local_time.strftime("%Y-%m"),
                        "model": str(model),
                        "cwd": str(cwd),
                        "reasoning": str(reasoning),
                        "approval": str(approval),
                        "entry_point": str(source),
                        "usage": usage,
                    }
                    file_events[key] = record
                    file_session_ids.add(session_id)
                    continue
                if '"turn_context"' in line:
                    model_match = MODEL_PATTERN.search(line)
                    cwd_match = CWD_PATTERN.search(line)
                    effort_match = EFFORT_PATTERN.search(line)
                    if model_match:
                        context["model"] = model_match.group(1)
                    if cwd_match:
                        try:
                            context["cwd"] = json.loads(f'"{cwd_match.group(1)}"')
                        except json.JSONDecodeError:
                            pass
                    if effort_match:
                        context["effort"] = effort_match.group(1)
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                payload = event.get("payload") or {}
                if event_type == "session_meta":
                    meta.update(payload)
                    continue
                if event_type == "turn_context":
                    for key in ("model", "effort", "cwd", "approval_policy", "sandbox_policy"):
                        if payload.get(key) is not None:
                            context[key] = payload.get(key)
                    continue
        finally:
            if handle is not None:
                handle.close()

        file_result = {
            "signature": signature,
            "events": file_events,
            "session_ids": file_session_ids,
            "raw_event_count": file_raw_event_count,
            "missing_last_usage_count": file_missing_last_usage_count,
        }
        _ROLLOUT_FILE_CACHE[str(path)] = file_result
        merge_file_result(path, file_result)

    active_paths = set(path_signatures)
    for cached_path in list(_ROLLOUT_FILE_CACHE):
        if cached_path not in active_paths:
            _ROLLOUT_FILE_CACHE.pop(cached_path, None)

    events = sorted(unique_events.values(), key=lambda row: row["timestamp"])
    usage_by_session = {}
    session_metadata = {}
    for event in events:
        bucket = usage_by_session.setdefault(event["session_id"], zero_usage())
        add_usage(bucket, event["usage"])
        session_metadata[event["session_id"]] = event
    sessions = []
    for session_id, usage in usage_by_session.items():
        event = session_metadata[session_id]
        session_events = [row for row in events if row["session_id"] == session_id]
        sessions.append(
            {
                "session_id": session_id,
                "cwd": event["cwd"],
                "model": event["model"],
                "reasoning": event["reasoning"],
                "approval": event["approval"],
                "entry_point": event["entry_point"],
                "first_seen": session_events[0]["timestamp"],
                "last_seen": session_events[-1]["timestamp"],
                "rollout_path_count": len(paths_by_session.get(session_id, ())),
                "usage": usage,
            }
        )

    return {
        "events": events,
        "sessions": sessions,
        "file_count": len(paths),
        "logical_session_count": len(sessions),
        "raw_event_count": raw_event_count,
        "unique_event_count": len(events),
        "duplicate_event_count": raw_event_count - missing_last_usage_count - len(events),
        "missing_last_usage_count": missing_last_usage_count,
        "unreadable_file_count": unreadable_file_count,
    }


def zero_usage():
    return {field: 0 for field in TOKEN_FIELDS}


def add_usage(target, usage):
    for field in TOKEN_FIELDS:
        target[field] += integer((usage or {}).get(field))
    return target


def aggregate_events(events):
    totals = zero_usage()
    session_ids = set()
    typed_session_ids = set()
    for event in events:
        add_usage(totals, event.get("usage"))
        session_id = event.get("session_id")
        if session_id:
            session_ids.add(session_id)
            if (event.get("usage") or {}).get("has_token_types"):
                typed_session_ids.add(session_id)
    totals["thread_count"] = len(session_ids)
    totals["typed_thread_count"] = len(typed_session_ids)
    totals["detail_coverage_pct"] = (
        round(100.0 * len(typed_session_ids) / len(session_ids), 2) if session_ids else 0.0
    )
    return totals


def group_events(events, keys):
    if isinstance(keys, str):
        keys = (keys,)
    grouped = {}
    for event in events:
        label = tuple(event.get(key) for key in keys)
        grouped.setdefault(label, []).append(event)
    rows = []
    for label, values in grouped.items():
        row = dict(zip(keys, label))
        row.update(aggregate_events(values))
        row["value"] = row["total_tokens"]
        rows.append(row)
    return rows
