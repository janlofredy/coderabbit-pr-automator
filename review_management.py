"""Review scheduling and execution orchestration."""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from auto_review_prs import AutoReviewEngine
from config_manager import ConfigManager
from github_client import GitHubClient
from state_manager import StateManager

logger = logging.getLogger("review_management")


class ReviewManagementService:
    """Owns review queue processing and review-engine execution independently from repository cache."""

    def __init__(
        self,
        config_manager: ConfigManager,
        state_manager: StateManager,
        github_client: GitHubClient,
        review_engine_or_repo_service: Any,
        review_engine: Optional[AutoReviewEngine] = None,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        self.config_manager = config_manager
        self.state_manager = state_manager
        self.github_client = github_client

        # Support both new signature (without repository_service) and old signature
        if review_engine is not None:
            # Old signature: (config_mgr, state_mgr, gh_client, repo_service, review_engine, executor)
            self.review_engine = review_engine
            self._executor = executor or ThreadPoolExecutor(max_workers=5)
        else:
            # New signature: (config_mgr, state_mgr, gh_client, review_engine, executor)
            self.review_engine = review_engine_or_repo_service
            self._executor = executor if isinstance(executor, ThreadPoolExecutor) else ThreadPoolExecutor(max_workers=5)

        self._scan_lock = threading.Lock()
        self._scan_in_progress = False
        self._scan_phase = "IDLE"
        self._review_queue = deque()
        self._active_queue_item = None
        self._init_queue_from_state()

    def _init_queue_from_state(self) -> None:
        try:
            persisted = self.state_manager.get_review_queue()
            for item in persisted:
                if isinstance(item, dict) and "pr_key" in item:
                    self._review_queue.append(dict(item))
        except Exception as e:
            logger.warning("Could not restore persisted review queue: %s", e)

    def _sync_queue_to_state_locked(self) -> None:
        try:
            items = [dict(item) for item in self._review_queue]
            self.state_manager.save_review_queue(items)
        except Exception as e:
            logger.warning("Could not persist review queue: %s", e)

    @property
    def scan_in_progress(self) -> bool:
        with self._scan_lock:
            return self._scan_in_progress

    @property
    def scan_phase(self) -> str:
        with self._scan_lock:
            return self._scan_phase

    def _set_scan_state(self, in_progress: bool, phase: str) -> None:
        with self._scan_lock:
            self._scan_in_progress = in_progress
            self._scan_phase = phase

    def get_review_queue(self) -> Dict[str, Any]:
        with self._scan_lock:
            return {
                "active": dict(self._active_queue_item) if self._active_queue_item else None,
                "pending": [dict(item) for item in self._review_queue],
            }

    def _start_queue_worker_locked(self) -> None:
        if self._scan_in_progress or not self._review_queue:
            return
        self._scan_in_progress = True
        self._scan_phase = "PROCESSING_REVIEW_QUEUE"
        try:
            self._executor.submit(self._drain_review_queue)
        except Exception:
            self._scan_in_progress = False
            self._scan_phase = "IDLE"
            raise

    def _drain_review_queue(self) -> None:
        while True:
            with self._scan_lock:
                if not self._review_queue:
                    self._active_queue_item = None
                    self._scan_in_progress = False
                    self._scan_phase = "IDLE"
                    return

                is_rl, remaining, reason = self.state_manager.is_rate_limit_active()
                if is_rl:
                    # Rate limited: only proceed if the head item is forced
                    next_item = self._review_queue[0]
                    if not next_item.get("force"):
                        logger.info("Review queue paused: rate limit active (%ds remaining, reason: %s).", remaining, reason)
                        self._active_queue_item = None
                        self._scan_in_progress = False
                        self._scan_phase = "RATE_LIMITED"
                        return

                item = self._review_queue.popleft()
                self._sync_queue_to_state_locked()
                self._active_queue_item = dict(item)
                self._scan_phase = "REVIEWING_QUEUED_PULL_REQUEST"

            was_rate_limited = False
            try:
                repo_name_full = item.get("repo")
                pr_num = item.get("number")
                if not repo_name_full or not pr_num:
                    if "#" in item["pr_key"]:
                        parts = item["pr_key"].split("#")
                        repo_name_full = parts[0]
                        pr_num = int(parts[1]) if parts[1].isdigit() else 0

                if not repo_name_full or not pr_num:
                    item["error"] = f"Invalid PR job format: {item.get('pr_key')}"
                    continue

                repo_info = next(
                    (repo for repo in self.config_manager.get_repos() if repo["full_name"].lower() == repo_name_full.lower()),
                    None,
                )
                if not repo_info:
                    item["error"] = f"Repository '{repo_name_full}' is no longer configured."
                    continue

                owner, repo_name = GitHubClient.split_repo(repo_name_full)
                full_pr = self.github_client.get_pr(owner, repo_name, pr_num)
                if not full_pr or full_pr.get("state") != "open":
                    item["error"] = "Pull request is closed or not available."
                    continue

                # Try available accounts with automatic failover
                accounts = self.config_manager.get_coderabbit_accounts()
                res = None
                if accounts:
                    while True:
                        selection = self.state_manager.select_next_available_account(accounts)
                        if not selection:
                            if not item["force"]:
                                was_rate_limited = True
                                res = {"status": "RATE_LIMITED", "reason": "All accounts on cooldown"}
                            else:
                                res = self.review_engine.review_single_pr(repo_info, full_pr, force=True)
                            break
                        account, _ = selection
                        res = self.review_engine.review_single_pr(repo_info, full_pr, force=item["force"], account=account)
                        if isinstance(res, dict) and res.get("status") == "RATE_LIMITED":
                            # Account hit rate limit, check if any other account can try right away
                            logger.info("Account '%s' hit rate limit on %s, checking if another account is available...", account.get("name"), item["pr_key"])
                            continue
                        break
                else:
                    res = self.review_engine.review_single_pr(repo_info, full_pr, force=item["force"])
                    if isinstance(res, dict) and res.get("status") == "RATE_LIMITED":
                        was_rate_limited = True
            except Exception as e:
                logger.exception("Error reviewing queued PR %s", item["pr_key"])
                item["error"] = str(e)
            finally:
                with self._scan_lock:
                    if was_rate_limited:
                        # Put back in first of the queue
                        # Check if it was not already re-added
                        if not any(queued["pr_key"] == item["pr_key"] for queued in self._review_queue):
                            self._review_queue.appendleft(item)
                            self._sync_queue_to_state_locked()
                    self._active_queue_item = None

    def enqueue_pr(self, pr_key: str, force: bool = False, to_front: bool = False, repo: Optional[str] = None, number: Optional[int] = None, head_sha: Optional[str] = None) -> bool:
        """Adds a PR to the queue if not already queued or actively reviewing (unless updating force)."""
        repo_name = repo or (pr_key.split("#")[0] if "#" in pr_key else "")
        pr_number = number or (int(pr_key.split("#")[1]) if "#" in pr_key and pr_key.split("#")[1].isdigit() else 0)

        with self._scan_lock:
            existing = next((queued for queued in self._review_queue if queued["pr_key"] == pr_key), None)
            if existing:
                if force and not existing.get("force"):
                    existing["force"] = True
                    self._sync_queue_to_state_locked()
                    self._start_queue_worker_locked()
                    return True
                return False

            if self._active_queue_item and self._active_queue_item["pr_key"] == pr_key:
                return False

            item = {
                "pr_key": pr_key,
                "repo": repo_name,
                "number": pr_number,
                "head_sha": head_sha or "",
                "force": bool(force),
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            if to_front:
                self._review_queue.appendleft(item)
            else:
                self._review_queue.append(item)
            self._sync_queue_to_state_locked()
            try:
                self._start_queue_worker_locked()
            except Exception:
                if item in self._review_queue:
                    self._review_queue.remove(item)
                    self._sync_queue_to_state_locked()
                raise
            return True

    def move_queue_item_to_top(self, pr_key: str) -> bool:
        """Moves a queued PR item to the front of the pending review queue."""
        with self._scan_lock:
            target_idx = next((i for i, item in enumerate(self._review_queue) if item["pr_key"] == pr_key), None)
            if target_idx is None:
                return False
            if target_idx == 0:
                return True
            item = self._review_queue[target_idx]
            del self._review_queue[target_idx]
            self._review_queue.appendleft(item)
            self._sync_queue_to_state_locked()
            self._start_queue_worker_locked()
            return True

    def scan_and_enqueue_pending(self, force: bool = False, repo_service=None) -> int:
        """
        Discovers open pull requests across all enabled repositories,
        identifies those that are pending review, and enqueues them.
        """
        if not self.config_manager.is_service_enabled() and not force:
            logger.info("Review service is currently disabled in config.")
            return 0

        # Sync queue from state in case external producers added items
        self._init_queue_from_state()

        service = repo_service or getattr(self, "repository_service", None)
        if not service:
            # If no repo service is attached, start queue worker on existing queue
            with self._scan_lock:
                self._start_queue_worker_locked()
            return 0

        enqueued_count = service.discover_and_enqueue_pending(self.state_manager, force=force)
        self._init_queue_from_state()
        with self._scan_lock:
            self._start_queue_worker_locked()
        return enqueued_count

    def run_review_scan(self, force: bool = False, pr_key: Optional[str] = None, repo_service=None) -> bool:
        """Queue a selected PR or trigger queue drain."""
        if pr_key:
            return self.enqueue_pr(pr_key, force=force)

        # Trigger processing of queue
        with self._scan_lock:
            self._init_queue_from_state()
            self._start_queue_worker_locked()
        return True

    def clear_rate_limit(self) -> None:
        self.state_manager.clear_rate_limit()
        with self._scan_lock:
            self._start_queue_worker_locked()

