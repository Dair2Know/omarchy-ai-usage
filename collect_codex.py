#!/usr/bin/python3
"""Read Codex rate windows and reset credits without consuming them."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


REFRESH_SECONDS = 15 * 60
RPC_TIMEOUT_SECONDS = 8


def runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    home = str(Path.home())
    extra = [f"{home}/.local/bin", f"{home}/.npm-global/bin", f"{home}/.local/share/mise/shims"]
    env["PATH"] = os.pathsep.join([part for part in [env.get("PATH", ""), *extra] if part])
    return env


def app_server_command(env: dict[str, str]) -> str | None:
    return shutil.which("codex", path=env.get("PATH"))


def rpc_request(proc: subprocess.Popen[bytes], request_id: int, method: str,
                params: dict[str, Any] | None = None, timeout: int = RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("Codex app-server pipes unavailable")
    proc.stdin.write((json.dumps({"id": request_id, "method": method, "params": params or {}}) + "\n").encode("utf-8"))
    proc.stdin.flush()
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    buffer = getattr(proc, "_ai_usage_buffer", bytearray())
    while time.monotonic() < deadline:
        newline = buffer.find(b"\n")
        if newline >= 0:
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                proc._ai_usage_buffer = buffer
                if message.get("error"):
                    raise RuntimeError("Codex rate-limit read failed")
                return message
            continue

        ready, _, _ = select.select([fd], [], [], min(0.25, max(0.0, deadline - time.monotonic())))
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > 1024 * 1024:
            raise RuntimeError("Codex app-server response exceeded the size limit")
    proc._ai_usage_buffer = buffer
    raise TimeoutError("Codex rate-limit read timed out")


def reset_iso(value: Any) -> str | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > 253_402_300_799:
        return None
    try:
        return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def window_label(name: str, window: dict[str, Any]) -> str:
    minutes = window.get("windowDurationMins")
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
        try:
            numeric = float(minutes)
            if math.isfinite(numeric) and 0 < numeric <= 10 * 365 * 24 * 60:
                if numeric == 5 * 60:
                    duration_label = "5h"
                elif numeric == 7 * 24 * 60:
                    duration_label = "Weekly"
                elif numeric == 30 * 24 * 60:
                    duration_label = "30d"
                elif numeric % (24 * 60) == 0:
                    duration_label = f"{int(numeric / (24 * 60))}d"
                elif numeric % 60 == 0:
                    duration_label = f"{int(numeric / 60)}h"
                else:
                    duration_label = f"{int(numeric)}m"
                return f"{name} · {duration_label}"
        except (OverflowError, ValueError):
            pass
    return name


def extract_windows(response: dict[str, Any]) -> list[dict[str, Any]]:
    buckets: list[tuple[str, dict[str, Any]]] = []
    by_limit = response.get("rateLimitsByLimitId")
    if isinstance(by_limit, dict):
        for limit_id, snapshot in sorted(by_limit.items()):
            if isinstance(snapshot, dict):
                display = str(snapshot.get("limitName") or limit_id or "Limit")
                display = display.replace("_", " ").title()
                buckets.append((display, snapshot))
    main = response.get("rateLimits")
    if isinstance(main, dict):
        # Prefer the full multi-bucket map when present, but keep the historic
        # bucket for older servers or an empty/null map.
        if not buckets:
            buckets.append((str(main.get("limitName") or "Codex"), main))

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for prefix, snapshot in buckets:
        plan = str(snapshot.get("planType") or "")
        for key, title in (("primary", "Primary"), ("secondary", "Secondary")):
            window = snapshot.get(key)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            try:
                fraction = float(used) / 100.0
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                continue
            duration = window.get("windowDurationMins")
            if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                    or not math.isfinite(float(duration)) or duration <= 0
                    or duration > 10 * 365 * 24 * 60):
                duration = None
            reset = reset_iso(window.get("resetsAt"))
            identity = (prefix, key)
            if identity in seen:
                continue
            seen.add(identity)
            label = f"{prefix} · {window_label(title, window)}"
            out.append({
                "label": label,
                "percent": fraction,
                "resetsAt": reset,
                "windowDurationMins": duration if isinstance(duration, (int, float)) else None,
                "limitId": prefix,
                "window": key,
                "planType": plan,
            })
    return out


def extract_reset_credits(response: dict[str, Any], fetched_at: str) -> dict[str, Any] | None:
    summary = response.get("rateLimitResetCredits")
    if not isinstance(summary, dict):
        return None
    count_value = summary.get("availableCount")
    count = count_value if isinstance(count_value, int) and not isinstance(count_value, bool) and count_value >= 0 else None
    raw_credits = summary.get("credits")
    details_available = isinstance(raw_credits, list)
    grants = []
    if details_available:
        for credit in raw_credits:
            if not isinstance(credit, dict) or credit.get("status") != "available":
                continue
            if credit.get("resetType") != "codexRateLimits":
                continue
            grants.append({
                "title": str(credit.get("title") or "Reset credit")[:100],
                "description": str(credit.get("description") or "")[:240],
                "grantedAt": reset_iso(credit.get("grantedAt")),
                "expiresAt": reset_iso(credit.get("expiresAt")),
                "expiresAtKnown": "expiresAt" in credit,
            })
    return {
        "availableCount": count,
        "detailsAvailable": details_available,
        "details": grants,
                "source": "Codex account usage",
        "sourceAt": fetched_at,
    }


def collect() -> dict[str, Any]:
    env = runtime_env()
    codex = app_server_command(env)
    if not codex:
        raise RuntimeError("Codex CLI is not available")
    proc = subprocess.Popen(
        [codex, "-s", "read-only", "-a", "on-request", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        initialize = rpc_request(proc, 1, "initialize", {
            "clientInfo": {"name": "icedeep-ai-usage", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        if not isinstance(initialize.get("result"), dict):
            raise RuntimeError("Codex initialization failed")
        if proc.stdin is None:
            raise RuntimeError("Codex app-server input unavailable")
        proc.stdin.write((json.dumps({"method": "initialized", "params": {}}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        message = rpc_request(proc, 2, "account/rateLimits/read")
        response = message.get("result")
        if not isinstance(response, dict):
            raise RuntimeError("Codex returned no rate-limit data")
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        credits = extract_reset_credits(response, fetched_at)
        return {
            "schemaVersion": 1,
            "id": "codex",
            "name": "Codex",
            "status": "ok",
            "updatedAt": fetched_at,
            "replaceLimits": True,
            "source": "Codex account usage",
            "tierLabel": str((response.get("rateLimits") or {}).get("planType") or ""),
            "limits": extract_windows(response),
            "supplemental": {
                "bankedResets": credits,
                "nextFreeResetAt": {
                    "at": None,
                    "status": "not_reported",
                    "source": "Codex account usage",
                    "sourceAt": fetched_at,
                },
            },
        }
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


def state_path() -> Path:
    override = os.environ.get("AI_USAGE_MONITOR_CODEX_STATE")
    if override:
        return Path(override)
    state_home = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    return state_home / "ai-usage-monitor/codex.json"


def load_previous(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".codex-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"), ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="skip the 15-minute refresh throttle")
    args = parser.parse_args()
    path = state_path()
    lock_path = path.with_suffix(".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with lock_path.open("a", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Codex usage refresh already running")
                return 0

            previous = load_previous(path)
            last_attempt = parse_timestamp(previous.get("lastAttemptAt"))
            now = time.time()
            if not args.force and last_attempt is not None and now - last_attempt < REFRESH_SECONDS:
                print("Codex usage refresh is still within its 15-minute interval")
                return 0

            attempt_at = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
            try:
                record = collect()
            except Exception:
                record = previous or {
                    "schemaVersion": 1,
                    "id": "codex",
                    "name": "Codex",
                    "limits": [],
                    "supplemental": {},
                }
                record["supplementalError"] = True
                record["supplementalStatusText"] = "Codex reset-credit refresh failed; showing the last saved response"
                record["supplementalAttemptAt"] = attempt_at
                record["lastAttemptAt"] = attempt_at
                write_record(path, record)
                print("Codex usage refresh failed")
                return 1

            record["lastAttemptAt"] = attempt_at
            record["supplementalError"] = False
            record["supplementalStatusText"] = ""
            record["supplementalAttemptAt"] = attempt_at
            write_record(path, record)
            print(json.dumps({"status": "ok", "updatedAt": record["updatedAt"]}, separators=(",", ":")))
            return 0
    except OSError:
        print("Could not store Codex usage data")
        return 1


def parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        return stamp / 1000 if stamp > 10_000_000_000 else stamp
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


if __name__ == "__main__":
    raise SystemExit(main())
