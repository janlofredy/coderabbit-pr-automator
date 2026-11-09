import os
import json
import shutil
import tempfile
import unittest
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from state_manager import StateManager
from config_manager import ConfigManager
from github_client import GitHubClient
from web_dashboard import DashboardBackend, ThreadedHTTPServer, DashboardRequestHandler


class TestReviewHistory(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.test_dir, "automation_state.json")
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.state_mgr = StateManager(state_path=self.state_path)
        self.cfg_mgr = ConfigManager(config_path=self.config_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_chronological_ordering_by_time_executed(self):
        """Verify reviews are sorted chronologically by time executed (desc and asc)."""
        now = datetime.now(timezone.utc)
        t1 = (now - timedelta(hours=3)).isoformat()
        t2 = (now - timedelta(hours=2)).isoformat()
        t3 = (now - timedelta(hours=1)).isoformat()
        t4 = now.isoformat()

        self.state_mgr.record_pr_log("owner/repo1#1", {
            "timestamp": t1,
            "title": "First PR",
            "review_outcome": "COMMENT",
            "elapsed_seconds": 10.0,
            "coderabbit_account": "Account A",
            "account_id": "acc_a"
        })
        self.state_mgr.record_pr_log("owner/repo1#2", {
            "timestamp": t3,
            "title": "Third PR",
            "review_outcome": "APPROVE",
            "elapsed_seconds": 5.0,
            "coderabbit_account": "Account B",
            "account_id": "acc_b"
        })
        self.state_mgr.record_pr_log("owner/repo2#1", {
            "timestamp": t2,
            "title": "Second PR",
            "review_outcome": "ERROR",
            "error": "Timeout occurred",
            "elapsed_seconds": 30.0,
            "coderabbit_account": "Account A",
            "account_id": "acc_a"
        })
        self.state_mgr.record_pr_log("owner/repo2#2", {
            "timestamp": t4,
            "title": "Fourth PR",
            "review_outcome": "APPROVE",
            "elapsed_seconds": 8.0,
            "coderabbit_account": "Account B",
            "account_id": "acc_b"
        })

        # Test Newest First (desc)
        result_desc = self.state_mgr.get_all_review_history(sort_order="desc")
        history_desc = result_desc["history"]
        self.assertEqual(len(history_desc), 4)
        self.assertEqual(history_desc[0]["title"], "Fourth PR")
        self.assertEqual(history_desc[1]["title"], "Third PR")
        self.assertEqual(history_desc[2]["title"], "Second PR")
        self.assertEqual(history_desc[3]["title"], "First PR")

        # Test Oldest First (asc)
        result_asc = self.state_mgr.get_all_review_history(sort_order="asc")
        history_asc = result_asc["history"]
        self.assertEqual(len(history_asc), 4)
        self.assertEqual(history_asc[0]["title"], "First PR")
        self.assertEqual(history_asc[1]["title"], "Second PR")
        self.assertEqual(history_asc[2]["title"], "Third PR")
        self.assertEqual(history_asc[3]["title"], "Fourth PR")

    def test_metrics_calculation(self):
        """Verify performance and aggregate metrics calculation."""
        now = datetime.now(timezone.utc)
        self.state_mgr.record_pr_log("owner/repo1#1", {
            "timestamp": now.isoformat(),
            "review_outcome": "APPROVE",
            "elapsed_seconds": 10.0
        })
        self.state_mgr.record_pr_log("owner/repo1#2", {
            "timestamp": now.isoformat(),
            "review_outcome": "COMMENT",
            "elapsed_seconds": 20.0
        })
        self.state_mgr.record_pr_log("owner/repo2#1", {
            "timestamp": now.isoformat(),
            "review_outcome": "ERROR",
            "elapsed_seconds": 30.0
        })
        self.state_mgr.record_pr_log("owner/repo2#2", {
            "timestamp": now.isoformat(),
            "review_outcome": "RATE_LIMITED",
            "elapsed_seconds": 0.0
        })

        res = self.state_mgr.get_all_review_history()
        metrics = res["metrics"]
        self.assertEqual(metrics["total_reviews"], 4)
        self.assertEqual(metrics["approved_count"], 1)
        self.assertEqual(metrics["commented_count"], 1)
        self.assertEqual(metrics["error_count"], 1)
        self.assertEqual(metrics["rate_limited_count"], 1)
        self.assertEqual(metrics["avg_duration_seconds"], 15.0)
        self.assertEqual(metrics["unique_prs_count"], 4)
        self.assertEqual(metrics["unique_repos_count"], 2)

    def test_filtering(self):
        """Verify outcome, repo, account, search, and time-range filters."""
        now = datetime.now(timezone.utc)
        t_recent = now.isoformat()
        t_old = (now - timedelta(days=10)).isoformat()

        self.state_mgr.record_pr_log("alpha/service#10", {
            "timestamp": t_recent,
            "title": "Fix login bug",
            "author": "alice",
            "review_outcome": "APPROVE",
            "account_id": "acc_1",
            "coderabbit_account": "Team Alpha",
            "summary": "Clean code changes"
        })
        self.state_mgr.record_pr_log("beta/service#20", {
            "timestamp": t_recent,
            "title": "Add analytics",
            "author": "bob",
            "review_outcome": "COMMENT",
            "account_id": "acc_2",
            "coderabbit_account": "Team Beta",
            "summary": "Needs more tests"
        })
        self.state_mgr.record_pr_log("gamma/service#30", {
            "timestamp": t_old,
            "title": "Legacy refactor",
            "author": "charlie",
            "review_outcome": "ERROR",
            "account_id": "acc_1",
            "coderabbit_account": "Team Alpha",
            "error": "Syntax error in patch"
        })

        # Outcome filter
        res = self.state_mgr.get_all_review_history(outcome="APPROVE")
        self.assertEqual(len(res["history"]), 1)
        self.assertEqual(res["history"][0]["title"], "Fix login bug")

        # Repo filter
        res = self.state_mgr.get_all_review_history(repo="beta/service")
        self.assertEqual(len(res["history"]), 1)
        self.assertEqual(res["history"][0]["title"], "Add analytics")

        # Account filter
        res = self.state_mgr.get_all_review_history(account_id="acc_1")
        self.assertEqual(len(res["history"]), 2)

        # Search query
        res = self.state_mgr.get_all_review_history(search="analytics")
        self.assertEqual(len(res["history"]), 1)
        self.assertEqual(res["history"][0]["title"], "Add analytics")

        # Time range filter (24h excludes 10 days ago)
        res = self.state_mgr.get_all_review_history(time_range="24h")
        self.assertEqual(len(res["history"]), 2)

    def test_delete_single_and_clear_all(self):
        """Verify deleting a single entry and clearing all review history."""
        now = datetime.now(timezone.utc).isoformat()
        id1 = self.state_mgr.record_pr_log("owner/repo#1", {"timestamp": now, "title": "PR 1"})
        id2 = self.state_mgr.record_pr_log("owner/repo#2", {"timestamp": now, "title": "PR 2"})

        self.assertEqual(self.state_mgr.get_total_reviews_count(), 2)

        # Delete single entry
        deleted = self.state_mgr.delete_review_history_entry(id1)
        self.assertTrue(deleted)
        self.assertEqual(self.state_mgr.get_total_reviews_count(), 1)
        res = self.state_mgr.get_all_review_history()
        self.assertEqual(len(res["history"]), 1)
        self.assertEqual(res["history"][0]["log_id"], id2)

        # Clear all
        self.state_mgr.clear_all_review_history()
        self.assertEqual(self.state_mgr.get_total_reviews_count(), 0)
        res = self.state_mgr.get_all_review_history()
        self.assertEqual(len(res["history"]), 0)

    def test_http_api_endpoints(self):
        """Verify GET /api/review-history and POST /api/review-history/clear via HTTP."""
        backend = DashboardBackend(
            config_manager=self.cfg_mgr,
            state_manager=self.state_mgr,
            github_client=GitHubClient(token="test_token"),
            auto_start_worker=False
        )

        server = ThreadedHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}"

        import threading
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        import web_dashboard
        web_dashboard.backend = backend

        try:
            now = datetime.now(timezone.utc)
            self.state_mgr.record_pr_log("org/proj#1", {
                "timestamp": (now - timedelta(minutes=5)).isoformat(),
                "title": "First Review",
                "review_outcome": "APPROVE",
                "elapsed_seconds": 12.0
            })
            log_id2 = self.state_mgr.record_pr_log("org/proj#2", {
                "timestamp": now.isoformat(),
                "title": "Second Review",
                "review_outcome": "COMMENT",
                "elapsed_seconds": 7.5
            })

            # 1. GET /api/review-history
            with urllib.request.urlopen(f"{base_url}/api/review-history?sort_order=desc") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data["total"], 2)
                self.assertEqual(data["history"][0]["title"], "Second Review")
                self.assertEqual(data["metrics"]["approved_count"], 1)

            # 2. GET /api/status includes total_reviews_count
            with urllib.request.urlopen(f"{base_url}/api/status") as resp:
                self.assertEqual(resp.status, 200)
                status_data = json.loads(resp.read().decode())
                self.assertEqual(status_data["total_reviews_count"], 2)

            # 3. POST /api/review-history/clear (single delete)
            req = urllib.request.Request(
                f"{base_url}/api/review-history/clear",
                data=json.dumps({"log_id": log_id2}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                del_res = json.loads(resp.read().decode())
                self.assertEqual(del_res["status"], "deleted")

            # 4. POST /api/review-history/clear (clear all)
            req_all = urllib.request.Request(
                f"{base_url}/api/review-history/clear",
                data=json.dumps({}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req_all) as resp:
                self.assertEqual(resp.status, 200)
                clear_res = json.loads(resp.read().decode())
                self.assertEqual(clear_res["status"], "all_cleared")

            # Verify history is empty now
            with urllib.request.urlopen(f"{base_url}/api/review-history") as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data["total"], 0)

        finally:
            server.shutdown()
            server.server_close()
            backend.stop()


if __name__ == "__main__":
    unittest.main()
