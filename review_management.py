"""Review scheduling and execution orchestration."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

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

    def run_review_scan(self, force: bool = False, pr_key: Optional[str] = None) -> bool:
        """Queue one review scan; return False when another scan is already active."""
        with self._scan_lock:
            if self._scan_in_progress:
                logger.info("Scan already in progress. Skipping.")
                return False
            self._scan_in_progress = True
            self._scan_phase = "CHECKING_REPOSITORIES"

        def task():
            try:
                self.repository_service.refresh_pr_cache()
                if pr_key:
                    self._set_scan_state(True, "LOADING_SELECTED_PULL_REQUEST")
                    target = self.repository_service.find_cached_pr(pr_key)
                    if target:
                        repo_info = next(
                            (repo for repo in self.config_manager.get_repos() if repo["full_name"] == target["repo"]),
                            None,
                        )
                        if repo_info:
                            self._set_scan_state(True, "REVIEWING_SELECTED_PULL_REQUEST")
                            owner, repo_name = GitHubClient.split_repo(target["repo"])
                            full_pr = self.github_client.get_pr(owner, repo_name, target["number"])
                            self.review_engine.review_single_pr(repo_info, full_pr, force=force)
                else:
                    self._set_scan_state(True, "DISCOVERING_PULL_REQUESTS")
                    self.review_engine.scan_and_review_all(force=force)
            except Exception as e:
                logger.error("Error in review scan task: %s", e)
            finally:
                self._set_scan_state(True, "REFRESHING_PR_STATUS")
                try:
                    self.repository_service.refresh_pr_cache()
                finally:
                    self._set_scan_state(False, "IDLE")

        try:
            self._executor.submit(task)
        except Exception:
            self._set_scan_state(False, "IDLE")
            raise
        return True

    def clear_rate_limit(self) -> None:
        self.state_manager.clear_rate_limit()
