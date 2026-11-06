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
        pr_executor: Optional[ThreadPoolExecutor] = None,
    ):
        self.config_manager = config_manager
        self.github_client = github_client
        self._executor = executor or ThreadPoolExecutor(max_workers=5)
        self._pr_executor = pr_executor or ThreadPoolExecutor(max_workers=10, thread_name_prefix="repo-pr")
        self._cached_prs: List[Dict[str, Any]] = []
        self._cache_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._status = {"active": False, "phase": "IDLE", "message": ""}

    def shutdown(self, wait: bool = False) -> None:
        """Shuts down the dedicated PR worker thread pool."""
        if hasattr(self, "_pr_executor"):
            self._pr_executor.shutdown(wait=wait)

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

    def _build_pr_item(
        self,
        full_name: str,
        pr: Dict[str, Any],
        auth_user: Optional[str] = None,
        cached_entry: Optional[Dict[str, Any]] = None,
        base_branch_tips: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Builds a cached PR dictionary, reusing cached review summary if PR has not changed."""
        num = pr.get("number")
        pr_key = f"{full_name}#{num}"
        author = (pr.get("user") or {}).get("login", "")
        is_own = bool(auth_user and author.lower() == auth_user.lower())
        head_sha = (pr.get("head") or {}).get("sha", "")
        base_ref = (pr.get("base") or {}).get("ref", "")
        base_sha = (pr.get("base") or {}).get("sha", "")
        updated_at = pr.get("updated_at", "")

        # Current tip SHA of the target base branch (e.g. tip of 'main')
        current_base_tip = (base_branch_tips or {}).get(base_ref, "")
        cached_base_tip = (cached_entry or {}).get("base_tip_sha", "")

        # Check if we can reuse previous review summary (Delta Caching)
        # Conditions to invalidate cache:
        # 1. head_sha changed (new commits pushed to PR)
        # 2. base_tip_sha changed (target branch advanced, which can introduce merge conflicts)
        # 3. updated_at changed (new comments, reviews, or events on PR)
        # 4. checks_status was "ONGOING" (CI checks still running, refresh to detect when checks finish)
        # 5. mergeable_state was "unknown" or None (GitHub was still computing mergeability, refresh to obtain final status)
        base_tip_matches = True
        if current_base_tip and cached_base_tip:
            base_tip_matches = (current_base_tip == cached_base_tip)
        elif current_base_tip and not cached_base_tip:
            # If current base tip is known but cache has no recorded tip, invalidate to establish baseline
            base_tip_matches = False

        can_reuse_cache = (
            cached_entry is not None
            and cached_entry.get("head_sha") == head_sha
            and base_tip_matches
            and (not base_sha or not cached_entry.get("base_sha") or cached_entry.get("base_sha") == base_sha)
            and cached_entry.get("updated_at") == updated_at
            and cached_entry.get("checks_status") != "ONGOING"
            and cached_entry.get("mergeable_state") not in ("unknown", None, "")
        )
        if can_reuse_cache:
            m_state = cached_entry.get("mergeable_state")
            if not m_state:
                m_state = "clean" if cached_entry.get("has_conflict") is False else "unknown"
            review_summary = {
                "has_other_changes_requested": cached_entry.get("has_other_changes_requested", False),
                "other_changes_requested_by": cached_entry.get("other_changes_requested_by", []),
                "other_approved_by": cached_entry.get("other_approved_by", []),
                "outdated_approvals_by": cached_entry.get("outdated_approvals_by", []),
                "outdated_changes_requested_by": cached_entry.get("outdated_changes_requested_by", []),
                "other_commented_by": cached_entry.get("other_commented_by", []),
                "has_other_commented": cached_entry.get("has_other_commented", False),
                "has_user_auto_approved": cached_entry.get("has_user_auto_approved", False),
                "has_user_manually_approved": cached_entry.get("has_user_manually_approved", False),
                "author_approved_in_text": cached_entry.get("author_approved_in_text", False),
                "has_check_error": cached_entry.get("has_check_error", False),
                "failed_checks": cached_entry.get("failed_checks", []),
                "checks_status": cached_entry.get("checks_status", None),
                "has_conflict": cached_entry.get("has_conflict", False),
                "mergeable_state": m_state,
                "has_user_reviewed": cached_entry.get("has_user_reviewed", False),
                "user_review_state": cached_entry.get("user_review_state"),
            }
        else:
            owner, repo_name = GitHubClient.split_repo(full_name)
            review_summary = self.github_client.get_pr_review_summary(
                owner, repo_name, num, commit_sha=head_sha, auth_user=auth_user, pr_author=author
            )

        m_state = review_summary.get("mergeable_state")
        if not m_state:
            m_state = "clean" if review_summary.get("has_conflict") is False else "unknown"

        return {
            "pr_key": pr_key,
            "repo": full_name,
            "number": num,
            "title": pr.get("title", ""),
            "author": author,
            "is_own_pr": is_own,
            "base_ref": base_ref,
            "base_sha": base_sha,
            "base_tip_sha": current_base_tip or cached_base_tip,
            "head_ref": (pr.get("head") or {}).get("ref", ""),
            "head_sha": head_sha,
            "html_url": pr.get("html_url", ""),
            "created_at": pr.get("created_at", ""),
            "updated_at": updated_at,
            "has_other_changes_requested": review_summary.get("has_other_changes_requested", False),
            "other_changes_requested_by": review_summary.get("other_changes_requested_by", []),
            "other_approved_by": review_summary.get("other_approved_by", []),
            "outdated_approvals_by": review_summary.get("outdated_approvals_by", []),
            "outdated_changes_requested_by": review_summary.get("outdated_changes_requested_by", []),
            "other_commented_by": review_summary.get("other_commented_by", []),
            "has_other_commented": review_summary.get("has_other_commented", False),
            "has_user_auto_approved": review_summary.get("has_user_auto_approved", False),
            "has_user_manually_approved": review_summary.get("has_user_manually_approved", False),
            "author_approved_in_text": review_summary.get("author_approved_in_text", False),
            "has_check_error": review_summary.get("has_check_error", False),
            "failed_checks": review_summary.get("failed_checks", []),
            "checks_status": review_summary.get("checks_status", None),
            "has_conflict": review_summary.get("has_conflict", False),
            "mergeable_state": m_state,
            "has_user_reviewed": review_summary.get("has_user_reviewed", False),
            "user_review_state": review_summary.get("user_review_state"),
        }

    def _build_fallback_pr_item(
        self,
        full_name: str,
        pr: Dict[str, Any],
        auth_user: Optional[str] = None,
    ) -> Dict[str, Any]:
        num = pr.get("number")
        pr_key = f"{full_name}#{num}"
        author = (pr.get("user") or {}).get("login", "")
        is_own = bool(auth_user and author.lower() == auth_user.lower())
        head_sha = (pr.get("head") or {}).get("sha", "")
        base_ref = (pr.get("base") or {}).get("ref", "")
        base_sha = (pr.get("base") or {}).get("sha", "")
        updated_at = pr.get("updated_at", "")

        return {
            "pr_key": pr_key,
            "repo": full_name,
            "number": num,
            "title": pr.get("title", ""),
            "author": author,
            "is_own_pr": is_own,
            "base_ref": base_ref,
            "base_sha": base_sha,
            "base_tip_sha": "",
            "head_ref": (pr.get("head") or {}).get("ref", ""),
            "head_sha": head_sha,
            "html_url": pr.get("html_url", ""),
            "created_at": pr.get("created_at", ""),
            "updated_at": updated_at,
            "has_other_changes_requested": False,
            "other_changes_requested_by": [],
            "other_approved_by": [],
            "outdated_approvals_by": [],
            "outdated_changes_requested_by": [],
            "other_commented_by": [],
            "has_other_commented": False,
            "has_user_auto_approved": False,
            "has_user_manually_approved": False,
            "author_approved_in_text": False,
            "has_check_error": False,
            "failed_checks": [],
            "checks_status": None,
            "has_conflict": False,
            "mergeable_state": "unknown",
            "has_user_reviewed": False,
            "user_review_state": None,
        }

    def fetch_repo_prs(self, repo_info: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        full_name = repo_info.get("full_name", "")
        if not full_name:
            return []

        owner, repo_name = GitHubClient.split_repo(full_name)
        try:
            prs = self.github_client.list_open_prs(owner, repo_name)
        except Exception as e:
            logger.warning("Error fetching open PR list for %s: %s", full_name, e)
            return None

        try:
            auth_user = self.github_client.get_username()

            # Pre-fetch the latest tip commit SHA for each distinct base branch of open PRs
            # (e.g. 'main', 'staging', 'develop') so we can detect when a base branch advances
            # (such as when another PR is merged into it).
            base_refs = {
                (pr.get("base") or {}).get("ref")
                for pr in prs
                if (pr.get("base") or {}).get("ref")
            }
            base_branch_tips: Dict[str, str] = {}
            for ref in base_refs:
                try:
                    if hasattr(self.github_client, "get_branch"):
                        branch_data = self.github_client.get_branch(owner, repo_name, ref)
                        sha = ((branch_data.get("commit") or {}).get("sha")) or ""
                        if sha:
                            base_branch_tips[ref] = sha
                except Exception as e:
                    logger.debug("Could not fetch branch tip for %s/%s branch %s: %s", owner, repo_name, ref, e)

            # Build a lookup of existing cached PRs for fast delta matching
            with self._cache_lock:
                cache_by_key = {p.get("pr_key"): dict(p) for p in self._cached_prs}

            # Fetch PR details concurrently using dedicated PR worker pool to avoid deadlock
            futures = [
                self._pr_executor.submit(
                    self._build_pr_item,
                    full_name,
                    pr,
                    auth_user,
                    cache_by_key.get(f"{full_name}#{pr.get('number')}"),
                    base_branch_tips,
                )
                for pr in prs
            ]

            results = []
            for pr, f in zip(prs, futures):
                try:
                    results.append(f.result())
                except Exception as e:
                    logger.warning("Error building PR item for %s#%s: %s", full_name, pr.get("number"), e)
                    cached = cache_by_key.get(f"{full_name}#{pr.get('number')}")
                    if cached:
                        results.append(cached)
                    else:
                        results.append(self._build_fallback_pr_item(full_name, pr, auth_user))

            return results
        except Exception as e:
            logger.warning("Error processing PR details for %s: %s", full_name, e)
            return None

    def refresh_pr_cache(self) -> List[Dict[str, Any]]:
        """Fetch open PRs for enabled repositories concurrently and cache them."""
        with self._activity("CHECKING_REPOSITORIES", "Loading enabled repository configuration"):
            enabled_repos = self.config_manager.get_enabled_repos()
            enabled_repo_names = {r.get("full_name", "").lower() for r in enabled_repos if r.get("full_name")}
            self._set_status(
                True,
                "FETCHING_PULL_REQUESTS",
                f"Fetching open pull requests from {len(enabled_repos)} enabled repositories",
            )
            future_to_repo = {
                self._executor.submit(self.fetch_repo_prs, repo): repo
                for repo in enabled_repos
            }

            # Map of repo_full_name (lower) -> List of PR items
            # Pre-populate from existing cache for currently enabled repos
            with self._cache_lock:
                repo_prs_map: Dict[str, List[Dict[str, Any]]] = {}
                for pr in self._cached_prs:
                    repo_key = (pr.get("repo") or "").lower()
                    if repo_key in enabled_repo_names:
                        repo_prs_map.setdefault(repo_key, []).append(dict(pr))

            for future, repo_info in future_to_repo.items():
                full_name = repo_info.get("full_name", "")
                repo_key = full_name.lower()
                try:
                    repo_results = future.result()
                    if repo_results is not None:
                        # Fetch succeeded! Update cache for this repo
                        repo_prs_map[repo_key] = repo_results
                    else:
                        # Fetch failed, keep existing cached PRs for this repo if any
                        logger.warning(
                            "Fetch failed for repository %s; preserving %d cached PR(s)",
                            full_name, len(repo_prs_map.get(repo_key, []))
                        )
                except Exception as e:
                    logger.error(
                        "Exception during repository PR fetch for %s: %s; preserving cache",
                        full_name, e
                    )

            # Flatten and sort all PRs across repositories
            collected = []
            for pr_list in repo_prs_map.values():
                collected.extend(pr_list)

            collected.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
            self._set_status(True, "UPDATING_PR_CACHE", f"Updating cache with {len(collected)} open pull requests")
            with self._cache_lock:
                self._cached_prs = collected
            logger.info("PR cache refreshed: %d open PR(s) found across %d repositories.", len(collected), len(enabled_repos))
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
