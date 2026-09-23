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

if __name__ == "__main__":
    unittest.main()

