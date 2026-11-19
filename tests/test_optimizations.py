import gzip
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from auto_review_prs import AutoReviewEngine
from config_manager import ConfigManager
from daily_summary import generate_daily_summary
from github_client import GitHubClient
from repository_management import RepositoryManagementService
from state_manager import StateManager
from web_dashboard import DashboardBackend, DashboardRequestHandler, ThreadedHTTPServer


class TestOptimizations(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.state_path = os.path.join(self.test_dir, "automation_state.json")
        self.repos_dir = os.path.join(self.test_dir, "repos")
        self.reviews_dir = os.path.join(self.test_dir, "reviews")
        self._closeables = []

    def tearDown(self):
        for obj in self._closeables:
            try:
                if hasattr(obj, "stop"):
                    obj.stop()
                elif hasattr(obj, "close"):
                    obj.close()
            except Exception:
                pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sqlite_wal_and_legacy_json_migration(self):
        now_epoch = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        legacy_payload = {
            "rate_limited": True,
            "rate_limit_expires_at": now_epoch + 300,
            "rate_limit_reason": "Legacy quota",
            "rate_limit_retry_after_seconds": 300,
            "rate_limit_set_at": now_epoch,
            "attempt_counts": {"owner/repo#1": 2},
            "active_reviews": {
                "owner/repo#1": {
                    "started_at": now_epoch,
                    "updated_at": now_epoch,
                    "attempt": 2,
                    "phase": "RUNNING_CODERABBIT",
                    "message": "Running",
                }
            },
            "pr_statuses": {
                "owner/repo#1": {"status": "COMPLETED", "review_outcome": "APPROVED", "head_sha": "abc123"}
            },
            "last_run_timestamp": now_iso,
            "review_queue": [{"pr_key": "owner/repo#2", "force": False}],
            "account_rate_limits": {
                "acc_1": {
                    "rate_limited": True,
                    "set_at": now_epoch,
                    "expires_at": now_epoch + 180,
                    "duration_seconds": 180,
                    "reason": "Account cooldown",
                }
            },
            "last_account_index": 0,
            "pr_logs": {
                "owner/repo#1": [
                    {
                        "log_id": "shared_log_1",
                        "pr_key": "owner/repo#1",
                        "account_id": "acc_1",
                        "coderabbit_account": "Acc 1",
                        "timestamp": now_iso,
                        "status": "COMPLETED",
                        "review_outcome": "APPROVED",
                        "elapsed_seconds": 11.5,
                        "stdout": "FULL_RAW_STDOUT_BLOB",
                        "stderr": "warning trace",
                        "findings": [{"file": "app.py", "line": 10, "severity": "INFO"}],
                    }
                ]
            },
            "account_logs": {
                "acc_1": [
                    {
                        "log_id": "shared_log_1",
                        "pr_key": "owner/repo#1",
                        "account_id": "acc_1",
                        "coderabbit_account": "Acc 1",
                        "timestamp": now_iso,
                        "status": "COMPLETED",
                        "review_outcome": "APPROVED",
                        "elapsed_seconds": 11.5,
                    }
                ]
            },
            "review_history": [
                {
                    "log_id": "shared_log_1",
                    "pr_key": "owner/repo#1",
                    "account_id": "acc_1",
                    "timestamp": now_iso,
                    "status": "COMPLETED",
                    "review_outcome": "APPROVED",
                },
                {
                    "log_id": "hist_only_2",
                    "pr_key": "owner/repo#3",
                    "account_id": "default",
                    "timestamp": now_iso,
                    "status": "ERROR",
                    "review_outcome": "ERROR",
                    "error": "CLI failed",
                },
            ],
            "daily_snapshots": [
                {
                    "id": "snap_legacy_1",
                    "date": "2026-11-18",
                    "created_at": now_iso,
                    "metrics": {"total_open_prs": 1},
                    "open_prs": [{"pr_key": "owner/repo#1"}],
                    "merged_prs": [],
                }
            ],
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(legacy_payload, f)

        sm = StateManager(state_path=self.state_path)
        self._closeables.append(sm)

        # Verify SQLite WAL mode
        conn = sqlite3.connect(sm.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
            run_count = conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0]
            self.assertEqual(run_count, 2)
        finally:
            conn.close()

        # Verify migrated operational state
        is_rl, _, reason = sm.is_rate_limit_active()
        self.assertTrue(is_rl)
        self.assertEqual(reason, "Legacy quota")
        self.assertEqual(sm.get_attempt_count("owner/repo#1"), 2)
        self.assertTrue(sm.is_pr_reviewing("owner/repo#1"))
        self.assertEqual(sm.get_review_queue()[0]["pr_key"], "owner/repo#2")
        self.assertTrue(sm.is_account_rate_limited("acc_1")[0])
        self.assertEqual(sm.get_daily_snapshot("snap_legacy_1")["date"], "2026-11-18")

        # History query strips heavy stdout/findings blobs while PR logs retain them
        hist = sm.get_all_review_history()
        self.assertEqual(hist["total"], 2)
        shared_hist = next(h for h in hist["history"] if h["log_id"] == "shared_log_1")
        self.assertNotIn("stdout", shared_hist)
        self.assertNotIn("findings", shared_hist)
        self.assertEqual(shared_hist.get("stderr"), "warning trace")

        pr_logs = sm.get_pr_logs("owner/repo#1")
        self.assertEqual(len(pr_logs), 1)
        self.assertEqual(pr_logs[0]["stdout"], "FULL_RAW_STDOUT_BLOB")
        self.assertEqual(len(pr_logs[0]["findings"]), 1)

    def test_pr_cache_persists_across_restarts(self):
        cfg = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        cfg.save_config({"repositories": [{"full_name": "owner/repo1", "enabled": True}], "service_enabled": True})
        sm1 = StateManager(state_path=self.state_path)
        self._closeables.append(sm1)

        gh = MagicMock(spec=GitHubClient)
        gh.get_username.return_value = "bot"
        gh.get_branch.return_value = {"commit": {"sha": "tip123"}}
        gh.list_open_prs.return_value = [
            {
                "number": 7,
                "title": "Warm PR",
                "user": {"login": "alice"},
                "base": {"ref": "main", "sha": "b1"},
                "head": {"ref": "feat", "sha": "h1"},
                "html_url": "https://github.com/owner/repo1/pull/7",
                "created_at": "2026-11-19T00:00:00Z",
                "updated_at": "2026-11-19T01:00:00Z",
            }
        ]
        gh.get_pr_review_summary.return_value = {
            "has_other_changes_requested": False,
            "other_changes_requested_by": [],
            "other_approved_by": [],
            "has_user_reviewed": False,
            "user_review_state": None,
            "has_conflict": False,
            "mergeable_state": "clean",
        }

        svc1 = RepositoryManagementService(cfg, gh, state_manager=sm1)
        svc1.refresh_pr_cache()
        self.assertEqual(len(svc1.get_cached_prs()), 1)

        sm2 = StateManager(state_path=self.state_path)
        self._closeables.append(sm2)
        svc2 = RepositoryManagementService(cfg, gh, state_manager=sm2)
        warmed = svc2.get_cached_prs()
        self.assertEqual(len(warmed), 1)
        self.assertEqual(warmed[0]["pr_key"], "owner/repo1#7")

    def test_config_manager_write_through_cache(self):
        cfg_mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        cfg_mgr.load_config()
        v0 = cfg_mgr.get_version()

        with patch("config_manager.json.load", wraps=json.load) as mock_load:
            c1 = cfg_mgr.load_config()
            c2 = cfg_mgr.load_config()
            self.assertEqual(mock_load.call_count, 0)
            self.assertEqual(c1["max_files_limit"], c2["max_files_limit"])

            cfg_mgr.save_config({**c1, "max_files_limit": 42})
            self.assertGreater(cfg_mgr.get_version(), v0)
            c3 = cfg_mgr.load_config()
            self.assertEqual(c3["max_files_limit"], 42)
            self.assertEqual(mock_load.call_count, 0)

            # Simulate external modification on disk with bumped mtime_ns
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({**c3, "max_files_limit": 77}, f)
            cur_stat = os.stat(self.config_path)
            os.utime(self.config_path, ns=(cur_stat.st_atime_ns, cur_stat.st_mtime_ns + 5_000_000))
            c4 = cfg_mgr.load_config()
            self.assertEqual(c4["max_files_limit"], 77)
            self.assertEqual(mock_load.call_count, 1)

        cfg_mgr.set_github_token("tok_initial")
        self.assertEqual(cfg_mgr.get_github_token(), "tok_initial")
        cfg_mgr.set_github_token("tok_updated")
        self.assertEqual(cfg_mgr.get_github_token(), "tok_updated")

    def test_github_client_single_fetch_summary(self):
        client = GitHubClient(token="fake")
        client.get_reviews_for_pr = MagicMock(return_value=[])
        client.get_comments_for_pr = MagicMock(return_value=[])
        client.get_check_runs_summary = MagicMock(return_value={"has_check_error": False, "failed_checks": [], "checks_status": "PASSED"})
        client.get_pr_mergeable_status = MagicMock(return_value={"has_conflict": False, "mergeable_state": "clean"})

        summary = client.get_pr_review_summary(
            "owner", "repo", 10, commit_sha="deadbeef1234", auth_user="bot", pr_author="dev"
        )
        self.assertFalse(summary["has_user_auto_approved"])
        self.assertEqual(client.get_reviews_for_pr.call_count, 1)
        self.assertEqual(client.get_comments_for_pr.call_count, 1)

    def test_github_client_etag_304_and_token_reset(self):
        client = GitHubClient(token="tok1")
        call_headers = []
        step = {"n": 0}

        def fake_urlopen(req, timeout=30):
            step["n"] += 1
            call_headers.append(dict(req.header_items()))
            if step["n"] == 1:
                resp = MagicMock()
                resp.read.return_value = b'{"number": 5}'
                resp.headers = {"ETag": '"etag-v1"'}
                resp.__enter__.return_value = resp
                resp.__exit__.return_value = None
                return resp
            if step["n"] == 2:
                raise urllib.error.HTTPError(
                    url=req.full_url,
                    code=304,
                    msg="Not Modified",
                    hdrs={},
                    fp=io.BytesIO(b""),
                )
            resp = MagicMock()
            resp.read.return_value = b'{"number": 6}'
            resp.headers = {}
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = None
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            r1 = client._request("GET", "/repos/owner/repo/pulls/5")
            r2 = client._request("GET", "/repos/owner/repo/pulls/5")
            self.assertEqual(r1, {"number": 5})
            self.assertEqual(r2, {"number": 5})
            self.assertEqual(call_headers[1].get("If-none-match"), '"etag-v1"')

            client.set_token("tok2")
            r3 = client._request("GET", "/repos/owner/repo/pulls/5")
            self.assertEqual(r3, {"number": 6})
            self.assertNotIn("If-none-match", call_headers[2])

    def test_github_client_empty_token_username(self):
        client = GitHubClient(token="")
        client._request = MagicMock()
        self.assertEqual(client.get_username(), "")
        client._request.assert_not_called()

    def test_github_client_graphql_batch_and_fallback(self):
        client = GitHubClient(token="tok")
        graphql_response = {
            "data": {
                "repository": {
                    "pullRequests": {
                        "nodes": [
                            {
                                "number": 12,
                                "title": "GraphQL PR",
                                "url": "https://github.com/o/r/pull/12",
                                "createdAt": "2026-11-18T10:00:00Z",
                                "updatedAt": "2026-11-19T10:00:00Z",
                                "mergeable": "MERGEABLE",
                                "mergeStateStatus": "CLEAN",
                                "author": {"login": "alice"},
                                "baseRefName": "main",
                                "baseRefOid": "base000",
                                "baseRef": {"target": {"oid": "tip999"}},
                                "headRefName": "feat-x",
                                "headRefOid": "head222",
                                "reviews": {
                                    "totalCount": 2,
                                    "nodes": [
                                        {
                                            "author": {"login": "bot"},
                                            "state": "APPROVED",
                                            "body": "## 🐰 CodeRabbit",
                                            "commit": {"oid": "head222"},
                                        },
                                        {
                                            "author": {"login": "bob"},
                                            "state": "APPROVED",
                                            "body": "LGTM",
                                            "commit": {"oid": "old111"},
                                        },
                                    ],
                                },
                                "comments": {
                                    "totalCount": 1,
                                    "nodes": [
                                        {"author": {"login": "alice"}, "body": "LGTM ready to merge"}
                                    ],
                                },
                                "commits": {
                                    "nodes": [
                                        {
                                            "commit": {
                                                "oid": "head222",
                                                "statusCheckRollup": {
                                                    "state": "SUCCESS",
                                                    "contexts": {
                                                        "nodes": [
                                                            {"__typename": "CheckRun", "name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS"},
                                                            {"__typename": "StatusContext", "context": "ci/lint", "state": "SUCCESS"},
                                                        ]
                                                    },
                                                },
                                            }
                                        }
                                    ]
                                },
                            },
                            {
                                "number": 13,
                                "title": "Huge Review Thread PR",
                                "url": "https://github.com/o/r/pull/13",
                                "createdAt": "2026-11-18T10:00:00Z",
                                "updatedAt": "2026-11-19T09:00:00Z",
                                "mergeable": "CONFLICTING",
                                "mergeStateStatus": "DIRTY",
                                "author": {"login": "charlie"},
                                "baseRefName": "main",
                                "baseRefOid": "base000",
                                "baseRef": {"target": {"oid": "tip999"}},
                                "headRefName": "feat-y",
                                "headRefOid": "head333",
                                "reviews": {"totalCount": 105, "nodes": []},
                                "comments": {"totalCount": 0, "nodes": []},
                                "commits": {"nodes": []},
                            },
                        ]
                    }
                }
            }
        }
        client._request = MagicMock(return_value=graphql_response)
        client.get_pr_review_summary = MagicMock(return_value={
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
            "has_conflict": True,
            "mergeable_state": "dirty",
            "has_user_reviewed": False,
            "user_review_state": None,
        })

        items = client.fetch_repo_open_prs_graphql("o", "r", auth_user="bot")
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 2)
        p12 = items[0]
        self.assertEqual(p12["base_tip_sha"], "tip999")
        self.assertTrue(p12["has_user_auto_approved"])
        self.assertIn("bob", p12["outdated_approvals_by"])
        self.assertTrue(p12["author_approved_in_text"])
        self.assertEqual(p12["checks_status"], "PASSED")
        self.assertFalse(p12["has_conflict"])
        # Overflow PR #13 fell back to REST summary
        client.get_pr_review_summary.assert_called_once()

        # GraphQL error payload returns None for clean REST fallback
        client._request = MagicMock(return_value={"errors": [{"message": "bad"}]})
        self.assertIsNone(client.fetch_repo_open_prs_graphql("o", "r", auth_user="bot"))

    def test_daily_summary_concurrent_and_ttl_cache(self):
        cfg = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        cfg.save_config({
            "repositories": [
                {"full_name": "owner/r1", "enabled": True},
                {"full_name": "owner/r2", "enabled": True},
            ],
            "service_enabled": True,
        })
        sm = StateManager(state_path=self.state_path)
        gh = MagicMock(spec=GitHubClient)
        gh.get_username.return_value = "bot"
        now_iso = datetime.now(timezone.utc).isoformat()
        gh.list_recently_merged_prs.side_effect = lambda o, r, since_timestamp=None: [
            {
                "number": 1 if r == "r1" else 2,
                "title": f"Merged {r}",
                "created_at": now_iso,
                "merged_at": now_iso,
                "updated_at": now_iso,
                "user": {"login": "dev"},
                "base": {"ref": "main"},
                "html_url": f"https://github.com/{o}/{r}/pull/1",
            }
        ]

        backend = DashboardBackend(cfg, sm, gh, auto_start_worker=False)
        self._closeables.append(backend)

        s1 = backend.get_daily_summary(hours=24)
        self.assertEqual(s1["metrics"]["merged_today"], 2)
        self.assertEqual(gh.list_recently_merged_prs.call_count, 2)

        # Second call hits TTL cache
        s2 = backend.get_daily_summary(hours=24)
        self.assertEqual(s2["metrics"]["merged_today"], 2)
        self.assertEqual(gh.list_recently_merged_prs.call_count, 2)

        # State mutation invalidates TTL cache
        sm.record_last_run()
        s3 = backend.get_daily_summary(hours=24)
        self.assertEqual(s3["metrics"]["merged_today"], 2)
        self.assertEqual(gh.list_recently_merged_prs.call_count, 4)

    def test_multi_account_parallel_reviews_barrier(self):
        cfg = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        cfg.save_config({
            "repositories": [{"full_name": "owner/repo1", "enabled": True, "path": os.path.join(self.repos_dir, "owner/repo1")}],
            "coderabbit_accounts": [
                {"id": "acc-a", "name": "Acc A", "type": "api_key", "api_key": "k1", "enabled": True},
                {"id": "acc-b", "name": "Acc B", "type": "api_key", "api_key": "k2", "enabled": True},
            ],
            "service_enabled": True,
        })
        sm = StateManager(state_path=self.state_path)
        gh = MagicMock(spec=GitHubClient)
        gh.get_username.return_value = "bot"
        gh.get_pr.side_effect = lambda o, r, num: {
            "number": num,
            "state": "open",
            "title": f"PR {num}",
            "user": {"login": "dev"},
            "base": {"ref": "main"},
            "head": {"ref": f"b{num}", "sha": f"sha{num}"},
        }

        backend = DashboardBackend(cfg, sm, gh, auto_start_worker=False)
        self._closeables.append(backend)
        review_svc = backend.review_service

        barrier = threading.Barrier(2, timeout=5.0)
        seen_accounts = []
        seen_worktree_flags = []
        seen_active_snapshots = []
        capture_lock = threading.Lock()

        def concurrent_review(repo_info, pr, force=False, account=None, use_worktree=False):
            barrier.wait()
            q_snap = review_svc.get_review_queue()
            with capture_lock:
                seen_accounts.append(account.get("id") if account else None)
                seen_worktree_flags.append(use_worktree)
                seen_active_snapshots.append(len(q_snap.get("active_items", [])))
            return {"status": "COMPLETED"}

        with patch.object(backend.review_engine, "review_single_pr", side_effect=concurrent_review):
            review_svc.enqueue_pr("owner/repo1#1", force=True)
            review_svc.enqueue_pr("owner/repo1#2", force=True)
            backend._executor.shutdown(wait=True)

        self.assertEqual(set(seen_accounts), {"acc-a", "acc-b"})
        self.assertEqual(seen_worktree_flags, [True, True])
        self.assertTrue(any(cnt == 2 for cnt in seen_active_snapshots))
        self.assertEqual(review_svc.get_review_queue()["active_items"], [])

    def test_prepare_repo_partial_clone_and_no_fetch_all(self):
        cfg = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        sm = StateManager(state_path=self.state_path)
        self._closeables.append(sm)
        gh = MagicMock(spec=GitHubClient)
        gh.token = "tok"
        engine = AutoReviewEngine(cfg, sm, gh, reviews_dir=self.reviews_dir)

        repo_path = os.path.join(self.repos_dir, "owner/my-repo")
        with patch("auto_review_prs.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            self.assertTrue(engine.prepare_repo("owner/my-repo", repo_path))
            clone_cmd = mock_run.call_args[0][0]
            self.assertIn("--filter=blob:none", clone_cmd)

        os.makedirs(os.path.join(repo_path, ".git"), exist_ok=True)
        with patch("auto_review_prs.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            self.assertTrue(engine.prepare_repo("owner/my-repo", repo_path))
            calls = [c[0][0] for c in mock_run.call_args_list]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:3], ["git", "remote", "set-url"])

    def test_git_worktree_lifecycle_real_repo(self):
        origin_dir = os.path.join(self.test_dir, "origin.git")
        seed_dir = os.path.join(self.test_dir, "seed")
        clone_dir = os.path.join(self.repos_dir, "owner/worktree-repo")

        subprocess.run(["git", "init", "--bare", origin_dir], check=True, capture_output=True)
        subprocess.run(["git", "clone", origin_dir, seed_dir], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=seed_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=seed_dir, check=True)
        subprocess.run(["git", "checkout", "-B", "main"], cwd=seed_dir, check=True, capture_output=True)
        with open(os.path.join(seed_dir, "README.md"), "w") as f:
            f.write("base\n")
        subprocess.run(["git", "add", "."], cwd=seed_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=seed_dir, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=seed_dir, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "-b", "feat-7"], cwd=seed_dir, check=True, capture_output=True)
        with open(os.path.join(seed_dir, "README.md"), "w") as f:
            f.write("pr change\n")
        subprocess.run(["git", "commit", "-am", "pr commit"], cwd=seed_dir, check=True, capture_output=True)
        pr_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=seed_dir, check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "push", "origin", "HEAD:refs/pull/7/head"], cwd=seed_dir, check=True, capture_output=True)

        os.makedirs(os.path.dirname(clone_dir), exist_ok=True)
        subprocess.run(["git", "clone", origin_dir, clone_dir], check=True, capture_output=True)

        cfg = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        sm = StateManager(state_path=self.state_path)
        self._closeables.append(sm)
        gh = MagicMock(spec=GitHubClient)
        gh.token = ""
        engine = AutoReviewEngine(cfg, sm, gh, reviews_dir=self.reviews_dir)

        wt_path = engine.prepare_pr_worktree(clone_dir, 7, "main")
        self.assertIsNotNone(wt_path)
        self.assertTrue(os.path.isdir(wt_path))
        self.assertIn(".worktrees", wt_path)
        self.assertFalse(os.path.commonpath([os.path.abspath(clone_dir), os.path.abspath(wt_path)]) == os.path.abspath(clone_dir))
        wt_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt_path, check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(wt_sha, pr_sha)

        engine.cleanup_pr_worktree(clone_dir, wt_path)
        self.assertFalse(os.path.exists(wt_path))

    def test_http_etag_304_gzip_and_single_pass_status(self):
        cfg = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        cfg.save_config({
            "repositories": [{"full_name": "owner/repo1", "enabled": True}],
            "service_enabled": True,
        })
        sm = StateManager(state_path=self.state_path)
        gh = MagicMock(spec=GitHubClient)
        gh.get_username.return_value = "bot"
        gh.get_branch.return_value = {"commit": {"sha": "tip123"}}
        gh.list_open_prs.return_value = [
            {
                "number": i,
                "title": f"Long PR title number {i} for payload size",
                "user": {"login": "dev"},
                "base": {"ref": "main", "sha": "b1"},
                "head": {"ref": f"feat-{i}", "sha": f"sha-{i}"},
                "html_url": f"https://github.com/owner/repo1/pull/{i}",
                "created_at": "2026-11-18T10:00:00Z",
                "updated_at": "2026-11-19T10:00:00Z",
            }
            for i in range(1, 5)
        ]
        gh.get_pr_review_summary.return_value = {
            "has_other_changes_requested": False,
            "other_changes_requested_by": [],
            "other_approved_by": [],
            "has_user_reviewed": False,
            "user_review_state": None,
            "has_conflict": False,
            "mergeable_state": "clean",
        }

        backend = DashboardBackend(cfg, sm, gh, auto_start_worker=False)
        self._closeables.append(backend)
        backend.refresh_pr_cache()

        # Single-pass status verification: 1 get_dashboard_snapshot(), 0 load_state()
        with patch.object(sm, "get_dashboard_snapshot", wraps=sm.get_dashboard_snapshot) as spy_snap, \
             patch.object(sm, "load_state", wraps=sm.load_state) as spy_load:
            status_data = backend.get_annotated_status()
            self.assertEqual(len(status_data["pull_requests"]), 4)
            self.assertEqual(spy_snap.call_count, 1)
            self.assertEqual(spy_load.call_count, 0)

        import web_dashboard
        web_dashboard.backend = backend
        server = ThreadedHTTPServer(("127.0.0.1", 0), DashboardRequestHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        base_url = f"http://127.0.0.1:{port}"

        try:
            with urllib.request.urlopen(f"{base_url}/api/status") as resp:
                self.assertEqual(resp.status, 200)
                etag = resp.headers.get("ETag")
                self.assertTrue(etag)
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(body["pull_requests"]), 4)

            req_304 = urllib.request.Request(f"{base_url}/api/status", headers={"If-None-Match": etag})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_304)
            self.assertEqual(ctx.exception.code, 304)
            ctx.exception.close()

            req_gz = urllib.request.Request(f"{base_url}/api/status", headers={"Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req_gz) as resp_gz:
                self.assertEqual(resp_gz.status, 200)
                self.assertEqual(resp_gz.headers.get("Content-Encoding"), "gzip")
                raw_gz = resp_gz.read()
                decompressed = json.loads(gzip.decompress(raw_gz).decode("utf-8"))
                self.assertEqual(len(decompressed["pull_requests"]), 4)
        finally:
            server.shutdown()
            server.server_close()

    def test_frontend_polling_and_queue_directives(self):
        with open("dashboard.html", "r", encoding="utf-8") as f:
            dash_html = f.read()
        for token in ("document.hidden", "visibilitychange", "If-None-Match", "active_items"):
            self.assertIn(token, dash_html)

        with open("pr_details.html", "r", encoding="utf-8") as f:
            details_html = f.read()
        for token in ("document.hidden", "visibilitychange", "If-None-Match"):
            self.assertIn(token, details_html)


if __name__ == "__main__":
    unittest.main()
