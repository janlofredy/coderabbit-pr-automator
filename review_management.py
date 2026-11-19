"""Review scheduling and execution orchestration."""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set

from timezone_utils import now_app_tz

from auto_review_prs import AutoReviewEngine
from config_manager import ConfigManager
from github_client import GitHubClient
from state_manager import StateManager

logger = logging.getLogger("review_management")


class ReviewManagementService:
    """Owns review queue processing and concurrent multi-account review execution."""

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
            self.review_engine = review_engine
            self._executor = executor or ThreadPoolExecutor(max_workers=5)
        else:
            self.review_engine = review_engine_or_repo_service
            self._executor = executor if isinstance(executor, ThreadPoolExecutor) else ThreadPoolExecutor(max_workers=5)

        self._scan_lock = threading.Lock()
        self._scan_in_progress = False
        self._scan_phase = "IDLE"
        self._review_queue = deque()
        self._active_items: Dict[str, Dict[str, Any]] = {}
        self._active_queue_item: Optional[Dict[str, Any]] = None
        self._busy_account_ids: Set[str] = set()
        self._active_workers = 0
        self._init_queue_from_state()

    def _sync_active_item_locked(self) -> None:
        self._active_queue_item = dict(next(iter(self._active_items.values()))) if self._active_items else None

    def _init_queue_from_state(self) -> None:
        with self._scan_lock:
            try:
                persisted = self.state_manager.get_review_queue()
                existing_keys = {
                    item["pr_key"]
                    for item in self._review_queue
                    if isinstance(item, dict) and "pr_key" in item
                }
                existing_keys.update(self._active_items.keys())

                for item in persisted:
                    if isinstance(item, dict) and "pr_key" in item:
                        pr_key = item["pr_key"]
                        if pr_key not in existing_keys:
                            self._review_queue.append(dict(item))
                            existing_keys.add(pr_key)
                        elif item.get("force"):
                            for existing in self._review_queue:
                                if existing.get("pr_key") == pr_key:
                                    existing["force"] = True
                                    break
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
            active_list = [dict(item) for item in self._active_items.values()]
            return {
                "active": active_list[0] if active_list else None,
                "active_items": active_list,
                "pending": [dict(item) for item in self._review_queue],
            }

    def _desired_worker_count_locked(self) -> int:
        try:
            accounts = self.config_manager.get_coderabbit_accounts() or []
        except Exception:
            accounts = []
        enabled = [a for a in accounts if isinstance(a, dict) and a.get("enabled", True)]
        if len(enabled) <= 1:
            return 1
        try:
            rl_map = self.state_manager.get_account_rate_limits()
        except Exception:
            rl_map = {}
        available = sum(1 for a in enabled if str(a.get("id", "")) not in rl_map)
        if available <= 0:
            return 1
        return min(available, 4)

    def _start_queue_worker_locked(self) -> None:
        if not self._review_queue:
            return

        if self._active_workers == 0:
            is_rl, _, _ = self.state_manager.is_rate_limit_active()
            if is_rl and not self._review_queue[0].get("force"):
                self._scan_in_progress = False
                self._scan_phase = "RATE_LIMITED"
                return

        desired = min(
            self._desired_worker_count_locked(),
            self._active_workers + len(self._review_queue),
        )
        while self._active_workers < desired:
            self._active_workers += 1
            self._scan_in_progress = True
            self._scan_phase = "PROCESSING_REVIEW_QUEUE"
            try:
                self._executor.submit(self._drain_review_queue, True)
            except Exception:
                self._active_workers = max(0, self._active_workers - 1)
                if self._active_workers == 0:
                    self._scan_in_progress = False
                    self._scan_phase = "IDLE"
                raise

    def _drain_review_queue(self, _spawned: bool = False) -> None:
        final_phase = "IDLE"
        try:
            while True:
                with self._scan_lock:
                    if not self._review_queue:
                        break

                    is_rl, remaining, reason = self.state_manager.is_rate_limit_active()
                    if is_rl:
                        next_item = self._review_queue[0]
                        if not next_item.get("force"):
                            logger.info("Review queue paused: rate limit active (%ds remaining, reason: %s).", remaining, reason)
                            final_phase = "RATE_LIMITED"
                            break

                    item = self._review_queue.popleft()
                    self._sync_queue_to_state_locked()
                    self._active_items[item["pr_key"]] = item
                    self._sync_active_item_locked()
                    self._scan_phase = "REVIEWING_QUEUED_PULL_REQUEST"

                was_rate_limited = False
                requeue_to_front = False
                retire_worker = False
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

                    # Try available accounts with automatic failover and multi-account parallelism
                    accounts = self.config_manager.get_coderabbit_accounts()
                    enabled_accounts = [a for a in accounts if isinstance(a, dict) and a.get("enabled", True)] if accounts else []
                    use_worktree = len(enabled_accounts) > 1
                    res = None
                    if accounts:
                        while True:
                            selected_acc_id: Optional[str] = None
                            account = None
                            with self._scan_lock:
                                selection = self.state_manager.select_next_available_account(
                                    accounts, exclude_ids=self._busy_account_ids
                                )
                                if selection:
                                    account, _ = selection
                                    selected_acc_id = str(account.get("id", ""))
                                    if selected_acc_id:
                                        self._busy_account_ids.add(selected_acc_id)
                                    item["account_id"] = selected_acc_id
                                    item["account_name"] = account.get("name", "")
                            if not selection:
                                rl_map = self.state_manager.get_account_rate_limits()
                                with self._scan_lock:
                                    any_unblocked_busy = any(
                                        str(a.get("id", "")) in self._busy_account_ids
                                        and str(a.get("id", "")) not in rl_map
                                        for a in enabled_accounts
                                    )
                                if any_unblocked_busy:
                                    requeue_to_front = True
                                    retire_worker = True
                                    break
                                if not item["force"]:
                                    was_rate_limited = True
                                    final_phase = "RATE_LIMITED"
                                    res = {"status": "RATE_LIMITED", "reason": "All accounts on cooldown"}
                                else:
                                    res = self.review_engine.review_single_pr(
                                        repo_info, full_pr, force=True, use_worktree=use_worktree
                                    )
                                break

                            try:
                                res = self.review_engine.review_single_pr(
                                    repo_info,
                                    full_pr,
                                    force=item["force"],
                                    account=account,
                                    use_worktree=use_worktree,
                                )
                            finally:
                                if selected_acc_id:
                                    with self._scan_lock:
                                        self._busy_account_ids.discard(selected_acc_id)

                            if isinstance(res, dict) and res.get("status") == "RATE_LIMITED":
                                logger.info(
                                    "Account '%s' hit rate limit on %s, checking if another account is available...",
                                    account.get("name"),
                                    item["pr_key"],
                                )
                                continue
                            break
                    else:
                        res = self.review_engine.review_single_pr(repo_info, full_pr, force=item["force"])
                        if isinstance(res, dict) and res.get("status") == "RATE_LIMITED":
                            was_rate_limited = True
                            final_phase = "RATE_LIMITED"
                except Exception as e:
                    logger.exception("Error reviewing queued PR %s", item["pr_key"])
                    item["error"] = str(e)
                finally:
                    with self._scan_lock:
                        self._active_items.pop(item["pr_key"], None)
                        self._sync_active_item_locked()
                        if was_rate_limited or requeue_to_front:
                            if not any(queued["pr_key"] == item["pr_key"] for queued in self._review_queue):
                                self._review_queue.appendleft(item)
                                self._sync_queue_to_state_locked()

                if was_rate_limited or retire_worker:
                    break
        finally:
            with self._scan_lock:
                if _spawned and self._active_workers > 0:
                    self._active_workers -= 1
                if self._active_workers == 0:
                    self._sync_active_item_locked()
                    self._scan_in_progress = False
                    self._scan_phase = final_phase

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

            if pr_key in self._active_items or (self._active_queue_item and self._active_queue_item["pr_key"] == pr_key):
                return False

            item = {
                "pr_key": pr_key,
                "repo": repo_name,
                "number": pr_number,
                "head_sha": head_sha or "",
                "force": bool(force),
                "queued_at": now_app_tz().isoformat(),
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

        self._init_queue_from_state()

        service = repo_service or getattr(self, "repository_service", None)
        if not service:
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

        with self._scan_lock:
            self._init_queue_from_state()
            self._start_queue_worker_locked()
        return True

    def clear_rate_limit(self) -> None:
        self.state_manager.clear_rate_limit()
        with self._scan_lock:
            self._start_queue_worker_locked()
