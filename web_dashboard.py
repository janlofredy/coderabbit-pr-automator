import os
import json
import time
import threading
import logging
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs

from config_manager import ConfigManager, safe_int_env
from state_manager import StateManager
from github_client import GitHubClient
from auto_review_prs import AutoReviewEngine, DEFAULT_REVIEWS_DIR
from repository_management import RepositoryManagementService
from review_management import ReviewManagementService

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

PORT = safe_int_env("PORT", 8765)
HOST = os.getenv("HOST", "0.0.0.0")

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP Server for fast sub-millisecond responses."""
    daemon_threads = True

class DashboardBackend:
    """Coordinates in-memory cache, background polling, and review executions."""

    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        state_manager: Optional[StateManager] = None,
        github_client: Optional[GitHubClient] = None,
        review_engine: Optional[AutoReviewEngine] = None,
        auto_start_worker: bool = True
    ):
        self.config_manager = config_manager or ConfigManager()
        self.state_manager = state_manager or StateManager()
        self.github_client = github_client or GitHubClient(token=self.config_manager.get_github_token())
        self.review_engine = review_engine or AutoReviewEngine(
            config_manager=self.config_manager,
            state_manager=self.state_manager,
            github_client=self.github_client
        )

        # Keep one shared executor for concurrent repository fetches and the
        # single-flight review task; the services own their respective logic.
        self._executor = ThreadPoolExecutor(max_workers=5)
        self.repository_service = RepositoryManagementService(
            self.config_manager, self.github_client, executor=self._executor
        )
        self.review_service = ReviewManagementService(
            self.config_manager,
            self.state_manager,
            self.github_client,
            self.review_engine,
            executor=self._executor,
        )
        self._is_running = True
        self._auth_lock = threading.Lock()
        self._auth_status_cache = {"status": "checking", "checked_at": 0}

        # Start independent background worker threads
        self._repo_worker_thread = None
        self._review_worker_thread = None
        if auto_start_worker:
            self._repo_worker_thread = threading.Thread(target=self._repo_polling_loop, daemon=True)
            self._review_worker_thread = threading.Thread(target=self._review_worker_loop, daemon=True)
            self._repo_worker_thread.start()
            self._review_worker_thread.start()

    def fetch_repo_prs(self, repo_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Compatibility facade; repository discovery lives in its service."""
        return self.repository_service.fetch_repo_prs(repo_info)

    def refresh_pr_cache(self) -> None:
        """Compatibility facade; repository cache management lives in its service."""
        self.repository_service.refresh_pr_cache()

    def _repo_polling_loop(self) -> None:
        """Independent daemon polling repositories, refreshing PR cache, and enqueuing jobs."""
        logger.info("Repository polling daemon started.")
        # Initial refresh
        self.refresh_pr_cache()

        while self._is_running:
            interval = self.config_manager.get_repo_poll_interval()
            if self.config_manager.is_service_enabled():
                try:
                    self.repository_service.discover_and_enqueue_pending(self.state_manager, force=False)
                    # Notify review service to check queue
                    self.review_service._init_queue_from_state()
                    with self.review_service._scan_lock:
                        self.review_service._start_queue_worker_locked()
                except Exception as e:
                    logger.error("Error in repository polling loop: %s", e)

            # Sleep in short increments for responsive shutdown
            for _ in range(max(1, interval // 5)):
                if not self._is_running:
                    break
                time.sleep(5)

    def _review_worker_loop(self) -> None:
        """Independent daemon draining the review queue at review_poll_interval."""
        logger.info("Review queue consumer daemon started.")
        while self._is_running:
            interval = self.config_manager.get_review_poll_interval()
            if self.config_manager.is_service_enabled():
                try:
                    self.review_service._init_queue_from_state()
                    with self.review_service._scan_lock:
                        self.review_service._start_queue_worker_locked()
                except Exception as e:
                    logger.error("Error in review worker loop: %s", e)

            for _ in range(max(1, interval // 2)):
                if not self._is_running:
                    break
                time.sleep(2)

    def run_review_scan(self, force: bool = False, pr_key: Optional[str] = None) -> bool:
        """Compatibility facade; triggers PR review or repository scan."""
        if pr_key:
            return self.review_service.enqueue_pr(pr_key, force=force)

        # Trigger repo discovery & enqueue, then wake up review worker
        def task():
            try:
                self.repository_service.discover_and_enqueue_pending(self.state_manager, force=force)
            except Exception as e:
                logger.error("Error during manual repository scan: %s", e)
            finally:
                self.review_service._init_queue_from_state()
                with self.review_service._scan_lock:
                    self.review_service._start_queue_worker_locked()

        self._executor.submit(task)
        return True

    def test_coderabbit_account_auth(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """Runs coderabbit auth status or a test review probe for a specific account."""
        acc_type = account.get("type", "api_key")
        env = os.environ.copy()
        cmd = ["coderabbit", "auth", "status", "--agent"]

        if acc_type == "api_key" and account.get("api_key"):
            env["CODERABBIT_API_KEY"] = account["api_key"]
            if account.get("region"):
                cmd.extend(["--region", account["region"]])
        elif acc_type == "profile" and account.get("profile_dir"):
            p_dir = account["profile_dir"]
            if os.path.isdir(p_dir):
                env["HOME"] = p_dir
            else:
                return {"authenticated": False, "status": "directory_not_found", "message": f"Directory not found: {p_dir}"}
        else:
            env.pop("CODERABBIT_API_KEY", None)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
                check=False,
            )
        except FileNotFoundError:
            return {"authenticated": False, "status": "unavailable", "message": "CodeRabbit CLI not installed"}
        except subprocess.TimeoutExpired:
            return {"authenticated": False, "status": "timeout", "message": "Authentication check timed out"}

        authenticated = False
        status = "needs_auth"
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict) and event.get("phase") == "auth":
                if event.get("authenticated") is True:
                    authenticated = True
                    status = "authenticated"
                elif event.get("authenticated") is False:
                    authenticated = False
                    status = "needs_auth"
                break

        return {"authenticated": authenticated, "status": status, "output": result.stdout.strip()}

    def get_coderabbit_auth_status(self) -> Dict[str, str]:
        """Return a sanitized, briefly cached status from the official CLI."""
        with self._auth_lock:
            if time.monotonic() - self._auth_status_cache["checked_at"] < 30:
                return {"status": self._auth_status_cache["status"]}
            try:
                result = subprocess.run(
                    ["coderabbit", "auth", "status", "--agent"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
            except FileNotFoundError:
                status = "unavailable"
            except subprocess.TimeoutExpired:
                status = "unavailable"
            else:
                status = "unavailable"
                for line in result.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(event, dict) and event.get("phase") == "auth":
                        if event.get("authenticated") is True:
                            status = "authenticated"
                        elif event.get("authenticated") is False:
                            status = "needs_auth"
                        break
            self._auth_status_cache = {"status": status, "checked_at": time.monotonic()}
            return {"status": status}

    def get_annotated_status(self) -> Dict[str, Any]:
        """Overlays real-time state manager data onto cached PR records for sub-millisecond response."""
        state = self.state_manager.load_state()
        config = self.config_manager.load_config()

        is_rate_limited, remaining, reason = self.state_manager.is_rate_limit_active()
        active_reviews = state.get("active_reviews", {})
        pr_statuses = state.get("pr_statuses", {})
        auth_user = self.github_client.get_username()

        annotated_prs = []
        cached_list = self.repository_service.get_cached_prs()
        review_queue = state.get("review_queue", [])
        queued_keys = {q.get("pr_key") for q in review_queue if isinstance(q, dict)}

        for pr in cached_list:
            item = dict(pr)
            key = item["pr_key"]
            status_entry = pr_statuses.get(key, {})

            has_other_changes = item.get("has_other_changes_requested", False)
            other_changers = item.get("other_changes_requested_by", [])
            changers_str = ", ".join(other_changers) if other_changers else "Reviewer"

            gh_review_state = item.get("user_review_state")
            has_gh_review = item.get("has_user_reviewed", False)

            is_auto_approved = (
                item.get("has_user_auto_approved", False)
                or (status_entry.get("review_outcome") or "").startswith("APPROVED")
                or (status_entry.get("review_state") == "APPROVED" and "CodeRabbit" in str(status_entry.get("review_body", "")))
                or ((gh_review_state or "").startswith("APPROVED") and item.get("has_user_auto_approved", False))
            )
            is_manual_approved = item.get("has_user_manually_approved", False)

            # Determine dynamic status badge
            if key in active_reviews:
                active = active_reviews[key]
                attempt = active.get("attempt", 1)
                phase = active.get("phase", "RUNNING_CODERABBIT")
                phase_labels = {
                    "PREPARING_REPOSITORY": "Preparing repository checkout",
                    "CHECKING_OUT_PR": "Checking out pull request",
                    "CHECKING_REVIEW_LIMITS": "Checking review limits",
                    "STARTING_REVIEW": "Starting CodeRabbit review",
                    "RUNNING_CODERABBIT": "CodeRabbit is analyzing this pull request",
                    "PREPARING_FINDINGS": "Preparing review findings",
                    "PUBLISHING_REVIEW": "Posting review to GitHub",
                }
                item["status_badge"] = "REVIEW_IN_PROGRESS"
                item["status_label"] = phase_labels.get(phase, "Review in progress")
                item["review_phase"] = phase
                item["review_message"] = active.get("message") or item["status_label"]
                item["status_description"] = item["review_message"]
                item["attempt"] = attempt
            elif status_entry.get("status") == "RATE_LIMITED":
                mins = max(1, remaining // 60) if is_rate_limited else 0
                item["status_badge"] = "RATE_LIMITED"
                item["status_label"] = f"Retrying in {mins}m" if is_rate_limited else "Retry queued"
                item["status_description"] = (
                    f"Shared CodeRabbit cooldown ({reason or 'request limit reached'}); retry in about {mins} minute(s). Use Force to retry early."
                    if is_rate_limited else "The cooldown has ended; the next scan will retry this pull request."
                )
                item["attempt"] = status_entry.get("attempt", 1)
                item["rate_limit_remaining_seconds"] = remaining
            elif status_entry.get("status") == "SKIPPED_MAX_FILES":
                item["status_badge"] = "SKIPPED_MAX_FILES"
                item["status_label"] = "Skipped: file limit exceeded"
                item["status_description"] = f"This PR changes {status_entry.get('file_count', 'too many')} files; the configured review limit is {status_entry.get('limit', config.get('max_files_limit', 100))}."
            elif status_entry.get("status") == "ERROR":
                item["status_badge"] = "REVIEW_FAILED"
                item["status_label"] = "Review failed"
                item["status_description"] = str(status_entry.get("error") or "The review could not be completed. Check Details & Logs for diagnostics.")
            elif status_entry.get("status") == "ALREADY_REVIEWED":
                item["status_badge"] = "ALREADY_REVIEWED"
                item["status_label"] = "Already reviewed"
                item["status_description"] = f"This commit already has an automated review ({status_entry.get('review_state') or 'review submitted'}). A new review will run when the PR head changes or you force a review."
            elif has_other_changes:
                item["status_badge"] = "OTHER_CHANGES_REQUESTED"
                item["status_label"] = f"Changes Requested by {changers_str}"
                item["status_description"] = f"{changers_str} requested changes on this pull request."
            elif is_auto_approved:
                item["status_badge"] = "APPROVED"
                item["status_label"] = "Auto Approved by You"
                item["status_description"] = "The automated review completed with no blocking findings and submitted an approval."
            elif is_manual_approved:
                item["status_badge"] = "MANUALLY_APPROVED"
                item["status_label"] = "Manually Approved by You"
                item["status_description"] = "You have approved this pull request manually."
            elif status_entry.get("review_outcome") == "NEEDS_WORK (Minor Issues Detected)" or gh_review_state == "NEEDS_WORK (Minor Issues Detected)":
                item["status_badge"] = "COMMENTS_POSTED"
                item["status_label"] = "Needs Work (Minor Issues)"
                item["status_description"] = "The review found minor issues and posted comments instead of approving."
            elif (
                status_entry.get("status") == "COMPLETED"
                or status_entry.get("review_state") == "CHANGES_REQUESTED"
                or gh_review_state in ("CHANGES_REQUESTED", "COMMENTED")
                or (has_gh_review and gh_review_state is not None)
            ):
                item["status_badge"] = "COMMENTS_POSTED"
                item["status_label"] = "Comments Posted"
                item["status_description"] = "The review completed and posted findings or comments to this pull request."
            elif key in queued_keys:
                item["status_badge"] = "QUEUED"
                item["status_label"] = "Queued for Review"
                item["status_description"] = "In the review queue. The review worker will process this pull request shortly."
            else:
                item["status_badge"] = "PENDING_REVIEW"
                item["status_label"] = "Pending Review"
                item["status_description"] = "Waiting for the next review scan to check eligibility and start an automated review."

            # Track bot review outcome
            bot_outcome = status_entry.get("review_outcome") or status_entry.get("review_state")
            if is_auto_approved:
                bot_outcome = "APPROVED"
            elif not bot_outcome and item.get("has_user_auto_approved"):
                bot_outcome = "APPROVED"
            item["bot_review_outcome"] = bot_outcome
            item["report_file"] = status_entry.get("report_file", "")
            annotated_prs.append(item)

        # Sort PRs: Main > Staging > Develop > others, then by creation date descending
        def get_bucket(p):
            b = (p.get("base_ref") or "").strip().lower()
            if b in ("main", "master"): return 0
            if b == "staging": return 1
            if b in ("develop", "dev"): return 2
            return 3

        def get_created_timestamp(p):
            created_str = p.get("created_at") or ""
            if not created_str:
                return 0.0
            try:
                return datetime.fromisoformat(created_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0

        annotated_prs.sort(key=lambda p: (
            get_bucket(p),
            -get_created_timestamp(p)
        ))

        return {
            "config": self.config_manager.get_settings(),
            "service_enabled": config.get("service_enabled", True),
            "strict_approval": config.get("strict_approval", True),
            "last_run": state.get("last_run_timestamp", ""),
            "rate_limited": is_rate_limited,
            "rate_limit_expires_at": state.get("rate_limit_expires_at", 0),
            "rate_limit_remaining_seconds": remaining,
            "rate_limit_reason": reason,
            "repositories": config.get("repositories", []),
            "pull_requests": annotated_prs,
            "authenticated_user": auth_user,
            "scan_in_progress": self.review_service.scan_in_progress,
            "scan_phase": self.review_service.scan_phase,
            "repository_status": self.repository_service.get_status(),
            "review_queue": self.review_service.get_review_queue(),
            "coderabbit_accounts": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "type": a.get("type", "api_key"),
                    "region": a.get("region", "us"),
                    "enabled": a.get("enabled", True),
                    "profile_dir": a.get("profile_dir", ""),
                    "api_key_masked": f"{a.get('api_key')[:4]}...{a.get('api_key')[-4:]}" if a.get("api_key") and len(a.get("api_key")) > 8 else ("configured" if a.get("api_key") else ""),
                    "rate_limited": self.state_manager.is_account_rate_limited(a.get("id", ""))[0],
                    "cooldown_remaining": self.state_manager.is_account_rate_limited(a.get("id", ""))[1],
                }
                for a in config.get("coderabbit_accounts", [])
            ],
            "account_rate_limits": self.state_manager.get_account_rate_limits(),
        }

    def get_pr_details(self, pr_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves aggregated details, status, and execution logs for a specific PR."""
        annotated_data = self.get_annotated_status()
        target_pr = next((p for p in annotated_data["pull_requests"] if p["pr_key"] == pr_key), None)
        
        pr_status = self.state_manager.get_pr_status(pr_key) or {}
        logs = self.state_manager.get_pr_logs(pr_key)

        if not target_pr and not pr_status and not logs:
            return None

        # Build fallback PR metadata if PR is closed or missing from cache
        if not target_pr:
            repo = pr_key.split("#")[0] if "#" in pr_key else ""
            num = int(pr_key.split("#")[1]) if "#" in pr_key and pr_key.split("#")[1].isdigit() else 0
            target_pr = {
                "pr_key": pr_key,
                "repo": repo,
                "number": num,
                "title": pr_status.get("title", f"PR #{num}"),
                "author": pr_status.get("author", "Unknown"),
                "is_own_pr": pr_status.get("is_own_pr", False),
                "base_ref": pr_status.get("base_ref", "main"),
                "head_ref": pr_status.get("head_ref", ""),
                "head_sha": pr_status.get("head_sha", ""),
                "html_url": pr_status.get("html_url", ""),
                "status_badge": pr_status.get("status", "UNKNOWN"),
                "status_label": pr_status.get("status", "Unknown"),
                "report_file": pr_status.get("report_file", "")
            }

        return {
            "pr": target_pr,
            "status": pr_status,
            "logs": logs,
            "total_logs": len(logs),
            "latest_log": logs[0] if logs else None
        }



# Global backend instance
backend: Optional[DashboardBackend] = None

class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler implementing REST API and UI serving."""

    def _set_headers(self, status: int = 200, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(204)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            # Serve dashboard.html
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
            try:
                with open(html_path, "rb") as f:
                    content = f.read()
                self._set_headers(200, "text/html; charset=utf-8")
                self.wfile.write(content)
            except Exception as e:
                self._set_headers(500, "text/plain")
                self.wfile.write(f"Error loading dashboard: {e}".encode("utf-8"))
            return

        if path in ("/pr-details", "/pr-details.html"):
            # Serve pr_details.html
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pr_details.html")
            try:
                with open(html_path, "rb") as f:
                    content = f.read()
                self._set_headers(200, "text/html; charset=utf-8")
                self.wfile.write(content)
            except Exception as e:
                self._set_headers(500, "text/plain")
                self.wfile.write(f"Error loading PR details page: {e}".encode("utf-8"))
            return


        if path in ("/favicon.ico", "/assets/favicon.ico"):
            ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "favicon.ico")
            if os.path.exists(ico_path):
                with open(ico_path, "rb") as f:
                    content = f.read()
                self._set_headers(200, "image/x-icon")
                self.wfile.write(content)
                return

        if path in ("/favicon.png", "/assets/icon.png"):
            png_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
            if os.path.exists(png_path):
                with open(png_path, "rb") as f:
                    content = f.read()
                self._set_headers(200, "image/png")
                self.wfile.write(content)
                return

        if path.startswith("/assets/"):
            filename = os.path.basename(path)
            asset_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", filename)
            if os.path.exists(asset_path):
                mime = "image/png" if filename.endswith(".png") else "image/x-icon"
                with open(asset_path, "rb") as f:
                    content = f.read()
                self._set_headers(200, mime)
                self.wfile.write(content)
                return

        if path == "/api/status":
            query_params = parse_qs(parsed.query)
            if query_params.get("force_refresh", [""])[0].lower() in ("true", "1", "yes"):
                backend.refresh_pr_cache()
            data = backend.get_annotated_status()
            self._set_headers(200)
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        if path in ("/api/pr/details", "/api/pr/logs"):
            query_params = parse_qs(parsed.query)
            pr_key = query_params.get("pr_key", [""])[0].strip()
            if not pr_key:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Missing 'pr_key' query parameter"}).encode("utf-8"))
                return
            details = backend.get_pr_details(pr_key)
            if details is None:
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": f"Pull request '{pr_key}' not found"}).encode("utf-8"))
                return
            self._set_headers(200)
            self.wfile.write(json.dumps(details).encode("utf-8"))
            return

        if path == "/api/config":
            cfg_data = backend.config_manager.get_settings()
            self._set_headers(200)
            self.wfile.write(json.dumps(cfg_data).encode("utf-8"))
            return

        if path == "/api/coderabbit-auth":
            self._set_headers(200)
            self.wfile.write(json.dumps(backend.get_coderabbit_auth_status()).encode("utf-8"))
            return

        if path == "/api/coderabbit-accounts":
            accounts = backend.config_manager.get_coderabbit_accounts()
            safe_accounts = []
            for a in accounts:
                is_rl, rem, reason = backend.state_manager.is_account_rate_limited(a.get("id", ""))
                safe_accounts.append({
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "type": a.get("type", "api_key"),
                    "region": a.get("region", "us"),
                    "enabled": a.get("enabled", True),
                    "profile_dir": a.get("profile_dir", ""),
                    "api_key_masked": f"{a.get('api_key')[:4]}...{a.get('api_key')[-4:]}" if a.get("api_key") and len(a.get("api_key")) > 8 else ("configured" if a.get("api_key") else ""),
                    "has_key": bool(a.get("api_key")),
                    "rate_limited": is_rl,
                    "cooldown_remaining": rem,
                    "rate_limit_reason": reason,
                })
            self._set_headers(200)
            self.wfile.write(json.dumps(safe_accounts).encode("utf-8"))
            return


        if path == "/api/repos":
            repos = backend.repository_service.get_repositories()
            self._set_headers(200)
            self.wfile.write(json.dumps(repos).encode("utf-8"))
            return

        if path.startswith("/reports/"):
            filename = os.path.basename(path)
            report_path = os.path.join(DEFAULT_REVIEWS_DIR, filename)
            if os.path.exists(report_path):
                with open(report_path, "rb") as f:
                    content = f.read()
                self._set_headers(200, "text/html; charset=utf-8")
                self.wfile.write(content)
                return
            else:
                self._set_headers(404, "text/plain")
                self.wfile.write(b"Report not found")
                return

        self._set_headers(404, "text/plain")
        self.wfile.write(b"Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if path == "/api/trigger":
            force = data.get("force", False)
            pr_key = data.get("pr_key")
            accepted = backend.run_review_scan(force=force, pr_key=pr_key)
            self._set_headers(200 if accepted else 409)
            response_status = "queued" if accepted and pr_key else "triggered" if accepted else "already_queued_or_busy"
            self.wfile.write(json.dumps({"status": response_status, "force": force, "pr_key": pr_key}).encode("utf-8"))
            return

        if path == "/api/clear-rate-limit":
            backend.review_service.clear_rate_limit()
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "cleared"}).encode("utf-8"))
            return

        if path == "/api/queue/move-to-top":
            pr_key = data.get("pr_key")
            if not pr_key:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "pr_key is required"}).encode("utf-8"))
                return
            success = backend.review_service.move_queue_item_to_top(pr_key)
            self._set_headers(200 if success else 404)
            self.wfile.write(json.dumps({"success": success, "pr_key": pr_key}).encode("utf-8"))
            return

        if path == "/api/config" or path == "/api/settings":
            updated = backend.config_manager.update_settings(data)
            backend.github_client.set_token(backend.config_manager.get_github_token())
            self._set_headers(200)
            self.wfile.write(json.dumps(updated).encode("utf-8"))
            return

        if path == "/api/service/toggle":
            enabled = data.get("enabled", True)
            new_state = backend.config_manager.set_service_enabled(enabled)
            self._set_headers(200)
            self.wfile.write(json.dumps({"service_enabled": new_state}).encode("utf-8"))
            return

        if path == "/api/strict-approval/toggle":
            current = backend.config_manager.is_strict_approval()
            new_state = backend.config_manager.set_strict_approval(not current)
            self._set_headers(200)
            self.wfile.write(json.dumps({"strict_approval": new_state}).encode("utf-8"))
            return

        if path == "/api/strict-approval":
            enabled = data.get("strict", data.get("enabled", True))
            new_state = backend.config_manager.set_strict_approval(enabled)
            self._set_headers(200)
            self.wfile.write(json.dumps({"strict_approval": new_state}).encode("utf-8"))
            return

        if path == "/api/repos/toggle":
            full_name = data.get("full_name")
            if not full_name:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Missing full_name"}).encode("utf-8"))
                return
            updated = backend.repository_service.toggle_repository(full_name, data.get("enabled"))
            self._set_headers(200)
            self.wfile.write(json.dumps(updated or {}).encode("utf-8"))
            return

        if path == "/api/repos/add":
            full_name = data.get("full_name", "").strip()
            if not full_name or "/" not in full_name:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Repository must be in 'owner/repo' format"}).encode("utf-8"))
                return

            # Instant GitHub API validation
            if not backend.repository_service.validate_repository(full_name):
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": f"Repository '{full_name}' not found or inaccessible with the configured GitHub token"}).encode("utf-8"))
                return

            repo_entry = backend.repository_service.add_repository(full_name)
            # Trigger immediate PR cache refresh
            backend.refresh_pr_cache()
            self._set_headers(200)
            self.wfile.write(json.dumps(repo_entry).encode("utf-8"))
            return

        if path == "/api/repos/remove":
            full_name = data.get("full_name", "").strip()
            removed = backend.repository_service.remove_repository(full_name)
            backend.refresh_pr_cache()
            self._set_headers(200)
            self.wfile.write(json.dumps({"removed": removed}).encode("utf-8"))
            return

        if path == "/api/coderabbit-accounts/add":
            account = backend.config_manager.add_coderabbit_account(data)
            self._set_headers(200)
            self.wfile.write(json.dumps(account).encode("utf-8"))
            return

        if path == "/api/coderabbit-accounts/update":
            acc_id = data.get("id")
            if not acc_id:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "id is required"}).encode("utf-8"))
                return
            updated = backend.config_manager.update_coderabbit_account(acc_id, data)
            if not updated:
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": "Account not found"}).encode("utf-8"))
                return
            self._set_headers(200)
            self.wfile.write(json.dumps(updated).encode("utf-8"))
            return

        if path == "/api/coderabbit-accounts/remove":
            acc_id = data.get("id")
            if not acc_id:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "id is required"}).encode("utf-8"))
                return
            removed = backend.config_manager.remove_coderabbit_account(acc_id)
            backend.state_manager.clear_rate_limit(acc_id)
            self._set_headers(200)
            self.wfile.write(json.dumps({"removed": removed, "id": acc_id}).encode("utf-8"))
            return

        if path == "/api/coderabbit-accounts/test":
            acc_id = data.get("id")
            target_account = None
            if acc_id:
                target_account = next((a for a in backend.config_manager.get_coderabbit_accounts() if a.get("id") == acc_id), None)
            else:
                target_account = data

            if not target_account:
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": "Account not found"}).encode("utf-8"))
                return

            result = backend.test_coderabbit_account_auth(target_account)
            self._set_headers(200)
            self.wfile.write(json.dumps(result).encode("utf-8"))
            return

        if path == "/api/coderabbit-accounts/clear-cooldown":
            acc_id = data.get("id")
            backend.state_manager.clear_rate_limit(acc_id)
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "cooldown_cleared", "id": acc_id}).encode("utf-8"))
            return

        self._set_headers(404, "text/plain")
        self.wfile.write(b"Not Found")


def run_server(host: str = HOST, port: int = PORT):
    global backend
    backend = DashboardBackend()
    server_address = (host, port)
    httpd = ThreadedHTTPServer(server_address, DashboardRequestHandler)
    logger.info("CodeRabbit Auto-Review Dashboard running on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        httpd.shutdown()

if __name__ == "__main__":
    run_server()
