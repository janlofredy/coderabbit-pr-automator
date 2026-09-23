import os
import json
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs

from config_manager import ConfigManager, safe_int_env
from state_manager import StateManager
from github_client import GitHubClient
from auto_review_prs import AutoReviewEngine, DEFAULT_REVIEWS_DIR

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
        self.github_client = github_client or GitHubClient()
        self.review_engine = review_engine or AutoReviewEngine(
            config_manager=self.config_manager,
            state_manager=self.state_manager,
            github_client=self.github_client
        )

        self._cached_prs: List[Dict[str, Any]] = []
        self._cache_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=5)
        self._is_running = True
        self._scan_in_progress = False

        # Start continuous background worker
        self._worker_thread = None
        if auto_start_worker:
            self._worker_thread = threading.Thread(target=self._background_loop, daemon=True)
            self._worker_thread.start()

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
                    "updated_at": pr.get("updated_at", "")
                })
            return results
        except Exception as e:
            logger.warning("Error fetching PRs for %s: %s", full_name, e)
            return []

    def refresh_pr_cache(self) -> None:
        """Fetches PRs across all enabled repositories concurrently and caches them."""
        enabled_repos = self.config_manager.get_enabled_repos()
        futures = [self._executor.submit(self.fetch_repo_prs, r) for r in enabled_repos]
        collected = []
        for f in futures:
            try:
                collected.extend(f.result())
            except Exception as e:
                logger.error("Exception during repo PR fetch: %s", e)

        # Sort by updated_at desc
        collected.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        with self._cache_lock:
            self._cached_prs = collected
        logger.info("PR cache refreshed: %d open PR(s) found.", len(collected))

    def _background_loop(self) -> None:
        """Background daemon polling repositories and auto-reviewing."""
        logger.info("Background review daemon started.")
        # Initial refresh
        self.refresh_pr_cache()

        while self._is_running:
            config = self.config_manager.load_config()
            interval = config.get("poll_interval_seconds", 900)

            if config.get("service_enabled", True):
                self.run_review_scan(force=False)

            # Sleep in short increments to allow rapid reaction to triggers
            for _ in range(max(1, interval // 5)):
                if not self._is_running:
                    break
                time.sleep(5)

    def run_review_scan(self, force: bool = False, pr_key: Optional[str] = None) -> None:
        """Runs a review pass over all PRs or a specific PR."""
        if self._scan_in_progress:
            logger.info("Scan already in progress. Skipping.")
            return

        def task():
            self._scan_in_progress = True
            try:
                self.refresh_pr_cache()
                if pr_key:
                    with self._cache_lock:
                        target = next((p for p in self._cached_prs if p["pr_key"] == pr_key), None)
                    if target:
                        repo_info = next((r for r in self.config_manager.get_repos() if r["full_name"] == target["repo"]), None)
                        if repo_info:
                            # Fetch full PR object
                            owner, repo_name = GitHubClient.split_repo(target["repo"])
                            full_pr = self.github_client.get_pr(owner, repo_name, target["number"])
                            self.review_engine.review_single_pr(repo_info, full_pr, force=force)
                else:
                    self.review_engine.scan_and_review_all(force=force)
            except Exception as e:
                logger.error("Error in review scan task: %s", e)
            finally:
                self._scan_in_progress = False
                self.refresh_pr_cache()

        self._executor.submit(task)

    def get_annotated_status(self) -> Dict[str, Any]:
        """Overlays real-time state manager data onto cached PR records for sub-millisecond response."""
        state = self.state_manager.load_state()
        config = self.config_manager.load_config()

        is_rate_limited, remaining, reason = self.state_manager.is_rate_limit_active()
        active_reviews = state.get("active_reviews", {})
        pr_statuses = state.get("pr_statuses", {})
        auth_user = self.github_client.get_username()

        annotated_prs = []
        with self._cache_lock:
            cached_list = list(self._cached_prs)

        for pr in cached_list:
            item = dict(pr)
            key = item["pr_key"]
            status_entry = pr_statuses.get(key, {})

            # Determine dynamic status badge
            if key in active_reviews:
                attempt = active_reviews[key].get("attempt", 1)
                item["status_badge"] = "REVIEW_IN_PROGRESS"
                item["status_label"] = f"Review in Progress (Attempt {attempt}/3)"
                item["attempt"] = attempt
            elif is_rate_limited and status_entry.get("status") == "RATE_LIMITED":
                mins = max(1, remaining // 60)
                item["status_badge"] = "RATE_LIMITED"
                item["status_label"] = f"Retrying in {mins}m"
                item["attempt"] = status_entry.get("attempt", 1)
            elif status_entry.get("status") == "SKIPPED_MAX_FILES":
                item["status_badge"] = "SKIPPED_MAX_FILES"
            elif status_entry.get("review_outcome") == "NEEDS_WORK (Minor Issues Detected)":
                item["status_badge"] = "COMMENTS_POSTED"
                item["status_label"] = "Needs Work (Minor Issues)"
            elif status_entry.get("review_outcome") == "APPROVED" or status_entry.get("review_state") == "APPROVED":
                item["status_badge"] = "APPROVED"
                item["status_label"] = "Approved by You"
            elif status_entry.get("status") == "COMPLETED" or status_entry.get("review_state") == "CHANGES_REQUESTED":
                item["status_badge"] = "COMMENTS_POSTED"
                item["status_label"] = "Comments Posted"
            elif item["is_own_pr"]:
                item["status_badge"] = "OWN_PR"
                item["status_label"] = "Your PR (Author)"
            else:
                item["status_badge"] = "PENDING_REVIEW"
                item["status_label"] = "Pending Review"

            item["report_file"] = status_entry.get("report_file", "")
            annotated_prs.append(item)

        return {
            "service_enabled": config.get("service_enabled", True),
            "strict_approval": config.get("strict_approval", True),
            "last_run": state.get("last_run_timestamp", ""),
            "rate_limited": is_rate_limited,
            "rate_limit_expires_at": state.get("rate_limit_expires_at", 0),
            "rate_limit_remaining_seconds": remaining,
            "rate_limit_reason": reason,
            "repositories": config.get("repositories", []),
            "pull_requests": annotated_prs,
            "authenticated_user": auth_user
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

        if path == "/api/status":
            data = backend.get_annotated_status()
            self._set_headers(200)
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        if path == "/api/repos":
            repos = backend.config_manager.get_repos()
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
            backend.run_review_scan(force=force, pr_key=pr_key)
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "triggered", "force": force, "pr_key": pr_key}).encode("utf-8"))
            return

        if path == "/api/clear-rate-limit":
            backend.state_manager.clear_rate_limit()
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "cleared"}).encode("utf-8"))
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
            updated = backend.config_manager.toggle_repo(full_name, data.get("enabled"))
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
            if not backend.github_client.validate_repo(full_name):
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": f"Repository '{full_name}' not found or inaccessible with current GITHUB_TOKEN"}).encode("utf-8"))
                return

            repo_entry = backend.config_manager.add_repo(full_name)
            # Trigger immediate PR cache refresh
            backend.refresh_pr_cache()
            self._set_headers(200)
            self.wfile.write(json.dumps(repo_entry).encode("utf-8"))
            return

        if path == "/api/repos/remove":
            full_name = data.get("full_name", "").strip()
            removed = backend.config_manager.remove_repo(full_name)
            backend.refresh_pr_cache()
            self._set_headers(200)
            self.wfile.write(json.dumps({"removed": removed}).encode("utf-8"))
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
