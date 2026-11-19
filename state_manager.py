import os
import json
import re
import time
import copy
import uuid
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional, List, Set
from timezone_utils import now_app_tz, parse_iso_datetime

logger = logging.getLogger("state_manager")

DEFAULT_CONFIG_DIR = os.getenv("CONFIG_DIR", os.path.expanduser("~/.coderabbit"))
DEFAULT_STATE_FILE = os.getenv("STATE_PATH", os.path.join(DEFAULT_CONFIG_DIR, "automation_state.json"))


class StateManager:
    """Manages automation state, rate limit cooldowns, logs, and queues backed by SQLite (WAL mode)."""

    _STANDARD_RUN_KEYS = frozenset({
        "log_id", "pr_key", "repo", "pr_number", "account_id", "coderabbit_account",
        "timestamp", "status", "review_outcome", "event", "title", "author",
        "head_sha", "base_ref", "head_ref", "html_url", "report_file",
        "elapsed_seconds", "file_count", "findings_count", "critical_major_count",
        "minor_count", "attempt", "summary", "error", "stdout", "stderr", "findings"
    })

    def __init__(self, state_path: str = DEFAULT_STATE_FILE, db_path: Optional[str] = None):
        self.state_path = state_path
        if db_path:
            self.db_path = db_path
        elif state_path.lower().endswith((".db", ".sqlite", ".sqlite3")):
            self.db_path = state_path
        elif state_path == DEFAULT_STATE_FILE and os.getenv("STATE_DB_PATH"):
            self.db_path = os.getenv("STATE_DB_PATH", "")
        else:
            root, _ = os.path.splitext(state_path)
            self.db_path = f"{root}.db"

        self._lock = threading.RLock()
        self._version: int = 0
        self._conn: Optional[sqlite3.Connection] = None
        self.ensure_state_exists()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            db_dir = os.path.dirname(os.path.abspath(self.db_path))
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA cache_size=-8000")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        """Closes the underlying SQLite connection safely."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def get_version(self) -> int:
        """Returns a monotonically increasing version counter bumped on state mutations."""
        with self._lock:
            return self._version

    def _bump_version(self) -> None:
        self._version += 1

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
            "last_run_timestamp": "",
            "review_queue": [],
            "account_rate_limits": {},
            "last_account_index": -1,
            "account_logs": {},
            "daily_snapshots": [],
            "review_history": [],
            "pr_logs": {}
        }

    def ensure_state_exists(self) -> None:
        with self._lock:
            self._init_schema()
            self._migrate_legacy_json_if_needed()

    def _init_schema(self) -> None:
        conn = self._get_conn()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_rate_limits (
                    account_id TEXT PRIMARY KEY,
                    rate_limited INTEGER NOT NULL DEFAULT 1,
                    set_at REAL NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL DEFAULT 0,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS pr_attempts (
                    pr_key TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS active_reviews (
                    pr_key TEXT PRIMARY KEY,
                    started_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    phase TEXT NOT NULL DEFAULT 'RUNNING_CODERABBIT',
                    message TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS pr_statuses (
                    pr_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT '',
                    head_sha TEXT NOT NULL DEFAULT '',
                    review_outcome TEXT NOT NULL DEFAULT '',
                    review_state TEXT NOT NULL DEFAULT '',
                    report_file TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_queue (
                    position INTEGER PRIMARY KEY,
                    pr_key TEXT UNIQUE NOT NULL,
                    force INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_runs (
                    log_id TEXT PRIMARY KEY,
                    pr_key TEXT NOT NULL DEFAULT '',
                    repo TEXT NOT NULL DEFAULT '',
                    pr_number INTEGER NOT NULL DEFAULT 0,
                    account_id TEXT NOT NULL DEFAULT 'default',
                    coderabbit_account TEXT NOT NULL DEFAULT '',
                    timestamp TEXT NOT NULL DEFAULT '',
                    timestamp_epoch REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '',
                    review_outcome TEXT NOT NULL DEFAULT '',
                    event TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    head_sha TEXT NOT NULL DEFAULT '',
                    base_ref TEXT NOT NULL DEFAULT '',
                    head_ref TEXT NOT NULL DEFAULT '',
                    html_url TEXT NOT NULL DEFAULT '',
                    report_file TEXT NOT NULL DEFAULT '',
                    elapsed_seconds REAL,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    findings_count INTEGER NOT NULL DEFAULT 0,
                    critical_major_count INTEGER NOT NULL DEFAULT 0,
                    minor_count INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    stderr_preview TEXT NOT NULL DEFAULT '',
                    in_pr_logs INTEGER NOT NULL DEFAULT 1,
                    in_account_logs INTEGER NOT NULL DEFAULT 1,
                    extra_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_review_runs_pr_ts
                    ON review_runs(pr_key, timestamp_epoch DESC);
                CREATE INDEX IF NOT EXISTS idx_review_runs_acc_ts
                    ON review_runs(account_id, timestamp_epoch DESC);
                CREATE INDEX IF NOT EXISTS idx_review_runs_ts
                    ON review_runs(timestamp_epoch DESC);
                CREATE INDEX IF NOT EXISTS idx_review_runs_repo
                    ON review_runs(repo);
                CREATE INDEX IF NOT EXISTS idx_review_runs_outcome
                    ON review_runs(review_outcome);

                CREATE TABLE IF NOT EXISTS review_run_logs (
                    log_id TEXT PRIMARY KEY REFERENCES review_runs(log_id) ON DELETE CASCADE,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    findings_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id TEXT PRIMARY KEY,
                    date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_friendly TEXT NOT NULL DEFAULT '',
                    open_prs_count INTEGER NOT NULL DEFAULT 0,
                    merged_prs_count INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_daily_snapshots_created
                    ON daily_snapshots(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_daily_snapshots_date
                    ON daily_snapshots(date);

                CREATE TABLE IF NOT EXISTS pr_cache (
                    pr_key TEXT PRIMARY KEY,
                    repo TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    cached_at REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_pr_cache_repo
                    ON pr_cache(repo);
                """
            )

    def _get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        conn = self._get_conn()
        row = conn.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_kv(self, key: str, value: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO kv_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _parse_timestamp_epoch(self, ts_str: str) -> float:
        if not ts_str:
            return now_app_tz().timestamp()
        try:
            dt = parse_iso_datetime(ts_str)
            if dt:
                return dt.timestamp()
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _split_pr_key(pr_key: str) -> Tuple[str, int]:
        repo_val = ""
        pr_num_val = 0
        if pr_key and "#" in pr_key:
            parts = pr_key.split("#", 1)
            repo_val = parts[0]
            try:
                pr_num_val = int(parts[1])
            except (ValueError, TypeError):
                pr_num_val = 0
        return repo_val, pr_num_val

    def _upsert_review_run_locked(
        self,
        entry: Dict[str, Any],
        pr_key: Optional[str] = None,
        account_id: Optional[str] = None,
        in_pr_logs: Optional[int] = None,
        in_account_logs: Optional[int] = None,
    ) -> str:
        conn = self._get_conn()
        now = now_app_tz()
        item = dict(entry)
        resolved_pr_key = str(pr_key if pr_key is not None else (item.get("pr_key") or ""))
        resolved_acc_id = str(account_id if account_id is not None else (item.get("account_id") or "default")) or "default"

        log_id = item.get("log_id")
        if not log_id:
            suffix = uuid.uuid4().hex[:6]
            key_slug = resolved_pr_key.replace("/", "_").replace("#", "_") if resolved_pr_key else f"acc_{resolved_acc_id}"
            log_id = f"{int(now.timestamp() * 1000000)}_{suffix}_{key_slug}"

        ts_str = str(item.get("timestamp") or now.isoformat())
        ts_epoch = self._parse_timestamp_epoch(ts_str)

        parsed_repo, parsed_num = self._split_pr_key(resolved_pr_key)
        repo_val = str(item.get("repo") or parsed_repo or "")
        pr_num_raw = item.get("pr_number")
        try:
            pr_num_val = int(pr_num_raw) if pr_num_raw else parsed_num
        except (ValueError, TypeError):
            pr_num_val = parsed_num

        elapsed_raw = item.get("elapsed_seconds")
        elapsed_val: Optional[float] = None
        if elapsed_raw is not None:
            try:
                elapsed_val = float(elapsed_raw)
            except (ValueError, TypeError):
                elapsed_val = None

        stdout_text = str(item.get("stdout") or "")
        stderr_text = str(item.get("stderr") or "")
        findings_val = item.get("findings")
        if not isinstance(findings_val, list):
            findings_val = []
        findings_json = json.dumps(findings_val)

        extra = {
            k: v for k, v in item.items()
            if k not in self._STANDARD_RUN_KEYS
        }
        extra_json = json.dumps(extra)

        pr_flag = 1 if in_pr_logs is None else int(bool(in_pr_logs))
        acc_flag = 1 if in_account_logs is None else int(bool(in_account_logs))

        conn.execute(
            """
            INSERT INTO review_runs (
                log_id, pr_key, repo, pr_number, account_id, coderabbit_account,
                timestamp, timestamp_epoch, status, review_outcome, event,
                title, author, head_sha, base_ref, head_ref, html_url, report_file,
                elapsed_seconds, file_count, findings_count, critical_major_count,
                minor_count, attempt, summary, error, stderr_preview,
                in_pr_logs, in_account_logs, extra_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(log_id) DO UPDATE SET
                pr_key = CASE WHEN excluded.pr_key != '' THEN excluded.pr_key ELSE review_runs.pr_key END,
                repo = CASE WHEN excluded.repo != '' THEN excluded.repo ELSE review_runs.repo END,
                pr_number = CASE WHEN excluded.pr_number != 0 THEN excluded.pr_number ELSE review_runs.pr_number END,
                account_id = CASE WHEN excluded.account_id != '' THEN excluded.account_id ELSE review_runs.account_id END,
                coderabbit_account = CASE WHEN excluded.coderabbit_account != '' THEN excluded.coderabbit_account ELSE review_runs.coderabbit_account END,
                timestamp = CASE WHEN excluded.timestamp != '' THEN excluded.timestamp ELSE review_runs.timestamp END,
                timestamp_epoch = CASE WHEN excluded.timestamp_epoch != 0 THEN excluded.timestamp_epoch ELSE review_runs.timestamp_epoch END,
                status = CASE WHEN excluded.status != '' THEN excluded.status ELSE review_runs.status END,
                review_outcome = CASE WHEN excluded.review_outcome != '' THEN excluded.review_outcome ELSE review_runs.review_outcome END,
                event = CASE WHEN excluded.event != '' THEN excluded.event ELSE review_runs.event END,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE review_runs.title END,
                author = CASE WHEN excluded.author != '' THEN excluded.author ELSE review_runs.author END,
                head_sha = CASE WHEN excluded.head_sha != '' THEN excluded.head_sha ELSE review_runs.head_sha END,
                base_ref = CASE WHEN excluded.base_ref != '' THEN excluded.base_ref ELSE review_runs.base_ref END,
                head_ref = CASE WHEN excluded.head_ref != '' THEN excluded.head_ref ELSE review_runs.head_ref END,
                html_url = CASE WHEN excluded.html_url != '' THEN excluded.html_url ELSE review_runs.html_url END,
                report_file = CASE WHEN excluded.report_file != '' THEN excluded.report_file ELSE review_runs.report_file END,
                elapsed_seconds = COALESCE(excluded.elapsed_seconds, review_runs.elapsed_seconds),
                file_count = CASE WHEN excluded.file_count != 0 THEN excluded.file_count ELSE review_runs.file_count END,
                findings_count = CASE WHEN excluded.findings_count != 0 THEN excluded.findings_count ELSE review_runs.findings_count END,
                critical_major_count = CASE WHEN excluded.critical_major_count != 0 THEN excluded.critical_major_count ELSE review_runs.critical_major_count END,
                minor_count = CASE WHEN excluded.minor_count != 0 THEN excluded.minor_count ELSE review_runs.minor_count END,
                attempt = CASE WHEN excluded.attempt != 0 THEN excluded.attempt ELSE review_runs.attempt END,
                summary = CASE WHEN excluded.summary != '' THEN excluded.summary ELSE review_runs.summary END,
                error = CASE WHEN excluded.error != '' THEN excluded.error ELSE review_runs.error END,
                stderr_preview = CASE WHEN excluded.stderr_preview != '' THEN excluded.stderr_preview ELSE review_runs.stderr_preview END,
                in_pr_logs = CASE WHEN ? IS NOT NULL THEN excluded.in_pr_logs ELSE review_runs.in_pr_logs END,
                in_account_logs = CASE WHEN ? IS NOT NULL THEN excluded.in_account_logs ELSE review_runs.in_account_logs END,
                extra_json = CASE WHEN excluded.extra_json != '{}' THEN excluded.extra_json ELSE review_runs.extra_json END
            """,
            (
                log_id,
                resolved_pr_key,
                repo_val,
                pr_num_val,
                resolved_acc_id,
                str(item.get("coderabbit_account") or ""),
                ts_str,
                ts_epoch,
                str(item.get("status") or ""),
                str(item.get("review_outcome") or ""),
                str(item.get("event") or ""),
                str(item.get("title") or ""),
                str(item.get("author") or ""),
                str(item.get("head_sha") or ""),
                str(item.get("base_ref") or ""),
                str(item.get("head_ref") or ""),
                str(item.get("html_url") or ""),
                str(item.get("report_file") or ""),
                elapsed_val,
                int(item.get("file_count") or 0),
                int(item.get("findings_count") or 0),
                int(item.get("critical_major_count") or 0),
                int(item.get("minor_count") or 0),
                int(item.get("attempt") or 0),
                str(item.get("summary") or ""),
                str(item.get("error") or ""),
                stderr_text[:4000],
                pr_flag,
                acc_flag,
                extra_json,
                in_pr_logs,
                in_account_logs,
            ),
        )

        conn.execute(
            """
            INSERT INTO review_run_logs (log_id, stdout, stderr, findings_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(log_id) DO UPDATE SET
                stdout = CASE WHEN excluded.stdout != '' THEN excluded.stdout ELSE review_run_logs.stdout END,
                stderr = CASE WHEN excluded.stderr != '' THEN excluded.stderr ELSE review_run_logs.stderr END,
                findings_json = CASE WHEN excluded.findings_json != '[]' THEN excluded.findings_json ELSE review_run_logs.findings_json END
            """,
            (log_id, stdout_text, stderr_text, findings_json),
        )
        return log_id

    @staticmethod
    def _row_to_run_dict(row: sqlite3.Row, include_blobs: bool = False) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        raw_extra = row["extra_json"] if "extra_json" in row.keys() else "{}"
        if raw_extra:
            try:
                parsed = json.loads(raw_extra)
                if isinstance(parsed, dict):
                    extra = parsed
            except Exception:
                pass

        result = dict(extra)
        result["log_id"] = row["log_id"]
        result["pr_key"] = row["pr_key"]
        result["repo"] = row["repo"]
        result["pr_number"] = row["pr_number"]
        result["account_id"] = row["account_id"]
        if row["coderabbit_account"]:
            result["coderabbit_account"] = row["coderabbit_account"]
        result["timestamp"] = row["timestamp"]
        if row["status"]:
            result["status"] = row["status"]
        if row["review_outcome"]:
            result["review_outcome"] = row["review_outcome"]
        if row["event"]:
            result["event"] = row["event"]
        if row["title"]:
            result["title"] = row["title"]
        if row["author"]:
            result["author"] = row["author"]
        if row["head_sha"]:
            result["head_sha"] = row["head_sha"]
        if row["base_ref"]:
            result["base_ref"] = row["base_ref"]
        if row["head_ref"]:
            result["head_ref"] = row["head_ref"]
        if row["html_url"]:
            result["html_url"] = row["html_url"]
        if row["report_file"]:
            result["report_file"] = row["report_file"]
        if row["elapsed_seconds"] is not None:
            result["elapsed_seconds"] = row["elapsed_seconds"]
        if row["file_count"]:
            result["file_count"] = row["file_count"]
        if row["findings_count"]:
            result["findings_count"] = row["findings_count"]
        if row["critical_major_count"]:
            result["critical_major_count"] = row["critical_major_count"]
        if row["minor_count"]:
            result["minor_count"] = row["minor_count"]
        if row["attempt"]:
            result["attempt"] = row["attempt"]
        if row["summary"]:
            result["summary"] = row["summary"]
        if row["error"]:
            result["error"] = row["error"]

        if include_blobs:
            result["stdout"] = row["stdout"] if "stdout" in row.keys() and row["stdout"] is not None else ""
            result["stderr"] = row["stderr"] if "stderr" in row.keys() and row["stderr"] is not None else ""
            raw_findings = row["findings_json"] if "findings_json" in row.keys() else "[]"
            try:
                result["findings"] = json.loads(raw_findings) if raw_findings else []
            except Exception:
                result["findings"] = []
        else:
            stderr_prev = row["stderr_preview"] if "stderr_preview" in row.keys() else ""
            if stderr_prev:
                result["stderr"] = stderr_prev

        return result

    def _migrate_legacy_json_if_needed(self) -> None:
        if not self.state_path or not self.state_path.lower().endswith(".json"):
            return
        if self._get_kv("migrated_from_json") == "1":
            return
        if not os.path.exists(self.state_path):
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
        except Exception as e:
            logger.warning("Could not parse legacy state JSON at %s for migration: %s", self.state_path, e)
            return

        conn = self._get_conn()
        with conn:
            rl_payload = {
                "rate_limited": bool(data.get("rate_limited", False)),
                "rate_limit_expires_at": float(data.get("rate_limit_expires_at", 0) or 0),
                "rate_limit_reason": str(data.get("rate_limit_reason", "") or ""),
                "rate_limit_retry_after_seconds": int(data.get("rate_limit_retry_after_seconds", 0) or 0),
                "rate_limit_set_at": float(data.get("rate_limit_set_at", 0) or 0),
            }
            self._set_kv("rate_limit", json.dumps(rl_payload))
            self._set_kv("last_run_timestamp", str(data.get("last_run_timestamp", "") or ""))
            self._set_kv("last_account_index", str(int(data.get("last_account_index", -1))))

            for acc_id, val in (data.get("account_rate_limits") or {}).items():
                if isinstance(val, dict):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO account_rate_limits
                        (account_id, rate_limited, set_at, expires_at, duration_seconds, reason)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(acc_id),
                            1 if val.get("rate_limited") else 0,
                            float(val.get("set_at", 0) or 0),
                            float(val.get("expires_at", 0) or 0),
                            int(val.get("duration_seconds", 0) or 0),
                            str(val.get("reason", "") or ""),
                        ),
                    )

            for pr_key, cnt in (data.get("attempt_counts") or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO pr_attempts (pr_key, count) VALUES (?, ?)",
                    (str(pr_key), int(cnt or 0)),
                )

            for pr_key, active in (data.get("active_reviews") or {}).items():
                if isinstance(active, dict):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO active_reviews
                        (pr_key, started_at, updated_at, attempt, phase, message)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(pr_key),
                            float(active.get("started_at", 0) or 0),
                            float(active.get("updated_at", 0) or 0),
                            int(active.get("attempt", 1) or 1),
                            str(active.get("phase", "RUNNING_CODERABBIT") or "RUNNING_CODERABBIT"),
                            str(active.get("message", "") or ""),
                        ),
                    )

            for pr_key, status_info in (data.get("pr_statuses") or {}).items():
                if isinstance(status_info, dict):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO pr_statuses
                        (pr_key, status, head_sha, review_outcome, review_state, report_file, updated_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(pr_key),
                            str(status_info.get("status", "") or ""),
                            str(status_info.get("head_sha", "") or ""),
                            str(status_info.get("review_outcome", "") or ""),
                            str(status_info.get("review_state", "") or ""),
                            str(status_info.get("report_file", "") or ""),
                            str(status_info.get("updated_at", "") or ""),
                            json.dumps(status_info),
                        ),
                    )

            seen_queue = set()
            pos = 0
            for q_item in (data.get("review_queue") or []):
                if isinstance(q_item, dict) and q_item.get("pr_key"):
                    k = str(q_item["pr_key"])
                    if k not in seen_queue:
                        seen_queue.add(k)
                        conn.execute(
                            "INSERT OR REPLACE INTO review_queue (position, pr_key, force, payload_json) VALUES (?, ?, ?, ?)",
                            (pos, k, 1 if q_item.get("force") else 0, json.dumps(q_item)),
                        )
                        pos += 1

            # Merge review_history, pr_logs, and account_logs deduplicated by log_id
            for entry in (data.get("review_history") or []):
                if isinstance(entry, dict):
                    self._upsert_review_run_locked(entry, in_pr_logs=0, in_account_logs=0)

            for pr_key, logs in (data.get("pr_logs") or {}).items():
                if isinstance(logs, list):
                    for entry in logs:
                        if isinstance(entry, dict):
                            self._upsert_review_run_locked(entry, pr_key=pr_key, in_pr_logs=1)

            for acc_id, logs in (data.get("account_logs") or {}).items():
                if isinstance(logs, list):
                    for entry in logs:
                        if isinstance(entry, dict):
                            self._upsert_review_run_locked(entry, account_id=acc_id, in_account_logs=1)

            for snap in (data.get("daily_snapshots") or []):
                if isinstance(snap, dict) and snap.get("id"):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO daily_snapshots
                        (id, date, created_at, created_at_friendly, open_prs_count, merged_prs_count, metrics_json, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(snap["id"]),
                            str(snap.get("date", "") or ""),
                            str(snap.get("created_at", "") or ""),
                            str(snap.get("created_at_friendly", "") or ""),
                            len(snap.get("open_prs", []) or []),
                            len(snap.get("merged_prs", []) or []),
                            json.dumps(snap.get("metrics", {}) or {}),
                            json.dumps(snap),
                        ),
                    )

            self._set_kv("migrated_from_json", "1")
        logger.info("Migrated legacy state from %s to SQLite database %s", self.state_path, self.db_path)

    def load_state(self) -> Dict[str, Any]:
        """Reconstructs full legacy-compatible state dictionary from SQLite."""
        with self._lock:
            conn = self._get_conn()
            state = self._default_state()

            raw_rl = self._get_kv("rate_limit")
            if raw_rl:
                try:
                    rl = json.loads(raw_rl)
                    state["rate_limited"] = bool(rl.get("rate_limited", False))
                    state["rate_limit_expires_at"] = float(rl.get("rate_limit_expires_at", 0) or 0)
                    state["rate_limit_reason"] = str(rl.get("rate_limit_reason", "") or "")
                    state["rate_limit_retry_after_seconds"] = int(rl.get("rate_limit_retry_after_seconds", 0) or 0)
                    state["rate_limit_set_at"] = float(rl.get("rate_limit_set_at", 0) or 0)
                except Exception:
                    pass

            last_run = self._get_kv("last_run_timestamp", "")
            state["last_run_timestamp"] = last_run or ""

            last_idx = self._get_kv("last_account_index", "-1")
            try:
                state["last_account_index"] = int(last_idx if last_idx is not None else -1)
            except (ValueError, TypeError):
                state["last_account_index"] = -1

            for row in conn.execute("SELECT pr_key, count FROM pr_attempts"):
                state["attempt_counts"][row["pr_key"]] = row["count"]

            for row in conn.execute("SELECT * FROM active_reviews"):
                state["active_reviews"][row["pr_key"]] = {
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"],
                    "attempt": row["attempt"],
                    "phase": row["phase"],
                    "message": row["message"],
                }

            for row in conn.execute("SELECT pr_key, payload_json FROM pr_statuses"):
                try:
                    state["pr_statuses"][row["pr_key"]] = json.loads(row["payload_json"])
                except Exception:
                    pass

            for row in conn.execute("SELECT payload_json FROM review_queue ORDER BY position ASC"):
                try:
                    state["review_queue"].append(json.loads(row["payload_json"]))
                except Exception:
                    pass

            for row in conn.execute("SELECT * FROM account_rate_limits"):
                state["account_rate_limits"][row["account_id"]] = {
                    "rate_limited": bool(row["rate_limited"]),
                    "set_at": row["set_at"],
                    "expires_at": row["expires_at"],
                    "duration_seconds": row["duration_seconds"],
                    "reason": row["reason"],
                }

            for row in conn.execute("SELECT payload_json FROM daily_snapshots ORDER BY created_at DESC, rowid DESC"):
                try:
                    state["daily_snapshots"].append(json.loads(row["payload_json"]))
                except Exception:
                    pass

            rows = conn.execute(
                """
                SELECT r.*, l.stdout, l.stderr, l.findings_json
                FROM review_runs r
                LEFT JOIN review_run_logs l ON l.log_id = r.log_id
                ORDER BY r.timestamp_epoch DESC, r.rowid DESC
                LIMIT 500
                """
            ).fetchall()
            for row in rows:
                entry = self._row_to_run_dict(row, include_blobs=True)
                state["review_history"].append(entry)
                if row["in_pr_logs"] and row["pr_key"]:
                    state["pr_logs"].setdefault(row["pr_key"], []).append(copy.deepcopy(entry))
                if row["in_account_logs"] and row["account_id"]:
                    state["account_logs"].setdefault(row["account_id"], []).append(copy.deepcopy(entry))

            return state

    def save_state(self, state_data: Dict[str, Any]) -> None:
        """Persists a full legacy-format state dictionary into SQLite."""
        with self._lock:
            conn = self._get_conn()
            with conn:
                rl_payload = {
                    "rate_limited": bool(state_data.get("rate_limited", False)),
                    "rate_limit_expires_at": float(state_data.get("rate_limit_expires_at", 0) or 0),
                    "rate_limit_reason": str(state_data.get("rate_limit_reason", "") or ""),
                    "rate_limit_retry_after_seconds": int(state_data.get("rate_limit_retry_after_seconds", 0) or 0),
                    "rate_limit_set_at": float(state_data.get("rate_limit_set_at", 0) or 0),
                }
                self._set_kv("rate_limit", json.dumps(rl_payload))
                self._set_kv("last_run_timestamp", str(state_data.get("last_run_timestamp", "") or ""))
                self._set_kv("last_account_index", str(int(state_data.get("last_account_index", -1))))

                conn.execute("DELETE FROM account_rate_limits")
                for acc_id, val in (state_data.get("account_rate_limits") or {}).items():
                    if isinstance(val, dict):
                        conn.execute(
                            """
                            INSERT INTO account_rate_limits
                            (account_id, rate_limited, set_at, expires_at, duration_seconds, reason)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(acc_id),
                                1 if val.get("rate_limited") else 0,
                                float(val.get("set_at", 0) or 0),
                                float(val.get("expires_at", 0) or 0),
                                int(val.get("duration_seconds", 0) or 0),
                                str(val.get("reason", "") or ""),
                            ),
                        )

                conn.execute("DELETE FROM pr_attempts")
                for pr_key, cnt in (state_data.get("attempt_counts") or {}).items():
                    conn.execute(
                        "INSERT INTO pr_attempts (pr_key, count) VALUES (?, ?)",
                        (str(pr_key), int(cnt or 0)),
                    )

                conn.execute("DELETE FROM active_reviews")
                for pr_key, active in (state_data.get("active_reviews") or {}).items():
                    if isinstance(active, dict):
                        conn.execute(
                            """
                            INSERT INTO active_reviews
                            (pr_key, started_at, updated_at, attempt, phase, message)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(pr_key),
                                float(active.get("started_at", 0) or 0),
                                float(active.get("updated_at", 0) or 0),
                                int(active.get("attempt", 1) or 1),
                                str(active.get("phase", "RUNNING_CODERABBIT") or "RUNNING_CODERABBIT"),
                                str(active.get("message", "") or ""),
                            ),
                        )

                conn.execute("DELETE FROM pr_statuses")
                for pr_key, status_info in (state_data.get("pr_statuses") or {}).items():
                    if isinstance(status_info, dict):
                        conn.execute(
                            """
                            INSERT INTO pr_statuses
                            (pr_key, status, head_sha, review_outcome, review_state, report_file, updated_at, payload_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(pr_key),
                                str(status_info.get("status", "") or ""),
                                str(status_info.get("head_sha", "") or ""),
                                str(status_info.get("review_outcome", "") or ""),
                                str(status_info.get("review_state", "") or ""),
                                str(status_info.get("report_file", "") or ""),
                                str(status_info.get("updated_at", "") or ""),
                                json.dumps(status_info),
                            ),
                        )

                conn.execute("DELETE FROM review_queue")
                seen_queue = set()
                pos = 0
                for q_item in (state_data.get("review_queue") or []):
                    if isinstance(q_item, dict) and q_item.get("pr_key"):
                        k = str(q_item["pr_key"])
                        if k not in seen_queue:
                            seen_queue.add(k)
                            conn.execute(
                                "INSERT INTO review_queue (position, pr_key, force, payload_json) VALUES (?, ?, ?, ?)",
                                (pos, k, 1 if q_item.get("force") else 0, json.dumps(q_item)),
                            )
                            pos += 1

                conn.execute("DELETE FROM review_runs")
                for entry in (state_data.get("review_history") or []):
                    if isinstance(entry, dict):
                        self._upsert_review_run_locked(entry, in_pr_logs=0, in_account_logs=0)
                for pr_key, logs in (state_data.get("pr_logs") or {}).items():
                    if isinstance(logs, list):
                        for entry in logs:
                            if isinstance(entry, dict):
                                self._upsert_review_run_locked(entry, pr_key=pr_key, in_pr_logs=1)
                for acc_id, logs in (state_data.get("account_logs") or {}).items():
                    if isinstance(logs, list):
                        for entry in logs:
                            if isinstance(entry, dict):
                                self._upsert_review_run_locked(entry, account_id=acc_id, in_account_logs=1)

                conn.execute("DELETE FROM daily_snapshots")
                for snap in (state_data.get("daily_snapshots") or []):
                    if isinstance(snap, dict) and snap.get("id"):
                        conn.execute(
                            """
                            INSERT INTO daily_snapshots
                            (id, date, created_at, created_at_friendly, open_prs_count, merged_prs_count, metrics_json, payload_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(snap["id"]),
                                str(snap.get("date", "") or ""),
                                str(snap.get("created_at", "") or ""),
                                str(snap.get("created_at_friendly", "") or ""),
                                len(snap.get("open_prs", []) or []),
                                len(snap.get("merged_prs", []) or []),
                                json.dumps(snap.get("metrics", {}) or {}),
                                json.dumps(snap),
                            ),
                        )
            self._bump_version()

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
        # Use regex word boundaries and avoid single generic code words (e.g. 'throttled')
        keyword_patterns = [
            r"\b429\b",
            r"\brate[-_\s]?limit\b",
            r"\btoo\s+many\s+requests\b",
            r"\bquota\s+(?:exceeded|exhausted|reached|limit)\b",
            r"\b(?:exceeded|exhausted)\s+quota\b",
            r"\brequest\s+throttled\b",
            r"\bthrottling\s+active\b",
        ]
        if any(re.search(pat, text) for pat in keyword_patterns):
            return default_delay

        return 0

    def _get_rate_limit_dict_locked(self) -> Dict[str, Any]:
        raw = self._get_kv("rate_limit")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {
            "rate_limited": False,
            "rate_limit_expires_at": 0,
            "rate_limit_reason": "",
            "rate_limit_retry_after_seconds": 0,
            "rate_limit_set_at": 0,
        }

    def is_rate_limit_active(self) -> Tuple[bool, int, str]:
        """
        Checks if rate limit cooldown is active.
        Returns: (is_active, remaining_seconds, reason)
        """
        with self._lock:
            rl = self._get_rate_limit_dict_locked()
            if not rl.get("rate_limited", False):
                return False, 0, ""

            expires_at = float(rl.get("rate_limit_expires_at", 0) or 0)
            remaining = int(expires_at - time.time())
            if remaining <= 0:
                self.clear_rate_limit()
                return False, 0, ""

            return True, remaining, str(rl.get("rate_limit_reason") or "Rate limit cooldown active")

    def record_rate_limit(self, duration_seconds: int, reason: str = "Rate limit reached", pr_key: Optional[str] = None) -> None:
        """Activates rate limit cooldown and records reason."""
        with self._lock:
            now = time.time()
            duration_seconds = max(10, duration_seconds)
            expires_at = now + duration_seconds
            rl_payload = {
                "rate_limited": True,
                "rate_limit_set_at": now,
                "rate_limit_expires_at": expires_at,
                "rate_limit_retry_after_seconds": duration_seconds,
                "rate_limit_reason": reason,
            }
            conn = self._get_conn()
            with conn:
                self._set_kv("rate_limit", json.dumps(rl_payload))
                if pr_key:
                    conn.execute(
                        "INSERT INTO pr_attempts (pr_key, count) VALUES (?, 1) "
                        "ON CONFLICT(pr_key) DO UPDATE SET count = pr_attempts.count + 1",
                        (pr_key,),
                    )
            self._bump_version()
            logger.warning("Rate limit recorded: %ds (reason: %s). Expires at %s", duration_seconds, reason, expires_at)

    def clear_rate_limit(self, account_id: Optional[str] = None) -> None:
        """Clears active rate limit cooldown globally or for a specific account."""
        with self._lock:
            conn = self._get_conn()
            with conn:
                if account_id:
                    conn.execute("DELETE FROM account_rate_limits WHERE account_id = ?", (str(account_id),))
                else:
                    self._set_kv("rate_limit", json.dumps({
                        "rate_limited": False,
                        "rate_limit_expires_at": 0,
                        "rate_limit_reason": "",
                        "rate_limit_retry_after_seconds": 0,
                        "rate_limit_set_at": 0,
                    }))
                    conn.execute("DELETE FROM account_rate_limits")
            self._bump_version()
            logger.info("Rate limit cleared%s.", f" for account {account_id}" if account_id else "")

    def is_account_rate_limited(self, account_id: str) -> Tuple[bool, int, str]:
        """Checks if a specific account is on rate limit cooldown."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT rate_limited, expires_at, reason FROM account_rate_limits WHERE account_id = ?",
                (str(account_id),),
            ).fetchone()
            if not row or not row["rate_limited"]:
                return False, 0, ""

            remaining = int(float(row["expires_at"] or 0) - time.time())
            if remaining <= 0:
                self.clear_rate_limit(account_id)
                return False, 0, ""
            return True, remaining, str(row["reason"] or "Account rate limit active")

    def record_account_rate_limit(self, account_id: str, duration_seconds: int, reason: str = "Rate limit reached") -> None:
        """Puts a specific account on rate limit cooldown."""
        with self._lock:
            now = time.time()
            duration_seconds = max(10, duration_seconds)
            expires_at = now + duration_seconds
            conn = self._get_conn()
            with conn:
                conn.execute(
                    """
                    INSERT INTO account_rate_limits
                    (account_id, rate_limited, set_at, expires_at, duration_seconds, reason)
                    VALUES (?, 1, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        rate_limited = 1,
                        set_at = excluded.set_at,
                        expires_at = excluded.expires_at,
                        duration_seconds = excluded.duration_seconds,
                        reason = excluded.reason
                    """,
                    (str(account_id), now, expires_at, duration_seconds, reason),
                )
            self._bump_version()
            logger.warning("Account %s rate limit recorded: %ds (reason: %s). Expires at %s", account_id, duration_seconds, reason, expires_at)

    def get_account_rate_limits(self) -> Dict[str, Any]:
        """Returns the dictionary of all active account rate limits."""
        with self._lock:
            conn = self._get_conn()
            now = time.time()
            cleaned = {}
            for row in conn.execute("SELECT * FROM account_rate_limits WHERE rate_limited = 1"):
                rem = int(float(row["expires_at"] or 0) - now)
                if rem > 0:
                    cleaned[row["account_id"]] = {
                        "rate_limited": True,
                        "set_at": row["set_at"],
                        "expires_at": row["expires_at"],
                        "duration_seconds": row["duration_seconds"],
                        "reason": row["reason"],
                        "remaining_seconds": rem,
                    }
            return cleaned

    def select_next_available_account(
        self,
        accounts: list,
        exclude_ids: Optional[Set[str]] = None,
    ) -> Optional[Tuple[Dict[str, Any], int]]:
        """
        Round-robin selection of next enabled account not currently in rate limit cooldown
        and not excluded by exclude_ids.
        Returns: (account_dict, index) or None if all are rate-limited, busy, or none enabled.
        """
        enabled = [(i, acc) for i, acc in enumerate(accounts) if acc.get("enabled", True)]
        if not enabled:
            return None

        with self._lock:
            conn = self._get_conn()
            raw_idx = self._get_kv("last_account_index", "-1")
            try:
                last_idx = int(raw_idx if raw_idx is not None else -1)
            except (ValueError, TypeError):
                last_idx = -1

            now = time.time()
            expired_ids = []
            active_limits: Dict[str, float] = {}
            for row in conn.execute("SELECT account_id, rate_limited, expires_at FROM account_rate_limits"):
                if row["rate_limited"]:
                    if float(row["expires_at"] or 0) > now:
                        active_limits[row["account_id"]] = float(row["expires_at"])
                    else:
                        expired_ids.append(row["account_id"])

            if expired_ids:
                with conn:
                    conn.executemany("DELETE FROM account_rate_limits WHERE account_id = ?", [(aid,) for aid in expired_ids])
                self._bump_version()

            total_enabled = len(enabled)
            start_pos = 0
            for pos, (orig_i, acc) in enumerate(enabled):
                if orig_i > last_idx:
                    start_pos = pos
                    break

            for step in range(total_enabled):
                pos = (start_pos + step) % total_enabled
                orig_i, acc = enabled[pos]
                acc_id = str(acc.get("id", ""))
                if exclude_ids and acc_id in exclude_ids:
                    continue
                if acc_id in active_limits:
                    continue
                with conn:
                    self._set_kv("last_account_index", str(orig_i))
                self._bump_version()
                return acc, orig_i

            return None

    def get_attempt_count(self, pr_key: str) -> int:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT count FROM pr_attempts WHERE pr_key = ?", (pr_key,)).fetchone()
            return int(row["count"]) if row else 0

    def increment_attempt_count(self, pr_key: str) -> int:
        with self._lock:
            conn = self._get_conn()
            with conn:
                conn.execute(
                    "INSERT INTO pr_attempts (pr_key, count) VALUES (?, 1) "
                    "ON CONFLICT(pr_key) DO UPDATE SET count = pr_attempts.count + 1",
                    (pr_key,),
                )
            row = conn.execute("SELECT count FROM pr_attempts WHERE pr_key = ?", (pr_key,)).fetchone()
            self._bump_version()
            return int(row["count"]) if row else 1

    def reset_attempt_count(self, pr_key: str) -> None:
        with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("DELETE FROM pr_attempts WHERE pr_key = ?", (pr_key,))
            if cur.rowcount:
                self._bump_version()

    def set_pr_reviewing(self, pr_key: str, is_reviewing: bool, attempt: int = 1,
                         phase: str = "RUNNING_CODERABBIT", message: str = "") -> None:
        with self._lock:
            conn = self._get_conn()
            now = time.time()
            with conn:
                if is_reviewing:
                    conn.execute(
                        """
                        INSERT INTO active_reviews (pr_key, started_at, updated_at, attempt, phase, message)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(pr_key) DO UPDATE SET
                            updated_at = excluded.updated_at,
                            attempt = excluded.attempt,
                            phase = excluded.phase,
                            message = excluded.message
                        """,
                        (pr_key, now, now, attempt, phase, message),
                    )
                else:
                    conn.execute("DELETE FROM active_reviews WHERE pr_key = ?", (pr_key,))
            self._bump_version()

    def is_pr_reviewing(self, pr_key: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT 1 FROM active_reviews WHERE pr_key = ?", (pr_key,)).fetchone()
            return row is not None

    def record_pr_status(self, pr_key: str, status_info: Dict[str, Any]) -> None:
        with self._lock:
            updated_at = now_app_tz().isoformat()
            status_info["updated_at"] = updated_at
            payload_json = json.dumps(status_info)
            conn = self._get_conn()
            with conn:
                conn.execute(
                    """
                    INSERT INTO pr_statuses
                    (pr_key, status, head_sha, review_outcome, review_state, report_file, updated_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pr_key) DO UPDATE SET
                        status = excluded.status,
                        head_sha = excluded.head_sha,
                        review_outcome = excluded.review_outcome,
                        review_state = excluded.review_state,
                        report_file = excluded.report_file,
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    (
                        pr_key,
                        str(status_info.get("status") or ""),
                        str(status_info.get("head_sha") or ""),
                        str(status_info.get("review_outcome") or ""),
                        str(status_info.get("review_state") or ""),
                        str(status_info.get("report_file") or ""),
                        updated_at,
                        payload_json,
                    ),
                )
            self._bump_version()

    def get_pr_status(self, pr_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT payload_json FROM pr_statuses WHERE pr_key = ?", (pr_key,)).fetchone()
            if not row:
                return None
            try:
                return json.loads(row["payload_json"])
            except Exception:
                return None

    def get_all_pr_statuses(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            statuses: Dict[str, Any] = {}
            for row in conn.execute("SELECT pr_key, payload_json FROM pr_statuses"):
                try:
                    statuses[row["pr_key"]] = json.loads(row["payload_json"])
                except Exception:
                    pass
            return statuses

    def record_last_run(self, timestamp: Optional[str] = None) -> None:
        with self._lock:
            ts = timestamp or now_app_tz().isoformat()
            conn = self._get_conn()
            with conn:
                self._set_kv("last_run_timestamp", ts)
            self._bump_version()

    def get_last_run(self) -> str:
        with self._lock:
            return self._get_kv("last_run_timestamp", "") or ""

    def record_pr_log(self, pr_key: str, log_entry: Dict[str, Any], max_history: int = 20) -> str:
        """
        Appends an execution log record for a pull request to persistent storage.
        Returns: the log_id generated for this entry.
        """
        with self._lock:
            now = now_app_tz()
            if "timestamp" not in log_entry:
                log_entry["timestamp"] = now.isoformat()
            if "pr_key" not in log_entry:
                log_entry["pr_key"] = pr_key

            conn = self._get_conn()
            with conn:
                log_id = self._upsert_review_run_locked(
                    log_entry,
                    pr_key=pr_key,
                    in_pr_logs=1,
                    in_account_logs=1,
                )
                log_entry["log_id"] = log_id

                conn.execute(
                    """
                    UPDATE review_runs SET in_pr_logs = 0
                    WHERE pr_key = ? AND in_pr_logs = 1 AND log_id NOT IN (
                        SELECT log_id FROM review_runs
                        WHERE pr_key = ? AND in_pr_logs = 1
                        ORDER BY timestamp_epoch DESC, rowid DESC
                        LIMIT ?
                    )
                    """,
                    (pr_key, pr_key, max(1, int(max_history))),
                )
            self._bump_version()
            return log_id

    def get_pr_logs(self, pr_key: str) -> list:
        """Retrieves list of recorded review logs for a PR (most recent first, with full stdout/stderr)."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """
                SELECT r.*, l.stdout, l.stderr, l.findings_json
                FROM review_runs r
                LEFT JOIN review_run_logs l ON l.log_id = r.log_id
                WHERE r.pr_key = ? AND r.in_pr_logs = 1
                ORDER BY r.timestamp_epoch DESC, r.rowid DESC
                LIMIT 20
                """,
                (pr_key,),
            ).fetchall()
            return [self._row_to_run_dict(row, include_blobs=True) for row in rows]

    def get_pr_log_by_id(self, pr_key: str, log_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a specific execution log entry by log_id."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                """
                SELECT r.*, l.stdout, l.stderr, l.findings_json
                FROM review_runs r
                LEFT JOIN review_run_logs l ON l.log_id = r.log_id
                WHERE r.pr_key = ? AND r.log_id = ? AND r.in_pr_logs = 1
                """,
                (pr_key, log_id),
            ).fetchone()
            return self._row_to_run_dict(row, include_blobs=True) if row else None

    def record_account_log(self, account_id: str, log_entry: Dict[str, Any], max_history: int = 100) -> str:
        """
        Appends an execution log record for a CodeRabbit account to persistent storage.
        Returns: the log_id generated for this entry.
        """
        acc_id = str(account_id or "default")
        with self._lock:
            entry = dict(log_entry)
            if "timestamp" not in entry:
                entry["timestamp"] = now_app_tz().isoformat()
            entry["account_id"] = acc_id

            conn = self._get_conn()
            with conn:
                # Preserve in_pr_logs=1 if updating an existing PR run, else 0 for account-only logs
                existing = None
                if entry.get("log_id"):
                    existing = conn.execute(
                        "SELECT in_pr_logs FROM review_runs WHERE log_id = ?",
                        (entry["log_id"],),
                    ).fetchone()
                in_pr = int(existing["in_pr_logs"]) if existing else 0

                log_id = self._upsert_review_run_locked(
                    entry,
                    account_id=acc_id,
                    in_pr_logs=in_pr,
                    in_account_logs=1,
                )

                conn.execute(
                    """
                    UPDATE review_runs SET in_account_logs = 0
                    WHERE account_id = ? AND in_account_logs = 1 AND log_id NOT IN (
                        SELECT log_id FROM review_runs
                        WHERE account_id = ? AND in_account_logs = 1
                        ORDER BY timestamp_epoch DESC, rowid DESC
                        LIMIT ?
                    )
                    """,
                    (acc_id, acc_id, max(1, int(max_history))),
                )
            self._bump_version()
            return log_id

    def get_account_logs(self, account_id: str) -> list:
        """Retrieves recorded review logs for a CodeRabbit account (most recent first)."""
        acc_id = str(account_id or "default")
        with self._lock:
            conn = self._get_conn()
            if acc_id == "default":
                rows = conn.execute(
                    """
                    SELECT * FROM review_runs
                    WHERE in_account_logs = 1
                      AND (account_id = 'default'
                           OR LOWER(account_id) LIKE '%default%'
                           OR LOWER(coderabbit_account) LIKE '%default%')
                    ORDER BY timestamp_epoch DESC, rowid DESC
                    LIMIT 100
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM review_runs
                    WHERE account_id = ? AND in_account_logs = 1
                    ORDER BY timestamp_epoch DESC, rowid DESC
                    LIMIT 100
                    """,
                    (acc_id,),
                ).fetchall()
            return [self._row_to_run_dict(row, include_blobs=False) for row in rows]

    def clear_account_logs(self, account_id: str) -> None:
        """Clears all review logs for a specific account without deleting PR details logs."""
        acc_id = str(account_id or "default")
        with self._lock:
            conn = self._get_conn()
            with conn:
                conn.execute("UPDATE review_runs SET in_account_logs = 0 WHERE account_id = ?", (acc_id,))
            self._bump_version()

    @staticmethod
    def _compute_account_metrics_from_rows(rows: List[sqlite3.Row]) -> Dict[str, Any]:
        total = len(rows)
        approved = 0
        commented = 0
        errors = 0
        durations: List[float] = []
        last_reviewed_at = ""

        for row in rows:
            outcome = (row["review_outcome"] or row["status"] or row["event"] or "").upper()
            if "APPROV" in outcome:
                approved += 1
            elif "COMMENT" in outcome:
                commented += 1
            elif "ERROR" in outcome or row["status"] == "ERROR":
                errors += 1

            dur = row["elapsed_seconds"]
            if dur is not None:
                try:
                    durations.append(float(dur))
                except (ValueError, TypeError):
                    pass

            if not last_reviewed_at and row["timestamp"]:
                last_reviewed_at = row["timestamp"]

        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0.0
        return {
            "total_reviews": total,
            "approved_count": approved,
            "commented_count": commented,
            "error_count": errors,
            "avg_duration_seconds": avg_dur,
            "last_reviewed_at": last_reviewed_at,
        }

    def get_account_metrics(self, account_id: str) -> Dict[str, Any]:
        """Calculates review performance metrics for a specific account."""
        acc_id = str(account_id or "default")
        with self._lock:
            conn = self._get_conn()
            if acc_id == "default":
                rows = conn.execute(
                    """
                    SELECT review_outcome, status, event, elapsed_seconds, timestamp
                    FROM review_runs
                    WHERE in_account_logs = 1
                      AND (account_id = 'default'
                           OR LOWER(account_id) LIKE '%default%'
                           OR LOWER(coderabbit_account) LIKE '%default%')
                    ORDER BY timestamp_epoch DESC, rowid DESC
                    LIMIT 100
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT review_outcome, status, event, elapsed_seconds, timestamp
                    FROM review_runs
                    WHERE account_id = ? AND in_account_logs = 1
                    ORDER BY timestamp_epoch DESC, rowid DESC
                    LIMIT 100
                    """,
                    (acc_id,),
                ).fetchall()
            return self._compute_account_metrics_from_rows(rows)

    def _get_all_account_metrics_locked(self) -> Dict[str, Dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT account_id, coderabbit_account, review_outcome, status, event, elapsed_seconds, timestamp
            FROM review_runs
            WHERE in_account_logs = 1
            ORDER BY timestamp_epoch DESC, rowid DESC
            """
        ).fetchall()

        grouped: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            aid = str(row["account_id"] or "default")
            if aid == "default" or "default" in aid.lower() or "default" in str(row["coderabbit_account"] or "").lower():
                bucket = grouped.setdefault("default", [])
                if len(bucket) < 100:
                    bucket.append(row)
                if aid != "default":
                    acc_bucket = grouped.setdefault(aid, [])
                    if len(acc_bucket) < 100:
                        acc_bucket.append(row)
            else:
                acc_bucket = grouped.setdefault(aid, [])
                if len(acc_bucket) < 100:
                    acc_bucket.append(row)

        return {aid: self._compute_account_metrics_from_rows(rlist) for aid, rlist in grouped.items()}

    def get_all_account_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Returns performance metrics grouped by account_id in a single query."""
        with self._lock:
            return self._get_all_account_metrics_locked()

    def get_review_queue(self) -> list:
        """Retrieves the list of queued review items."""
        with self._lock:
            conn = self._get_conn()
            items = []
            for row in conn.execute("SELECT payload_json FROM review_queue ORDER BY position ASC"):
                try:
                    items.append(json.loads(row["payload_json"]))
                except Exception:
                    pass
            return items

    def _replace_queue_locked(self, queue_items: List[Dict[str, Any]]) -> None:
        conn = self._get_conn()
        seen_keys = set()
        deduped = []
        for item in queue_items:
            if isinstance(item, dict) and "pr_key" in item:
                k = item["pr_key"]
                if k not in seen_keys:
                    seen_keys.add(k)
                    deduped.append(dict(item))
        with conn:
            conn.execute("DELETE FROM review_queue")
            conn.executemany(
                "INSERT INTO review_queue (position, pr_key, force, payload_json) VALUES (?, ?, ?, ?)",
                [
                    (idx, str(item["pr_key"]), 1 if item.get("force") else 0, json.dumps(item))
                    for idx, item in enumerate(deduped)
                ],
            )
        self._bump_version()

    def save_review_queue(self, queue_items: list) -> None:
        """Persists the review queue list to SQLite storage."""
        with self._lock:
            self._replace_queue_locked(queue_items)

    def enqueue_review_job(self, job_item: Dict[str, Any], to_front: bool = False) -> bool:
        """
        Thread-safely adds or updates a PR job in the review queue.
        Returns True if newly added or upgraded to force, False if already queued.
        """
        with self._lock:
            queue = self.get_review_queue()
            pr_key = job_item.get("pr_key")

            for existing in queue:
                if existing.get("pr_key") == pr_key:
                    if job_item.get("force") and not existing.get("force"):
                        existing["force"] = True
                        self._replace_queue_locked(queue)
                        return True
                    return False

            if to_front:
                queue.insert(0, dict(job_item))
            else:
                queue.append(dict(job_item))

            self._replace_queue_locked(queue)
            return True

    def pop_next_review_job(self) -> Optional[Dict[str, Any]]:
        """Thread-safely pops the next pending PR job from the front of the queue."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT position, payload_json FROM review_queue ORDER BY position ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            with conn:
                conn.execute("DELETE FROM review_queue WHERE position = ?", (row["position"],))
            self._bump_version()
            try:
                return json.loads(row["payload_json"])
            except Exception:
                return None

    def requeue_review_job(self, job_item: Dict[str, Any], to_front: bool = True) -> None:
        """Puts an unfinished job back into the review queue (e.g. on rate limit)."""
        with self._lock:
            queue = self.get_review_queue()
            pr_key = job_item.get("pr_key")
            if not any(item.get("pr_key") == pr_key for item in queue):
                if to_front:
                    queue.insert(0, dict(job_item))
                else:
                    queue.append(dict(job_item))
                self._replace_queue_locked(queue)

    def move_review_job_to_top(self, pr_key: str) -> bool:
        """Moves the matching pr_key to the head of the queue."""
        with self._lock:
            queue = self.get_review_queue()
            target_idx = next((i for i, item in enumerate(queue) if item.get("pr_key") == pr_key), None)
            if target_idx is None:
                return False
            if target_idx == 0:
                return True
            item = queue.pop(target_idx)
            queue.insert(0, item)
            self._replace_queue_locked(queue)
            return True

    def save_daily_snapshot(self, snapshot: Dict[str, Any], max_snapshots: int = 30) -> str:
        """
        Saves a daily summary snapshot to SQLite.
        Ensures an id, date, and created_at exist.
        Keeps at most max_snapshots snapshots (newest first).
        """
        with self._lock:
            now = now_app_tz()
            snap_id = snapshot.get("id") or f"snap_{int(now.timestamp())}_{uuid.uuid4().hex[:6]}"
            snap_date = snapshot.get("date") or now.strftime("%Y-%m-%d")

            entry = dict(snapshot)
            entry["id"] = snap_id
            entry["date"] = snap_date
            if "created_at" not in entry:
                entry["created_at"] = now.isoformat()

            conn = self._get_conn()
            with conn:
                conn.execute(
                    "DELETE FROM daily_snapshots WHERE id = ? OR date = ?",
                    (snap_id, snap_date),
                )
                conn.execute(
                    """
                    INSERT INTO daily_snapshots
                    (id, date, created_at, created_at_friendly, open_prs_count, merged_prs_count, metrics_json, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snap_id,
                        snap_date,
                        str(entry["created_at"]),
                        str(entry.get("created_at_friendly", "") or ""),
                        len(entry.get("open_prs", []) or []),
                        len(entry.get("merged_prs", []) or []),
                        json.dumps(entry.get("metrics", {}) or {}),
                        json.dumps(entry),
                    ),
                )
                conn.execute(
                    """
                    DELETE FROM daily_snapshots
                    WHERE id NOT IN (
                        SELECT id FROM daily_snapshots
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT ?
                    )
                    """,
                    (max(1, int(max_snapshots)),),
                )
            self._bump_version()
            return snap_id

    def get_daily_snapshots(self) -> List[Dict[str, Any]]:
        """Returns the list of stored daily snapshots (newest first)."""
        with self._lock:
            conn = self._get_conn()
            snapshots = []
            for row in conn.execute("SELECT payload_json FROM daily_snapshots ORDER BY created_at DESC, rowid DESC"):
                try:
                    snapshots.append(json.loads(row["payload_json"]))
                except Exception:
                    pass
            return snapshots

    def get_daily_snapshot_summaries(self) -> List[Dict[str, Any]]:
        """Returns lightweight metadata summaries for stored daily snapshots without parsing full payloads."""
        with self._lock:
            conn = self._get_conn()
            summaries = []
            rows = conn.execute(
                """
                SELECT id, date, created_at, created_at_friendly, open_prs_count, merged_prs_count, metrics_json
                FROM daily_snapshots
                ORDER BY created_at DESC, rowid DESC
                """
            ).fetchall()
            for row in rows:
                try:
                    metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
                except Exception:
                    metrics = {}
                summaries.append({
                    "id": row["id"],
                    "date": row["date"],
                    "created_at": row["created_at"],
                    "created_at_friendly": row["created_at_friendly"],
                    "metrics": metrics,
                    "open_prs_count": row["open_prs_count"],
                    "merged_prs_count": row["merged_prs_count"],
                })
            return summaries

    def get_daily_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a specific snapshot by id or date."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT payload_json FROM daily_snapshots WHERE id = ? OR date = ? LIMIT 1",
                (snapshot_id, snapshot_id),
            ).fetchone()
            if not row:
                return None
            try:
                return json.loads(row["payload_json"])
            except Exception:
                return None

    def delete_daily_snapshot(self, snapshot_id: str) -> bool:
        """Deletes a specific snapshot by id."""
        with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("DELETE FROM daily_snapshots WHERE id = ?", (snapshot_id,))
            if cur.rowcount > 0:
                self._bump_version()
                return True
            return False

    def get_all_recent_logs(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Retrieves review execution logs across all PRs and accounts that occurred within the last N hours.
        Returns entries sorted newest first.
        """
        with self._lock:
            now_epoch = now_app_tz().timestamp()
            min_epoch = now_epoch - (hours * 3600)
            conn = self._get_conn()
            rows = conn.execute(
                """
                SELECT * FROM review_runs
                WHERE timestamp != '' AND timestamp_epoch >= ? AND timestamp_epoch <= ?
                ORDER BY timestamp_epoch DESC, rowid DESC
                """,
                (min_epoch, now_epoch),
            ).fetchall()
            return [self._row_to_run_dict(row, include_blobs=False) for row in rows]

    def get_total_reviews_count(self) -> int:
        """Returns the total number of recorded review executions across all history."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT COUNT(*) AS cnt FROM review_runs").fetchone()
            return int(row["cnt"]) if row else 0

    def get_all_review_history(
        self,
        limit: Optional[int] = None,
        offset: int = 0,
        sort_order: str = "desc",
        sort_by: str = "time",
        outcome: Optional[str] = None,
        repo: Optional[str] = None,
        account_id: Optional[str] = None,
        search: Optional[str] = None,
        time_range: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retrieves all review executions ordered chronologically (or by elapsed duration),
        with SQL-accelerated aggregate metrics, filtering, sorting, and pagination.
        """
        with self._lock:
            conn = self._get_conn()
            now_epoch = now_app_tz().timestamp()

            outcome_expr = "UPPER(COALESCE(NULLIF(review_outcome, ''), NULLIF(status, ''), NULLIF(event, ''), 'UNKNOWN'))"
            agg_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_reviews,
                    COALESCE(SUM(CASE WHEN {outcome_expr} LIKE '%APPROV%' THEN 1 ELSE 0 END), 0) AS approved_count,
                    COALESCE(SUM(CASE WHEN {outcome_expr} NOT LIKE '%APPROV%' AND {outcome_expr} LIKE '%COMMENT%' THEN 1 ELSE 0 END), 0) AS commented_count,
                    COALESCE(SUM(CASE WHEN {outcome_expr} NOT LIKE '%APPROV%' AND {outcome_expr} NOT LIKE '%COMMENT%' AND ({outcome_expr} LIKE '%ERROR%' OR status = 'ERROR') THEN 1 ELSE 0 END), 0) AS error_count,
                    COALESCE(SUM(CASE WHEN {outcome_expr} NOT LIKE '%APPROV%' AND {outcome_expr} NOT LIKE '%COMMENT%' AND {outcome_expr} NOT LIKE '%ERROR%' AND status != 'ERROR' AND ({outcome_expr} LIKE '%RATE_LIMIT%' OR status = 'RATE_LIMITED') THEN 1 ELSE 0 END), 0) AS rate_limited_count,
                    AVG(elapsed_seconds) AS avg_dur,
                    COUNT(DISTINCT NULLIF(pr_key, '')) AS unique_prs_count,
                    COUNT(DISTINCT NULLIF(repo, '')) AS unique_repos_count
                FROM review_runs
                """
            ).fetchone()

            total_reviews = int(agg_row["total_reviews"] or 0)
            avg_dur = round(float(agg_row["avg_dur"]), 1) if agg_row["avg_dur"] is not None else 0.0
            overall_metrics = {
                "total_reviews": total_reviews,
                "approved_count": int(agg_row["approved_count"] or 0),
                "commented_count": int(agg_row["commented_count"] or 0),
                "error_count": int(agg_row["error_count"] or 0),
                "rate_limited_count": int(agg_row["rate_limited_count"] or 0),
                "avg_duration_seconds": avg_dur,
                "unique_prs_count": int(agg_row["unique_prs_count"] or 0),
                "unique_repos_count": int(agg_row["unique_repos_count"] or 0),
            }

            repo_rows = conn.execute(
                "SELECT DISTINCT repo FROM review_runs WHERE repo != '' ORDER BY repo ASC"
            ).fetchall()
            repositories_list = [r["repo"] for r in repo_rows]

            acc_rows = conn.execute(
                "SELECT account_id, coderabbit_account FROM review_runs"
            ).fetchall()
            accounts_map: Dict[str, str] = {}
            for r in acc_rows:
                aid = r["account_id"] or "default"
                aname = r["coderabbit_account"] or aid
                accounts_map[aid] = aname
            accounts_list = [{"id": k, "name": v} for k, v in sorted(accounts_map.items())]

            where_clauses: List[str] = []
            params: List[Any] = []

            if time_range and time_range.lower() != "all":
                cutoff_hours = None
                tr = time_range.lower()
                if tr in ("24h", "today", "1d"):
                    cutoff_hours = 24
                elif tr in ("7d", "week"):
                    cutoff_hours = 7 * 24
                elif tr in ("30d", "month"):
                    cutoff_hours = 30 * 24
                if cutoff_hours is not None:
                    min_epoch = now_epoch - (cutoff_hours * 3600)
                    where_clauses.append("timestamp != '' AND timestamp_epoch >= ? AND timestamp_epoch <= ?")
                    params.extend([min_epoch, now_epoch])

            if outcome and outcome.upper() != "ALL":
                target_outcome = outcome.upper()
                if target_outcome == "ERROR":
                    where_clauses.append(f"({outcome_expr} LIKE ? OR status = 'ERROR')")
                    params.append(f"%{target_outcome}%")
                elif target_outcome == "RATE_LIMITED":
                    where_clauses.append(f"({outcome_expr} LIKE ? OR {outcome_expr} LIKE '%RATE_LIMIT%' OR status = 'RATE_LIMITED')")
                    params.append(f"%{target_outcome}%")
                else:
                    where_clauses.append(f"{outcome_expr} LIKE ?")
                    params.append(f"%{target_outcome}%")

            if repo and repo.upper() != "ALL":
                rep_lower = repo.lower()
                where_clauses.append("(LOWER(repo) = ? OR LOWER(pr_key) LIKE ?)")
                params.extend([rep_lower, f"{rep_lower}#%"])

            if account_id and account_id.upper() != "ALL":
                acc_lower = account_id.lower()
                where_clauses.append("(LOWER(account_id) = ? OR LOWER(coderabbit_account) = ?)")
                params.extend([acc_lower, acc_lower])

            if search and search.strip():
                q = f"%{search.strip().lower()}%"
                where_clauses.append(
                    "(LOWER(pr_key) LIKE ? OR LOWER(title) LIKE ? OR LOWER(author) LIKE ? "
                    "OR LOWER(repo) LIKE ? OR LOWER(coderabbit_account) LIKE ? "
                    "OR LOWER(summary) LIKE ? OR LOWER(error) LIKE ?)"
                )
                params.extend([q, q, q, q, q, q, q])

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            filtered_count_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM review_runs {where_sql}",
                params,
            ).fetchone()
            filtered_count = int(filtered_count_row["cnt"] or 0)

            if sort_by in ("duration", "dur_desc", "dur_asc") or sort_order in ("dur_desc", "dur_asc"):
                dur_dir = "ASC" if (sort_by == "dur_asc" or sort_order == "dur_asc") else "DESC"
                order_sql = f"COALESCE(elapsed_seconds, 0.0) {dur_dir}, timestamp_epoch DESC, rowid DESC"
            else:
                time_dir = "ASC" if sort_order in ("time_asc", "asc") else "DESC"
                order_sql = f"timestamp_epoch {time_dir}, rowid {time_dir}"

            query_sql = f"SELECT * FROM review_runs {where_sql} ORDER BY {order_sql}"
            query_params = list(params)
            if limit is not None and limit > 0:
                query_sql += " LIMIT ? OFFSET ?"
                query_params.extend([int(limit), max(0, int(offset))])
            elif offset > 0:
                query_sql += " LIMIT -1 OFFSET ?"
                query_params.append(int(offset))

            rows = conn.execute(query_sql, query_params).fetchall()
            history = []
            for row in rows:
                entry = self._row_to_run_dict(row, include_blobs=False)
                entry["review_outcome"] = (
                    entry.get("review_outcome")
                    or entry.get("status")
                    or entry.get("event")
                    or "UNKNOWN"
                ).upper()
                entry["timestamp"] = entry.get("timestamp") or ""
                history.append(entry)

            return {
                "total": total_reviews,
                "filtered_count": filtered_count,
                "metrics": overall_metrics,
                "repositories": repositories_list,
                "accounts": accounts_list,
                "history": history,
                "sort_order": sort_order,
                "sort_by": sort_by,
            }

    def clear_all_review_history(self) -> int:
        """Clears all review history entries across all stores."""
        with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("DELETE FROM review_runs")
            count = int(cur.rowcount or 0)
            self._bump_version()
            return count

    def delete_review_history_entry(self, log_id: str) -> bool:
        """Deletes a single review history entry by its log_id."""
        if not log_id:
            return False
        with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("DELETE FROM review_runs WHERE log_id = ?", (log_id,))
            if cur.rowcount > 0:
                self._bump_version()
                return True
            return False

    def save_pr_cache(self, prs: List[Dict[str, Any]]) -> None:
        """Persists cached open PR entries to SQLite so restarts start warm."""
        with self._lock:
            conn = self._get_conn()
            now_ts = time.time()
            with conn:
                conn.execute("DELETE FROM pr_cache")
                conn.executemany(
                    "INSERT INTO pr_cache (pr_key, repo, updated_at, payload_json, cached_at) VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            str(pr.get("pr_key", "")),
                            str(pr.get("repo", "")),
                            str(pr.get("updated_at", "")),
                            json.dumps(pr),
                            now_ts,
                        )
                        for pr in prs
                        if isinstance(pr, dict) and pr.get("pr_key")
                    ],
                )

    def load_pr_cache(self) -> List[Dict[str, Any]]:
        """Loads persisted open PR cache entries from SQLite."""
        with self._lock:
            conn = self._get_conn()
            prs = []
            for row in conn.execute("SELECT payload_json FROM pr_cache ORDER BY updated_at DESC"):
                try:
                    item = json.loads(row["payload_json"])
                    if isinstance(item, dict):
                        prs.append(item)
                except Exception:
                    pass
            return prs

    def get_dashboard_snapshot(self) -> Dict[str, Any]:
        """
        Reads all state fields needed by DashboardBackend.get_annotated_status()
        in a single locked SQLite read without loading stdout/findings blobs.
        """
        with self._lock:
            conn = self._get_conn()
            now = time.time()

            rl = self._get_rate_limit_dict_locked()
            is_rate_limited = False
            remaining = 0
            reason = ""
            expires_at = float(rl.get("rate_limit_expires_at", 0) or 0)
            if rl.get("rate_limited", False):
                remaining = int(expires_at - now)
                if remaining <= 0:
                    self.clear_rate_limit()
                    expires_at = 0
                    remaining = 0
                else:
                    is_rate_limited = True
                    reason = str(rl.get("rate_limit_reason") or "Rate limit cooldown active")

            last_run_timestamp = self._get_kv("last_run_timestamp", "") or ""

            active_reviews: Dict[str, Dict[str, Any]] = {}
            for row in conn.execute("SELECT * FROM active_reviews"):
                active_reviews[row["pr_key"]] = {
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"],
                    "attempt": row["attempt"],
                    "phase": row["phase"],
                    "message": row["message"],
                }

            pr_statuses: Dict[str, Any] = {}
            for row in conn.execute("SELECT pr_key, payload_json FROM pr_statuses"):
                try:
                    pr_statuses[row["pr_key"]] = json.loads(row["payload_json"])
                except Exception:
                    pass

            latest_pr_runs: Dict[str, Dict[str, str]] = {}
            for row in conn.execute(
                """
                SELECT pr_key, review_outcome, status, report_file
                FROM (
                    SELECT pr_key, review_outcome, status, report_file,
                           ROW_NUMBER() OVER (PARTITION BY pr_key ORDER BY timestamp_epoch DESC, rowid DESC) AS rn
                    FROM review_runs
                    WHERE in_pr_logs = 1 AND pr_key != ''
                )
                WHERE rn = 1
                """
            ):
                latest_pr_runs[row["pr_key"]] = {
                    "review_outcome": row["review_outcome"] or "",
                    "status": row["status"] or "",
                    "report_file": row["report_file"] or "",
                }

            review_queue: List[Dict[str, Any]] = []
            for row in conn.execute("SELECT payload_json FROM review_queue ORDER BY position ASC"):
                try:
                    review_queue.append(json.loads(row["payload_json"]))
                except Exception:
                    pass

            cnt_row = conn.execute("SELECT COUNT(*) AS cnt FROM review_runs").fetchone()
            total_reviews_count = int(cnt_row["cnt"]) if cnt_row else 0

            account_rate_limits: Dict[str, Any] = {}
            for row in conn.execute("SELECT * FROM account_rate_limits WHERE rate_limited = 1"):
                rem = int(float(row["expires_at"] or 0) - now)
                if rem > 0:
                    account_rate_limits[row["account_id"]] = {
                        "rate_limited": True,
                        "set_at": row["set_at"],
                        "expires_at": row["expires_at"],
                        "duration_seconds": row["duration_seconds"],
                        "reason": row["reason"],
                        "remaining_seconds": rem,
                    }

            account_metrics = self._get_all_account_metrics_locked()

            return {
                "rate_limited": is_rate_limited,
                "rate_limit_expires_at": expires_at if is_rate_limited else 0,
                "rate_limit_remaining_seconds": remaining,
                "rate_limit_reason": reason,
                "last_run_timestamp": last_run_timestamp,
                "active_reviews": active_reviews,
                "pr_statuses": pr_statuses,
                "latest_pr_runs": latest_pr_runs,
                "review_queue": review_queue,
                "total_reviews_count": total_reviews_count,
                "account_rate_limits": account_rate_limits,
                "account_metrics": account_metrics,
            }
