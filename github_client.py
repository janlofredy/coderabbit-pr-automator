import os
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("github_client")

GITHUB_API_BASE = "https://api.github.com"

class GitHubClient:
    """GitHub REST API Client using Python standard library."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self._current_user = None

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "CodeRabbit-AutoReview-Service/1.0",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Any:
        url = endpoint if endpoint.startswith("http") else f"{GITHUB_API_BASE}{endpoint}"
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        headers = self._headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode("utf-8")
                if resp_body:
                    return json.loads(resp_body)
                return {}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            logger.error("GitHub API error %s on %s %s: %s", e.code, method, url, error_body)
            try:
                err_json = json.loads(error_body)
            except Exception:
                err_json = {"message": error_body}
            raise GitHubAPIException(e.code, err_json.get("message", error_body), err_json) from e
        except urllib.error.URLError as e:
            logger.error("Network error accessing GitHub API on %s %s: %s", method, url, e)
            raise GitHubAPIException(0, str(e.reason), {}) from e

    def get_authenticated_user(self) -> Dict[str, Any]:
        """Gets profile for the authenticated GitHub user."""
        if not self._current_user:
            self._current_user = self._request("GET", "/user")
        return self._current_user

    def get_username(self) -> str:
        """Returns the username of the authenticated user or empty string."""
        try:
            user = self.get_authenticated_user()
            return user.get("login", "")
        except Exception as e:
            logger.warning("Could not fetch authenticated username: %s", e)
            return ""

    def validate_repo(self, full_name: str) -> bool:
        """Validates that a repository exists and is accessible."""
        owner, repo = self.split_repo(full_name)
        try:
            self._request("GET", f"/repos/{owner}/{repo}")
            return True
        except Exception:
            return False

    def list_open_prs(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """Lists open pull requests for a repository."""
        return self._request("GET", f"/repos/{owner}/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=50")

    def get_pr(self, owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
        """Retrieves detailed information for a single pull request."""
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")

    def get_reviews_for_pr(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Retrieves reviews submitted on a pull request."""
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews")

    def get_comments_for_pr(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Retrieves conversation issue comments on a pull request."""
        return self._request("GET", f"/repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=100")

    def get_check_runs_summary(self, owner: str, repo: str, commit_sha: str) -> Dict[str, Any]:
        """
        Retrieves CI check runs for a commit to detect failures/errors.
        Returns: { 'has_check_error': bool, 'failed_checks': List[str] }
        """
        if not commit_sha:
            return {"has_check_error": False, "failed_checks": []}
        try:
            res = self._request("GET", f"/repos/{owner}/{repo}/commits/{commit_sha}/check-runs")
            check_runs = res.get("check_runs", []) if isinstance(res, dict) else []
            failed = []
            for cr in check_runs:
                conclusion = (cr.get("conclusion") or "").lower()
                name = cr.get("name", "Unknown Check")
                if conclusion in ("failure", "timed_out", "action_required", "cancelled", "startup_failure"):
                    failed.append(name)
            return {
                "has_check_error": len(failed) > 0,
                "failed_checks": failed
            }
        except Exception as e:
            logger.debug("Could not fetch check-runs for %s/%s@%s: %s", owner, repo, commit_sha[:8], e)
            return {"has_check_error": False, "failed_checks": []}

    def get_pr_mergeable_status(self, owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
        """
        Retrieves mergeable status for a pull request.
        Returns: { 'has_conflict': bool, 'mergeable_state': str }
        """
        try:
            pr = self.get_pr(owner, repo, pr_number)
            mergeable = pr.get("mergeable")
            mergeable_state = (pr.get("mergeable_state") or "").lower()

            # mergeable is False or mergeable_state is dirty/conflicting
            has_conflict = (mergeable is False) or (mergeable_state in ("dirty", "conflicting"))
            return {
                "has_conflict": has_conflict,
                "mergeable_state": mergeable_state
            }
        except Exception as e:
            logger.debug("Could not fetch mergeable status for %s/%s PR #%s: %s", owner, repo, pr_number, e)
            return {"has_conflict": False, "mergeable_state": "unknown"}

    def has_user_reviewed_sha(self, owner: str, repo: str, pr_number: int, commit_sha: str, username: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        Checks if the PR has already been automatically reviewed by CodeRabbit on the given commit SHA.
        Returns (has_reviewed, review_state).
        Only returns True for automated CodeRabbit reviews so manual reviews don't prevent automatic review runs.
        """
        user = username or self.get_username()
        short_sha = commit_sha[:8] if commit_sha else ""
        short7_sha = commit_sha[:7] if commit_sha else ""

        # 1. Check formal reviews for automated CodeRabbit signature
        try:
            reviews = self.get_reviews_for_pr(owner, repo, pr_number)
            for r in reversed(reviews):
                reviewer = (r.get("user") or {}).get("login", "")
                r_commit = r.get("commit_id", "")
                state = r.get("state", "").upper()
                body = r.get("body", "")

                commit_matched = (r_commit == commit_sha) or (short_sha and r_commit.startswith(short_sha))
                reviewer_matched = bool(user and reviewer.lower() == user.lower())

                if commit_matched and reviewer_matched:
                    is_cr_auto = ("🐰" in body) or ("CodeRabbit" in body)
                    if is_cr_auto:
                        if state in ("APPROVED", "CHANGES_REQUESTED"):
                            return True, state
                        elif state == "COMMENTED":
                            outcome = "COMMENTED"
                            if "NEEDS_WORK" in body:
                                outcome = "NEEDS_WORK (Minor Issues Detected)"
                            elif "CHANGES_REQUESTED" in body:
                                outcome = "CHANGES_REQUESTED"
                            elif "APPROVED" in body:
                                outcome = "APPROVED"
                            return True, outcome
        except Exception as e:
            logger.warning("Error fetching reviews for %s/%s PR #%s: %s", owner, repo, pr_number, e)

        # 2. Check PR comment history for automated CodeRabbit comments
        try:
            comments = self.get_comments_for_pr(owner, repo, pr_number)
            for comment in reversed(comments):
                author = (comment.get("user") or {}).get("login", "")
                body = comment.get("body", "")

                is_bot_author = bool(user and author.lower() == user.lower())
                is_coderabbit_author = "coderabbit" in author.lower()
                has_review_signature = (
                    "🐰 **Automated CodeRabbit Review Completed**" in body or
                    "🐰 CodeRabbit Automated Review" in body or
                    "CodeRabbit Review Completed" in body or
                    "<!-- coderabbit-review-complete -->" in body
                )

                if is_bot_author or is_coderabbit_author or has_review_signature:
                    sha_found = (
                        (commit_sha and commit_sha in body) or
                        (short_sha and short_sha in body) or
                        (short7_sha and short7_sha in body)
                    )

                    if sha_found:
                        if "Completed" in body or "Review Outcome" in body or "Review complete" in body:
                            if "NEEDS_WORK" in body:
                                outcome = "NEEDS_WORK (Minor Issues Detected)"
                            elif "CHANGES_REQUESTED" in body:
                                outcome = "CHANGES_REQUESTED"
                            elif "APPROVED" in body:
                                outcome = "APPROVED"
                            else:
                                outcome = "COMMENTED"
                            return True, outcome
        except Exception as e:
            logger.warning("Error fetching comments for %s/%s PR #%s: %s", owner, repo, pr_number, e)

        return False, None

    def get_pr_review_summary(self, owner: str, repo: str, pr_number: int, commit_sha: str = "", auth_user: Optional[str] = None) -> Dict[str, Any]:
        """
        Inspects all reviews, comments, and CI check runs on a PR to detect:
        1. Whether any reviewer requested changes (and who).
        2. Whether the PR is Auto Approved by You (CodeRabbit automated review on current SHA).
        3. Whether the PR is Manually Approved by you (human GitHub approval without automated signature).
        4. Whether other reviewers commented on the PR.
        5. Whether any CI checks have errors/failures.
        """
        user = (auth_user or self.get_username() or "").lower()
        short_sha = commit_sha[:8] if commit_sha else ""

        other_changes_requested = []
        other_approved = []
        other_commented = set()
        user_auto_approved = False
        user_manually_approved = False
        user_review_state = None
        has_user_reviewed = False

        try:
            reviews = self.get_reviews_for_pr(owner, repo, pr_number)
            latest_by_reviewer: Dict[str, Dict[str, Any]] = {}
            for r in reviews:
                reviewer = ((r.get("user") or {}).get("login") or "").strip()
                if not reviewer:
                    continue
                state = (r.get("state") or "").upper()
                if state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"):
                    latest_by_reviewer[reviewer.lower()] = r

            for rev_lower, r in latest_by_reviewer.items():
                reviewer_login = (r.get("user") or {}).get("login") or rev_lower
                state = (r.get("state") or "").upper()
                r_commit = r.get("commit_id", "")
                body = r.get("body", "")
                commit_matched = bool(not commit_sha or r_commit == commit_sha or (short_sha and r_commit.startswith(short_sha)))
                is_cr_auto = ("🐰" in body) or ("CodeRabbit" in body)

                if user and rev_lower == user:
                    if state == "APPROVED":
                        if is_cr_auto:
                            if commit_matched:
                                user_auto_approved = True
                                has_user_reviewed = True
                                user_review_state = "APPROVED"
                        else:
                            user_manually_approved = True
                            if not has_user_reviewed:
                                user_review_state = "APPROVED"
                    elif state == "CHANGES_REQUESTED":
                        if commit_matched:
                            has_user_reviewed = True
                            user_review_state = "CHANGES_REQUESTED"
                else:
                    if state == "CHANGES_REQUESTED":
                        other_changes_requested.append(reviewer_login)
                    elif state == "APPROVED":
                        other_approved.append(reviewer_login)
                    elif state == "COMMENTED":
                        other_commented.add(reviewer_login)

        except Exception as e:
            logger.warning("Error inspecting review summary for %s/%s PR #%s: %s", owner, repo, pr_number, e)

        # Fallback check for automated review in issue comments / has_user_reviewed_sha
        if not user_auto_approved and commit_sha:
            has_auto, auto_state = self.has_user_reviewed_sha(owner, repo, pr_number, commit_sha, username=user)
            if has_auto:
                has_user_reviewed = True
                user_review_state = auto_state
                if auto_state == "APPROVED":
                    user_auto_approved = True

        # Check for other commenters from PR issue comments
        try:
            comments = self.get_comments_for_pr(owner, repo, pr_number)
            for c in comments:
                c_user = ((c.get("user") or {}).get("login") or "").strip()
                if c_user and (not user or c_user.lower() != user) and "coderabbit" not in c_user.lower():
                    other_commented.add(c_user)
        except Exception as e:
            logger.debug("Could not fetch issue comments for PR #%s: %s", pr_number, e)

        # Check CI check runs for errors
        check_summary = self.get_check_runs_summary(owner, repo, commit_sha) if commit_sha else {"has_check_error": False, "failed_checks": []}

        # Check merge conflict status
        conflict_summary = self.get_pr_mergeable_status(owner, repo, pr_number)

        return {
            "has_other_changes_requested": len(other_changes_requested) > 0,
            "other_changes_requested_by": other_changes_requested,
            "other_approved_by": other_approved,
            "other_commented_by": sorted(list(other_commented)),
            "has_other_commented": len(other_commented) > 0,
            "has_user_auto_approved": user_auto_approved,
            "has_user_manually_approved": user_manually_approved,
            "has_check_error": check_summary.get("has_check_error", False),
            "failed_checks": check_summary.get("failed_checks", []),
            "has_conflict": conflict_summary.get("has_conflict", False),
            "mergeable_state": conflict_summary.get("mergeable_state", "unknown"),
            "has_user_reviewed": has_user_reviewed,
            "user_review_state": user_review_state
        }

    def find_bot_comment(self, owner: str, repo: str, pr_number: int, marker: str = "🐰 **Automated CodeRabbit Review") -> Optional[Dict[str, Any]]:
        """Finds existing bot status comment on an issue/PR to prevent duplicate spam."""
        comments = self.get_comments_for_pr(owner, repo, pr_number)
        for comment in comments:
            body = comment.get("body", "")
            if marker in body:
                return comment
        return None

    def create_or_update_comment(self, owner: str, repo: str, pr_number: int, body: str, comment_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Creates or updates a PR comment using PATCH when existing comment is found.
        """
        if not comment_id:
            existing = self.find_bot_comment(owner, repo, pr_number)
            if existing:
                comment_id = existing.get("id")

        if comment_id:
            logger.info("Updating existing PR comment #%s in %s/%s PR #%s", comment_id, owner, repo, pr_number)
            return self._request("PATCH", f"/repos/{owner}/{repo}/issues/comments/{comment_id}", {"body": body})
        else:
            logger.info("Creating new PR comment in %s/%s PR #%s", owner, repo, pr_number)
            return self._request("POST", f"/repos/{owner}/{repo}/issues/{pr_number}/comments", {"body": body})

    def submit_pull_request_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        event: str,
        body: str,
        comments: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Submits a PR review. Handles fallback to body comment if GitHub returns 422 on line offsets
        or if self-approval triggers an error.
        """
        payload: Dict[str, Any] = {
            "commit_id": commit_sha,
            "body": body,
            "event": event
        }

        if comments:
            payload["comments"] = comments

        try:
            return self._request("POST", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", payload)
        except GitHubAPIException as e:
            if e.status_code == 422:
                logger.warning("GitHub returned 422 on review submission. Falling back to body comment.")
                # Fallback: Merge inline comments into the review body and retry without inline comments
                fallback_body = body
                if comments:
                    fallback_body += "\n\n### 📝 Inline Findings Summary\n"
                    for c in comments:
                        fallback_body += f"\n- **`{c.get('path')}` (line {c.get('line')})**:\n  {c.get('body')}\n"

                # Check if self-approval caused 422: convert APPROVE to COMMENT
                fallback_event = "COMMENT" if event == "APPROVE" else event
                fallback_payload = {
                    "commit_id": commit_sha,
                    "body": fallback_body,
                    "event": fallback_event
                }
                return self._request("POST", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", fallback_payload)
            raise

    def create_repo(self, name: str, description: str = "", private: bool = False) -> Dict[str, Any]:
        """Creates a new repository on GitHub under the authenticated user."""
        payload = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": False
        }
        return self._request("POST", "/user/repos", payload)

    @staticmethod
    def split_repo(full_name: str) -> Tuple[str, str]:
        parts = full_name.strip().split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid repository full name '{full_name}'. Expected 'owner/repo'.")
        return parts[0].strip(), parts[1].strip()


class GitHubAPIException(Exception):
    def __init__(self, status_code: int, message: str, details: Dict[str, Any]):
        super().__init__(f"GitHub API Error {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.details = details
