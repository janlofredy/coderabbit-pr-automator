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

    def has_user_reviewed_sha(self, owner: str, repo: str, pr_number: int, commit_sha: str, username: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        Checks if the PR has already been reviewed on the given commit SHA based on:
        1. Formal PR reviews (/pulls/{pr_number}/reviews)
        2. PR issue comment history (/issues/{pr_number}/comments)
        Returns (has_reviewed, review_state).
        """
        user = username or self.get_username()
        short_sha = commit_sha[:8] if commit_sha else ""
        short7_sha = commit_sha[:7] if commit_sha else ""

        # 1. Check formal reviews
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
                    if state in ("APPROVED", "CHANGES_REQUESTED"):
                        return True, state
                    elif state == "COMMENTED":
                        if "🐰" in body or "CodeRabbit" in body:
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

        # 2. Check PR comment history
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
