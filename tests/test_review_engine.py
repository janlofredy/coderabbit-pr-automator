import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config_manager import ConfigManager
from state_manager import StateManager
from github_client import GitHubClient
from auto_review_prs import AutoReviewEngine

class TestReviewEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.state_path = os.path.join(self.test_dir, "automation_state.json")
        self.reviews_dir = os.path.join(self.test_dir, "reviews")
        self.repos_dir = os.path.join(self.test_dir, "repos")

        self.cfg_mgr = ConfigManager(config_path=self.config_path, repos_base_dir=self.repos_dir)
        self.state_mgr = StateManager(state_path=self.state_path)
        self.gh_client = MagicMock(spec=GitHubClient)
        self.gh_client.token = "fake-token"

        self.engine = AutoReviewEngine(
            config_manager=self.cfg_mgr,
            state_manager=self.state_mgr,
            github_client=self.gh_client,
            reviews_dir=self.reviews_dir,
            cli_timeout=30
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_parse_coderabbit_output(self):
        # 1. Standard JSON with findings
        sample_json = '{"findings": [{"file": "src/app.py", "line": 42, "severity": "CRITICAL", "message": "SQL Injection vulnerability"}], "summary": "Identified 1 critical issue."}'
        parsed = self.engine.parse_coderabbit_output(sample_json)
        self.assertEqual(len(parsed["findings"]), 1)
        self.assertEqual(parsed["findings"][0]["severity"], "CRITICAL")

        # 2. Raw JSON list
        sample_list = '[{"file": "index.js", "line": 10, "severity": "INFO", "message": "Code style"}]'
        parsed = self.engine.parse_coderabbit_output(sample_list)
        self.assertEqual(len(parsed["findings"]), 1)

        # 3. Markdown wrapped JSON block
        sample_md = 'Here is your review:\n```json\n{"findings": [], "summary": "Clean code!"}\n```'
        parsed = self.engine.parse_coderabbit_output(sample_md)
        self.assertEqual(len(parsed["findings"]), 0)
        self.assertEqual(parsed["summary"], "Clean code!")

    def test_html_report_generation(self):
        report_file = self.engine.generate_html_report(
            full_name="owner/test-repo",
            pr_number=12,
            commit_sha="a1b2c3d4e5f6",
            base_ref="main",
            head_ref="feature-1",
            findings=[{"file": "main.py", "line": 5, "severity": "MAJOR", "message": "Missing null check"}],
            summary="Found one major issue.",
            outcome="CHANGES_REQUESTED"
        )

        full_path = os.path.join(self.reviews_dir, report_file)
        self.assertTrue(os.path.exists(full_path))
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("owner/test-repo", content)
            self.assertIn("Pull Request #12", content)
            self.assertIn("Missing null check", content)
            self.assertIn("CHANGES_REQUESTED", content)

    def test_100_file_ceiling_guard(self):
        self.gh_client.get_username.return_value = "coderabbit-bot"
        self.gh_client.has_user_reviewed_sha.return_value = (False, None)
        self.gh_client.create_or_update_comment.return_value = {"id": 101}

        repo_info = {"full_name": "owner/huge-repo", "path": os.path.join(self.repos_dir, "owner/huge-repo")}
        pr = {
            "number": 5,
            "title": "Massive refactor",
            "changed_files": 120, # Exceeds 100
            "user": {"login": "contributor"},
            "base": {"ref": "main"},
            "head": {"ref": "big-branch", "sha": "1234567890abcdef"},
            "html_url": "https://github.com/owner/huge-repo/pull/5"
        }

        with patch.object(self.engine, "prepare_repo", return_value=True), \
             patch.object(self.engine, "checkout_pr", return_value=True):
            res = self.engine.review_single_pr(repo_info, pr)

        self.assertEqual(res["status"], "SKIPPED_MAX_FILES")
        self.assertEqual(res["file_count"], 120)
        # Verify explanation comment was posted
        self.gh_client.create_or_update_comment.assert_called_once()
        call_args = self.gh_client.create_or_update_comment.call_args[0]
        self.assertIn("exceeds the CodeRabbit Free Tier ceiling", call_args[3])

    def test_self_approval_prevention(self):
        # Authenticated user is the PR author!
        self.gh_client.get_username.return_value = "my-bot-account"
        self.gh_client.has_user_reviewed_sha.return_value = (False, None)
        self.gh_client.create_or_update_comment.return_value = {"id": 202}
        self.gh_client.submit_pull_request_review.return_value = {"id": 303}

        repo_info = {"full_name": "owner/my-repo", "path": os.path.join(self.repos_dir, "owner/my-repo")}
        pr = {
            "number": 8,
            "title": "Bot automated PR",
            "changed_files": 2,
            "user": {"login": "my-bot-account"}, # Matches authenticated user
            "base": {"ref": "main"},
            "head": {"ref": "bot-update", "sha": "abcdef123456"},
            "html_url": "https://github.com/owner/my-repo/pull/8"
        }

        # Mock successful CodeRabbit execution with 0 critical issues
        with patch.object(self.engine, "prepare_repo", return_value=True), \
             patch.object(self.engine, "checkout_pr", return_value=True), \
             patch.object(self.engine, "count_changed_files", return_value=2), \
             patch.object(self.engine, "get_valid_diff_lines", return_value={}), \
             patch.object(self.engine, "execute_coderabbit_cli", return_value=(0, '{"findings": [], "summary": "Clean"}', "")):

            res = self.engine.review_single_pr(repo_info, pr)

        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(res["is_own_pr"])
        # Crucial: Event MUST NOT be APPROVE for own PR! It should be COMMENT!
        self.assertEqual(res["event"], "COMMENT")
        self.assertIn("Self PR: Commented", res["review_outcome"])
        self.gh_client.submit_pull_request_review.assert_called_once()
        review_call = self.gh_client.submit_pull_request_review.call_args
        self.assertEqual(review_call.kwargs["event"], "COMMENT")

    def test_rate_limit_comment_and_cooldown(self):
        self.gh_client.get_username.return_value = "coderabbit-bot"
        self.gh_client.has_user_reviewed_sha.return_value = (False, None)
        self.gh_client.create_or_update_comment.return_value = {"id": 505}

        repo_info = {"full_name": "owner/my-repo", "path": os.path.join(self.repos_dir, "owner/my-repo")}
        pr = {
            "number": 9,
            "title": "Feature PR",
            "changed_files": 5,
            "user": {"login": "dev1"},
            "base": {"ref": "main"},
            "head": {"ref": "feature", "sha": "99887766"},
            "html_url": "https://github.com/owner/my-repo/pull/9"
        }

        # Mock CodeRabbit returning 429 rate limit
        rate_limit_cli_output = "Error: 429 Too Many Requests. CodeRabbit Free Tier quota reached. Please try again in 20 mins."

        with patch.object(self.engine, "prepare_repo", return_value=True), \
             patch.object(self.engine, "checkout_pr", return_value=True), \
             patch.object(self.engine, "count_changed_files", return_value=5), \
             patch.object(self.engine, "execute_coderabbit_cli", return_value=(1, "", rate_limit_cli_output)):

            res = self.engine.review_single_pr(repo_info, pr)

        self.assertEqual(res["status"], "RATE_LIMITED")
        self.assertEqual(res["retry_delay_seconds"], 1200) # 20 mins = 1200s
        self.assertEqual(res["attempt"], 1)

        # Check that state manager recorded the rate limit
        is_active, remaining, reason = self.state_mgr.is_rate_limit_active()
        self.assertTrue(is_active)
        self.assertGreater(remaining, 1100)

        # Verify rate limit comment format
        comment_calls = self.gh_client.create_or_update_comment.call_args_list
        # Second call should be the rate-limit PATCH
        rate_limit_call = comment_calls[-1]
        body = rate_limit_call[0][3]
        self.assertIn("CodeRabbit Free Tier Rate Limit Active", body)
        self.assertIn("Will retry in", body)
        self.assertIn("Attempt**: 1 of 3", body)

    def test_strict_approval_blocks_approval_on_minor_issue(self):
        self.gh_client.get_username.return_value = "coderabbit-bot"
        self.gh_client.has_user_reviewed_sha.return_value = (False, None)
        self.gh_client.create_or_update_comment.return_value = {"id": 601}
        self.gh_client.submit_pull_request_review.return_value = {"id": 602}

        repo_info = {"full_name": "owner/my-repo", "path": os.path.join(self.repos_dir, "owner/my-repo")}
        pr = {
            "number": 11,
            "title": "Minor styling PR",
            "changed_files": 2,
            "user": {"login": "contributor"},
            "base": {"ref": "main"},
            "head": {"ref": "patch-1", "sha": "11223344"},
            "html_url": "https://github.com/owner/my-repo/pull/11"
        }

        # Minor finding
        minor_output = '{"findings": [{"file": "main.py", "line": 10, "severity": "MINOR", "message": "Variable name could be more descriptive"}], "summary": "One minor improvement suggestion."}'

        with patch.object(self.engine, "prepare_repo", return_value=True), \
             patch.object(self.engine, "checkout_pr", return_value=True), \
             patch.object(self.engine, "count_changed_files", return_value=2), \
             patch.object(self.engine, "get_valid_diff_lines", return_value={"main.py": [10]}), \
             patch.object(self.engine, "execute_coderabbit_cli", return_value=(0, minor_output, "")):

            res = self.engine.review_single_pr(repo_info, pr)

        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["event"], "COMMENT")
        self.assertEqual(res["review_outcome"], "NEEDS_WORK (Minor Issues Detected)")
        self.assertEqual(res["critical_major_count"], 0)
        self.assertEqual(res["minor_count"], 1)

        # Check review call on GitHub client
        review_call = self.gh_client.submit_pull_request_review.call_args
        self.assertEqual(review_call.kwargs["event"], "COMMENT")
        self.assertIn("NEEDS_WORK (Minor Issues Detected)", review_call.kwargs["body"])

    def test_strict_approval_allows_approval_when_disabled(self):
        self.gh_client.get_username.return_value = "coderabbit-bot"
        self.gh_client.has_user_reviewed_sha.return_value = (False, None)
        self.gh_client.create_or_update_comment.return_value = {"id": 701}
        self.gh_client.submit_pull_request_review.return_value = {"id": 702}

        # Disable strict approval in config
        self.cfg_mgr.set_strict_approval(False)

        repo_info = {"full_name": "owner/my-repo", "path": os.path.join(self.repos_dir, "owner/my-repo")}
        pr = {
            "number": 12,
            "title": "Minor styling PR",
            "changed_files": 2,
            "user": {"login": "contributor"},
            "base": {"ref": "main"},
            "head": {"ref": "patch-2", "sha": "55667788"},
            "html_url": "https://github.com/owner/my-repo/pull/12"
        }

        minor_output = '{"findings": [{"file": "main.py", "line": 10, "severity": "WARNING", "message": "Minor style issue"}], "summary": "Minor warning."}'

        with patch.object(self.engine, "prepare_repo", return_value=True), \
             patch.object(self.engine, "checkout_pr", return_value=True), \
             patch.object(self.engine, "count_changed_files", return_value=2), \
             patch.object(self.engine, "get_valid_diff_lines", return_value={}), \
             patch.object(self.engine, "execute_coderabbit_cli", return_value=(0, minor_output, "")):

            res = self.engine.review_single_pr(repo_info, pr)

        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["event"], "APPROVE")
        self.assertEqual(res["review_outcome"], "APPROVED")
        self.assertEqual(res["minor_count"], 1)

    def test_clean_pr_approved_in_strict_mode(self):
        self.gh_client.get_username.return_value = "coderabbit-bot"
        self.gh_client.has_user_reviewed_sha.return_value = (False, None)
        self.gh_client.create_or_update_comment.return_value = {"id": 801}
        self.gh_client.submit_pull_request_review.return_value = {"id": 802}

        self.cfg_mgr.set_strict_approval(True)

        repo_info = {"full_name": "owner/my-repo", "path": os.path.join(self.repos_dir, "owner/my-repo")}
        pr = {
            "number": 13,
            "title": "Perfect clean PR",
            "changed_files": 1,
            "user": {"login": "contributor"},
            "base": {"ref": "main"},
            "head": {"ref": "perfect-feature", "sha": "aabbccdd"},
            "html_url": "https://github.com/owner/my-repo/pull/13"
        }

        clean_output = '{"findings": [], "summary": "Code looks spotless!"}'

        with patch.object(self.engine, "prepare_repo", return_value=True), \
             patch.object(self.engine, "checkout_pr", return_value=True), \
             patch.object(self.engine, "count_changed_files", return_value=1), \
             patch.object(self.engine, "get_valid_diff_lines", return_value={}), \
             patch.object(self.engine, "execute_coderabbit_cli", return_value=(0, clean_output, "")):

            res = self.engine.review_single_pr(repo_info, pr)

        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["event"], "APPROVE")
        self.assertEqual(res["review_outcome"], "APPROVED")
        self.assertEqual(res["critical_major_count"], 0)
        self.assertEqual(res["minor_count"], 0)

    def test_already_reviewed_detection_via_comment_history(self):
        real_gh_client = GitHubClient(token="fake-token")
        # Mock get_reviews_for_pr returning empty (no formal review)
        real_gh_client.get_reviews_for_pr = MagicMock(return_value=[])
        # Mock get_comments_for_pr returning existing completed status comment
        real_gh_client.get_comments_for_pr = MagicMock(return_value=[
            {
                "user": {"login": "coderabbit-bot"},
                "body": """🐰 **Automated CodeRabbit Review Completed**
- **Reviewer**: @coderabbit-bot
- **Target Base Branch**: `main`
- **Head Branch**: `feature-branch` (`112233445566`)
- **Status**: APPROVED (0 critical/major findings)
---
Review complete. Detailed findings submitted directly to this pull request."""
            }
        ])

        has_reviewed, outcome = real_gh_client.has_user_reviewed_sha(
            owner="owner",
            repo="repo",
            pr_number=50,
            commit_sha="1122334455667788",
            username="coderabbit-bot"
        )
        self.assertTrue(has_reviewed)
        self.assertEqual(outcome, "APPROVED")

    def test_cli_uses_api_key_from_config(self):
        self.cfg_mgr.set_coderabbit_api_key("test-cr-api-key-999")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"findings": []}', stderr="")
            retcode, stdout, stderr = self.engine.execute_coderabbit_cli("/fake/path", "main")
            self.assertEqual(retcode, 0)
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            self.assertIn("env", call_kwargs)
            self.assertEqual(call_kwargs["env"].get("CODERABBIT_API_KEY"), "test-cr-api-key-999")

if __name__ == "__main__":
    unittest.main()


