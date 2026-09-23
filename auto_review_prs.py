import os
import re
import json
import time
import subprocess
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from config_manager import ConfigManager
from state_manager import StateManager
from github_client import GitHubClient, GitHubAPIException

logger = logging.getLogger("auto_review")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DEFAULT_REVIEWS_DIR = os.getenv("REVIEWS_DIR", os.path.expanduser("~/.coderabbit/reviews"))
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("CODERABBIT_TIMEOUT", "240"))

class AutoReviewEngine:
    """Core review engine coordinating Git sync, CodeRabbit CLI runs, and GitHub PR reviews."""

    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        state_manager: Optional[StateManager] = None,
        github_client: Optional[GitHubClient] = None,
        reviews_dir: str = DEFAULT_REVIEWS_DIR,
        cli_timeout: int = DEFAULT_TIMEOUT_SECONDS
    ):
        self.config_manager = config_manager or ConfigManager()
        self.state_manager = state_manager or StateManager()
        self.github_client = github_client or GitHubClient()
        self.reviews_dir = reviews_dir
        self.cli_timeout = cli_timeout
        os.makedirs(self.reviews_dir, exist_ok=True)

    def run_git(self, repo_dir: str, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Executes a git command inside the target repo directory."""
        cmd = ["git"] + args
        return subprocess.run(
            cmd,
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=check
        )

    def prepare_repo(self, full_name: str, repo_path: str) -> bool:
        """Clones or updates the local git clone for the repository."""
        os.makedirs(os.path.dirname(os.path.abspath(repo_path)), exist_ok=True)
        token = self.github_client.token

        if token:
            clone_url = f"https://x-access-token:{token}@github.com/{full_name}.git"
        else:
            clone_url = f"https://github.com/{full_name}.git"

        if not os.path.exists(os.path.join(repo_path, ".git")):
            logger.info("Cloning %s into %s...", full_name, repo_path)
            res = subprocess.run(
                ["git", "clone", clone_url, repo_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if res.returncode != 0:
                logger.error("Failed to clone %s: %s", full_name, res.stderr)
                return False
        else:
            # Update remote URL and fetch latest
            subprocess.run(["git", "remote", "set-url", "origin", clone_url], cwd=repo_path, capture_output=True)
            res = subprocess.run(["git", "fetch", "--all", "--prune"], cwd=repo_path, capture_output=True, text=True)
            if res.returncode != 0:
                logger.error("Failed to fetch %s: %s", full_name, res.stderr)
                return False

        return True

    def checkout_pr(self, repo_path: str, pr_number: int, base_ref: str) -> bool:
        """Fetches the PR head ref and checks it out locally."""
        try:
            # Fetch base branch
            self.run_git(repo_path, ["fetch", "origin", base_ref])
            # Fetch PR branch ref
            self.run_git(repo_path, ["fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}", "--force"])
            # Checkout PR branch
            self.run_git(repo_path, ["checkout", f"pr-{pr_number}", "--force"])
            # Clean untracked files
            self.run_git(repo_path, ["clean", "-fd"])
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Git error checking out PR #%s in %s: %s", pr_number, repo_path, e.stderr)
            return False

    def count_changed_files(self, repo_path: str, base_ref: str, pr_number: int) -> int:
        """Counts modified files between base and PR branch."""
        try:
            res = self.run_git(repo_path, ["diff", "--name-only", f"origin/{base_ref}...pr-{pr_number}"])
            files = [line.strip() for line in res.stdout.strip().split("\n") if line.strip()]
            return len(files)
        except Exception as e:
            logger.warning("Could not count changed files via git diff: %s", e)
            return 0

    def get_valid_diff_lines(self, repo_path: str, base_ref: str, pr_number: int) -> Dict[str, set]:
        """
        Parses `git diff -U0` to obtain valid added/modified line numbers per file.
        Returns a dict mapping file_path -> set of line numbers on the 'new' side (RIGHT side).
        """
        valid_lines: Dict[str, set] = {}
        try:
            res = self.run_git(repo_path, ["diff", "-U0", f"origin/{base_ref}...pr-{pr_number}"])
            current_file = None
            diff_pattern = re.compile(r"^\@\@\s+-[0-9]+(?:,[0-9]+)?\s+\+([0-9]+)(?:,([0-9]+))?\s+\@\@")

            for line in res.stdout.splitlines():
                if line.startswith("+++ b/"):
                    current_file = line[6:].strip()
                    if current_file not in valid_lines:
                        valid_lines[current_file] = set()
                elif line.startswith("@@") and current_file:
                    match = diff_pattern.match(line)
                    if match:
                        start_line = int(match.group(1))
                        count = int(match.group(2)) if match.group(2) else 1
                        for l in range(start_line, start_line + count):
                            valid_lines[current_file].add(l)
        except Exception as e:
            logger.warning("Could not calculate diff lines: %s", e)

        return valid_lines

    def execute_coderabbit_cli(self, repo_path: str, base_ref: str) -> Tuple[int, str, str]:
        """
        Executes CodeRabbit CLI in agent mode.
        Returns: (returncode, stdout, stderr)
        """
        cmd = ["coderabbit", "review", "--agent", "--base", base_ref]
        logger.info("Executing CodeRabbit CLI: %s in %s", " ".join(cmd), repo_path)
        try:
            res = subprocess.run(
                cmd,
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.cli_timeout
            )
            return res.returncode, res.stdout, res.stderr
        except subprocess.TimeoutExpired as e:
            logger.error("CodeRabbit CLI timed out after %ds", self.cli_timeout)
            return -1, e.stdout or "", f"TimeoutExpired: Review exceeded {self.cli_timeout} seconds"
        except FileNotFoundError:
            logger.error("CodeRabbit CLI binary 'coderabbit' not found in PATH")
            return 127, "", "CodeRabbit CLI ('coderabbit') is not installed or not in PATH"

    def parse_coderabbit_output(self, stdout: str) -> Dict[str, Any]:
        """Parses structured JSON from CodeRabbit agent output."""
        if not stdout.strip():
            return {"findings": [], "summary": "No output from CodeRabbit CLI", "raw": ""}

        # Attempt direct JSON parse
        try:
            data = json.loads(stdout.strip())
            if isinstance(data, dict):
                return data
            elif isinstance(data, list):
                return {"findings": data, "summary": "CodeRabbit review completed"}
        except json.JSONDecodeError:
            pass

        # Attempt to extract JSON block enclosed in markdown or stdout
        json_match = re.search(r"(\{.*\}|\[.*\])", stdout, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                if isinstance(data, dict):
                    return data
                elif isinstance(data, list):
                    return {"findings": data, "summary": "CodeRabbit review completed"}
            except Exception:
                pass

        return {
            "findings": [],
            "summary": stdout.strip(),
            "raw": stdout
        }

    def generate_html_report(
        self,
        full_name: str,
        pr_number: int,
        commit_sha: str,
        base_ref: str,
        head_ref: str,
        findings: List[Dict[str, Any]],
        summary: str,
        outcome: str
    ) -> str:
        """Generates and writes an HTML report for the review."""
        safe_repo = full_name.replace("/", "_")
        filename = f"{safe_repo}_pr{pr_number}_{commit_sha[:8]}.html"
        report_path = os.path.join(self.reviews_dir, filename)

        findings_html = ""
        for idx, f in enumerate(findings, 1):
            severity = f.get("severity", "INFO").upper()
            color = "#ef4444" if severity in ("CRITICAL", "MAJOR", "ERROR") else ("#f59e0b" if severity in ("WARNING", "WARN") else "#3b82f6")
            file_path = f.get("file", f.get("path", "unknown"))
            line_no = f.get("line", "N/A")
            msg = f.get("message", f.get("description", str(f)))

            findings_html += f"""
            <div style="border: 1px solid #334155; border-radius: 8px; margin-bottom: 12px; padding: 12px; background: #1e293b;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <span style="background: {color}; color: #ffffff; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 12px;">{severity}</span>
                    <span style="font-family: monospace; color: #94a3b8; font-size: 13px;">{file_path}:{line_no}</span>
                </div>
                <div style="color: #e2e8f0; font-size: 14px; white-space: pre-wrap;">{msg}</div>
            </div>
            """

        if not findings_html:
            findings_html = """
            <div style="padding: 24px; text-align: center; color: #10b981; background: #064e3b22; border-radius: 8px; border: 1px solid #059669;">
                <h3>🎉 No Issues Detected</h3>
                <p>CodeRabbit verified the pull request with 0 critical or major findings.</p>
            </div>
            """

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>CodeRabbit Review Report - {full_name} #{pr_number}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 24px; margin: 0; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        .header {{ border-bottom: 1px solid #334155; padding-bottom: 16px; margin-bottom: 24px; }}
        .badge {{ padding: 4px 10px; border-radius: 6px; font-size: 13px; font-weight: bold; }}
        .approved {{ background: #059669; color: #ffffff; }}
        .changes {{ background: #dc2626; color: #ffffff; }}
        .comment {{ background: #475569; color: #ffffff; }}
        .meta-table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
        .meta-table td {{ padding: 6px 12px; border-bottom: 1px solid #1e293b; color: #94a3b8; font-size: 14px; }}
        .meta-table td strong {{ color: #e2e8f0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 style="margin: 0 0 8px 0;">🐰 CodeRabbit Review Report</h1>
            <h2 style="margin: 0; color: #38bdf8; font-size: 18px;">{full_name} &bull; Pull Request #{pr_number}</h2>
        </div>

        <table class="meta-table">
            <tr><td><strong>Target Base Branch:</strong> {base_ref}</td><td><strong>Head Commit:</strong> <code>{commit_sha}</code></td></tr>
            <tr><td><strong>Head Branch:</strong> {head_ref}</td><td><strong>Outcome:</strong> <span class="badge {outcome.lower()}">{outcome}</span></td></tr>
            <tr><td colspan="2"><strong>Generated At:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</td></tr>
        </table>

        <h3>Summary</h3>
        <div style="background: #1e293b; padding: 16px; border-radius: 8px; margin-bottom: 24px; color: #cbd5e1; line-height: 1.5;">
            {summary or "Automated review completed via CodeRabbit CLI."}
        </div>

        <h3>Findings ({len(findings)})</h3>
        {findings_html}
    </div>
</body>
</html>
"""
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        return filename

    def review_single_pr(self, repo_info: Dict[str, Any], pr: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """
        Executes the full review workflow for a single pull request.
        """
        full_name = repo_info["full_name"]
        repo_path = repo_info["path"]
        owner, repo_name = GitHubClient.split_repo(full_name)
        pr_number = pr["number"]
        pr_key = f"{full_name}#{pr_number}"
        head_sha = pr["head"]["sha"]
        base_ref = pr["base"]["ref"]
        head_ref = pr["head"]["ref"]
        pr_author = (pr.get("user") or {}).get("login", "")
        auth_user = self.github_client.get_username()
        is_own_pr = bool(auth_user and pr_author.lower() == auth_user.lower())

        config = self.config_manager.load_config()
        max_files_limit = config.get("max_files_limit", 100)
        auto_approve = config.get("auto_approve", True)

        # Check rate limit cooldown unless force is requested
        if not force:
            is_rate_limited, remaining, reason = self.state_manager.is_rate_limit_active()
            if is_rate_limited:
                logger.info("Skipping review of %s: Rate limit cooldown active (%ds remaining)", pr_key, remaining)
                return {
                    "pr_key": pr_key,
                    "status": "RATE_LIMITED",
                    "cooldown_remaining": remaining,
                    "reason": reason
                }

        # Check if already reviewed on this SHA
        review_check = self.github_client.has_user_reviewed_sha(owner, repo_name, pr_number, head_sha, auth_user)
        if isinstance(review_check, (tuple, list)) and len(review_check) == 2:
            has_reviewed, review_state = review_check
        else:
            has_reviewed, review_state = False, None
        if has_reviewed and not force:
            logger.info("PR %s already reviewed on SHA %s (State: %s)", pr_key, head_sha[:8], review_state)
            status_data = {
                "pr_key": pr_key,
                "status": "ALREADY_REVIEWED",
                "review_state": review_state,
                "head_sha": head_sha,
                "title": pr.get("title", ""),
                "html_url": pr.get("html_url", ""),
                "author": pr_author,
                "is_own_pr": is_own_pr
            }
            self.state_manager.record_pr_status(pr_key, status_data)
            return status_data

        # Prepare repository clone
        if not self.prepare_repo(full_name, repo_path):
            return {"pr_key": pr_key, "status": "ERROR", "error": "Failed to prepare repository"}

        # Checkout PR
        if not self.checkout_pr(repo_path, pr_number, base_ref):
            return {"pr_key": pr_key, "status": "ERROR", "error": "Failed to checkout PR branch"}

        # 100-File Free-Tier Ceiling Guard
        changed_files_count = self.count_changed_files(repo_path, base_ref, pr_number)
        api_files_count = pr.get("changed_files", 0)
        file_count = max(changed_files_count, api_files_count)

        if file_count > max_files_limit:
            logger.warning("PR %s has %d files (limit %d). Skipping review.", pr_key, file_count, max_files_limit)
            skip_comment = f"""🐰 **Automated CodeRabbit Review Skipped**
- **Reviewer**: @{auth_user or 'coderabbit-bot'}
- **Target Base Branch**: `{base_ref}`
- **Head Branch**: `{head_ref}` (`{head_sha[:8]}`)
---
> 🛑 **Review Skipped**: The pull request modifies **{file_count} files**, which exceeds the CodeRabbit Free Tier ceiling of **{max_files_limit} files**.
>
> **Action**: Please split this pull request into smaller, focused changes or review manually.
"""
            self.github_client.create_or_update_comment(owner, repo_name, pr_number, skip_comment)
            status_data = {
                "pr_key": pr_key,
                "status": "SKIPPED_MAX_FILES",
                "file_count": file_count,
                "limit": max_files_limit,
                "head_sha": head_sha,
                "title": pr.get("title", ""),
                "html_url": pr.get("html_url", ""),
                "author": pr_author,
                "is_own_pr": is_own_pr
            }
            self.state_manager.record_pr_status(pr_key, status_data)
            return status_data

        # Post or update start comment to notify progress
        attempt = self.state_manager.get_attempt_count(pr_key) + 1
        self.state_manager.set_pr_reviewing(pr_key, True, attempt)

        in_progress_comment = f"""🐰 **Automated CodeRabbit Review In Progress**
- **Reviewer**: @{auth_user or 'coderabbit-bot'}
- **Target Base Branch**: `{base_ref}`
- **Head Branch**: `{head_ref}` (`{head_sha[:8]}`)
---
⚡ **Status**: Running local CodeRabbit CLI review (Attempt {attempt} of 3)...
"""
        bot_comment = self.github_client.create_or_update_comment(owner, repo_name, pr_number, in_progress_comment)
        comment_id = bot_comment.get("id")

        # Run CodeRabbit CLI
        start_time = time.time()
        retcode, stdout, stderr = self.execute_coderabbit_cli(repo_path, base_ref)
        elapsed = time.time() - start_time
        combined_output = f"{stdout}\n{stderr}"

        # Rate Limit / Quota / Timeout Detection
        retry_delay = self.state_manager.parse_retry_delay_from_text(combined_output)
        is_timeout = retcode == -1 or "TimeoutExpired" in stderr

        if retry_delay > 0 or is_timeout:
            delay_sec = retry_delay if retry_delay > 0 else 900
            mins = max(1, delay_sec // 60)
            resume_time = (datetime.now(timezone.utc) + timedelta(seconds=delay_sec)).strftime("%H:%M:%S UTC")
            reason_msg = "CLI Execution Timeout (240s)" if is_timeout else "Free Tier request quota / rate limit reached"

            logger.warning("CodeRabbit rate limit or timeout triggered on %s: delay %ds (%s)", pr_key, delay_sec, reason_msg)
            self.state_manager.record_rate_limit(delay_sec, reason_msg, pr_key)
            self.state_manager.set_pr_reviewing(pr_key, False)

            rate_limit_comment = f"""🐰 **Automated CodeRabbit Review In Progress**
- **Reviewer**: @{auth_user or 'coderabbit-bot'}
- **Target Base Branch**: `{base_ref}`
- **Head Branch**: `{head_ref}` (`{head_sha[:8]}`)
---
### ⏳ CodeRabbit Free Tier Rate Limit Active
> ⚠️ **CodeRabbit Free Tier request quota / rate limit reached**
> - **Will retry in**: **~{mins} mins** (at `{resume_time}`)
> - **Attempt**: {attempt} of 3
"""
            self.github_client.create_or_update_comment(owner, repo_name, pr_number, rate_limit_comment, comment_id)

            status_data = {
                "pr_key": pr_key,
                "status": "RATE_LIMITED",
                "retry_delay_seconds": delay_sec,
                "retry_at": resume_time,
                "attempt": attempt,
                "head_sha": head_sha,
                "title": pr.get("title", ""),
                "html_url": pr.get("html_url", ""),
                "author": pr_author,
                "is_own_pr": is_own_pr
            }
            self.state_manager.record_pr_status(pr_key, status_data)
            return status_data

        # If CodeRabbit returned an error not related to rate limits
        if retcode != 0 and not stdout.strip():
            logger.error("CodeRabbit review failed with exit code %s: %s", retcode, stderr)
            self.state_manager.set_pr_reviewing(pr_key, False)
            err_comment = f"""🐰 **Automated CodeRabbit Review Failed**
- **Reviewer**: @{auth_user or 'coderabbit-bot'}
- **Target Base Branch**: `{base_ref}`
- **Head Branch**: `{head_ref}` (`{head_sha[:8]}`)
---
> ❌ **Error during CodeRabbit CLI execution** (Exit code: `{retcode}`):
```
{stderr[:600]}
```
"""
            self.github_client.create_or_update_comment(owner, repo_name, pr_number, err_comment, comment_id)
            status_data = {
                "pr_key": pr_key,
                "status": "ERROR",
                "error": stderr,
                "head_sha": head_sha,
                "title": pr.get("title", ""),
                "html_url": pr.get("html_url", ""),
                "author": pr_author,
                "is_own_pr": is_own_pr
            }
            self.state_manager.record_pr_status(pr_key, status_data)
            return status_data

        # Parse findings
        parsed = self.parse_coderabbit_output(stdout)
        raw_findings = parsed.get("findings", [])
        if isinstance(raw_findings, dict):
            findings_list = list(raw_findings.values())
        elif isinstance(raw_findings, list):
            findings_list = raw_findings
        else:
            findings_list = []

        summary_text = parsed.get("summary", "Review completed successfully.")

        # Check line offsets against diff
        valid_lines_by_file = self.get_valid_diff_lines(repo_path, base_ref, pr_number)
        line_comments = []
        critical_major_count = 0

        for f in findings_list:
            if not isinstance(f, dict):
                continue

            file_path = f.get("file", f.get("path", ""))
            line_no = f.get("line")
            severity = str(f.get("severity", "INFO")).upper()
            msg = f.get("message", f.get("description", str(f)))

            if severity in ("CRITICAL", "MAJOR", "ERROR"):
                critical_major_count += 1

            if file_path and line_no is not None:
                try:
                    line_int = int(line_no)
                    # Check if line exists in valid diff lines
                    if file_path in valid_lines_by_file and line_int in valid_lines_by_file[file_path]:
                        line_comments.append({
                            "path": file_path,
                            "line": line_int,
                            "side": "RIGHT",
                            "body": f"🐰 **CodeRabbit [{severity}]**: {msg}"
                        })
                except (ValueError, TypeError):
                    pass

        # Determine Review Event
        if critical_major_count > 0:
            if is_own_pr:
                # Self-Approval / Request Changes Prevention Guard on own PR
                event = "COMMENT"
                review_outcome = "CHANGES_REQUESTED (Self PR: Commented)"
            else:
                event = "REQUEST_CHANGES"
                review_outcome = "CHANGES_REQUESTED"
        else:
            if auto_approve and not is_own_pr:
                event = "APPROVE"
                review_outcome = "APPROVED"
            else:
                event = "COMMENT"
                review_outcome = "APPROVED (Self PR: Commented)" if is_own_pr else "COMMENTED"

        # Generate HTML report
        report_file = self.generate_html_report(
            full_name,
            pr_number,
            head_sha,
            base_ref,
            head_ref,
            findings_list,
            summary_text,
            review_outcome
        )

        # Build GitHub review body
        review_body = f"""## 🐰 CodeRabbit Automated Review

- **Review Outcome**: `{review_outcome}`
- **Files Modified**: {file_count}
- **Critical / Major Issues**: {critical_major_count}
- **Total Findings**: {len(findings_list)}
- **Execution Time**: {elapsed:.1f}s

{summary_text}
"""
        # Submit GitHub PR Review
        try:
            self.github_client.submit_pull_request_review(
                owner=owner,
                repo=repo_name,
                pr_number=pr_number,
                commit_sha=head_sha,
                event=event,
                body=review_body,
                comments=line_comments if line_comments else None
            )
            logger.info("Submitted %s review on %s (PR #%s)", event, full_name, pr_number)
        except Exception as e:
            logger.error("Failed to submit PR review on %s: %s", pr_key, e)

        # Update bot status comment to Completed
        completed_comment = f"""🐰 **Automated CodeRabbit Review Completed**
- **Reviewer**: @{auth_user or 'coderabbit-bot'}
- **Target Base Branch**: `{base_ref}`
- **Head Branch**: `{head_ref}` (`{head_sha[:8]}`)
- **Status**: {review_outcome} ({critical_major_count} critical/major findings)
---
Review complete. Detailed findings submitted directly to this pull request.
"""
        self.github_client.create_or_update_comment(owner, repo_name, pr_number, completed_comment, comment_id)

        # Clear active review and reset attempt
        self.state_manager.set_pr_reviewing(pr_key, False)
        self.state_manager.reset_attempt_count(pr_key)

        status_data = {
            "pr_key": pr_key,
            "status": "COMPLETED",
            "review_outcome": review_outcome,
            "event": event,
            "findings_count": len(findings_list),
            "critical_major_count": critical_major_count,
            "report_file": report_file,
            "head_sha": head_sha,
            "title": pr.get("title", ""),
            "html_url": pr.get("html_url", ""),
            "author": pr_author,
            "is_own_pr": is_own_pr,
            "elapsed_seconds": round(elapsed, 1)
        }
        self.state_manager.record_pr_status(pr_key, status_data)
        return status_data

    def scan_and_review_all(self, force: bool = False) -> List[Dict[str, Any]]:
        """Scans all enabled repositories and reviews open pull requests."""
        if not self.config_manager.is_service_enabled() and not force:
            logger.info("Review service is currently disabled in config.")
            return []

        enabled_repos = self.config_manager.get_enabled_repos()
        results = []

        for repo_info in enabled_repos:
            full_name = repo_info.get("full_name")
            if not full_name:
                continue

            owner, repo_name = GitHubClient.split_repo(full_name)
            try:
                prs = self.github_client.list_open_prs(owner, repo_name)
                logger.info("Found %d open PR(s) for %s", len(prs), full_name)
                for pr in prs:
                    res = self.review_single_pr(repo_info, pr, force=force)
                    results.append(res)
            except Exception as e:
                logger.error("Error processing repository %s: %s", full_name, e)

        self.state_manager.record_last_run()
        return results
