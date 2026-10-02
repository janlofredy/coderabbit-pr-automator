"""Repository configuration and GitHub pull-request discovery services."""

import logging
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Iterator

from config_manager import ConfigManager
from github_client import GitHubClient

logger = logging.getLogger("repository_management")


class RepositoryManagementService:
    """Owns repository configuration operations and the open-PR cache."""

    def __init__(
        self,
        config_manager: ConfigManager,
        github_client: GitHubClient,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        self.config_manager = config_manager
        self.github_client = github_client
        self._executor = executor or ThreadPoolExecutor(max_workers=5)
        self._cached_prs: List[Dict[str, Any]] = []
        self._cache_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._status = {"active": False, "phase": "IDLE", "message": ""}

    def _set_status(self, active: bool, phase: str, message: str = "") -> None:
        with self._status_lock:
            self._status = {"active": active, "phase": phase, "message": message}

    def get_status(self) -> Dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    @contextmanager
    def _activity(self, phase: str, message: str) -> Iterator[None]:
        with self._activity_lock:
            self._set_status(True, phase, message)
            try:
                yield
            finally:
                self._set_status(False, "IDLE", "")

    def get_repositories(self) -> List[Dict[str, Any]]:
        return self.config_manager.get_repos()

    def validate_repository(self, full_name: str) -> bool:
        with self._activity("VALIDATING_REPOSITORY", f"Checking GitHub access to {full_name}"):
            return self.github_client.validate_repo(full_name)

    def add_repository(self, full_name: str) -> Dict[str, Any]:
        with self._activity("ADDING_REPOSITORY", f"Adding {full_name} to the repository list"):
            return self.config_manager.add_repo(full_name)

    def toggle_repository(self, full_name: str, enabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        action = "Updating"
        if enabled is True:
            action = "Enabling"
        elif enabled is False:
            action = "Pausing"
        with self._activity("UPDATING_REPOSITORY", f"{action} repository {full_name}"):
            return self.config_manager.toggle_repo(full_name, enabled)

    def remove_repository(self, full_name: str) -> bool:
        with self._activity("REMOVING_REPOSITORY", f"Removing {full_name} from the repository list"):
            return self.config_manager.remove_repo(full_name)

    def fetch_repo_prs(self, repo_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        full_name = repo_info.get("full_name", "")
        if not full_name:
            return []

        owner, repo_name = GitHubClient.split_repo(full_name)
        try:
            prs = self.github_client.list_open_prs(owner, repo_name)
            auth_user = self.github_client.get_username()
            results = []

            for pr in prs:
                num = pr.get("number")
                pr_key = f"{full_name}#{num}"
                author = (pr.get("user") or {}).get("login", "")
                is_own = bool(auth_user and author.lower() == auth_user.lower())
                head_sha = (pr.get("head") or {}).get("sha", "")
                review_summary = self.github_client.get_pr_review_summary(
                    owner, repo_name, num, commit_sha=head_sha, auth_user=auth_user
                )

                results.append({
                    "pr_key": pr_key,
                    "repo": full_name,
                    "number": num,
                    "title": pr.get("title", ""),
                    "author": author,
                    "is_own_pr": is_own,
                    "base_ref": (pr.get("base") or {}).get("ref", ""),
                    "head_ref": (pr.get("head") or {}).get("ref", ""),
                    "head_sha": head_sha,
                    "html_url": pr.get("html_url", ""),
                    "created_at": pr.get("created_at", ""),
                    "updated_at": pr.get("updated_at", ""),
                    "has_other_changes_requested": review_summary.get("has_other_changes_requested", False),
                    "other_changes_requested_by": review_summary.get("other_changes_requested_by", []),
                    "other_approved_by": review_summary.get("other_approved_by", []),
                    "other_commented_by": review_summary.get("other_commented_by", []),
                    "has_other_commented": review_summary.get("has_other_commented", False),
                    "has_user_auto_approved": review_summary.get("has_user_auto_approved", False),
                    "has_user_manually_approved": review_summary.get("has_user_manually_approved", False),
                    "has_check_error": review_summary.get("has_check_error", False),
                    "failed_checks": review_summary.get("failed_checks", []),
                    "has_conflict": review_summary.get("has_conflict", False),
                    "mergeable_state": review_summary.get("mergeable_state", "unknown"),
                    "has_user_reviewed": review_summary.get("has_user_reviewed", False),
                    "user_review_state": review_summary.get("user_review_state"),
                })
            return results
        except Exception as e:
            logger.warning("Error fetching PRs for %s: %s", full_name, e)
            return []

    def refresh_pr_cache(self) -> List[Dict[str, Any]]:
        """Fetch open PRs for enabled repositories concurrently and cache them."""
        with self._activity("CHECKING_REPOSITORIES", "Loading enabled repository configuration"):
            enabled_repos = self.config_manager.get_enabled_repos()
            self._set_status(
                True,
                "FETCHING_PULL_REQUESTS",
                f"Fetching open pull requests from {len(enabled_repos)} enabled repositories",
            )
            futures = [self._executor.submit(self.fetch_repo_prs, repo) for repo in enabled_repos]
            collected = []
            for future in futures:
                try:
                    collected.extend(future.result())
                except Exception as e:
                    logger.error("Exception during repository PR fetch: %s", e)

            collected.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
            self._set_status(True, "UPDATING_PR_CACHE", f"Updating cache with {len(collected)} open pull requests")
            with self._cache_lock:
                self._cached_prs = collected
            logger.info("PR cache refreshed: %d open PR(s) found.", len(collected))
            return collected

    def get_cached_prs(self) -> List[Dict[str, Any]]:
        with self._cache_lock:
            return [dict(pr) for pr in self._cached_prs]

    def find_cached_pr(self, pr_key: str) -> Optional[Dict[str, Any]]:
        with self._cache_lock:
            pr = next((item for item in self._cached_prs if item.get("pr_key") == pr_key), None)
            return dict(pr) if pr else None

    def discover_and_enqueue_pending(self, state_manager, force: bool = False) -> int:
        """
        Discovers open pull requests across all enabled repositories,
        identifies those pending review, and enqueues self-contained jobs into state_manager.
        """
        if not self.config_manager.is_service_enabled() and not force:
            logger.info("Review service is currently disabled in config.")
            return 0

        cached_prs = self.refresh_pr_cache()
        pr_statuses = state_manager.get_all_pr_statuses()
        auth_user = self.github_client.get_username()
        enqueued_count = 0

        for pr in cached_prs:
            pr_key = pr["pr_key"]
            status_entry = pr_statuses.get(pr_key, {})
            status_name = status_entry.get("status")

            # Check eligibility:
            # 1. Skip if already reviewed on current commit SHA unless force
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

            # Enqueue self-contained PR review job
            job = {
                "pr_key": pr_key,
                "repo": pr.get("repo", pr_key.split("#")[0] if "#" in pr_key else ""),
                "number": pr.get("number", int(pr_key.split("#")[1]) if "#" in pr_key and pr_key.split("#")[1].isdigit() else 0),
                "head_sha": head_sha,
                "base_ref": pr.get("base_ref", "main"),
                "force": bool(force),
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            if state_manager.enqueue_review_job(job, to_front=False):
                enqueued_count += 1

        state_manager.record_last_run()
        return enqueued_count
