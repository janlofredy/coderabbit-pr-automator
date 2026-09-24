import os
import shutil
import tempfile
import unittest
import json
from unittest.mock import MagicMock

from config_manager import ConfigManager
from state_manager import StateManager
from github_client import GitHubClient
from web_dashboard import DashboardBackend

class TestDashboardBackend(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.state_path = os.path.join(self.test_dir, "automation_state.json")
        self.repos_dir = os.path.join(self.test_dir, "repos")

        self.cfg_mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        self.cfg_mgr.save_config({
            "repositories": [
                {"full_name": "owner/repo1", "enabled": True, "path": os.path.join(self.repos_dir, "owner/repo1")},
                {"full_name": "owner/repo2", "enabled": False, "path": os.path.join(self.repos_dir, "owner/repo2")}
            ],
            "poll_interval_seconds": 900,
            "max_files_limit": 100,
            "auto_approve": True,
            "service_enabled": True
        })

        self.state_mgr = StateManager(state_path=self.state_path)
        self.gh_client = MagicMock(spec=GitHubClient)
        self.gh_client.get_username.return_value = "my-test-bot"

        # Mock list_open_prs
        self.gh_client.list_open_prs.return_value = [
            {
                "number": 101,
                "title": "Add awesome feature",
                "user": {"login": "contributor_jane"},
                "base": {"ref": "main"},
                "head": {"ref": "feature-awesome", "sha": "abcdef1234567890"},
                "html_url": "https://github.com/owner/repo1/pull/101",
                "created_at": "2026-09-23T10:00:00Z",
                "updated_at": "2026-09-23T10:05:00Z"
            }
        ]
        self.gh_client.has_user_reviewed_sha.return_value = (False, None)
        self.gh_client.get_pr_review_summary.return_value = {
            "has_other_changes_requested": False,
            "other_changes_requested_by": [],
            "other_approved_by": [],
            "has_user_reviewed": False,
            "user_review_state": None
        }

        self.backend = DashboardBackend(
            config_manager=self.cfg_mgr,
            state_manager=self.state_mgr,
            github_client=self.gh_client,
            auto_start_worker=False
        )

    def tearDown(self):
        self.backend._is_running = False
        self.backend._executor.shutdown(wait=False)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_annotated_status_overlay(self):
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()

        self.assertTrue(status["service_enabled"])
        self.assertEqual(len(status["repositories"]), 2)
        self.assertEqual(len(status["pull_requests"]), 1)

        pr = status["pull_requests"][0]
        self.assertEqual(pr["pr_key"], "owner/repo1#101")
        self.assertEqual(pr["author"], "contributor_jane")
        self.assertFalse(pr["is_own_pr"])
        self.assertEqual(pr["status_badge"], "PENDING_REVIEW")

    def test_status_badge_with_active_rate_limit(self):
        self.backend.refresh_pr_cache()
        pr_key = "owner/repo1#101"

        # Record a rate limit
        self.state_mgr.record_rate_limit(600, reason="Quota limit reached", pr_key=pr_key)
        self.state_mgr.record_pr_status(pr_key, {"status": "RATE_LIMITED", "attempt": 1})

        status = self.backend.get_annotated_status()
        self.assertTrue(status["rate_limited"])
        self.assertGreater(status["rate_limit_remaining_seconds"], 500)

        pr = status["pull_requests"][0]
        self.assertEqual(pr["status_badge"], "RATE_LIMITED")
        self.assertIn("Retrying in", pr["status_label"])

    def test_status_badge_approved_by_you(self):
        self.backend.refresh_pr_cache()
        pr_key = "owner/repo1#101"

        self.state_mgr.record_pr_status(pr_key, {
            "status": "COMPLETED",
            "review_outcome": "APPROVED",
            "report_file": "owner_repo1_pr101_abcdef.html"
        })

        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertEqual(pr["status_badge"], "APPROVED")
        self.assertEqual(pr["status_label"], "Auto Approved by You")
        self.assertEqual(pr["report_file"], "owner_repo1_pr101_abcdef.html")

    def test_status_badge_manually_approved(self):
        self.gh_client.get_pr_review_summary.return_value = {
            "has_other_changes_requested": False,
            "other_changes_requested_by": [],
            "other_approved_by": [],
            "other_commented_by": [],
            "has_other_commented": False,
            "has_user_auto_approved": False,
            "has_user_manually_approved": True,
            "has_check_error": False,
            "failed_checks": [],
            "has_user_reviewed": False,
            "user_review_state": "APPROVED"
        }
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertEqual(pr["status_badge"], "MANUALLY_APPROVED")
        self.assertEqual(pr["status_label"], "Manually Approved by You")
        self.assertTrue(pr["has_user_manually_approved"])

    def test_status_badge_own_pr(self):
        # Configure PR where author is the bot
        self.gh_client.list_open_prs.return_value = [
            {
                "number": 102,
                "title": "Bot maintenance PR",
                "user": {"login": "my-test-bot"}, # Matches authenticated user
                "base": {"ref": "main"},
                "head": {"ref": "maint", "sha": "99998888"},
                "html_url": "https://github.com/owner/repo1/pull/102",
                "created_at": "2026-09-23T11:00:00Z",
                "updated_at": "2026-09-23T11:05:00Z"
            }
        ]
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertTrue(pr["is_own_pr"])
        self.assertEqual(pr["status_badge"], "OWN_PR")
        self.assertEqual(pr["status_label"], "Your PR (Author)")

    def test_status_badge_other_changes_requested(self):
        self.gh_client.get_pr_review_summary.return_value = {
            "has_other_changes_requested": True,
            "other_changes_requested_by": ["alice", "bob"],
            "other_approved_by": [],
            "has_user_reviewed": False,
            "user_review_state": None
        }
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertEqual(pr["status_badge"], "OTHER_CHANGES_REQUESTED")
        self.assertEqual(pr["status_label"], "Changes Requested by alice, bob")
        self.assertTrue(pr["has_other_changes_requested"])
        self.assertEqual(pr["other_changes_requested_by"], ["alice", "bob"])

    def test_http_endpoints(self):
        import web_dashboard
        import urllib.request
        from http.server import HTTPServer

        web_dashboard.backend = self.backend
        server = HTTPServer(("127.0.0.1", 0), web_dashboard.DashboardRequestHandler)
        port = server.server_port

        import threading
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        base_url = f"http://127.0.0.1:{port}"

        # 1. Test GET /
        with urllib.request.urlopen(f"{base_url}/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"CodeRabbit PR Auto-Reviewer", resp.read())

        # 2. Test GET /api/status
        with urllib.request.urlopen(f"{base_url}/api/status") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertIn("pull_requests", data)
            self.assertIn("rate_limited", data)

        # 3. Test GET /api/repos
        with urllib.request.urlopen(f"{base_url}/api/repos") as resp:
            self.assertEqual(resp.status, 200)
            repos = json.loads(resp.read().decode())
            self.assertEqual(len(repos), 2)

        # 4. Test POST /api/clear-rate-limit
        req = urllib.request.Request(f"{base_url}/api/clear-rate-limit", data=b"{}", method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode())
            self.assertEqual(res["status"], "cleared")

        # 5. Test POST /api/strict-approval/toggle
        req = urllib.request.Request(f"{base_url}/api/strict-approval/toggle", data=b"{}", method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode())
            self.assertIn("strict_approval", res)

        # 6. Test GET /api/config
        with urllib.request.urlopen(f"{base_url}/api/config") as resp:
            self.assertEqual(resp.status, 200)
            cfg_res = json.loads(resp.read().decode())
            self.assertIn("poll_interval_seconds", cfg_res)
            self.assertIn("max_files_limit", cfg_res)
            self.assertIn("auto_approve", cfg_res)
            self.assertIn("strict_approval", cfg_res)

        # 7. Test POST /api/config
        update_data = json.dumps({"poll_interval_seconds": 600, "max_files_limit": 80}).encode()
        req = urllib.request.Request(f"{base_url}/api/config", data=update_data, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            cfg_updated = json.loads(resp.read().decode())
            self.assertEqual(cfg_updated["poll_interval_seconds"], 600)
            self.assertEqual(cfg_updated["max_files_limit"], 80)

        server.shutdown()
        server.server_close()

    def test_status_badge_needs_work_minor_issues(self):
        self.backend.refresh_pr_cache()
        pr_key = "owner/repo1#101"

        self.state_mgr.record_pr_status(pr_key, {
            "status": "COMPLETED",
            "review_outcome": "NEEDS_WORK (Minor Issues Detected)",
            "minor_count": 2,
            "report_file": "report.html"
        })

        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertEqual(pr["status_badge"], "COMMENTS_POSTED")
        self.assertEqual(pr["status_label"], "Needs Work (Minor Issues)")

    def test_pr_sorting_base_branch_and_creation(self):
        self.gh_client.list_open_prs.return_value = [
            {"number": 1, "title": "Old Dev PR", "base": {"ref": "develop"}, "head": {"ref": "f1", "sha": "111"}, "created_at": "2026-09-20T10:00:00Z"},
            {"number": 2, "title": "New Dev PR", "base": {"ref": "develop"}, "head": {"ref": "f2", "sha": "222"}, "created_at": "2026-09-22T10:00:00Z"},
            {"number": 3, "title": "Staging PR", "base": {"ref": "staging"}, "head": {"ref": "f3", "sha": "333"}, "created_at": "2026-09-21T10:00:00Z"},
            {"number": 4, "title": "Main PR", "base": {"ref": "main"}, "head": {"ref": "f4", "sha": "444"}, "created_at": "2026-09-21T08:00:00Z"},
            {"number": 5, "title": "Feature base PR", "base": {"ref": "custom-feature"}, "head": {"ref": "f5", "sha": "555"}, "created_at": "2026-09-23T10:00:00Z"},
        ]
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()
        pr_nums = [p["number"] for p in status["pull_requests"]]
        # Expected: Main (4) -> Staging (3) -> New Dev (2) -> Old Dev (1) -> Custom (5)
        self.assertEqual(pr_nums, [4, 3, 2, 1, 5])

if __name__ == "__main__":
    unittest.main()


