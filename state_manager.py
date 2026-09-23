import os
import json
import re
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("state_manager")

DEFAULT_CONFIG_DIR = os.getenv("CONFIG_DIR", os.path.expanduser("~/.coderabbit"))
DEFAULT_STATE_FILE = os.getenv("STATE_PATH", os.path.join(DEFAULT_CONFIG_DIR, "automation_state.json"))

class StateManager:
    """Manages automation state, rate limit cooldowns, and PR attempt counters."""

    def __init__(self, state_path: str = DEFAULT_STATE_FILE):
        self.state_path = state_path
        self.ensure_state_exists()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "rate_limited": False,
            "rate_limit_expires_at": 0,
            "rate_limit_reason": "",
            "rate_limit_retry_after_seconds": 0,
            "rate_limit_set_at": 0,
            "attempt_counts": {},
            "active_reviews": {},
            "pr_statuses": {},
            "last_run_timestamp": ""
        }

    def ensure_state_exists(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        if not os.path.exists(self.state_path):
            self.save_state(self._default_state())

    def load_state(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    defaults = self._default_state()
                    for k, v in defaults.items():
                        if k not in data:
                            data[k] = v
                    return data
        except Exception as e:
            logger.error("Error reading state file at %s: %s", self.state_path, e)
        return self._default_state()

    def save_state(self, state_data: Dict[str, Any]) -> None:
        target_dir = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(target_dir, exist_ok=True)
        temp_path = f"{self.state_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
        os.replace(temp_path, self.state_path)

    @staticmethod
    def parse_retry_delay_from_text(cli_output: str, default_delay: int = 900) -> int:
        """
        Parses retry duration from CLI output or error text.
        Returns duration in seconds. Defaults to 900s (15 mins) on rate limit matches.
        """
        if not cli_output:
            return 0

        text = cli_output.lower()

        # Check for explicit phrases like "try again in X mins", "wait X minutes", "retry in X sec"
        pattern = r"(?:try again|retry|wait|in)\s*(?:after\s*)?([0-9]+(?:\.[0-9]+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b"
        match = re.search(pattern, text)
        if match:
            val = float(match.group(1))
            unit = match.group(2)
            if unit.startswith("s"):
                return max(10, int(val))
            elif unit.startswith("m"):
                return max(10, int(val * 60))
            elif unit.startswith("h"):
                return max(10, int(val * 3600))

        # Check for retry-after: 120 or similar headers in error responses
        header_match = re.search(r"retry-after[:\s=]+([0-9]+)", text)
        if header_match:
            return max(10, int(header_match.group(1)))

        # Rate limit or quota keywords detected without explicit duration
        keywords = ["429", "rate limit", "rate-limit", "rate_limit", "too many requests", "quota", "exhausted", "throttled"]
        if any(k in text for k in keywords):
            return default_delay

        return 0

    def is_rate_limit_active(self) -> Tuple[bool, int, str]:
        """
        Checks if rate limit cooldown is active.
        Returns: (is_active, remaining_seconds, reason)
        """
        state = self.load_state()
        if not state.get("rate_limited", False):
            return False, 0, ""

        expires_at = state.get("rate_limit_expires_at", 0)
        now = time.time()
        remaining = int(expires_at - now)

        if remaining <= 0:
            # Cooldown expired; clear state
            self.clear_rate_limit()
            return False, 0, ""

        return True, remaining, state.get("rate_limit_reason", "Rate limit cooldown active")

    def record_rate_limit(self, duration_seconds: int, reason: str = "Rate limit reached", pr_key: Optional[str] = None) -> None:
        """Activates rate limit cooldown and records reason."""
        now = time.time()
        duration_seconds = max(10, duration_seconds)
        expires_at = now + duration_seconds

        state = self.load_state()
        state["rate_limited"] = True
        state["rate_limit_set_at"] = now
        state["rate_limit_expires_at"] = expires_at
        state["rate_limit_retry_after_seconds"] = duration_seconds
        state["rate_limit_reason"] = reason

        if pr_key:
            state["attempt_counts"][pr_key] = state["attempt_counts"].get(pr_key, 0) + 1

        self.save_state(state)
        logger.warning("Rate limit recorded: %ds (reason: %s). Expires at %s", duration_seconds, reason, expires_at)

    def clear_rate_limit(self) -> None:
        """Clears active rate limit cooldown."""
        state = self.load_state()
        state["rate_limited"] = False
        state["rate_limit_expires_at"] = 0
        state["rate_limit_reason"] = ""
        state["rate_limit_retry_after_seconds"] = 0
        state["rate_limit_set_at"] = 0
        self.save_state(state)
        logger.info("Rate limit cleared.")

    def get_attempt_count(self, pr_key: str) -> int:
        state = self.load_state()
        return state.get("attempt_counts", {}).get(pr_key, 0)

    def increment_attempt_count(self, pr_key: str) -> int:
        state = self.load_state()
        counts = state.get("attempt_counts", {})
        counts[pr_key] = counts.get(pr_key, 0) + 1
        state["attempt_counts"] = counts
        self.save_state(state)
        return counts[pr_key]

    def reset_attempt_count(self, pr_key: str) -> None:
        state = self.load_state()
        counts = state.get("attempt_counts", {})
        if pr_key in counts:
            del counts[pr_key]
            state["attempt_counts"] = counts
            self.save_state(state)

    def set_pr_reviewing(self, pr_key: str, is_reviewing: bool, attempt: int = 1) -> None:
        state = self.load_state()
        active = state.get("active_reviews", {})
        if is_reviewing:
            active[pr_key] = {
                "started_at": time.time(),
                "attempt": attempt
            }
        else:
            active.pop(pr_key, None)
        state["active_reviews"] = active
        self.save_state(state)

    def is_pr_reviewing(self, pr_key: str) -> bool:
        state = self.load_state()
        return pr_key in state.get("active_reviews", {})

    def record_pr_status(self, pr_key: str, status_info: Dict[str, Any]) -> None:
        state = self.load_state()
        statuses = state.get("pr_statuses", {})
        status_info["updated_at"] = datetime.now(timezone.utc).isoformat()
        statuses[pr_key] = status_info
        state["pr_statuses"] = statuses
        self.save_state(state)

    def get_pr_status(self, pr_key: str) -> Optional[Dict[str, Any]]:
        state = self.load_state()
        return state.get("pr_statuses", {}).get(pr_key)

    def get_all_pr_statuses(self) -> Dict[str, Any]:
        state = self.load_state()
        return state.get("pr_statuses", {})

    def record_last_run(self, timestamp: Optional[str] = None) -> None:
        state = self.load_state()
        state["last_run_timestamp"] = timestamp or datetime.now(timezone.utc).isoformat()
        self.save_state(state)

    def get_last_run(self) -> str:
        state = self.load_state()
        return state.get("last_run_timestamp", "")
