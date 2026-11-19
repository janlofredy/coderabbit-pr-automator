import os
import io
import re
import copy
import json
import time
import http.client
import logging
import threading
import urllib.request
import urllib.response
import urllib.error
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("github_client")

GITHUB_API_BASE = "https://api.github.com"

AUTHOR_APPROVAL_PATTERN = re.compile(
    r"(?i)\b(lgtm|looks\s+good(?:\s+to\s+merge)?|approved|ready\s+to\s+merge|self[- ]approved|go\s+ahead\s+and\s+merge)\b"
)


class KeepAliveHTTPSHandler(urllib.request.HTTPSHandler):
    """Thread-local HTTP/1.1 keep-alive connection pool for urllib.request HTTPS calls."""

    def __init__(self, debuglevel: int = 0, context: Any = None):
        super().__init__(debuglevel=debuglevel, context=context)
        self._tls = threading.local()

    def _get_pool(self) -> Dict[Tuple[str, int], http.client.HTTPSConnection]:
        pool = getattr(self._tls, "connections", None)
        if pool is None:
            pool = {}
            self._tls.connections = pool
        return pool

    @staticmethod
    def _close_conn(pool: Dict[Tuple[str, int], http.client.HTTPSConnection], key: Tuple[str, int]) -> None:
        conn = pool.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def https_open(self, req: urllib.request.Request):
        parsed = urllib.parse.urlsplit(req.full_url)
        hostname = parsed.hostname
        if not hostname or (req.host and req.host.split(":")[0].lower() != hostname.lower()):
            # Proxy or non-standard host override: fall back to default urllib handler
            return super().https_open(req)

        port = parsed.port or 443
        key = (hostname, port)
        selector = parsed.path or "/"
        if parsed.query:
            selector = f"{selector}?{parsed.query}"

        timeout = getattr(req, "timeout", 30)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            timeout = 30

        headers: Dict[str, str] = {}
        for k, v in req.header_items():
            headers[k] = v
        if not any(k.lower() == "connection" for k in headers):
            headers["Connection"] = "keep-alive"
        if req.data is not None and not any(k.lower() == "content-length" for k in headers):
            headers["Content-Length"] = str(len(req.data))

        pool = self._get_pool()
        for attempt in range(2):
            reused = key in pool
            conn = pool.get(key)
            if conn is None:
                conn = http.client.HTTPSConnection(hostname, port, timeout=timeout, context=self._context)
                pool[key] = conn
            else:
                conn.timeout = timeout

            try:
                conn.request(req.get_method(), selector, body=req.data, headers=headers)
                resp = conn.getresponse()
                raw_body = resp.read()
                conn_header = (resp.getheader("Connection") or "").lower()
                if resp.will_close or "close" in conn_header:
                    self._close_conn(pool, key)

                wrapped = urllib.response.addinfourl(
                    io.BytesIO(raw_body),
                    resp.msg,
                    req.full_url,
                    resp.status,
                )
                wrapped.msg = resp.reason
                return wrapped
            except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError, BrokenPipeError) as e:
                self._close_conn(pool, key)
                if reused and attempt == 0:
                    continue
                raise urllib.error.URLError(e) from e
            except (OSError, http.client.HTTPException) as e:
                self._close_conn(pool, key)
                raise urllib.error.URLError(e) from e


def _install_keepalive_opener() -> None:
    try:
        opener = urllib.request.build_opener(KeepAliveHTTPSHandler())
        urllib.request.install_opener(opener)
    except Exception as e:
        logger.debug("Could not install keep-alive opener: %s", e)


_install_keepalive_opener()


class GitHubClient:
    """GitHub REST & GraphQL API Client using Python standard library with pooling and ETag caching."""

    def __init__(self, token: Optional[str] = None, use_graphql_batch: bool = True):
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")
        self._current_user = None
        self._user_fetch_failed_at: float = 0.0
        self._etag_lock = threading.Lock()
        self._etag_cache: Dict[str, Tuple[str, Any]] = {}
        self._use_graphql_batch = bool(use_graphql_batch)

    def set_token(self, token: str) -> None:
        """Update API credentials and invalidate cached profile and ETags."""
        self.token = token or ""
        self._current_user = None
        self._user_fetch_failed_at = 0.0
        with self._etag_lock:
            self._etag_cache.clear()

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
        is_get = method.upper() == "GET"

        max_retries = 3
        for attempt in range(max_retries):
            headers = self._headers()
            if payload is not None:
                headers["Content-Type"] = "application/json"
            if is_get:
                with self._etag_lock:
                    cached = self._etag_cache.get(url)
                if cached:
                    headers["If-None-Match"] = cached[0]

            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_body = resp.read().decode("utf-8")
                    parsed = json.loads(resp_body) if resp_body else {}
                    resp_headers = getattr(resp, "headers", None)
                    etag = resp_headers.get("ETag") if resp_headers is not None else None
                    if is_get and isinstance(etag, str) and etag.strip():
                        with self._etag_lock:
                            if len(self._etag_cache) >= 512 and url not in self._etag_cache:
                                oldest_key = next(iter(self._etag_cache))
                                self._etag_cache.pop(oldest_key, None)
                            self._etag_cache[url] = (etag.strip(), copy.deepcopy(parsed))
                    return parsed
            except urllib.error.HTTPError as e:
                if e.code == 304 and is_get:
                    with self._etag_lock:
                        cached = self._etag_cache.get(url)
                    if cached is not None:
                        return copy.deepcopy(cached[1])

                error_body = e.read().decode("utf-8", errors="replace")
                is_rate_limit = e.code in (429,) or (
                    e.code == 403 and (
                        "secondary rate limit" in error_body.lower()
                        or "rate limit" in error_body.lower()
                    )
                )
                is_transient = e.code in (500, 502, 503, 504)
                if (is_rate_limit or is_transient) and attempt < max_retries - 1:
                    retry_after = 1.5 * (attempt + 1)
                    header_retry = e.headers.get("Retry-After") if hasattr(e, "headers") and e.headers else None
                    if header_retry and str(header_retry).isdigit():
                        retry_after = min(float(header_retry), 10.0)
                    logger.warning(
                        "GitHub API %s on %s %s (attempt %d/%d). Retrying in %.1fs...",
                        e.code, method, url, attempt + 1, max_retries, retry_after
                    )
                    time.sleep(retry_after)
                    continue

                logger.error("GitHub API error %s on %s %s: %s", e.code, method, url, error_body)
                try:
                    err_json = json.loads(error_body)
                except Exception:
                    err_json = {"message": error_body}
                raise GitHubAPIException(e.code, err_json.get("message", error_body), err_json) from e
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                logger.error("Network error accessing GitHub API on %s %s: %s", method, url, e)
                raise GitHubAPIException(0, str(e.reason), {}) from e

    def resolve_previous_review_threads(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        username: str,
        keep_review_id: Optional[int] = None,
    ) -> int:
        """Resolve older unresolved review threads authored by this account.

        GitHub only exposes review-thread resolution through GraphQL. Failures are
        intentionally best-effort so comment cleanup cannot invalidate a review.
        """
        if not username:
            return 0

        try:
            pr = self.get_pr(owner, repo, pr_number)
            pr_node_id = pr.get("node_id")
            if not pr_node_id:
                logger.warning("Cannot resolve old review threads: PR node ID missing")
                return 0

            query = """query($id: ID!, $cursor: String) {
              node(id: $id) {
                ... on PullRequest {
                  reviewThreads(first: 100, after: $cursor) {
                    nodes {
                      id
                      isResolved
                      comments(first: 100) {
                        nodes {
                          author { login }
                          pullRequestReview { databaseId }
                        }
                      }
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                }
              }
            }"""
            mutation = """mutation($threadId: ID!) {
              resolveReviewThread(input: {threadId: $threadId}) {
                thread { id isResolved }
              }
            }"""

            cursor = None
            resolved = 0
            while True:
                result = self._request("POST", f"{GITHUB_API_BASE}/graphql", {
                    "query": query,
                    "variables": {"id": pr_node_id, "cursor": cursor},
                })
                if result.get("errors"):
                    raise RuntimeError(result["errors"])
                threads = result["data"]["node"]["reviewThreads"]
                for thread in threads.get("nodes", []):
                    if thread.get("isResolved"):
                        continue
                    comments = thread.get("comments", {}).get("nodes", [])
                    authored_by_user = any(
                        (comment.get("author") or {}).get("login", "").lower() == username.lower()
                        for comment in comments
                    )
                    includes_new_review = keep_review_id is not None and any(
                        (comment.get("pullRequestReview") or {}).get("databaseId") == keep_review_id
                        for comment in comments
                    )
                    if authored_by_user and not includes_new_review:
                        resolved_result = self._request("POST", f"{GITHUB_API_BASE}/graphql", {
                            "query": mutation,
                            "variables": {"threadId": thread["id"]},
                        })
                        if resolved_result.get("errors"):
                            logger.warning("Could not resolve review thread %s: %s", thread["id"], resolved_result["errors"])
                        else:
                            resolved += 1
                page_info = threads.get("pageInfo", {})
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
            return resolved
        except Exception as e:
            logger.warning("Could not resolve previous review threads on %s/%s PR #%s: %s", owner, repo, pr_number, e)
            return 0

    def minimize_previous_reviews(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        username: str,
        keep_review_id: Optional[int] = None,
    ) -> int:
        """
        Minimizes top-level pull request review comments submitted by the bot prior to keep_review_id.
        Marks them as OUTDATED via GraphQL minimizeComment mutation so they appear collapsed.
        Failures are best-effort.
        """
        if not username:
            return 0

        try:
            pr = self.get_pr(owner, repo, pr_number)
            pr_node_id = pr.get("node_id")
            if not pr_node_id:
                logger.warning("Cannot minimize old reviews: PR node ID missing")
                return 0

            query = """query($id: ID!, $cursor: String) {
              node(id: $id) {
                ... on PullRequest {
                  reviews(first: 100, after: $cursor) {
                    nodes {
                      id
                      databaseId
                      body
                      author { login }
                      ... on Minimizable {
                        isMinimized
                      }
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                }
              }
            }"""
            mutation = """mutation($subjectId: ID!) {
              minimizeComment(input: {subjectId: $subjectId, classifier: OUTDATED}) {
                minimizedComment { isMinimized }
              }
            }"""

            cursor = None
            minimized = 0
            while True:
                result = self._request("POST", f"{GITHUB_API_BASE}/graphql", {
                    "query": query,
                    "variables": {"id": pr_node_id, "cursor": cursor},
                })
                if result.get("errors"):
                    raise RuntimeError(result["errors"])
                reviews = result["data"]["node"]["reviews"]
                for review in reviews.get("nodes", []):
                    if review.get("isMinimized"):
                        continue
                    db_id = review.get("databaseId")
                    if keep_review_id is not None and db_id == keep_review_id:
                        continue
                    author_login = ((review.get("author") or {}).get("login") or "").lower()
                    if author_login != username.lower():
                        continue
                    body = review.get("body") or ""
                    # Only minimize bot's CodeRabbit reviews
                    if "🐰" in body or "CodeRabbit" in body:
                        min_res = self._request("POST", f"{GITHUB_API_BASE}/graphql", {
                            "query": mutation,
                            "variables": {"subjectId": review["id"]},
                        })
                        if min_res.get("errors"):
                            logger.warning("Could not minimize review %s: %s", review["id"], min_res["errors"])
                        else:
                            minimized += 1
                page_info = reviews.get("pageInfo", {})
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
            return minimized
        except Exception as e:
            logger.warning("Could not minimize previous reviews on %s/%s PR #%s: %s", owner, repo, pr_number, e)
            return 0

    def get_authenticated_user(self) -> Dict[str, Any]:
        """Gets profile for the authenticated GitHub user."""
        if not self._current_user:
            self._current_user = self._request("GET", "/user")
        return self._current_user

    def get_username(self) -> str:
        """Returns the username of the authenticated user or empty string."""
        if not self.token:
            return ""
        if self._current_user:
            return self._current_user.get("login", "")
        if self._user_fetch_failed_at and (time.monotonic() - self._user_fetch_failed_at) < 60.0:
            return ""
        try:
            user = self.get_authenticated_user()
            self._user_fetch_failed_at = 0.0
            return user.get("login", "")
        except GitHubAPIException as e:
            if e.status_code in (401, 403):
                self._user_fetch_failed_at = time.monotonic()
            logger.warning("Could not fetch authenticated username: %s", e)
            return ""
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

    def list_recently_merged_prs(
        self,
        owner: str,
        repo: str,
        since_timestamp: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Lists pull requests that were merged recently for a repository.
        If since_timestamp (ISO 8601 string) is provided, filters for PRs with merged_at >= since_timestamp.
        """
        try:
            closed_prs = self._request("GET", f"/repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={limit}")
        except Exception as e:
            logger.warning("Error fetching closed PRs for %s/%s: %s", owner, repo, e)
            return []

        if not isinstance(closed_prs, list):
            return []

        merged_prs = []
        for pr in closed_prs:
            if not isinstance(pr, dict):
                continue
            merged_at = pr.get("merged_at")
            if not merged_at:
                continue
            if since_timestamp and str(merged_at) < str(since_timestamp):
                continue
            merged_prs.append(pr)
        return merged_prs

    def get_pr(self, owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
        """Retrieves detailed information for a single pull request."""
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")

    def get_branch(self, owner: str, repo: str, branch: str) -> Dict[str, Any]:
        """Retrieves branch details including its latest commit SHA."""
        quoted_branch = urllib.parse.quote(branch, safe="")
        return self._request("GET", f"/repos/{owner}/{repo}/branches/{quoted_branch}")

    def get_reviews_for_pr(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Retrieves all reviews submitted on a pull request with pagination."""
        all_reviews: List[Dict[str, Any]] = []
        page = 1
        while True:
            reviews = self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews?per_page=100&page={page}")
            if not reviews or not isinstance(reviews, list):
                break
            all_reviews.extend(reviews)
            if len(reviews) < 100:
                break
            page += 1
            if page > 10:  # safety cap up to 1000 reviews
                break
        return all_reviews

    def get_comments_for_pr(self, owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
        """Retrieves conversation issue comments on a pull request with pagination."""
        all_comments: List[Dict[str, Any]] = []
        page = 1
        while True:
            comments = self._request("GET", f"/repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=100&page={page}")
            if not comments or not isinstance(comments, list):
                break
            all_comments.extend(comments)
            if len(comments) < 100:
                break
            page += 1
            if page > 10:  # safety cap up to 1000 comments
                break
        return all_comments

    def get_check_runs_summary(self, owner: str, repo: str, commit_sha: str) -> Dict[str, Any]:
        """
        Retrieves CI check runs for a commit to detect failures/errors and checks status.
        Returns: { 'has_check_error': bool, 'failed_checks': List[str], 'checks_status': Optional[str] }
        checks_status is one of: 'PASSED', 'ONGOING', 'FAILED', or None (if no checks).
        """
        if not commit_sha:
            return {"has_check_error": False, "failed_checks": [], "checks_status": None}
        try:
            res = self._request("GET", f"/repos/{owner}/{repo}/commits/{commit_sha}/check-runs?per_page=100")
            check_runs = res.get("check_runs", []) if isinstance(res, dict) else []
            failed = []
            ongoing = []
            passed = []
            for cr in check_runs:
                status = (cr.get("status") or "").lower()
                conclusion = (cr.get("conclusion") or "").lower()
                name = cr.get("name", "Unknown Check")
                if conclusion in ("failure", "timed_out", "action_required", "cancelled", "startup_failure"):
                    failed.append(name)
                elif status in ("queued", "in_progress", "waiting", "requested", "pending") or (status != "completed" and not conclusion):
                    ongoing.append(name)
                elif conclusion in ("success", "neutral", "skipped"):
                    passed.append(name)

            # If no check runs found, check the commit status API
            if not check_runs:
                try:
                    status_res = self._request("GET", f"/repos/{owner}/{repo}/commits/{commit_sha}/status")
                    statuses = status_res.get("statuses", []) if isinstance(status_res, dict) else []
                    for st in statuses:
                        state = (st.get("state") or "").lower()
                        context = st.get("context", "Unknown Status")
                        if state in ("failure", "error"):
                            failed.append(context)
                        elif state == "pending":
                            ongoing.append(context)
                        elif state == "success":
                            passed.append(context)
                except Exception as e:
                    logger.debug("Could not fetch commit status for %s/%s@%s: %s", owner, repo, commit_sha[:8], e)

            if failed:
                checks_status = "FAILED"
            elif ongoing:
                checks_status = "ONGOING"
            elif passed:
                checks_status = "PASSED"
            else:
                checks_status = None

            return {
                "has_check_error": len(failed) > 0,
                "failed_checks": failed,
                "checks_status": checks_status
            }
        except Exception as e:
            logger.debug("Could not fetch check-runs for %s/%s@%s: %s", owner, repo, commit_sha[:8], e)
            return {"has_check_error": False, "failed_checks": [], "checks_status": None}

    def get_pr_mergeable_status(self, owner: str, repo: str, pr_number: int, max_retries: int = 2) -> Dict[str, Any]:
        """
        Retrieves mergeable status for a pull request.
        GitHub calculates mergeability asynchronously. If 'mergeable' is None or
        'mergeable_state' is 'unknown', it retries briefly so GitHub's background job can complete.
        Returns: { 'has_conflict': bool, 'mergeable_state': str }
        """
        for attempt in range(max_retries + 1):
            try:
                pr = self.get_pr(owner, repo, pr_number)
                mergeable = pr.get("mergeable")
                mergeable_state = (pr.get("mergeable_state") or "").lower()

                # If GitHub background calculation is pending, retry briefly
                if (mergeable is None or mergeable_state == "unknown") and attempt < max_retries:
                    time.sleep(1.0)
                    continue

                # mergeable is False or mergeable_state is dirty/conflicting
                has_conflict = (mergeable is False) or (mergeable_state in ("dirty", "conflicting"))
                return {
                    "has_conflict": has_conflict,
                    "mergeable_state": mergeable_state
                }
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(0.5)
                    continue
                logger.warning("Could not fetch mergeable status for %s/%s PR #%s: %s", owner, repo, pr_number, e)
                return {"has_conflict": False, "mergeable_state": "unknown"}

        return {"has_conflict": False, "mergeable_state": "unknown"}

    def has_user_reviewed_sha(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        username: Optional[str] = None,
        reviews: Optional[List[Dict[str, Any]]] = None,
        comments: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, Optional[str]]:
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
            review_list = reviews if reviews is not None else self.get_reviews_for_pr(owner, repo, pr_number)
            for r in reversed(review_list):
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
            comment_list = comments if comments is not None else self.get_comments_for_pr(owner, repo, pr_number)
            for comment in reversed(comment_list):
                author = (comment.get("user") or {}).get("login", "")
                body = comment.get("body", "")

                is_bot_author = bool(user and author.lower() == user.lower())
                is_coderabbit_author = "coderabbit" in author.lower()
                has_review_signature = (
                    "## 🐰 CodeRabbit" in body or
                    "🐰 **Automated CodeRabbit Review" in body or
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

    def _summarize_pr_data(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        reviews: List[Dict[str, Any]],
        comments: List[Dict[str, Any]],
        commit_sha: str,
        auth_user: Optional[str],
        pr_author: Optional[str],
        check_summary: Dict[str, Any],
        conflict_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Classifies reviews, comments, CI checks, and mergeability into a summary dict."""
        user = (auth_user or "").lower()
        author_lower = (pr_author or "").strip().lower()
        author_text_approved = False
        short_sha = commit_sha[:8] if commit_sha else ""

        other_changes_requested: List[str] = []
        other_approved: List[str] = []
        outdated_approvals: List[str] = []
        outdated_changes_requested: List[str] = []
        other_commented = set()
        user_auto_approved = False
        user_manually_approved = False
        user_review_state = None
        has_user_reviewed = False

        latest_by_reviewer: Dict[str, Dict[str, Any]] = {}
        latest_decisive_by_reviewer: Dict[str, Dict[str, Any]] = {}
        has_commented_by_reviewer: Dict[str, bool] = {}

        for r in reviews:
            reviewer = ((r.get("user") or {}).get("login") or "").strip()
            if not reviewer:
                continue
            rev_key = reviewer.lower()
            # A developer (PR author) replying to a comment is not a review
            if author_lower and rev_key == author_lower:
                continue
            latest_by_reviewer[rev_key] = r
            state = (r.get("state") or "").upper()
            if state in ("APPROVED", "CHANGES_REQUESTED"):
                latest_decisive_by_reviewer[rev_key] = r
            elif state == "DISMISSED":
                latest_decisive_by_reviewer.pop(rev_key, None)
            elif state == "COMMENTED":
                has_commented_by_reviewer[rev_key] = True

        all_reviewers = set(latest_by_reviewer.keys()) | set(latest_decisive_by_reviewer.keys())
        for rev_lower in all_reviewers:
            r_decisive = latest_decisive_by_reviewer.get(rev_lower)
            r_latest = latest_by_reviewer.get(rev_lower)

            if r_decisive and not r_latest:
                r = r_decisive
            elif r_latest and not r_decisive:
                r = r_latest
            else:
                dec_commit = (r_decisive.get("commit_id") or "").strip()
                lat_commit = (r_latest.get("commit_id") or "").strip()

                dec_matched = bool(commit_sha and (dec_commit == commit_sha or (short_sha and dec_commit.startswith(short_sha))))
                lat_matched = bool(commit_sha and (lat_commit == commit_sha or (short_sha and lat_commit.startswith(short_sha))))

                if dec_matched:
                    r = r_decisive
                elif lat_matched:
                    r = r_latest
                elif dec_commit != lat_commit:
                    r = r_latest
                else:
                    r = r_decisive

            reviewer_login = (r.get("user") or {}).get("login") or rev_lower
            state = (r.get("state") or "").upper()
            r_commit = r.get("commit_id", "")
            body = r.get("body", "") or ""
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
                elif state == "COMMENTED":
                    if commit_matched and is_cr_auto:
                        has_user_reviewed = True
                        outcome = "COMMENTED"
                        if "NEEDS_WORK" in body:
                            outcome = "NEEDS_WORK (Minor Issues Detected)"
                        elif "CHANGES_REQUESTED" in body:
                            outcome = "CHANGES_REQUESTED"
                        elif "APPROVED" in body:
                            outcome = "APPROVED"
                            user_auto_approved = True
                        user_review_state = outcome
            else:
                if state == "CHANGES_REQUESTED":
                    other_changes_requested.append(reviewer_login)
                    if not commit_matched and reviewer_login not in outdated_changes_requested:
                        outdated_changes_requested.append(reviewer_login)
                elif state == "APPROVED":
                    if reviewer_login not in other_approved:
                        other_approved.append(reviewer_login)
                    if not commit_matched and reviewer_login not in outdated_approvals:
                        outdated_approvals.append(reviewer_login)
                elif state == "COMMENTED":
                    if not is_cr_auto and "coderabbit" not in rev_lower:
                        other_commented.add(reviewer_login)

        for rev_lower, did_comment in has_commented_by_reviewer.items():
            if did_comment and rev_lower not in [u.lower() for u in other_approved] and rev_lower not in [u.lower() for u in other_changes_requested]:
                if not (user and rev_lower == user) and "coderabbit" not in rev_lower:
                    r_comm = latest_by_reviewer.get(rev_lower, {})
                    c_login = (r_comm.get("user") or {}).get("login") or rev_lower
                    other_commented.add(c_login)

        # Fallback check for automated review in issue comments using already-fetched data
        if not user_auto_approved and commit_sha:
            has_auto, auto_state = self.has_user_reviewed_sha(
                owner, repo, pr_number, commit_sha, username=user, reviews=reviews, comments=comments
            )
            if has_auto:
                has_user_reviewed = True
                user_review_state = auto_state
                if auto_state == "APPROVED":
                    user_auto_approved = True

        for c in comments:
            c_user = ((c.get("user") or {}).get("login") or "").strip()
            c_body = c.get("body", "") or ""
            if not c_user or "coderabbit" in c_user.lower():
                continue
            if user and c_user.lower() == user:
                continue

            if author_lower and c_user.lower() == author_lower:
                if AUTHOR_APPROVAL_PATTERN.search(c_body):
                    author_text_approved = True
                    if c_user not in other_approved:
                        other_approved.append(c_user)
                continue

            other_commented.add(c_user)

        other_commented = {
            u for u in other_commented
            if u.lower() not in [a.lower() for a in other_approved]
            and u.lower() not in [c.lower() for c in other_changes_requested]
        }
        if author_lower:
            other_commented = {u for u in other_commented if u.lower() != author_lower}

        return {
            "has_other_changes_requested": len(other_changes_requested) > 0,
            "other_changes_requested_by": other_changes_requested,
            "other_approved_by": other_approved,
            "outdated_approvals_by": outdated_approvals,
            "outdated_changes_requested_by": outdated_changes_requested,
            "other_commented_by": sorted(list(other_commented)),
            "has_other_commented": len(other_commented) > 0,
            "has_user_auto_approved": user_auto_approved,
            "has_user_manually_approved": user_manually_approved,
            "author_approved_in_text": author_text_approved,
            "has_check_error": check_summary.get("has_check_error", False),
            "failed_checks": check_summary.get("failed_checks", []),
            "checks_status": check_summary.get("checks_status", None),
            "has_conflict": conflict_summary.get("has_conflict", False),
            "mergeable_state": conflict_summary.get("mergeable_state", "unknown"),
            "has_user_reviewed": has_user_reviewed,
            "user_review_state": user_review_state
        }

    def get_pr_review_summary(self, owner: str, repo: str, pr_number: int, commit_sha: str = "", auth_user: Optional[str] = None, pr_author: Optional[str] = None) -> Dict[str, Any]:
        """
        Inspects all reviews, comments, and CI check runs on a PR to detect:
        1. Whether any reviewer requested changes (and who).
        2. Whether the PR is Auto Approved by You (CodeRabbit automated review on current SHA).
        3. Whether the PR is Manually Approved by you (human GitHub approval without automated signature).
        4. Whether other reviewers commented on the PR.
        5. Whether the author approved in text or commented/replied.
        6. Whether any CI checks have errors/failures.
        """
        resolved_auth_user = auth_user if auth_user is not None else self.get_username()

        reviews: List[Dict[str, Any]] = []
        try:
            reviews = self.get_reviews_for_pr(owner, repo, pr_number)
        except Exception as e:
            logger.warning("Error inspecting review summary for %s/%s PR #%s: %s", owner, repo, pr_number, e)

        comments: List[Dict[str, Any]] = []
        try:
            comments = self.get_comments_for_pr(owner, repo, pr_number)
        except Exception as e:
            logger.debug("Could not fetch issue comments for PR #%s: %s", pr_number, e)

        check_summary = self.get_check_runs_summary(owner, repo, commit_sha) if commit_sha else {"has_check_error": False, "failed_checks": [], "checks_status": None}
        conflict_summary = self.get_pr_mergeable_status(owner, repo, pr_number)

        return self._summarize_pr_data(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            reviews=reviews,
            comments=comments,
            commit_sha=commit_sha,
            auth_user=resolved_auth_user,
            pr_author=pr_author,
            check_summary=check_summary,
            conflict_summary=conflict_summary,
        )

    @staticmethod
    def _summarize_graphql_checks(commit_node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        rollup = ((commit_node or {}).get("commit") or {}).get("statusCheckRollup")
        if not isinstance(rollup, dict):
            return {"has_check_error": False, "failed_checks": [], "checks_status": None}

        contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
        failed: List[str] = []
        ongoing: List[str] = []
        passed: List[str] = []

        for ctx in contexts:
            if not isinstance(ctx, dict):
                continue
            typename = ctx.get("__typename", "")
            if typename == "CheckRun" or ("status" in ctx or "conclusion" in ctx):
                status = (ctx.get("status") or "").lower()
                conclusion = (ctx.get("conclusion") or "").lower()
                name = ctx.get("name") or "Unknown Check"
                if conclusion in ("failure", "timed_out", "action_required", "cancelled", "startup_failure"):
                    failed.append(name)
                elif status in ("queued", "in_progress", "waiting", "requested", "pending") or (status != "completed" and not conclusion):
                    ongoing.append(name)
                elif conclusion in ("success", "neutral", "skipped"):
                    passed.append(name)
            elif typename == "StatusContext" or "context" in ctx:
                state = (ctx.get("state") or "").lower()
                context_name = ctx.get("context") or "Unknown Status"
                if state in ("failure", "error"):
                    failed.append(context_name)
                elif state == "pending":
                    ongoing.append(context_name)
                elif state == "success":
                    passed.append(context_name)

        if failed:
            checks_status = "FAILED"
        elif ongoing:
            checks_status = "ONGOING"
        elif passed:
            checks_status = "PASSED"
        else:
            rollup_state = (rollup.get("state") or "").upper()
            if rollup_state in ("FAILURE", "ERROR"):
                checks_status = "FAILED"
            elif rollup_state == "PENDING":
                checks_status = "ONGOING"
            elif rollup_state == "SUCCESS":
                checks_status = "PASSED"
            else:
                checks_status = None

        return {
            "has_check_error": len(failed) > 0 or checks_status == "FAILED",
            "failed_checks": failed,
            "checks_status": checks_status,
        }

    def fetch_repo_open_prs_graphql(
        self,
        owner: str,
        repo: str,
        auth_user: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Fetches open pull requests and their reviews, comments, CI rollup, mergeability,
        and base branch tips in a single GraphQL request. Returns None on fallback/error.
        """
        if not self.token:
            return None

        query = """query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            pullRequests(first: 50, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {
              nodes {
                number
                title
                url
                createdAt
                updatedAt
                mergeable
                mergeStateStatus
                author { login }
                baseRefName
                baseRefOid
                baseRef { target { oid } }
                headRefName
                headRefOid
                reviews(last: 100) {
                  totalCount
                  nodes {
                    author { login }
                    state
                    body
                    commit { oid }
                  }
                }
                comments(last: 100) {
                  totalCount
                  nodes {
                    author { login }
                    body
                  }
                }
                commits(last: 1) {
                  nodes {
                    commit {
                      oid
                      statusCheckRollup {
                        state
                        contexts(first: 100) {
                          nodes {
                            __typename
                            ... on CheckRun {
                              name
                              status
                              conclusion
                            }
                            ... on StatusContext {
                              context
                              state
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }"""

        try:
            res = self._request("POST", f"{GITHUB_API_BASE}/graphql", {
                "query": query,
                "variables": {"owner": owner, "repo": repo},
            })
            if not isinstance(res, dict) or res.get("errors"):
                return None
            repo_data = (res.get("data") or {}).get("repository")
            if not repo_data:
                return None
            nodes = ((repo_data.get("pullRequests") or {}).get("nodes")) or []
        except Exception as e:
            logger.debug("GraphQL batch PR fetch failed for %s/%s, falling back to REST: %s", owner, repo, e)
            return None

        resolved_auth_user = auth_user if auth_user is not None else self.get_username()
        full_name = f"{owner}/{repo}"
        items: List[Dict[str, Any]] = []

        for node in nodes:
            if not isinstance(node, dict):
                continue
            num = node.get("number")
            if num is None:
                continue
            pr_key = f"{full_name}#{num}"
            author = ((node.get("author") or {}).get("login")) or ""
            is_own = bool(resolved_auth_user and author.lower() == resolved_auth_user.lower())
            base_ref = node.get("baseRefName") or ""
            base_sha = node.get("baseRefOid") or ""
            base_tip_sha = (((node.get("baseRef") or {}).get("target") or {}).get("oid")) or ""
            head_ref = node.get("headRefName") or ""
            head_sha = node.get("headRefOid") or ""
            updated_at = node.get("updatedAt") or ""

            reviews_conn = node.get("reviews") or {}
            comments_conn = node.get("comments") or {}
            if (reviews_conn.get("totalCount") or 0) > 100 or (comments_conn.get("totalCount") or 0) > 100:
                review_summary = self.get_pr_review_summary(
                    owner, repo, num, commit_sha=head_sha, auth_user=resolved_auth_user, pr_author=author
                )
            else:
                reviews = [
                    {
                        "user": {"login": ((r.get("author") or {}).get("login")) or ""},
                        "state": r.get("state") or "",
                        "body": r.get("body") or "",
                        "commit_id": ((r.get("commit") or {}).get("oid")) or "",
                    }
                    for r in (reviews_conn.get("nodes") or [])
                    if isinstance(r, dict)
                ]
                comments = [
                    {
                        "user": {"login": ((c.get("author") or {}).get("login")) or ""},
                        "body": c.get("body") or "",
                    }
                    for c in (comments_conn.get("nodes") or [])
                    if isinstance(c, dict)
                ]
                commit_nodes = ((node.get("commits") or {}).get("nodes")) or []
                latest_commit_node = commit_nodes[-1] if commit_nodes else None
                check_summary = self._summarize_graphql_checks(latest_commit_node)

                gql_mergeable = str(node.get("mergeable") or "").upper()
                gql_state = str(node.get("mergeStateStatus") or "").lower()
                has_conflict = (gql_mergeable == "CONFLICTING") or (gql_state in ("dirty", "conflicting"))
                if gql_mergeable == "CONFLICTING" and gql_state in ("", "unknown"):
                    gql_state = "dirty"
                elif gql_mergeable == "UNKNOWN" and not gql_state:
                    gql_state = "unknown"
                elif not gql_state:
                    gql_state = "clean" if not has_conflict else "dirty"
                conflict_summary = {"has_conflict": has_conflict, "mergeable_state": gql_state}

                review_summary = self._summarize_pr_data(
                    owner=owner,
                    repo=repo,
                    pr_number=num,
                    reviews=reviews,
                    comments=comments,
                    commit_sha=head_sha,
                    auth_user=resolved_auth_user,
                    pr_author=author,
                    check_summary=check_summary,
                    conflict_summary=conflict_summary,
                )

            m_state = review_summary.get("mergeable_state")
            if not m_state:
                m_state = "clean" if review_summary.get("has_conflict") is False else "unknown"

            items.append({
                "pr_key": pr_key,
                "repo": full_name,
                "number": num,
                "title": node.get("title") or "",
                "author": author,
                "is_own_pr": is_own,
                "base_ref": base_ref,
                "base_sha": base_sha,
                "base_tip_sha": base_tip_sha,
                "head_ref": head_ref,
                "head_sha": head_sha,
                "html_url": node.get("url") or "",
                "created_at": node.get("createdAt") or "",
                "updated_at": updated_at,
                "has_other_changes_requested": review_summary.get("has_other_changes_requested", False),
                "other_changes_requested_by": review_summary.get("other_changes_requested_by", []),
                "other_approved_by": review_summary.get("other_approved_by", []),
                "outdated_approvals_by": review_summary.get("outdated_approvals_by", []),
                "outdated_changes_requested_by": review_summary.get("outdated_changes_requested_by", []),
                "other_commented_by": review_summary.get("other_commented_by", []),
                "has_other_commented": review_summary.get("has_other_commented", False),
                "has_user_auto_approved": review_summary.get("has_user_auto_approved", False),
                "has_user_manually_approved": review_summary.get("has_user_manually_approved", False),
                "author_approved_in_text": review_summary.get("author_approved_in_text", False),
                "has_check_error": review_summary.get("has_check_error", False),
                "failed_checks": review_summary.get("failed_checks", []),
                "checks_status": review_summary.get("checks_status", None),
                "has_conflict": review_summary.get("has_conflict", False),
                "mergeable_state": m_state,
                "has_user_reviewed": review_summary.get("has_user_reviewed", False),
                "user_review_state": review_summary.get("user_review_state"),
            })

        return items

    def find_bot_comment(self, owner: str, repo: str, pr_number: int, marker: str = "🐰 CodeRabbit") -> Optional[Dict[str, Any]]:
        """Finds existing bot status comment on an issue/PR to prevent duplicate spam."""
        comments = self.get_comments_for_pr(owner, repo, pr_number)
        for comment in comments:
            body = comment.get("body", "")
            if "## 🐰 CodeRabbit" in body or "🐰 **Automated CodeRabbit Review" in body or marker in body:
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
