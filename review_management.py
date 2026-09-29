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
                item = self._review_queue.popleft()
                self._active_queue_item = dict(item)
                self._scan_phase = "REVIEWING_QUEUED_PULL_REQUEST"

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
                self.review_engine.review_single_pr(repo_info, full_pr, force=item["force"])
            except Exception as e:
                logger.exception("Error reviewing queued PR %s", item["pr_key"])
                item["error"] = str(e)
            finally:
                with self._scan_lock:
                    self._active_queue_item = None

    def run_review_scan(self, force: bool = False, pr_key: Optional[str] = None) -> bool:
        """Queue a selected PR or start a full review scan."""
        if pr_key:
            item = {
                "pr_key": pr_key,
                "force": bool(force),
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            with self._scan_lock:
                if any(queued["pr_key"] == pr_key for queued in self._review_queue) or (
                    self._active_queue_item and self._active_queue_item["pr_key"] == pr_key
                ):
                    return False
                self._review_queue.append(item)
                try:
                    self._start_queue_worker_locked()
                except Exception:
                    self._review_queue.remove(item)
                    raise
            return True

        with self._scan_lock:
            if self._scan_in_progress:
                logger.info("Scan already in progress. Skipping.")
                return False
            self._scan_in_progress = True
            self._scan_phase = "CHECKING_REPOSITORIES"

        def task():
            try:
                self.repository_service.refresh_pr_cache()
                self._set_scan_state(True, "DISCOVERING_PULL_REQUESTS")
                self.review_engine.scan_and_review_all(force=force)
            except Exception as e:
                logger.error("Error in review scan task: %s", e)
            finally:
                self._set_scan_state(True, "REFRESHING_PR_STATUS")
                try:
                    self.repository_service.refresh_pr_cache()
                finally:
                    with self._scan_lock:
                        self._scan_in_progress = False
                        self._scan_phase = "IDLE"
                        self._start_queue_worker_locked()

        try:
            self._executor.submit(task)
        except Exception:
            self._set_scan_state(False, "IDLE")
            raise
        return True

    def clear_rate_limit(self) -> None:
        self.state_manager.clear_rate_limit()
