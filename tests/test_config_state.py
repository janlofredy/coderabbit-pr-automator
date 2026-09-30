import os
import shutil
import tempfile
import unittest
from config_manager import ConfigManager
from state_manager import StateManager

class TestConfigAndStateManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.state_path = os.path.join(self.test_dir, "automation_state.json")
        self.repos_dir = os.path.join(self.test_dir, "repos")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        for key in ["REPOSITORIES", "POLL_INTERVAL_SECONDS", "MAX_FILES_LIMIT", "AUTO_APPROVE", "STRICT_APPROVAL"]:
            os.environ.pop(key, None)

    def test_config_initialization(self):
        os.environ["REPOSITORIES"] = "org/repo-a, org/repo-b"
        os.environ["POLL_INTERVAL_SECONDS"] = "600"
        os.environ["MAX_FILES_LIMIT"] = "75"
        os.environ["AUTO_APPROVE"] = "false"

        mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        cfg = mgr.load_config()

        self.assertEqual(len(cfg["repositories"]), 2)
        self.assertEqual(cfg["repositories"][0]["full_name"], "org/repo-a")
        self.assertEqual(cfg["repositories"][1]["full_name"], "org/repo-b")
        self.assertEqual(cfg["poll_interval_seconds"], 600)
        self.assertEqual(cfg["max_files_limit"], 75)
        self.assertFalse(cfg["auto_approve"])
        self.assertTrue(mgr.is_service_enabled())

    def test_repo_crud_operations(self):
        mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        mgr.save_config({"repositories": [], "poll_interval_seconds": 900, "max_files_limit": 100, "auto_approve": True, "service_enabled": True})

        # Add repo
        repo = mgr.add_repo("test-org/test-repo")
        self.assertEqual(repo["full_name"], "test-org/test-repo")
        self.assertTrue(repo["enabled"])
        self.assertEqual(len(mgr.get_repos()), 1)

        # Toggle repo
        toggled = mgr.toggle_repo("test-org/test-repo")
        self.assertIsNotNone(toggled)
        self.assertFalse(toggled["enabled"])
        self.assertEqual(len(mgr.get_enabled_repos()), 0)

        mgr.toggle_repo("test-org/test-repo", enabled=True)
        self.assertEqual(len(mgr.get_enabled_repos()), 1)

        # Remove repo
        removed = mgr.remove_repo("test-org/test-repo")
        self.assertTrue(removed)
        self.assertEqual(len(mgr.get_repos()), 0)

    def test_retry_delay_parsing(self):
        # Explicit minutes
        out1 = "Rate limit reached. Please try again in 15 mins."
        self.assertEqual(StateManager.parse_retry_delay_from_text(out1), 900)

        # Explicit seconds
        out2 = "Too many requests. Retry after 45 seconds."
        self.assertEqual(StateManager.parse_retry_delay_from_text(out2), 45)

        # Explicit hours
        out3 = "Quota exhausted. Please retry in 2 hours."
        self.assertEqual(StateManager.parse_retry_delay_from_text(out3), 7200)

        # Keyword match with default backoff
        out4 = "Error 429: Too Many Requests on CodeRabbit Free Tier."
        self.assertEqual(StateManager.parse_retry_delay_from_text(out4, default_delay=600), 600)

        # Normal text (no rate limit)
        out5 = "Review complete: 0 findings detected."
        self.assertEqual(StateManager.parse_retry_delay_from_text(out5), 0)

    def test_state_rate_limit_and_attempts(self):
        sm = StateManager(state_path=self.state_path)
        is_active, remaining, reason = sm.is_rate_limit_active()
        self.assertFalse(is_active)

        # Record rate limit for 300 seconds
        sm.record_rate_limit(300, reason="Quota limit exceeded", pr_key="owner/repo#42")
        is_active, remaining, reason = sm.is_rate_limit_active()
        self.assertTrue(is_active)
        self.assertGreater(remaining, 250)
        self.assertIn("Quota", reason)
        self.assertEqual(sm.get_attempt_count("owner/repo#42"), 1)

        # Increment attempt
        sm.increment_attempt_count("owner/repo#42")
        self.assertEqual(sm.get_attempt_count("owner/repo#42"), 2)

        # Clear rate limit
        sm.clear_rate_limit()
        is_active, remaining, _ = sm.is_rate_limit_active()
        self.assertFalse(is_active)
        self.assertEqual(remaining, 0)

        # Reset attempts
        sm.reset_attempt_count("owner/repo#42")
        self.assertEqual(sm.get_attempt_count("owner/repo#42"), 0)

    def test_active_reviews_and_statuses(self):
        sm = StateManager(state_path=self.state_path)
        pr_key = "owner/repo#10"

        self.assertFalse(sm.is_pr_reviewing(pr_key))
        sm.set_pr_reviewing(pr_key, True, attempt=2)
        self.assertTrue(sm.is_pr_reviewing(pr_key))
        active = sm.load_state().get("active_reviews", {})
        self.assertEqual(active[pr_key]["attempt"], 2)

        sm.set_pr_reviewing(pr_key, False)
        self.assertFalse(sm.is_pr_reviewing(pr_key))

        # Record PR status
        sm.record_pr_status(pr_key, {"status": "COMPLETED", "review_outcome": "APPROVED"})
        status = sm.get_pr_status(pr_key)
        self.assertIsNotNone(status)
        self.assertEqual(status["review_outcome"], "APPROVED")
        self.assertIn("updated_at", status)

    def test_strict_approval_config(self):
        mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        # Default should be True
        self.assertTrue(mgr.is_strict_approval())
        # Toggle to False
        mgr.set_strict_approval(False)
        self.assertFalse(mgr.is_strict_approval())
        # Toggle back to True
        mgr.set_strict_approval(True)
        self.assertTrue(mgr.is_strict_approval())

    def test_runtime_settings_update(self):
        mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        settings = mgr.get_settings()
        self.assertEqual(settings["poll_interval_seconds"], 900)
        self.assertEqual(settings["max_files_limit"], 100)
        self.assertTrue(settings["auto_approve"])
        self.assertTrue(settings["strict_approval"])

        # Update settings at runtime
        updated = mgr.update_settings({
            "poll_interval_seconds": 300,
            "max_files_limit": 50,
            "auto_approve": False,
            "strict_approval": False
        })
        self.assertEqual(updated["poll_interval_seconds"], 300)
        self.assertEqual(updated["max_files_limit"], 50)
        self.assertFalse(updated["auto_approve"])
        self.assertFalse(updated["strict_approval"])

    def test_pr_logs_persistence(self):
        sm = StateManager(state_path=self.state_path)
        pr_key = "owner/repo#42"

        # Record 2 logs
        log_id1 = sm.record_pr_log(pr_key, {
            "status": "COMPLETED",
            "review_outcome": "APPROVED",
            "stdout": "cli output 1",
            "stderr": ""
        })
        log_id2 = sm.record_pr_log(pr_key, {
            "status": "ERROR",
            "review_outcome": "ERROR",
            "stdout": "",
            "stderr": "cli error trace"
        })

        logs = sm.get_pr_logs(pr_key)
        self.assertEqual(len(logs), 2)
        # Most recent first
        self.assertEqual(logs[0]["log_id"], log_id2)
        self.assertEqual(logs[0]["review_outcome"], "ERROR")
        self.assertEqual(logs[1]["log_id"], log_id1)
        self.assertEqual(logs[1]["review_outcome"], "APPROVED")

        # By id lookup
        fetched = sm.get_pr_log_by_id(pr_key, log_id1)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["stdout"], "cli output 1")

    def test_review_queue_persistence(self):
        sm = StateManager(state_path=self.state_path)
        self.assertEqual(sm.get_review_queue(), [])

        queue_items = [
            {"pr_key": "owner/repo#1", "force": False, "queued_at": "2026-09-30T10:00:00Z"},
            {"pr_key": "owner/repo#2", "force": True, "queued_at": "2026-09-30T10:05:00Z"}
        ]
        sm.save_review_queue(queue_items)

        loaded = sm.get_review_queue()
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["pr_key"], "owner/repo#1")
        self.assertFalse(loaded[0]["force"])
        self.assertEqual(loaded[1]["pr_key"], "owner/repo#2")
        self.assertTrue(loaded[1]["force"])

    def test_coderabbit_accounts_crud(self):
        mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        self.assertEqual(mgr.get_coderabbit_accounts(), [])

        # Add account
        acc1 = mgr.add_coderabbit_account({
            "name": "Account 1",
            "type": "api_key",
            "api_key": "cr-secret-1",
            "region": "us"
        })
        self.assertEqual(acc1["name"], "Account 1")
        self.assertEqual(acc1["api_key"], "cr-secret-1")
        self.assertTrue(acc1["enabled"])

        # Add second account with omitted profile_dir
        acc2 = mgr.add_coderabbit_account({
            "name": "Team Devs",
            "type": "profile",
            "region": "eu"
        })
        self.assertEqual(len(mgr.get_coderabbit_accounts()), 2)
        self.assertTrue(os.path.isdir(acc2["profile_dir"]))
        self.assertTrue(acc2["profile_dir"].endswith("team_devs"))

        # Update account
        updated = mgr.update_coderabbit_account(acc1["id"], {"name": "Account 1 Renamed", "enabled": False})
        self.assertEqual(updated["name"], "Account 1 Renamed")
        self.assertFalse(updated["enabled"])

        # Remove account
        removed = mgr.remove_coderabbit_account(acc1["id"])
        self.assertTrue(removed)
        accounts_left = mgr.get_coderabbit_accounts()
        self.assertEqual(len(accounts_left), 1)
        self.assertEqual(accounts_left[0]["id"], acc2["id"])

    def test_account_rate_limiting_and_rotation(self):
        sm = StateManager(state_path=self.state_path)
        accounts = [
            {"id": "acc-1", "name": "Account 1", "enabled": True},
            {"id": "acc-2", "name": "Account 2", "enabled": True},
            {"id": "acc-3", "name": "Account 3", "enabled": False},
        ]

        # Initial selection chooses first enabled
        sel1 = sm.select_next_available_account(accounts)
        self.assertIsNotNone(sel1)
        self.assertEqual(sel1[0]["id"], "acc-1")

        # Next selection rotates to second enabled
        sel2 = sm.select_next_available_account(accounts)
        self.assertIsNotNone(sel2)
        self.assertEqual(sel2[0]["id"], "acc-2")

        # Third selection wraps around to first enabled
        sel3 = sm.select_next_available_account(accounts)
        self.assertIsNotNone(sel3)
        self.assertEqual(sel3[0]["id"], "acc-1")

        # Rate limit acc-1
        sm.record_account_rate_limit("acc-1", 300, reason="Quota reached")
        is_rl, rem, reason = sm.is_account_rate_limited("acc-1")
        self.assertTrue(is_rl)
        self.assertGreater(rem, 200)

        # Now selection must skip acc-1 and pick acc-2
        sel_after_rl = sm.select_next_available_account(accounts)
        self.assertIsNotNone(sel_after_rl)
        self.assertEqual(sel_after_rl[0]["id"], "acc-2")

        # Rate limit acc-2 as well
        sm.record_account_rate_limit("acc-2", 300, reason="Quota reached")

        # All enabled accounts are now rate-limited -> returns None
        sel_all_rl = sm.select_next_available_account(accounts)
        self.assertIsNone(sel_all_rl)

        # Clear cooldown for acc-1
        sm.clear_rate_limit("acc-1")
        self.assertFalse(sm.is_account_rate_limited("acc-1")[0])
        self.assertTrue(sm.is_account_rate_limited("acc-2")[0])

        sel_recovered = sm.select_next_available_account(accounts)
        self.assertIsNotNone(sel_recovered)
        self.assertEqual(sel_recovered[0]["id"], "acc-1")

    def test_profile_auto_discovery(self):
        mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        accounts_dir = mgr.get_accounts_dir()

        # Simulate docker exec creating a profile directory with auth.json
        profile_path = os.path.join(accounts_dir, "work_profile")
        os.makedirs(profile_path, exist_ok=True)
        with open(os.path.join(profile_path, "auth.json"), "w") as f:
            f.write('{"authenticated": true}')

        # get_coderabbit_accounts should discover it automatically
        accounts = mgr.get_coderabbit_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], "profile_work_profile")
        self.assertEqual(accounts[0]["name"], "Work Profile")
        self.assertEqual(accounts[0]["type"], "profile")
        self.assertEqual(accounts[0]["profile_dir"], profile_path)

if __name__ == "__main__":
    unittest.main()




