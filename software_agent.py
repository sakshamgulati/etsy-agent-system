"""
software_agent.py — Always-on SRE process.
Watches errors.log in real time, auto-heals minor issues,
stages patches for Saksham's approval via Telegram.
Run this as a separate process alongside main.py.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import psutil
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent
_ENV_PATH = _PROJECT_ROOT / ".env"

# Load .env FIRST so all os.environ.get() calls below see the values from .env
load_dotenv(_ENV_PATH)

_LOGS_DIR = _PROJECT_ROOT / "logs"
_PATCHES_DIR = _LOGS_DIR / "patches"
_ERRORS_LOG = _LOGS_DIR / "errors.log"
_AGENT_LOG = _LOGS_DIR / "software_agent.log"
_PROMPTS_DIR = _PROJECT_ROOT / "prompts"
_DB_PATH = os.environ.get("ETSY_DB_PATH", str(_PROJECT_ROOT / "db" / "etsy_agent.db"))

# Ensure directories exist
_LOGS_DIR.mkdir(parents=True, exist_ok=True)
_PATCHES_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [software_agent] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_AGENT_LOG)),
    ],
)
log = logging.getLogger("software_agent")

# ---------------------------------------------------------------------------
# Deduplication: track seen errors (error_key -> datetime first seen)
# ---------------------------------------------------------------------------

_seen_errors: dict[str, datetime] = {}
_DEDUP_WINDOW = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# Agent expected intervals (for watchdog)
# ---------------------------------------------------------------------------

# Maps agent_name -> max expected minutes between runs
_AGENT_INTERVALS: dict[str, int] = {
    "analytics": 20,
    "seo": 25 * 60,        # 25 hours in minutes
    "finance": 170 * 60,   # 170 hours in minutes
    "marketing": 25 * 60,  # 25 hours in minutes
    "ceo": 25 * 60,        # 25 hours in minutes
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_prompt_template() -> str:
    prompt_path = _PROMPTS_DIR / "software_agent.md"
    return prompt_path.read_text()


def _get_telegram():
    """Lazily import and construct TelegramClient."""
    from integrations.telegram_client import TelegramClient
    return TelegramClient()


def _is_duplicate(error_key: str) -> bool:
    """Return True if this error was seen in the last 5 minutes."""
    now = _utcnow()
    if error_key in _seen_errors:
        if now - _seen_errors[error_key] < _DEDUP_WINDOW:
            return True
    _seen_errors[error_key] = now
    # Prune old entries
    stale = [k for k, v in _seen_errors.items() if now - v >= _DEDUP_WINDOW * 2]
    for k in stale:
        del _seen_errors[k]
    return False


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def _is_auth_error(message: str) -> bool:
    return "EtsyAuthError" in message or "401 Unauthorized" in message or "401" in message


def _is_rate_limit_error(message: str) -> bool:
    return "429" in message or "Too Many Requests" in message


def _is_network_error(message: str) -> bool:
    return any(t in message for t in ("ConnectionError", "TimeoutError", "requests.exceptions.Connection",
                                       "requests.exceptions.Timeout"))


def _has_python_traceback(message: str) -> bool:
    return "Traceback (most recent call last)" in message


# ---------------------------------------------------------------------------
# Traceback parsing
# ---------------------------------------------------------------------------

def _parse_traceback(text: str) -> tuple[str | None, int | None]:
    """
    Extract the last file path and line number from a Python traceback.
    Returns (file_path, line_number) or (None, None).
    """
    # Match lines like: File "/path/to/file.py", line 42, in some_function
    pattern = re.compile(r'File "([^"]+)", line (\d+)')
    matches = pattern.findall(text)
    if not matches:
        return None, None
    # The last match is the innermost frame (actual error location)
    file_path, line_num = matches[-1]
    return file_path, int(line_num)


def _read_source_context(file_path: str, line_num: int, context: int = 20) -> str:
    """Read ±context lines around line_num from file_path."""
    try:
        source_lines = Path(file_path).read_text().splitlines()
        start = max(0, line_num - context - 1)
        end = min(len(source_lines), line_num + context)
        numbered = []
        for i, line in enumerate(source_lines[start:end], start=start + 1):
            marker = ">>>" if i == line_num else "   "
            numbered.append(f"{marker} {i:4d}: {line}")
        return "\n".join(numbered)
    except Exception as exc:
        log.warning("Could not read source context for %s: %s", file_path, exc)
        return f"(could not read file: {exc})"


# ---------------------------------------------------------------------------
# Claude diagnosis
# ---------------------------------------------------------------------------

def _call_claude(error_log: str, file_path: str, line_num: int, source_code: str) -> dict:
    """Send the error to Claude and return the parsed JSON diagnosis."""
    template = _load_prompt_template()
    prompt = template.replace("{error_log}", error_log)
    prompt = prompt.replace("{source_code}", source_code)
    prompt = prompt.replace("{file_path}", file_path)
    prompt = prompt.replace("{line_context}", str(line_num))

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Claude response was not valid JSON: %s", raw[:200])
        return {
            "diagnosis": raw[:500],
            "fix_description": "Could not parse Claude response.",
            "patch": None,
            "confidence": "low",
            "requires_human": True,
        }


# ---------------------------------------------------------------------------
# Patch management
# ---------------------------------------------------------------------------

ALLOWED_PATCH_EXTENSIONS = {".py"}
PROJECT_ROOT = _PROJECT_ROOT  # alias used by _is_safe_patch


def _is_safe_patch(patch_content: str) -> bool:
    """Verify patch only touches .py files within the project root."""
    for line in patch_content.splitlines():
        if line.startswith(("--- ", "+++ ")):
            # Extract file path from diff header
            parts = line.split()
            if len(parts) >= 2:
                path_str = parts[1]
                if path_str == "/dev/null":
                    continue
                path = Path(path_str)
                # Must be a .py file
                if path.suffix not in ALLOWED_PATCH_EXTENSIONS:
                    return False
                # Must not escape project root (no absolute paths, no ..)
                try:
                    resolved = (PROJECT_ROOT / path).resolve()
                    resolved.relative_to(PROJECT_ROOT.resolve())
                except (ValueError, OSError):
                    return False
    return True


def _save_patch(patch_id: str, patch_text: str, error_summary: str, diagnosis: dict) -> Path:
    """Write patch and metadata to logs/patches/."""
    patch_file = _PATCHES_DIR / f"patch_{patch_id}.txt"
    meta_file = _PATCHES_DIR / f"patch_{patch_id}.meta.json"

    patch_file.write_text(patch_text)
    meta_file.write_text(json.dumps({
        "patch_id": patch_id,
        "error_summary": error_summary,
        "diagnosis": diagnosis,
        "created_at": _utcnow().isoformat(),
    }, indent=2))

    log.info("Patch saved: %s", patch_file)
    return patch_file


def _check_and_apply_patches():
    """Check for .flag files and apply the corresponding patches."""
    for flag_file in _PATCHES_DIR.glob("apply_*.flag"):
        patch_id = flag_file.stem.replace("apply_", "")
        patch_file = _PATCHES_DIR / f"patch_{patch_id}.txt"
        meta_file = _PATCHES_DIR / f"patch_{patch_id}.meta.json"

        log.info("Found apply flag for patch %s — attempting to apply.", patch_id)

        try:
            telegram = _get_telegram()
        except Exception:
            telegram = None

        if not patch_file.exists():
            log.error("Patch file not found for patch_id=%s", patch_id)
            flag_file.unlink(missing_ok=True)
            continue

        try:
            patch_content = patch_file.read_text()
            if not _is_safe_patch(patch_content):
                log.warning("Unsafe patch rejected for patch_id=%s (non-.py file or path traversal attempt).", patch_id)
                if telegram:
                    telegram.send_alert(
                        "⚠️ Unsafe patch rejected (non-.py file or path traversal attempt)"
                    )
                flag_file.unlink(missing_ok=True)
                continue

            result = subprocess.run(
                ["patch", "-p0", "--forward", "-i", str(patch_file)],
                capture_output=True,
                text=True,
                cwd=str(_PROJECT_ROOT),
            )
            if result.returncode == 0:
                log.info("Patch %s applied successfully.", patch_id)
                _restart_affected_agent(meta_file)
                msg = f"Patch `{patch_id}` applied successfully and affected agent restarted."
                if telegram:
                    telegram.send_message(f"Patch `{patch_id}` applied and agent restarted.")
            else:
                log.error("Patch %s failed: %s", patch_id, result.stderr)
                if telegram:
                    telegram.send_alert(
                        f"Failed to apply patch `{patch_id}`:\n```{result.stderr[:500]}```"
                    )
        except Exception as exc:
            log.exception("Exception while applying patch %s: %s", patch_id, exc)
            if telegram:
                try:
                    telegram.send_alert(f"Exception applying patch `{patch_id}`: {exc}")
                except Exception:
                    pass
        finally:
            flag_file.unlink(missing_ok=True)


def _restart_affected_agent(meta_file: Path):
    """Signal main.py to reload by writing a restart flag, rather than killing it."""
    try:
        if not meta_file.exists():
            return
        meta = json.loads(meta_file.read_text())
        file_path = meta.get("diagnosis", {}).get("file_path", "")
        restart_flag = _PROJECT_ROOT / "logs" / "restart_requested.flag"
        restart_flag.touch()
        log.info(
            "restart_flag_written",
            extra={
                "file": file_path,
                "flag": str(restart_flag),
                "note": "main.py watchdog or systemd will handle restart",
            },
        )
        try:
            telegram = _get_telegram()
            telegram.send_alert(
                f"🔄 Patch applied to `{Path(file_path).name}`. "
                f"Restart flag written — please restart main.py or let systemd handle it."
            )
        except Exception as exc:
            log.error("Telegram alert failed in _restart_affected_agent: %s", exc)
    except Exception as exc:
        log.warning("Could not write restart flag: %s", exc)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

def _handle_auth_error(log_entry: dict):
    log.info("Auto-handling auth error: refreshing Etsy token.")
    try:
        from integrations.etsy_client import EtsyClient
        client = EtsyClient()
        client._refresh_token()
        log.info("Token refreshed successfully.")
    except Exception as exc:
        log.error("Token refresh failed: %s", exc)
        try:
            _get_telegram().send_alert(f"Etsy token refresh FAILED: {exc}")
        except Exception:
            pass


def _handle_rate_limit(log_entry: dict):
    log.info("Rate limit (429) detected — EtsyClient handles backoff automatically. Logging only.")


def _handle_network_error(log_entry: dict):
    log.info("Transient network error detected — will retry on next run.")


def _handle_python_traceback(log_entry: dict, raw_line: str):
    """Parse traceback, call Claude, stage patch, notify via Telegram."""
    message = log_entry.get("action", raw_line)

    # Extract file + line from traceback
    file_path, line_num = _parse_traceback(message)
    if not file_path or not line_num:
        log.warning("Could not parse file/line from traceback. Sending raw alert.")
        _handle_unknown_error(log_entry, raw_line)
        return

    # Build a short error summary (last line of traceback or action)
    lines = message.strip().splitlines()
    error_summary = lines[-1] if lines else message[:200]

    log.info("Diagnosing traceback in %s:%d with Claude...", file_path, line_num)

    source_code = _read_source_context(file_path, line_num)
    try:
        diagnosis = _call_claude(message, file_path, line_num, source_code)
    except Exception as exc:
        log.error("Claude call failed: %s", exc)
        _handle_unknown_error(log_entry, raw_line)
        return

    patch_text = diagnosis.get("patch")
    patch_id = _utcnow().strftime("%Y%m%d_%H%M%S")

    if patch_text:
        _save_patch(patch_id, patch_text, error_summary, diagnosis)
        patch_info = f"Reply /apply\\_{patch_id} to deploy, /skip\\_{patch_id} to ignore"
    else:
        patch_info = "No auto-patch available — manual fix required."

    # Send Telegram alert
    agent_name = log_entry.get("agent", "unknown")
    file_basename = Path(file_path).name
    msg = (
        f"Bug detected in `{file_basename}:{line_num}`\n"
        f"Agent: `{agent_name}`\n"
        f"Error: `{error_summary}`\n\n"
        f"Diagnosis: {diagnosis.get('diagnosis', 'N/A')}\n\n"
        f"Proposed fix: {diagnosis.get('fix_description', 'N/A')}\n"
        f"Confidence: {diagnosis.get('confidence', 'unknown')}\n\n"
        f"{patch_info}"
    )
    try:
        _get_telegram().send_message(f"🔧 {msg}")
    except Exception as exc:
        log.error("Failed to send Telegram alert: %s", exc)


def _handle_unknown_error(log_entry: dict, raw_line: str):
    """Send immediate Telegram alert for unclassified errors."""
    message = log_entry.get("action", raw_line)
    agent_name = log_entry.get("agent", "unknown")
    ts = log_entry.get("ts", "")
    alert_text = (
        f"Unknown critical error in agent `{agent_name}` at {ts}:\n"
        f"```\n{message[:800]}\n```"
    )
    log.warning("Sending unknown error alert to Telegram.")
    try:
        _get_telegram().send_alert(alert_text)
    except Exception as exc:
        log.error("Failed to send Telegram alert: %s", exc)


# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------

def tail_errors_log(log_path: Path):
    """Generator that yields new lines appended to log_path (like `tail -f`)."""
    # Open and seek to end on first run
    position = 0
    if log_path.exists():
        position = log_path.stat().st_size

    while True:
        time.sleep(5)
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r") as f:
                f.seek(position)
                new_lines = f.readlines()
                position = f.tell()
            for line in new_lines:
                line = line.strip()
                if line:
                    yield line
        except Exception as exc:
            log.warning("Error reading %s: %s", log_path, exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_software_agent():
    """Main loop — tails errors.log and processes each new line."""
    log.info("Software Agent started. Watching %s", _ERRORS_LOG)

    patch_check_counter = 0  # check patches every 6 tails (≈30s)

    for raw_line in tail_errors_log(_ERRORS_LOG):
        # Check for patch apply flags periodically
        patch_check_counter += 1
        if patch_check_counter >= 6:
            patch_check_counter = 0
            try:
                _check_and_apply_patches()
            except Exception as exc:
                log.warning("Patch check error: %s", exc)

        # Parse log line as JSON
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            log.debug("Non-JSON log line (skipping): %s", raw_line[:100])
            continue

        # Only process ERROR level lines
        if entry.get("level") != "ERROR":
            continue

        message = entry.get("action", "")

        # Deduplication
        error_key = message[:200]
        if _is_duplicate(error_key):
            log.debug("Duplicate error suppressed: %s", error_key[:80])
            continue

        log.info("Processing new ERROR: %s", error_key[:120])

        # Classify and handle
        if _is_auth_error(message):
            _handle_auth_error(entry)
        elif _is_rate_limit_error(message):
            _handle_rate_limit(entry)
        elif _is_network_error(message):
            _handle_network_error(entry)
        elif _has_python_traceback(message):
            _handle_python_traceback(entry, raw_line)
        else:
            _handle_unknown_error(entry, raw_line)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def run_watchdog():
    """
    Runs in a background thread.
    Every 10 minutes, checks agent last-run times from SQLite.
    Alerts via Telegram if any agent is overdue by 2x its expected interval.
    Also checks if main.py process is alive.
    """
    log.info("Watchdog started.")

    while True:
        time.sleep(600)  # 10 minutes

        # --- Check main.py is alive ---
        main_alive = any(
            "main.py" in " ".join(proc.info.get("cmdline") or [])
            for proc in psutil.process_iter(["cmdline"])
            if proc.info.get("cmdline")
        )
        if not main_alive:
            log.warning("main.py process not detected!")
            try:
                _get_telegram().send_alert(
                    "main.py process is NOT running. The Etsy Agent System may be down."
                )
            except Exception as exc:
                log.error("Telegram alert failed: %s", exc)

        # --- Check agent last-run times ---
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT agent_name, MAX(started_at) AS last_run
                FROM agent_runs
                GROUP BY agent_name
                """
            ).fetchall()
            conn.close()
        except Exception as exc:
            log.warning("Watchdog DB query failed: %s", exc)
            continue

        last_runs = {row["agent_name"]: row["last_run"] for row in rows}
        now = _utcnow()

        for agent_name, expected_minutes in _AGENT_INTERVALS.items():
            last_run_str = last_runs.get(agent_name)
            if last_run_str is None:
                log.debug("No run recorded yet for agent: %s", agent_name)
                continue

            try:
                # Parse ISO timestamp (ends in Z)
                last_run_dt = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
                elapsed_minutes = (now - last_run_dt).total_seconds() / 60
                overdue_threshold = expected_minutes * 2

                if elapsed_minutes > overdue_threshold:
                    elapsed_h = elapsed_minutes / 60
                    log.warning(
                        "Agent %s is overdue: last run %.1f hours ago (threshold %.1f hours)",
                        agent_name, elapsed_h, overdue_threshold / 60,
                    )
                    try:
                        _get_telegram().send_alert(
                            f"{agent_name} agent hasn't run in {elapsed_h:.1f}h "
                            f"(expected every {expected_minutes / 60:.1f}h). May be stuck."
                        )
                    except Exception as exc:
                        log.error("Telegram alert failed: %s", exc)
            except Exception as exc:
                log.warning("Could not parse last_run for %s: %s", agent_name, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    watchdog_thread = threading.Thread(target=run_watchdog, daemon=True)
    watchdog_thread.start()
    run_software_agent()
