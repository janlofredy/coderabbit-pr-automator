# 🐰 CodeRabbit PR Auto-Reviewer

[![Docker Publish](https://github.com/janlofredy/coderabbit-pr-automator/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/janlofredy/coderabbit-pr-automator/actions/workflows/docker-publish.yml)
[![Docker Multi-Arch](https://img.shields.io/badge/docker-linux%2Famd64%20%7C%20linux%2Farm64-blue?logo=docker)](https://github.com/janlofredy/coderabbit-pr-automator/pkgs/container/coderabbit-pr-automator)
[![CasaOS Ready](https://img.shields.io/badge/CasaOS-Ready-00D1B2?logo=house&logoColor=white)](https://casaos.io)
[![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A headless, continuous CI/CD background automation service and real-time Web Dashboard that monitors specified GitHub repositories for open pull requests, performs AI code reviews using the local [CodeRabbit CLI](https://coderabbit.ai) (`coderabbit review --agent`), posts per-file inline line comments and reviews to GitHub, handles CodeRabbit Free Tier constraints (rate limits, 100-file ceiling, retry backoffs), and provides an interactive web UI. Tailored for **CasaOS**, **Docker**, and standalone deployments.

---

## ✨ Features

- **Continuous Background Monitoring**: Periodically scans configured GitHub repositories for unreviewed pull requests.
- **Headless CodeRabbit CLI Execution**: Leverages the official CodeRabbit CLI in agent mode (`--agent --base <ref>`) with a configurable 45-minute default timeout (`CODERABBIT_TIMEOUT`).
- **Intelligent Diff & Per-File Line Comments**:
  - Automatically fetches the target base branch and PR head commit.
  - Matches CodeRabbit findings to line numbers in `git diff -U0`.
  - Submits per-file inline comments to `/pulls/{number}/reviews`.
  - Automatic 422 error recovery: Gracefully merges comments into the main review body if line offsets change or fail validation.
- **Self-Approval Prevention (422 Guard)**: Detects PRs authored by the authenticated bot account and marks them as `OWN_PR`, preventing self-approval API rejections.
- **100-File Free-Tier Ceiling Guard**: Prevents review failures on massive PRs by checking changed file count prior to review and posting a polite explanation comment.
- **Anti-Spam Rate-Limit Management**:
  - Catches CLI timeouts and rate-limit strings (`429`, `rate limit`, `too many requests`, `quota`).
  - Automatically parses cooldown duration (e.g. "try again in 15 mins") or applies backoff.
  - **Single Comment Updates**: Updates the existing PR comment via `PATCH /repos/{owner}/{repo}/issues/comments/{id}` with a live retry countdown banner instead of polluting PR conversations with new comments.
  - Persists cooldown state in `automation_state.json` to prevent burning quota.
- **High-Performance Real-Time Web Dashboard (Port 8765)**:
  - Multi-threaded Python server (`ThreadingMixIn`) with non-blocking concurrent polling via `ThreadPoolExecutor` (sub-millisecond `/api/status`).
  - Real-time JavaScript countdown ticker: `"Will retry in 14 min 23s (at 16:45:00)"`.
  - Color-coded PR cards:
    - ⏳ **Rate Limited** (Orange)
    - ⚡ **Review in Progress** (Blue)
    - ✅ **Approved by You** (Green)
    - 💬 **Comments Posted** (Yellow)
    - 🛑 **Skipped (>100 Files)** (Red)
    - 👤 **Your PR (Author)** (Purple)
    - ⏳ **Pending Review** (Gray)
  - Quick action controls: `📄 View Report`, `GitHub ↗`, `⚡ Review`, `⚡ Force Review` (bypasses cooldown), `⚙️ Manage Repos`.
- **Dynamic Repository Management**:
  - Real-time repository toggling (Active vs Paused) without container restarts.
  - Add new repositories with instant GitHub API validation.

## 🧩 Service Boundaries

The dashboard composes two management services:

1. **Repository Management** (`repository_management.py`) owns repository configuration operations, GitHub repository validation, open-PR discovery, and the PR cache.
2. **Review Management** (`review_management.py`) owns review scheduling, scan phases, single-PR review triggers, cooldown controls, and coordination with the review engine.

`web_dashboard.py` connects these services to the HTTP API and builds the dashboard status response. Repository discovery and review execution remain separate responsibilities; review runs are still processed one PR at a time.

Repository discovery appears in the global activity notification. Review phases and outcomes appear on their respective PR cards, which are grouped by repository while retaining their current order within each repository. PR-specific review requests enter a FIFO Review Queue shown on the dashboard; one queued PR is reviewed at a time.

---

## 🏠 CasaOS Setup (1-to-1 Copy & Paste Import)

Deploying to CasaOS is a single copy-paste operation:

1. Open your **CasaOS Web UI** (e.g. `http://<your-server-ip>:8080`).
2. Click the **App Store** icon.
3. In the top-right corner of the App Store, click **Custom Install**.
4. Click **Import** in the top-right corner of the modal.
5. Copy and paste the complete contents of [`casaos-compose.yml`](casaos-compose.yml) into the box.
6. Replace `YOUR_GITHUB_TOKEN_HERE` with your GitHub Personal Access Token (classic with `repo` scope or fine-grained token).
7. Click **Install**.
8. Once installed, the **CodeRabbit Auto-Reviewer** tile appears on your CasaOS dashboard with the official icon and opens directly to `http://<casaos-ip>:8765`.

---

## 🐳 Docker Compose Setup (Standalone)

### 1. Clone the Repository
```bash
git clone https://github.com/janlofredy/coderabbit-pr-automator.git
cd coderabbit-pr-automator
```

### 2. Configure Environment Variables
Copy the example environment file:
```bash
cp .env.example .env
```
Edit `.env` with your GitHub token:
```env
GITHUB_TOKEN=ghp_yourPersonalAccessTokenHere
```
*(All other settings including monitored repositories, scan intervals, limits, and approval policies are managed directly from the Web Dashboard).*

### 3. Start the Container
```bash
docker compose up -d
```

Access the dashboard at `http://localhost:8765`.

---

## 🔑 Authentication Options

### 1. GitHub Authentication
Requires a **GitHub Personal Access Token** (Classic or Fine-Grained) with:
- `repo` scope (for private repositories) or `public_repo` (for public repositories).
- Set via `GITHUB_TOKEN` environment variable.

### 2. CodeRabbit Authentication
The CodeRabbit CLI disables browser OAuth inside Docker/CI. Run `coderabbit auth login` on the Docker host, choose **Continue with Google** in the browser, and verify with `coderabbit auth status`. The default Compose file mounts the host's `~/.coderabbit` directory directly into the container, so no copy is needed. If `CODERABBIT_CLI_HOME` points to a different host path, copy the authenticated `.coderabbit` contents there. EU users can run `coderabbit auth login --region eu` on the host.

The CasaOS compose file uses one storage mount, `/DATA/AppData/coderabbit:/app/data`. Inside it, app settings are stored under `config`, review reports under `reviews`, repository checkouts under `repos`, and CodeRabbit CLI credentials under `coderabbit-cli`. Authenticate with `coderabbit auth login` on a machine with a browser, then securely copy that machine's `.coderabbit` directory contents to `/DATA/AppData/coderabbit/coderabbit-cli/`. Keep the CLI state private; it contains login credentials.

The dashboard header shows the current CodeRabbit CLI auth status. Click the status bar for these instructions. For headless or bot-driven authentication, use an Agentic API key with `coderabbit auth login --api-key "<key>"`; see the [CLI auth reference](https://docs.coderabbit.ai/cli/reference).

---

## ⚙️ Environment Variables Reference

| Variable | Description | Default |
| :--- | :--- | :--- |
| `GITHUB_TOKEN` | GitHub Personal Access Token (`repo` scope) | *Required* |
| `CODERABBIT_CLI_HOME` | Host path containing the authenticated CodeRabbit CLI state mounted by Docker Compose | `${HOME}/.coderabbit` |

> 💡 **Note**: Repository settings, polling intervals, max file limits, auto-approvals, and strict approval mode are persisted in the app's mounted data directory and can be changed live from the Web Dashboard without recreating or restarting the container. CodeRabbit authentication is managed by the CLI, outside the dashboard.

---

## 📡 REST API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/status` | `GET` | Returns aggregated status, active rate limits, strict approval mode, PR list, and active/waiting review queue items |
| `/api/coderabbit-auth` | `GET` | Returns the current CodeRabbit CLI authentication status |
| `/api/config` | `GET` | Returns runtime operational settings (`poll_interval_seconds`, `max_files_limit`, etc.) |
| `/api/config` | `POST` | Updates runtime operational settings in `config.json` live |
| `/api/strict-approval/toggle` | `POST` | Toggles strict approval mode on/off |
| `/api/repos` | `GET` | Lists all configured repositories and their active status |
| `/api/repos/toggle` | `POST` | Toggles repository state: `{"full_name": "owner/repo"}` |
| `/api/repos/add` | `POST` | Adds and validates repository: `{"full_name": "owner/repo"}` |
| `/api/repos/remove` | `POST` | Removes repository: `{"full_name": "owner/repo"}` |
| `/api/trigger` | `POST` | Starts a full scan, or queues a selected PR: `{"force": false, "pr_key": "owner/repo#1"}` |
| `/api/clear-rate-limit` | `POST` | Clears active rate limit cooldown |
| `/api/service/toggle` | `POST` | Pauses or resumes background review worker |
| `/reports/<file>` | `GET` | Serves generated HTML review reports |

---

## 🧪 Testing

Run the included automated test suite locally:
```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

---

## 📄 License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.
