#!/usr/bin/python3
"""Read and normalize local Omarchy AI usage records for the dashboard."""

from __future__ import annotations

import datetime as dt
import base64
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any


MAX_RECORD_BYTES = 2 * 1024 * 1024
STALE_AFTER_SECONDS = 30 * 60
MAX_WINDOW_SECONDS = 10 * 365 * 86400


def parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            stamp = float(value)
        except (OverflowError, ValueError):
            return None
        if stamp > 10_000_000_000:
            stamp /= 1000
        return stamp if math.isfinite(stamp) and 0 <= stamp <= 253_402_300_799 else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def iso_timestamp(value: Any) -> str | None:
    stamp = parse_timestamp(value)
    if stamp is None:
        return None
    try:
        return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def duration_seconds(entry: dict[str, Any]) -> int | None:
    """Use the provider's numeric duration first, then explicit label units."""
    for key in ("windowDurationSeconds", "durationSeconds"):
        value = entry.get(key)
        if key in entry and isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                numeric = float(value)
                if math.isfinite(numeric) and 0 < numeric <= MAX_WINDOW_SECONDS:
                    return int(numeric)
            except (OverflowError, ValueError):
                return None
            return None
        elif key in entry:
            return None
    for key in ("windowDurationMins", "durationMinutes"):
        value = entry.get(key)
        if key in entry and isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                numeric = float(value)
                if math.isfinite(numeric) and 0 < numeric <= MAX_WINDOW_SECONDS / 60:
                    return int(numeric * 60)
            except (OverflowError, ValueError):
                return None
            return None
        elif key in entry:
            return None

    label = str(entry.get("label") or entry.get("title") or "").strip().lower()[:240]
    match = re.search(r"\b(\d+)\s*[- ]?\s*(hours?|hrs?|h)\b", label)
    if match:
        value = int(match.group(1)) * 3600
        return value if 0 < value <= MAX_WINDOW_SECONDS else None
    match = re.search(r"\b(\d+)\s*[- ]?\s*(minutes?|mins?|m)\b", label)
    if match:
        value = int(match.group(1)) * 60
        return value if 0 < value <= MAX_WINDOW_SECONDS else None
    match = re.search(r"\b(\d+)\s*[- ]?\s*(days?|d)\b", label)
    if match:
        value = int(match.group(1)) * 86400
        return value if 0 < value <= MAX_WINDOW_SECONDS else None
    match = re.search(r"\b(\d+)\s*[- ]?\s*(weeks?|wks?|w)\b", label)
    if match:
        value = int(match.group(1)) * 7 * 86400
        return value if 0 < value <= MAX_WINDOW_SECONDS else None
    if "weekly" in label or "week" in label:
        return 7 * 86400
    return None


def normalize_limit(entry: Any, now: float, as_of: float | None = None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("percent"), bool):
        return None
    try:
        percent = float(entry.get("percent"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(percent):
        return None
    # Omarchy records and this plugin's normalized contract use fractions.
    # Reject values outside the contract rather than guessing a scale.
    if not 0 <= percent <= 1:
        return None

    reset_at = iso_timestamp(entry.get("resetsAt", entry.get("resetAt")))
    reset_epoch = parse_timestamp(reset_at)
    duration = duration_seconds(entry)
    expired = reset_epoch is not None and reset_epoch <= now
    elapsed = None
    expected = None
    gap_pp = None
    pace = "unknown"
    projection = None

    # Pacing is measured as of the provider snapshot, so stale percentages do
    # not masquerade as a live reading. Expiration is always checked against
    # current wall time to avoid extrapolating a completed window.
    pace_now = min(as_of, now) if as_of is not None else now
    if not expired and reset_epoch is not None and duration is not None:
        start_epoch = reset_epoch - duration
        elapsed = max(0.0, min(1.0, (pace_now - start_epoch) / duration))
        expected = elapsed
        gap_pp = round((percent - expected) * 100, 1)
        pace = "ahead" if gap_pp > 1.0 else "within"
        if elapsed > 0.01:
            projection = round(percent / elapsed, 3)

    return {
        "label": str(entry.get("label") or entry.get("title") or "Usage window")[:120],
        "title": str(entry.get("title") or ""),
        "percent": percent,
        "remainingPercent": round(max(0.0, 1.0 - percent), 3),
        "resetsAt": reset_at,
        "resetExpired": expired,
        "durationSeconds": duration,
        "elapsedPercent": round(elapsed, 3) if elapsed is not None else None,
        "expectedPercent": round(expected, 3) if expected is not None else None,
        "paceGapPp": gap_pp,
        "pace": pace,
        "projectionPercent": projection,
    }


def safe_display_name(record_id: str, value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:100]
    return record_id.replace("-", " ").replace("_", " ").title() or "Provider"


def normalize_subscription(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only provider-reported subscription metadata; never infer pricing."""
    source = raw.get("subscription")
    source = source if isinstance(source, dict) else {}
    result: dict[str, Any] = {}
    for key in ("product", "plan", "status", "billingInterval", "currency", "source", "periodSource"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:100]
    if not result.get("plan") and isinstance(raw.get("tierLabel"), str) and raw["tierLabel"].strip():
        result["plan"] = raw["tierLabel"].strip()[:100]
    for key in ("nextBillingAt", "renewsAt", "endsAt", "periodStartedAt", "periodEndsAt", "periodSourceAt", "sourceAt"):
        if key in source:
            result[key] = iso_timestamp(source.get(key))
    amount = source.get("amount")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        try:
            numeric = float(amount)
            if math.isfinite(numeric) and numeric >= 0:
                result["amount"] = numeric
        except (OverflowError, ValueError):
            pass
    return result


def read_codex_saved_subscription(auth_path: Path | None = None) -> dict[str, str]:
    """Read only whitelisted subscription dates from Codex's saved ID token."""
    if auth_path is None:
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        auth_path = codex_home / "auth.json"
    try:
        if auth_path.is_symlink() or auth_path.stat().st_size > MAX_RECORD_BYTES:
            return {}
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if not isinstance(id_token, str) or len(id_token) > MAX_RECORD_BYTES:
        return {}
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, json.JSONDecodeError, base64.binascii.Error):
        return {}
    service = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    if not isinstance(service, dict):
        return {}
    metadata: dict[str, str] = {}
    saved_plan = service.get("chatgpt_plan_type")
    if isinstance(saved_plan, str) and saved_plan.strip():
        metadata["periodPlan"] = saved_plan.strip()[:80]
    for source_key, output_key in (
        ("chatgpt_subscription_active_start", "periodStartedAt"),
        ("chatgpt_subscription_active_until", "periodEndsAt"),
        ("chatgpt_subscription_last_checked", "periodSourceAt"),
    ):
        stamp = iso_timestamp(service.get(source_key))
        if stamp:
            metadata[output_key] = stamp
    if not any(key != "periodPlan" for key in metadata):
        return {}
    return metadata


def read_record(path: Path, source: str, now: float) -> dict[str, Any]:
    record_id = path.stem[:80] or "unknown"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError("record is not a regular-sized local file")
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
        if not isinstance(raw, dict):
            raise ValueError("record root is not an object")
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "providerId": record_id,
            "providerName": safe_display_name(record_id, None),
            "source": source,
            "updatedAt": None,
            "ageSeconds": None,
            "stale": None,
            "statusText": "Provider record could not be read",
            "isError": True,
            "limits": [],
            "supplemental": {},
            "supplementalUpdatedAt": None,
            "supplementalAgeSeconds": None,
            "supplementalStale": None,
            "supplementalStatusText": "",
            "supplementalError": False,
            "supplementalAttemptAt": None,
            "replaceLimits": False,
            "subscription": {},
            "subscriptionUpdatedAt": None,
            "subscriptionAgeSeconds": None,
            "subscriptionExplicitFields": [],
            "subscriptionPeriodPlan": None,
            "latestErrorText": "Provider record could not be read",
            "latestErrorAt": None,
            "placeholder": False,
        }

    record_id = str(raw.get("id") or raw.get("providerId") or record_id)[:80]
    updated = raw.get("updatedAt", raw.get("sourceTimestamp"))
    updated_epoch = parse_timestamp(updated)
    age = max(0, int(now - updated_epoch)) if updated_epoch is not None else None
    stale = age > STALE_AFTER_SECONDS if age is not None else None
    status_text = str(raw.get("statusText") or raw.get("usageStatusText") or "")[:240]
    is_error = raw.get("isError") is True or raw.get("status") == "error"
    lowered_status = status_text.lower()
    if any(marker in lowered_status for marker in ("limits unavailable", "sign-in expired", "waiting for auth", "waiting for authentication")):
        is_error = True
    if raw.get("ready") is False and not status_text:
        status_text = "Provider has not returned usage data"
        is_error = True

    raw_limits = raw.get("limits")
    if not isinstance(raw_limits, list):
        raw_limits = []
    normalized_limits = [normalize_limit(item, now, updated_epoch) for item in raw_limits]

    supplemental = raw.get("supplemental")
    supplemental = dict(supplemental) if isinstance(supplemental, dict) else {}
    # Allow small companion records to use concise top-level optional fields.
    for key in ("bankedResets", "nextFreeResetAt"):
        if key in raw and key not in supplemental:
            supplemental[key] = raw[key]
    supplemental_updated = raw.get("supplementalUpdatedAt") or supplemental.get("sourceAt") or (updated if supplemental else None)
    supplemental_epoch = parse_timestamp(supplemental_updated)
    supplemental_age = max(0, int(now - supplemental_epoch)) if supplemental_epoch is not None else None
    has_known_grant = False
    if isinstance(supplemental.get("bankedResets"), dict):
        grant_count = supplemental["bankedResets"].get("availableCount", supplemental["bankedResets"].get("count"))
        has_known_grant = grant_count is not None or bool(supplemental["bankedResets"].get("details"))
    has_known_grant = has_known_grant or bool(supplemental.get("nextFreeResetAt"))
    has_local_stats = raw.get("hasLocalStats") is True or bool(raw.get("modelUsage"))
    for key in ("totalPrompts", "totalSessions", "todayTotalTokens"):
        try:
            has_local_stats = has_local_stats or float(raw.get(key) or 0) > 0
        except (TypeError, ValueError, OverflowError):
            pass
    has_data = any(entry is not None for entry in normalized_limits) or isinstance(raw.get("balance"), dict) or has_known_grant or has_local_stats
    placeholder = raw.get("ready") is False and not has_data
    subscription = normalize_subscription(raw)
    raw_subscription = raw.get("subscription")
    subscription_updated = raw.get("subscriptionUpdatedAt")
    if not subscription_updated and isinstance(raw_subscription, dict):
        subscription_updated = raw_subscription.get("sourceAt")
    if subscription and not subscription_updated:
        subscription_updated = updated
    subscription_epoch = parse_timestamp(subscription_updated)
    subscription_age = max(0, int(now - subscription_epoch)) if subscription_epoch is not None else None
    period_metadata = read_codex_saved_subscription() if record_id == "codex" else {}
    used_saved_period = False
    subscription_period_plan = None
    saved_plan = period_metadata.pop("periodPlan", "")
    reported_plan = subscription.get("plan") or raw.get("tierLabel")
    same_plan = isinstance(saved_plan, str) and isinstance(reported_plan, str) and saved_plan.casefold() == reported_plan.casefold()
    if not subscription.get("periodEndsAt") and same_plan:
        for key, value in period_metadata.items():
            if key not in subscription:
                subscription[key] = value
                used_saved_period = True
    if used_saved_period:
        subscription["periodSource"] = "Saved Codex account metadata"
        subscription_period_plan = saved_plan
    explicit_subscription_fields = [key for key in subscription if isinstance(raw_subscription, dict) and key in raw_subscription]
    if used_saved_period:
        explicit_subscription_fields.extend(key for key in period_metadata if key not in explicit_subscription_fields)
        explicit_subscription_fields.append("periodSource")

    if isinstance(supplemental.get("bankedResets"), dict):
        banked = supplemental["bankedResets"]
        details = banked.get("details")
        if isinstance(details, list):
            banked["details"] = [
                {key: detail[key] for key in ("title", "description", "grantedAt", "expiresAt", "expiresAtKnown") if key in detail}
                for detail in details[:50] if isinstance(detail, dict)
            ]
    return {
        "providerId": record_id,
        "providerName": safe_display_name(record_id, raw.get("name", raw.get("providerName"))),
        "source": str(raw.get("source") or source)[:120],
        "updatedAt": iso_timestamp(updated),
        "ageSeconds": age,
        "stale": stale,
        "statusText": status_text,
        "isError": is_error,
        "tierLabel": str(raw.get("tierLabel") or "")[:80],
        "subscription": subscription,
        "subscriptionUpdatedAt": iso_timestamp(subscription_updated),
        "subscriptionAgeSeconds": subscription_age,
        "subscriptionExplicitFields": sorted(set(explicit_subscription_fields)),
        "subscriptionPeriodPlan": subscription_period_plan,
        "limits": [entry for entry in normalized_limits if entry is not None],
        "supplemental": supplemental,
        "supplementalUpdatedAt": iso_timestamp(supplemental_updated),
        "supplementalAgeSeconds": supplemental_age,
        "supplementalStale": supplemental_age > STALE_AFTER_SECONDS if supplemental_age is not None else None,
        "supplementalStatusText": str(raw.get("supplementalStatusText") or "")[:240],
        "supplementalError": raw.get("supplementalError") is True,
        "supplementalAttemptAt": iso_timestamp(raw.get("lastAttemptAt") or raw.get("supplementalAttemptAt")),
        "replaceLimits": raw.get("replaceLimits") is True,
        "latestErrorText": status_text if is_error else str(raw.get("latestErrorText") or "")[:240],
        "latestErrorAt": iso_timestamp(updated if is_error else raw.get("latestErrorAt")),
        "placeholder": placeholder,
    }


def discover_records(directory: Path, source: str, now: float) -> list[dict[str, Any]]:
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records = [read_record(path, source, now) for path in paths if path.is_file() or path.is_symlink()]
    return [record for record in records if not record.get("placeholder")]


def merge_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        provider_id = record["providerId"]
        current = merged.get(provider_id)
        if current is None:
            merged[provider_id] = record
            continue

        record_epoch = parse_timestamp(record["updatedAt"])
        current_epoch = parse_timestamp(current["updatedAt"])
        is_newer_usage = record_epoch is not None and (current_epoch is None or record_epoch >= current_epoch)

        # A newer failed scan with no limits is not a newer usage sample.
        # Prefer a successful sample over an empty/error record, but retain the
        # scan error separately so its age and warning remain visible.
        record_success = not record["isError"] and bool(record["limits"] or record["replaceLimits"])
        can_fallback_to_success = current["isError"] and not current["limits"] and record_success
        if record["isError"] and record_epoch is not None and (current_epoch is None or record_epoch >= current_epoch):
            current["latestErrorText"] = record["statusText"] or "Provider usage refresh failed"
            current["latestErrorAt"] = record["updatedAt"]
        if record_success and (is_newer_usage or can_fallback_to_success):
            current["limits"] = record["limits"]
            current["updatedAt"] = record["updatedAt"]
            current["ageSeconds"] = record["ageSeconds"]
            current["stale"] = record["stale"]
            current["statusText"] = record["statusText"]
            current["isError"] = False
            if record["tierLabel"]:
                current["tierLabel"] = record["tierLabel"]
            if record["source"] != "Omarchy usage record":
                current["source"] = record["source"]
            error_epoch = parse_timestamp(current.get("latestErrorAt"))
            if error_epoch is None or (record_epoch is not None and record_epoch >= error_epoch):
                current["latestErrorText"] = ""
                current["latestErrorAt"] = None

        subscription_epoch = parse_timestamp(record.get("subscriptionUpdatedAt"))
        current_subscription_epoch = parse_timestamp(current.get("subscriptionUpdatedAt"))
        newer_subscription = subscription_epoch is not None and (
            current_subscription_epoch is None or subscription_epoch >= current_subscription_epoch
        )
        incoming_subscription = record.get("subscription") or {}
        if incoming_subscription:
            current_subscription = current.setdefault("subscription", {})
            current_explicit = set(current.get("subscriptionExplicitFields", []))
            incoming_explicit = set(record.get("subscriptionExplicitFields", []))
            for key, value in incoming_subscription.items():
                if key == "plan":
                    if key in current_explicit and key not in incoming_explicit:
                        continue
                    if key in incoming_explicit and key not in current_explicit and not newer_subscription:
                        continue
                    if not newer_subscription and current_subscription.get(key):
                        continue
                elif key in incoming_explicit:
                    if key in current_explicit and not newer_subscription:
                        continue
                    if record.get("subscriptionPeriodPlan"):
                        current_plan = current_subscription.get("plan") or current.get("tierLabel")
                        if current_plan and record["subscriptionPeriodPlan"].casefold() != str(current_plan).casefold():
                            continue
                elif key in current_explicit or not newer_subscription:
                    continue
                current_subscription[key] = value
                if key in incoming_explicit:
                    current_explicit.add(key)
                if key in ("periodEndsAt", "periodStartedAt") and record.get("subscriptionPeriodPlan"):
                    current["subscriptionPeriodPlan"] = record["subscriptionPeriodPlan"]
            current["subscriptionExplicitFields"] = sorted(current_explicit)
            if newer_subscription:
                current["subscriptionUpdatedAt"] = record["subscriptionUpdatedAt"]
                current["subscriptionAgeSeconds"] = record["subscriptionAgeSeconds"]
            if incoming_subscription.get("plan") and (newer_subscription or not current.get("tierLabel")):
                current["tierLabel"] = incoming_subscription["plan"]
            saved_plan = current.get("subscriptionPeriodPlan")
            current_plan = current_subscription.get("plan") or current.get("tierLabel")
            if saved_plan and current_plan and saved_plan.casefold() != str(current_plan).casefold():
                for key in ("periodStartedAt", "periodEndsAt", "periodSourceAt", "periodSource"):
                    current_subscription.pop(key, None)
                current["subscriptionPeriodPlan"] = None

        grant_epoch = parse_timestamp(record["supplementalUpdatedAt"])
        current_grant_epoch = parse_timestamp(current.get("supplementalUpdatedAt"))
        is_newer_grant = grant_epoch is not None and (current_grant_epoch is None or grant_epoch >= current_grant_epoch)
        if record["supplemental"] and is_newer_grant:
            current["supplemental"].update(record["supplemental"])
            current["supplementalUpdatedAt"] = record["supplementalUpdatedAt"]
            current["supplementalAgeSeconds"] = record["supplementalAgeSeconds"]
            current["supplementalStale"] = record["supplementalStale"]
        attempt_epoch = parse_timestamp(record.get("supplementalAttemptAt"))
        current_attempt_epoch = parse_timestamp(current.get("supplementalAttemptAt"))
        status_is_newer = attempt_epoch is not None and (current_attempt_epoch is None or attempt_epoch >= current_attempt_epoch)
        if status_is_newer and (record.get("supplementalAttemptAt") or record.get("supplementalStatusText")):
            current["supplementalStatusText"] = record.get("supplementalStatusText", "")
            current["supplementalError"] = record.get("supplementalError", False)
            current["supplementalAttemptAt"] = record.get("supplementalAttemptAt")

    return sorted(merged.values(), key=lambda entry: (entry["providerName"].lower(), entry["providerId"]))


def placeholder_provider(provider_id: str, name: str) -> dict[str, Any]:
    return {
        "providerId": provider_id,
        "providerName": name,
        "source": "Local usage records",
        "updatedAt": None,
        "ageSeconds": None,
        "stale": None,
        "statusText": "No local provider record found",
        "isError": False,
        "tierLabel": "",
        "subscription": {},
        "subscriptionUpdatedAt": None,
        "subscriptionAgeSeconds": None,
        "subscriptionExplicitFields": [],
        "subscriptionPeriodPlan": None,
        "limits": [],
        "supplemental": {},
        "supplementalUpdatedAt": None,
        "supplementalAgeSeconds": None,
        "supplementalStale": None,
        "supplementalStatusText": "",
        "supplementalError": False,
        "supplementalAttemptAt": None,
        "replaceLimits": False,
        "latestErrorText": "",
        "latestErrorAt": None,
        "placeholder": False,
    }


def default_paths() -> tuple[Path, Path, Path]:
    state_home = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    omarchy_dir = state_home / "omarchy/agents/usage"
    own_codex = state_home / "ai-usage-monitor/codex.json"
    supplemental_dir = config_home / "omarchy/ai-usage-monitor/providers.d"
    return omarchy_dir, own_codex, supplemental_dir


def build_snapshot(now: float | None = None, usage_dir: Path | None = None,
                   codex_path: Path | None = None, supplemental_dir: Path | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    default_usage, default_codex, default_supplemental = default_paths()
    usage_dir = usage_dir or default_usage
    codex_path = codex_path or default_codex
    supplemental_dir = supplemental_dir or default_supplemental

    records = discover_records(usage_dir, "Omarchy usage record", now)
    if codex_path.exists() or codex_path.is_symlink():
        records.append(read_record(codex_path, "Codex app-server", now))
    records.extend(discover_records(supplemental_dir, "Supplemental provider record", now))
    present = {record["providerId"] for record in records}
    for provider_id, name in (("claude", "Claude"), ("codex", "Codex")):
        if provider_id not in present:
            records.append(placeholder_provider(provider_id, name))
    return {
        "generatedAt": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        "staleAfterSeconds": STALE_AFTER_SECONDS,
        "providers": merge_records(records),
    }


def main() -> int:
    try:
        snapshot = build_snapshot()
        print(json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False))
        return 0
    except Exception:
        # Do not leak paths, raw provider data, or environment details to the UI.
        print(json.dumps({"providers": [], "error": "Unable to read local AI usage records"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
