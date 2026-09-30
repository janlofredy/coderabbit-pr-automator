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
from repository_management import RepositoryManagementService
from state_manager import StateManager

logger = logging.getLogger("review_management")


class ReviewManagementService:
    """Owns scan scheduling, selected-PR runs, and review-engine execution."""

    def __init__(
        self,
        config_manager: ConfigManager,
        state_manager: StateManager,
        github_client: GitHubClient,
        repository_service: RepositoryManagementService,
        review_engine: AutoReviewEngine,
        executor: ThreadPoolExecutor,
    ):
        self.config_manager = config_manager
        self.state_manager = state_manager
        self.github_client = github_client
        self.repository_service = repository_service
        self.review_engine = review_engine
        self._executor = executor
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
                self.repository_service.refresh_pr_cache()
                target = self.repository_service.find_cached_pr(item["pr_key"])
                if not target:
                    item["error"] = "Pull request is no longer open or available."
                    continue
                repo_info = next(
                    (repo for repo in self.config_manager.get_repos() if repo["full_name"] == target["repo"]),
                    None,
                )
                if not repo_info:
                    item["error"] = "Repository is no longer configured."
                    continue
                owner, repo_name = GitHubClient.split_repo(target["repo"])
                full_pr = self.github_client.get_pr(owner, repo_name, target["number"])
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

    def enqueue_pr(self, pr_key: str, force: bool = False, to_front: bool = False) -> bool:
        """Adds a PR to the queue if not already queued or actively reviewing (unless updating force)."""
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

    def scan_and_enqueue_pending(self, force: bool = False) -> int:
        """
        Discovers open pull requests across all enabled repositories,
        identifies those that are pending review, and enqueues them.
        """
        if not self.config_manager.is_service_enabled() and not force:
            logger.info("Review service is currently disabled in config.")
            return 0

        cached_prs = self.repository_service.refresh_pr_cache()
        pr_statuses = self.state_manager.get_all_pr_statuses()
        auth_user = self.github_client.get_username()
        enqueued_count = 0

        for pr in cached_prs:
            pr_key = pr["pr_key"]
            status_entry = pr_statuses.get(pr_key, {})
            status_name = status_entry.get("status")

            # Check eligibility:
            # 1. Skip if own PR (author matches auth_user) unless force
            if pr.get("is_own_pr", False) and not force:
                continue

            # 2. Skip if already reviewed on current commit SHA unless force
            head_sha = pr.get("head_sha", "")
            if status_name == "ALREADY_REVIEWED" and status_entry.get("head_sha") == head_sha and not force:
                continue
            if status_name == "COMPLETED" and status_entry.get("head_sha") == head_sha and not force:
                continue
            if pr.get("has_user_auto_approved") and not force:
                continue

            # 3. Skip if max file limit exceeded on current head unless force
            if status_name == "SKIPPED_MAX_FILES" and status_entry.get("head_sha") == head_sha and not force:
                continue

            # Enqueue eligible pending PR
            if self.enqueue_pr(pr_key, force=force):
                enqueued_count += 1

        self.state_manager.record_last_run()
        return enqueued_count

    def run_review_scan(self, force: bool = False, pr_key: Optional[str] = None) -> bool:
        """Queue a selected PR or scan for pending PRs and enqueue them."""
        if pr_key:
            return self.enqueue_pr(pr_key, force=force)

        with self._scan_lock:
            # If a queue worker is running, we can still discover and enqueue PRs in background
            if self._scan_in_progress and self._scan_phase != "PROCESSING_REVIEW_QUEUE":
                logger.info("Scan already in progress. Skipping.")
                return False

        def task():
            try:
                self.scan_and_enqueue_pending(force=force)
            except Exception as e:
                logger.error("Error scanning and enqueuing pending PRs: %s", e)
            finally:
                with self._scan_lock:
                    self._start_queue_worker_locked()

        try:
            self._executor.submit(task)
        except Exception:
            raise
        return True

    def clear_rate_limit(self) -> None:
        self.state_manager.clear_rate_limit()
        with self._scan_lock:
            self._start_queue_worker_locked()

