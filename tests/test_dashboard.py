import os
import shutil
import tempfile
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
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
        self.gh_client.token = "test-token"
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
        self.gh_client.get_pr.return_value = {
            "number": 101,
            "title": "Add awesome feature",
            "state": "open",
            "user": {"login": "contributor_jane"},
            "base": {"ref": "main"},
            "head": {"ref": "feature-awesome", "sha": "abcdef1234567890"},
            "html_url": "https://github.com/owner/repo1/pull/101",
        }
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
        self.backend._executor.shutdown(wait=True)
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
        self.assertEqual(pr["bot_review_outcome"], "APPROVED")
        self.assertEqual(pr["report_file"], "owner_repo1_pr101_abcdef.html")

    def test_bot_review_outcome_tracking(self):
        self.backend.refresh_pr_cache()
        pr_key = "owner/repo1#101"

        self.state_mgr.record_pr_status(pr_key, {
            "status": "COMPLETED",
            "review_outcome": "CHANGES_REQUESTED"
        })

        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertEqual(pr["bot_review_outcome"], "CHANGES_REQUESTED")


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

        # 8. Test GET /pr-details
        with urllib.request.urlopen(f"{base_url}/pr-details?pr_key=owner/repo1%23101") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"Pull Request Details & Logs", resp.read())

        # 9. Test GET /api/pr/details
        # First record a sample review log
        self.state_mgr.record_pr_log("owner/repo1#101", {
            "status": "COMPLETED",
            "review_outcome": "APPROVED",
            "retcode": 0,
            "stdout": "CodeRabbit finished review. 0 issues.",
            "stderr": "",
            "elapsed_seconds": 12.5,
            "findings": []
        })
        with urllib.request.urlopen(f"{base_url}/api/pr/details?pr_key=owner/repo1%23101") as resp:
            self.assertEqual(resp.status, 200)
            pr_data = json.loads(resp.read().decode())
            self.assertIn("pr", pr_data)
            self.assertIn("logs", pr_data)
            self.assertEqual(len(pr_data["logs"]), 1)
            self.assertEqual(pr_data["logs"][0]["stdout"], "CodeRabbit finished review. 0 issues.")

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

    def test_pr_merge_conflict(self):
        self.gh_client.get_pr_review_summary.return_value = {
            "has_other_changes_requested": False,
            "other_changes_requested_by": [],
            "other_approved_by": [],
            "other_commented_by": [],
            "has_other_commented": False,
            "has_user_auto_approved": False,
            "has_user_manually_approved": False,
            "has_check_error": False,
            "failed_checks": [],
            "has_conflict": True,
            "mergeable_state": "dirty",
            "has_user_reviewed": False,
            "user_review_state": None
        }
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertTrue(pr["has_conflict"])
        self.assertEqual(pr["mergeable_state"], "dirty")

    def test_queue_only_takes_when_no_rate_limit_or_forced(self):
        review_service = self.backend.review_service
        # Simulate active rate limit
        self.state_mgr.record_rate_limit(600, reason="Quota reached")

        # Enqueue non-forced PR
        review_service.enqueue_pr("owner/repo1#101", force=False)
        review_service._start_queue_worker_locked()

        queue_state = review_service.get_review_queue()
        # Should stay in pending queue and not be actively reviewing because of rate limit
        self.assertIsNone(queue_state["active"])
        self.assertEqual(len(queue_state["pending"]), 1)
        self.assertEqual(queue_state["pending"][0]["pr_key"], "owner/repo1#101")

        # Now enqueue or force a PR
        with unittest.mock.patch.object(self.backend.review_engine, "review_single_pr") as mock_review:
            mock_review.return_value = {"status": "COMPLETED"}
            review_service.enqueue_pr("owner/repo1#101", force=True)
            # Ensure background thread has finished or drain directly
            self.backend._executor.shutdown(wait=True)
            self.backend._executor = ThreadPoolExecutor(max_workers=5)
            self.backend.review_service._executor = self.backend._executor
            mock_review.assert_called_once()

    def test_rate_limited_pr_put_back_to_first_of_queue(self):
        review_service = self.backend.review_service
        # Add another PR to queue first
        review_service._review_queue.append({"pr_key": "owner/repo1#102", "force": False, "queued_at": "2026-09-23T10:00:00Z"})

        # Prepend item
        review_service._review_queue.appendleft({"pr_key": "owner/repo1#101", "force": False, "queued_at": "2026-09-23T10:01:00Z"})

        with unittest.mock.patch.object(self.backend.review_engine, "review_single_pr") as mock_review:
            # First PR gets rate limited when reviewed
            def side_effect(repo_info, pr, force=False):
                self.state_mgr.record_rate_limit(600, reason="Hit rate limit")
                return {"status": "RATE_LIMITED", "pr_key": "owner/repo1#101"}

            mock_review.side_effect = side_effect
            review_service._drain_review_queue()

        queue_state = review_service.get_review_queue()
        # Item owner/repo1#101 should be placed back at first (index 0) of the queue
        self.assertEqual(queue_state["pending"][0]["pr_key"], "owner/repo1#101")
        self.assertEqual(queue_state["pending"][1]["pr_key"], "owner/repo1#102")

    def test_scan_and_enqueue_pending(self):
        review_service = self.backend.review_service
        repo_service = self.backend.repository_service
        # Pause queue worker so item remains in pending queue to inspect
        with unittest.mock.patch.object(self.backend.review_engine, "review_single_pr"):
            with unittest.mock.patch.object(review_service, "_start_queue_worker_locked"):
                enqueued = repo_service.discover_and_enqueue_pending(self.state_mgr, force=False)
                self.assertEqual(enqueued, 1)
                review_service._init_queue_from_state()
                queue_state = review_service.get_review_queue()
                self.assertTrue(any(item["pr_key"] == "owner/repo1#101" for item in queue_state["pending"]))


    def test_move_queue_item_to_top(self):
        review_service = self.backend.review_service
        # Add 3 items to queue
        review_service._review_queue.append({"pr_key": "owner/repo1#101", "force": False, "queued_at": "2026-09-23T10:00:00Z"})
        review_service._review_queue.append({"pr_key": "owner/repo1#102", "force": False, "queued_at": "2026-09-23T10:01:00Z"})
        review_service._review_queue.append({"pr_key": "owner/repo1#103", "force": False, "queued_at": "2026-09-23T10:02:00Z"})

        with unittest.mock.patch.object(review_service, "_start_queue_worker_locked"):
            # Move last item to top
            success = review_service.move_queue_item_to_top("owner/repo1#103")
            self.assertTrue(success)

            queue = review_service.get_review_queue()
            self.assertEqual(queue["pending"][0]["pr_key"], "owner/repo1#103")
            self.assertEqual(queue["pending"][1]["pr_key"], "owner/repo1#101")
            self.assertEqual(queue["pending"][2]["pr_key"], "owner/repo1#102")

            # Moving non-existent item returns False
            self.assertFalse(review_service.move_queue_item_to_top("nonexistent#999"))

            # Moving already top item returns True
            self.assertTrue(review_service.move_queue_item_to_top("owner/repo1#103"))


    def test_coderabbit_accounts_status_and_testing(self):
        # Configure test accounts
        self.cfg_mgr.add_coderabbit_account({
            "name": "Account API",
            "type": "api_key",
            "api_key": "cr-testkey123",
            "region": "us"
        })

        status = self.backend.get_annotated_status()
        self.assertIn("coderabbit_accounts", status)
        self.assertEqual(len(status["coderabbit_accounts"]), 1)
        acc = status["coderabbit_accounts"][0]
        self.assertEqual(acc["name"], "Account API")
        self.assertIn("cr-t...y123", acc["api_key_masked"])

        # Test auth tester method
        with unittest.mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = unittest.mock.MagicMock(
                returncode=0,
                stdout='{"type":"status","phase":"auth","status":"authenticated","authenticated":true}'
            )
            res = self.backend.test_coderabbit_account_auth(acc)
            self.assertTrue(res["authenticated"])
            self.assertEqual(res["status"], "authenticated")

    def test_decoupled_queue_review_execution(self):
        """Verify that ReviewManagementService drains the queue using only the queue job without repo service cache."""
        review_service = self.backend.review_service
        # Enqueue a self-contained PR job
        job = {
            "pr_key": "owner/repo1#101",
            "repo": "owner/repo1",
            "number": 101,
            "head_sha": "abcdef1234567890",
            "force": True,
            "queued_at": "2026-09-30T10:00:00Z"
        }
        self.state_mgr.enqueue_review_job(job)
        review_service._init_queue_from_state()

        # Clear repo cache completely to ensure consumer does not rely on it
        self.backend.repository_service._cached_prs = []

        with unittest.mock.patch.object(self.backend.review_engine, "review_single_pr") as mock_review:
            mock_review.return_value = {"status": "COMPLETED"}
            review_service._drain_review_queue()
            mock_review.assert_called_once()
            # Verify passed repo_info and full_pr
            call_repo_info, call_pr = mock_review.call_args[0][0], mock_review.call_args[0][1]
            self.assertEqual(call_repo_info["full_name"], "owner/repo1")
            self.assertEqual(call_pr["number"], 101)

    def test_status_badge_github_commented_review(self):
        """Verify that when GitHub has a COMMENT review by the bot, the badge reflects COMMENTS_POSTED instead of PENDING_REVIEW."""
        self.gh_client.get_pr_review_summary.return_value = {
            "has_other_changes_requested": False,
            "other_changes_requested_by": [],
            "other_approved_by": [],
            "other_commented_by": [],
            "has_other_commented": False,
            "has_user_auto_approved": False,
            "has_user_manually_approved": False,
            "has_check_error": False,
            "failed_checks": [],
            "has_user_reviewed": True,
            "user_review_state": "COMMENTED"
        }
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertEqual(pr["status_badge"], "COMMENTS_POSTED")
        self.assertEqual(pr["status_label"], "Comments Posted")

    def test_status_badge_queued(self):
        """Verify that when a PR is in the review queue, its badge shows QUEUED instead of PENDING_REVIEW."""
        self.state_mgr.enqueue_review_job({"pr_key": "owner/repo1#101", "force": False})
        self.backend.refresh_pr_cache()
        status = self.backend.get_annotated_status()
        pr = status["pull_requests"][0]
        self.assertEqual(pr["status_badge"], "QUEUED")
        self.assertEqual(pr["status_label"], "Queued for Review")

if __name__ == "__main__":
    unittest.main()



