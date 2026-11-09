import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from config_manager import ConfigManager
from state_manager import StateManager
from github_client import GitHubClient
from daily_summary import (
    generate_daily_summary,
    compute_pr_summary_status,
    format_markdown_summary,
    format_html_summary,
    format_relative_time,
    format_friendly_datetime,
    parse_iso_datetime
)
from web_dashboard import DashboardBackend, DashboardRequestHandler


class TestDailySummaryEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.state_path = os.path.join(self.test_dir, "automation_state.json")
        self.repos_dir = os.path.join(self.test_dir, "repos")

        self.cfg_mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        self.state_mgr = StateManager(state_path=self.state_path)
        self.gh_client = MagicMock(spec=GitHubClient)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_config_daily_summary_settings(self):
        # Default settings
        settings = self.cfg_mgr.get_settings()
        self.assertEqual(settings["daily_summary_time"], "00:00")
        self.assertTrue(settings["daily_summary_enabled"])
        self.assertEqual(self.cfg_mgr.get_daily_summary_time(), "00:00")
        self.assertTrue(self.cfg_mgr.is_daily_summary_enabled())

        # Update settings
        updated = self.cfg_mgr.update_settings({
            "daily_summary_time": "08:30",
            "daily_summary_enabled": False
        })
        self.assertEqual(updated["daily_summary_time"], "08:30")
        self.assertFalse(updated["daily_summary_enabled"])
        self.assertEqual(self.cfg_mgr.get_daily_summary_time(), "08:30")
        self.assertFalse(self.cfg_mgr.is_daily_summary_enabled())

        # Invalid time format ignored
        self.cfg_mgr.update_settings({"daily_summary_time": "invalid-time"})
        self.assertEqual(self.cfg_mgr.get_daily_summary_time(), "08:30")

    def test_state_daily_snapshots(self):
        # Initial empty
        self.assertEqual(self.state_mgr.get_daily_snapshots(), [])
        self.assertIsNone(self.state_mgr.get_daily_snapshot("nonexistent"))

        # Save snapshot
        snap_id = self.state_mgr.save_daily_snapshot({
            "date": "2026-11-09",
            "metrics": {"total_open_prs": 5, "merged_today": 2},
            "open_prs": [{"pr_key": "owner/repo#1"}],
            "merged_prs": [{"pr_key": "owner/repo#2"}]
        })
        self.assertTrue(snap_id.startswith("snap_"))

        # Retrieve by id and by date
        by_id = self.state_mgr.get_daily_snapshot(snap_id)
        self.assertIsNotNone(by_id)
        self.assertEqual(by_id["date"], "2026-11-09")
        self.assertEqual(by_id["metrics"]["total_open_prs"], 5)

        by_date = self.state_mgr.get_daily_snapshot("2026-11-09")
        self.assertIsNotNone(by_date)
        self.assertEqual(by_date["id"], snap_id)

        # Max snapshots limit pruning
        for i in range(1, 35):
            self.state_mgr.save_daily_snapshot({
                "id": f"snap_test_{i}",
                "date": f"2026-10-{i:02d}",
                "metrics": {"index": i}
            }, max_snapshots=10)

        snapshots = self.state_mgr.get_daily_snapshots()
        self.assertLessEqual(len(snapshots), 10)

        # Delete snapshot
        deleted = self.state_mgr.delete_daily_snapshot(snapshots[0]["id"])
        self.assertTrue(deleted)
        self.assertEqual(len(self.state_mgr.get_daily_snapshots()), len(snapshots) - 1)

    def test_state_recent_logs_aggregation(self):
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(hours=2)).isoformat()
        old_ts = (now - timedelta(hours=30)).isoformat()

        # Record logs in pr_logs
        self.state_mgr.record_pr_log("owner/repo#1", {
            "action": "REVIEW",
            "outcome": "APPROVE",
            "timestamp": recent_ts,
            "duration_seconds": 12
        })
        self.state_mgr.record_pr_log("owner/repo#2", {
            "action": "REVIEW",
            "outcome": "COMMENT",
            "timestamp": old_ts,
            "duration_seconds": 15
        })

        recent_logs = self.state_mgr.get_all_recent_logs(hours=24)
        self.assertEqual(len(recent_logs), 1)
        self.assertEqual(recent_logs[0]["pr_key"], "owner/repo#1")
        self.assertEqual(recent_logs[0]["outcome"], "APPROVE")

    def test_github_client_list_recently_merged_prs(self):
        now = datetime.now(timezone.utc)
        recent_iso = (now - timedelta(hours=3)).isoformat()
        old_iso = (now - timedelta(hours=48)).isoformat()

        client = GitHubClient(token="test")
        client._request = MagicMock(return_value=[
            {
                "number": 1,
                "title": "Recently merged PR",
                "merged_at": recent_iso,
                "created_at": (now - timedelta(hours=10)).isoformat(),
                "updated_at": recent_iso,
                "user": {"login": "alice"},
                "base": {"ref": "main"},
                "html_url": "https://github.com/owner/repo/pull/1"
            },
            {
                "number": 2,
                "title": "Old merged PR",
                "merged_at": old_iso,
                "created_at": (now - timedelta(hours=60)).isoformat(),
                "updated_at": old_iso,
                "user": {"login": "bob"},
                "base": {"ref": "main"},
                "html_url": "https://github.com/owner/repo/pull/2"
            },
            {
                "number": 3,
                "title": "Closed without merge",
                "merged_at": None,
                "created_at": (now - timedelta(hours=5)).isoformat(),
                "updated_at": (now - timedelta(hours=2)).isoformat(),
                "user": {"login": "charlie"},
                "base": {"ref": "main"},
                "html_url": "https://github.com/owner/repo/pull/3"
            }
        ])

        cutoff = (now - timedelta(hours=24)).isoformat()
        merged = client.list_recently_merged_prs("owner", "repo", since_timestamp=cutoff)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["number"], 1)

    def test_generate_daily_summary(self):
        now = datetime.now(timezone.utc)
        created_time = (now - timedelta(days=2)).isoformat()
        updated_time = (now - timedelta(minutes=45)).isoformat()
        merged_time = (now - timedelta(hours=3)).isoformat()

        annotated_status = {
            "repositories": [
                {"full_name": "owner/repo1", "enabled": True},
                {"full_name": "owner/repo2", "enabled": True}
            ],
            "pull_requests": [
                {
                    "pr_key": "owner/repo1#10",
                    "repo": "owner/repo1",
                    "number": 10,
                    "title": "Feature Alpha",
                    "author": "jane",
                    "html_url": "https://github.com/owner/repo1/pull/10",
                    "created_at": created_time,
                    "updated_at": updated_time,
                    "status_badge": "APPROVED_BY_YOU",
                    "status_label": "Approved by You",
                    "has_conflict": False,
                    "has_check_error": False,
                    "checks_status": "PASSED",
                    "ready_to_merge": True
                },
                {
                    "pr_key": "owner/repo1#11",
                    "repo": "owner/repo1",
                    "number": 11,
                    "title": "Fix Beta",
                    "author": "bob",
                    "html_url": "https://github.com/owner/repo1/pull/11",
                    "created_at": created_time,
                    "updated_at": updated_time,
                    "status_badge": "RATE_LIMITED",
                    "status_label": "Rate Limited",
                    "has_conflict": True,
                    "has_check_error": True,
                    "failed_checks": ["ci/tests"],
                    "ready_to_merge": False
                }
            ]
        }

        self.gh_client.list_recently_merged_prs.return_value = [
            {
                "number": 20,
                "title": "Merged Gamma",
                "created_at": (now - timedelta(hours=12)).isoformat(),
                "merged_at": merged_time,
                "updated_at": merged_time,
                "user": {"login": "charlie"},
                "base": {"ref": "main"},
                "html_url": "https://github.com/owner/repo1/pull/20"
            }
        ]

        # Log a recent review
        self.state_mgr.record_pr_log("owner/repo1#10", {
            "action": "REVIEW",
            "outcome": "APPROVE",
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "duration_seconds": 18,
            "account_name": "Pro Account"
        })

        summary = generate_daily_summary(
            annotated_status=annotated_status,
            github_client=self.gh_client,
            state_manager=self.state_mgr,
            hours=24
        )

        # Verify Metrics
        self.assertEqual(summary["metrics"]["total_open_prs"], 2)
        self.assertEqual(summary["metrics"]["merged_today"], 2) # returned for both repos
        self.assertEqual(summary["metrics"]["needs_attention_count"], 1) # repo1#11
        self.assertEqual(summary["metrics"]["ready_to_merge_count"], 1)
        self.assertEqual(summary["metrics"]["auto_approved_count"], 1)
        self.assertEqual(summary["metrics"]["reviews_completed_24h"], 1)

        # Verify Open PRs breakdown fields
        pr10 = next(p for p in summary["open_prs"] if p["number"] == 10)
        self.assertEqual(pr10["author"], "jane")
        self.assertIn("ago", pr10["created_at_relative"])
        self.assertIn("ago", pr10["updated_at_relative"])
        self.assertEqual(pr10["status_badge"], "APPROVED_BY_YOU")
        self.assertTrue(pr10["ready_to_merge"])

        # Verify Merged PRs breakdown fields
        self.assertGreaterEqual(len(summary["merged_prs"]), 1)
        m_pr = summary["merged_prs"][0]
        self.assertEqual(m_pr["title"], "Merged Gamma")
        self.assertEqual(m_pr["status"], "MERGED")
        self.assertIn("ago", m_pr["merged_at_relative"])

        # Verify Needs Attention
        self.assertEqual(len(summary["needs_attention"]), 1)
        self.assertEqual(summary["needs_attention"][0]["pr_key"], "owner/repo1#11")
        self.assertGreater(len(summary["needs_attention"][0]["reasons"]), 0)

        # Verify Markdown formatting
        md = format_markdown_summary(summary)
        self.assertIn("# 🐰 CodeRabbit Daily PR Summary", md)
        self.assertIn("## 📂 Open Pull Requests", md)
        self.assertIn("Feature Alpha", md)
        self.assertIn("## 🟣 Merged Pull Requests Today", md)
        self.assertIn("Merged Gamma", md)
        self.assertIn("## ⚠️ Action Required", md)

        # Verify HTML formatting
        html_rep = format_html_summary(summary)
        self.assertIn("<!DOCTYPE html>", html_rep)
        self.assertIn("Feature Alpha", html_rep)
        self.assertIn("Merged Gamma", html_rep)
        self.assertIn("Action Required", html_rep)

    def test_compute_pr_summary_status(self):
        # 1. Merge Conflict
        self.assertEqual(compute_pr_summary_status({"has_conflict": True}), "Merge Conflict")
        self.assertEqual(compute_pr_summary_status({"mergeable_state": "dirty"}), "Merge Conflict")
        self.assertEqual(compute_pr_summary_status({"mergeable_state": "conflicting"}), "Merge Conflict")

        # 2. CI Failed
        self.assertEqual(compute_pr_summary_status({"has_check_error": True}), "CI Failed")
        self.assertEqual(compute_pr_summary_status({"checks_status": "FAILED"}), "CI Failed")
        self.assertEqual(compute_pr_summary_status({"checks_status": "CHECKS FAILED"}), "CI Failed")
        self.assertEqual(compute_pr_summary_status({"checks_status": "FAILURE"}), "CI Failed")
        self.assertEqual(compute_pr_summary_status({"checks_status": "ACTION_REQUIRED"}), "CI Failed")

        # 3. Needs Work
        self.assertEqual(compute_pr_summary_status({"status_badge": "COMMENTS_POSTED"}), "Needs Work")
        self.assertEqual(compute_pr_summary_status({"status_badge": "CHANGES_REQUESTED"}), "Needs Work")
        self.assertEqual(compute_pr_summary_status({"has_other_changes_requested": True}), "Needs Work")
        self.assertEqual(compute_pr_summary_status({"other_changes_requested_by": ["alice"]}), "Needs Work")
        self.assertEqual(compute_pr_summary_status({"user_review_state": "CHANGES_REQUESTED"}), "Needs Work")
        self.assertEqual(compute_pr_summary_status({"bot_review_outcome": "NEEDS_WORK (Minor Issues Detected)"}), "Needs Work")
        self.assertEqual(compute_pr_summary_status({"is_stack_blocked": True}), "Needs Work")
        # Other reviewer requested changes even if bot auto-approved
        self.assertEqual(
            compute_pr_summary_status({"has_user_auto_approved": True, "has_other_changes_requested": True, "ready_to_merge": False}),
            "Needs Work"
        )
        self.assertEqual(
            compute_pr_summary_status({"has_user_auto_approved": True, "other_changes_requested_by": ["alice"], "ready_to_merge": False}),
            "Needs Work"
        )
        # Other reviewer left comments and PR is not ready to merge
        self.assertEqual(
            compute_pr_summary_status({"has_user_auto_approved": True, "other_commented_by": ["bob"], "ready_to_merge": False}),
            "Needs Work"
        )

        # 4. Ready to Merge - ONLY when ready_to_merge is True
        self.assertEqual(compute_pr_summary_status({"ready_to_merge": True}), "Ready to Merge")
        self.assertEqual(compute_pr_summary_status({"ready_to_merge": True, "status_badge": "APPROVED_BY_YOU"}), "Ready to Merge")

        # 5. Not Reviewed - bot approved alone is not enough if PR is not ready to merge
        self.assertEqual(compute_pr_summary_status({}), "Not Reviewed")
        self.assertEqual(compute_pr_summary_status({"status_badge": "PENDING_REVIEW"}), "Not Reviewed")
        self.assertEqual(compute_pr_summary_status({"status_badge": "RATE_LIMITED"}), "Not Reviewed")
        self.assertEqual(compute_pr_summary_status({"status_badge": "IN_PROGRESS"}), "Not Reviewed")
        self.assertEqual(compute_pr_summary_status({"status_badge": "OWN_PR"}), "Not Reviewed")
        self.assertEqual(compute_pr_summary_status({"has_user_auto_approved": True, "ready_to_merge": False}), "Not Reviewed")
        self.assertEqual(compute_pr_summary_status({"has_user_manually_approved": True, "ready_to_merge": False}), "Not Reviewed")
        self.assertEqual(compute_pr_summary_status({"status_badge": "APPROVED_BY_YOU", "ready_to_merge": False}), "Not Reviewed")

    def test_format_markdown_summary_by_group(self):
        sample_data = {
            "date": "2026-11-09",
            "needs_attention": [
                {
                    "pr_key": "owner/repo#1",
                    "title": "Fix Bug",
                    "html_url": "https://github.com/owner/repo/pull/1",
                    "author": "alice",
                    "reasons": ["Merge Conflict"],
                    "has_conflict": True,
                    "has_check_error": False,
                }
            ],
            "open_prs": [
                {
                    "number": 1,
                    "title": "Fix Bug",
                    "repo": "owner/repo",
                    "author": "alice",
                    "html_url": "https://github.com/owner/repo/pull/1",
                    "created_at_friendly": "2026-11-08 10:00 UTC",
                    "created_at_relative": "1d ago",
                    "updated_at_friendly": "2026-11-09 08:00 UTC",
                    "updated_at_relative": "2h ago",
                    "summary_status": "Merge Conflict",
                }
            ],
            "merged_prs": [
                {
                    "number": 2,
                    "title": "Add Feature",
                    "repo": "owner/repo",
                    "author": "bob",
                    "html_url": "https://github.com/owner/repo/pull/2",
                    "created_at_friendly": "2026-11-07 10:00 UTC",
                    "created_at_relative": "2d ago",
                    "merged_at_friendly": "2026-11-09 09:00 UTC",
                    "merged_at_relative": "1h ago",
                    "base_ref": "main",
                }
            ],
            "recent_reviews": [
                {
                    "timestamp_friendly": "2026-11-09 08:30 UTC",
                    "timestamp_relative": "30m ago",
                    "pr_key": "owner/repo#1",
                    "account_name": "Default",
                    "outcome": "APPROVE",
                    "duration_seconds": 15,
                }
            ],
            "repository_breakdown": [
                {
                    "repo": "owner/repo",
                    "open_count": 1,
                    "merged_count": 1,
                    "attention_count": 1,
                    "ready_to_merge_count": 0,
                }
            ],
        }

        # Test group='open_prs'
        md_open = format_markdown_summary(sample_data, group="open_prs")
        self.assertIn("## 📂 Open Pull Requests", md_open)
        self.assertIn("Fix Bug", md_open)
        self.assertNotIn("## 🟣 Merged Pull Requests Today", md_open)
        self.assertNotIn("## ⚠️ Action Required", md_open)

        # Test group='merged'
        md_merged = format_markdown_summary(sample_data, group="merged")
        self.assertIn("## 🟣 Merged Pull Requests Today", md_merged)
        self.assertIn("Add Feature", md_merged)
        self.assertNotIn("## 📂 Open Pull Requests", md_merged)

        # Test group='attention'
        md_att = format_markdown_summary(sample_data, group="attention")
        self.assertIn("## ⚠️ Action Required", md_att)
        self.assertNotIn("## 📂 Open Pull Requests", md_att)

        # Test group='reviews'
        md_rev = format_markdown_summary(sample_data, group="reviews")
        self.assertIn("## ⚡ 24-Hour Review Activity", md_rev)
        self.assertNotIn("## 📂 Open Pull Requests", md_rev)

        # Test group='repositories'
        md_repo = format_markdown_summary(sample_data, group="repositories")
        self.assertIn("## 📁 Monitored Repositories", md_repo)
        self.assertNotIn("## 📂 Open Pull Requests", md_repo)

    def test_dashboard_backend_daily_summary_integration(self):
        backend = DashboardBackend(
            config_manager=self.cfg_mgr,
            state_manager=self.state_mgr,
            github_client=self.gh_client,
            auto_start_worker=False
        )
        try:
            # Live summary
            summary = backend.get_daily_summary(hours=24)
            self.assertIn("metrics", summary)
            self.assertIn("open_prs", summary)
            self.assertIn("merged_prs", summary)

            # Create Snapshot
            snap = backend.create_daily_snapshot()
            self.assertIsNotNone(snap.get("id"))
            self.assertEqual(len(backend.state_manager.get_daily_snapshots()), 1)

            # Schedule checking
            backend.config_manager.update_settings({"daily_summary_enabled": True, "daily_summary_time": datetime.now().strftime("%H:%M")})
            backend._check_daily_snapshot_schedule()
            # Already exists for today, so snapshot count remains 1
            self.assertEqual(len(backend.state_manager.get_daily_snapshots()), 1)
        finally:
            backend.stop()

    def test_http_api_daily_summary_endpoints(self):
        import web_dashboard
        backend = DashboardBackend(
            config_manager=self.cfg_mgr,
            state_manager=self.state_mgr,
            github_client=self.gh_client,
            auto_start_worker=False
        )
        web_dashboard.backend = backend

        try:
            from io import BytesIO

            # Test GET /api/daily-summary
            handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            handler.path = "/api/daily-summary"
            handler.command = "GET"
            handler.wfile = BytesIO()
            handler.rfile = BytesIO()
            handler.send_response = MagicMock()
            handler.send_header = MagicMock()
            handler.end_headers = MagicMock()

            handler.do_GET()
            output = handler.wfile.getvalue().decode("utf-8")
            import json
            data = json.loads(output)
            self.assertIn("metrics", data)
            self.assertIn("open_prs", data)
            self.assertIn("merged_prs", data)

            # Test POST /api/daily-summary/snapshot
            post_handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            post_handler.path = "/api/daily-summary/snapshot"
            post_handler.command = "POST"
            post_handler.headers = {"Content-Length": "2"}
            post_handler.rfile = BytesIO(b"{}")
            post_handler.wfile = BytesIO()
            post_handler.send_response = MagicMock()
            post_handler.send_header = MagicMock()
            post_handler.end_headers = MagicMock()

            post_handler.do_POST()
            post_output = post_handler.wfile.getvalue().decode("utf-8")
            snap_data = json.loads(post_output)
            snap_id = snap_data.get("id")
            self.assertIsNotNone(snap_id)

            # Test GET /api/daily-summary/snapshots
            list_handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            list_handler.path = "/api/daily-summary/snapshots"
            list_handler.command = "GET"
            list_handler.wfile = BytesIO()
            list_handler.rfile = BytesIO()
            list_handler.send_response = MagicMock()
            list_handler.send_header = MagicMock()
            list_handler.end_headers = MagicMock()

            list_handler.do_GET()
            snaps_list = json.loads(list_handler.wfile.getvalue().decode("utf-8"))
            self.assertEqual(len(snaps_list), 1)
            self.assertEqual(snaps_list[0]["id"], snap_id)

            # Test GET /api/daily-summary/snapshots/{id}
            get_snap_handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            get_snap_handler.path = f"/api/daily-summary/snapshots/{snap_id}"
            get_snap_handler.command = "GET"
            get_snap_handler.wfile = BytesIO()
            get_snap_handler.rfile = BytesIO()
            get_snap_handler.send_response = MagicMock()
            get_snap_handler.send_header = MagicMock()
            get_snap_handler.end_headers = MagicMock()

            get_snap_handler.do_GET()
            retrieved_snap = json.loads(get_snap_handler.wfile.getvalue().decode("utf-8"))
            self.assertEqual(retrieved_snap["id"], snap_id)

            # Test GET /api/daily-summary/export?format=markdown
            export_md_handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            export_md_handler.path = "/api/daily-summary/export?format=markdown"
            export_md_handler.command = "GET"
            export_md_handler.wfile = BytesIO()
            export_md_handler.rfile = BytesIO()
            export_md_handler.send_response = MagicMock()
            export_md_handler.send_header = MagicMock()
            export_md_handler.end_headers = MagicMock()

            export_md_handler.do_GET()
            md_content = export_md_handler.wfile.getvalue().decode("utf-8")
            self.assertIn("# 🐰 CodeRabbit Daily PR Summary", md_content)

            # Test GET /api/daily-summary/export?format=markdown&group=open_prs
            export_group_handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            export_group_handler.path = "/api/daily-summary/export?format=markdown&group=open_prs"
            export_group_handler.command = "GET"
            export_group_handler.wfile = BytesIO()
            export_group_handler.rfile = BytesIO()
            export_group_handler.send_response = MagicMock()
            export_group_handler.send_header = MagicMock()
            export_group_handler.end_headers = MagicMock()

            export_group_handler.do_GET()
            group_md_content = export_group_handler.wfile.getvalue().decode("utf-8")
            self.assertIn("## 📂 Open Pull Requests", group_md_content)
            self.assertNotIn("# 🐰 CodeRabbit Daily PR Summary", group_md_content)

            # Test GET /api/daily-summary/export?format=html
            export_html_handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            export_html_handler.path = "/api/daily-summary/export?format=html"
            export_html_handler.command = "GET"
            export_html_handler.wfile = BytesIO()
            export_html_handler.rfile = BytesIO()
            export_html_handler.send_response = MagicMock()
            export_html_handler.send_header = MagicMock()
            export_html_handler.end_headers = MagicMock()

            export_html_handler.do_GET()
            html_content = export_html_handler.wfile.getvalue().decode("utf-8")
            self.assertIn("<!DOCTYPE html>", html_content)

            # Test POST /api/daily-summary/snapshots/delete
            del_handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
            del_handler.path = "/api/daily-summary/snapshots/delete"
            del_handler.command = "POST"
            body = json.dumps({"id": snap_id}).encode("utf-8")
            del_handler.headers = {"Content-Length": str(len(body))}
            del_handler.rfile = BytesIO(body)
            del_handler.wfile = BytesIO()
            del_handler.send_response = MagicMock()
            del_handler.send_header = MagicMock()
            del_handler.end_headers = MagicMock()

            del_handler.do_POST()
            del_res = json.loads(del_handler.wfile.getvalue().decode("utf-8"))
            self.assertTrue(del_res.get("deleted"))
        finally:
            backend.stop()
            web_dashboard.backend = None

    def test_github_client_review_decisive_state_preserved_after_comment(self):
        """Verify that when a reviewer requests changes and later comments, CHANGES_REQUESTED is preserved."""
        client = GitHubClient(token="fake-token")
        client.get_reviews_for_pr = MagicMock(return_value=[
            {
                "id": 101,
                "user": {"login": "senior_dev"},
                "state": "CHANGES_REQUESTED",
                "body": "Please address edge case and add tests.",
                "commit_id": "sha123"
            },
            {
                "id": 102,
                "user": {"login": "senior_dev"},
                "state": "COMMENTED",
                "body": "Here is an example snippet.",
                "commit_id": "sha123"
            }
        ])
        client.get_comments_for_pr = MagicMock(return_value=[])
        client.get_check_runs_summary = MagicMock(return_value={"has_check_error": False, "failed_checks": [], "checks_status": "PASSED"})
        client.get_pr_mergeable_status = MagicMock(return_value={"has_conflict": False, "mergeable_state": "clean"})

        summary = client.get_pr_review_summary("owner", "repo", 42, commit_sha="sha123", auth_user="bot_user")
        self.assertTrue(summary["has_other_changes_requested"])
        self.assertIn("senior_dev", summary["other_changes_requested_by"])

    def test_needs_attention_surfaces_other_reviewer_changes(self):
        """Verify that open PRs with other reviewer changes requested are flagged in needs_attention."""
        annotated_status = {
            "repositories": [{"full_name": "owner/repo", "enabled": True}],
            "pull_requests": [
                {
                    "pr_key": "owner/repo#42",
                    "repo": "owner/repo",
                    "number": 42,
                    "title": "Core Feature",
                    "author": "dev1",
                    "html_url": "https://github.com/owner/repo/pull/42",
                    "created_at": "2026-11-09T10:00:00Z",
                    "updated_at": "2026-11-09T12:00:00Z",
                    "status_badge": "APPROVED_BY_YOU",
                    "has_user_auto_approved": True,
                    "has_other_changes_requested": True,
                    "other_changes_requested_by": ["senior_dev"],
                    "ready_to_merge": False,
                }
            ]
        }
        self.gh_client.list_recently_merged_prs.return_value = []
        summary = generate_daily_summary(
            annotated_status=annotated_status,
            github_client=self.gh_client,
            state_manager=self.state_mgr,
            hours=24
        )
        self.assertEqual(len(summary["needs_attention"]), 1)
        item = summary["needs_attention"][0]
        self.assertEqual(item["summary_status"], "Needs Work")
        self.assertTrue(any("senior_dev" in r for r in item["reasons"]))


if __name__ == "__main__":
    unittest.main()
