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
  - Complete raw CodeRabbit CLI stdout and stderr are written to container logs and retained in each PR's **Details & Logs** history.
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
6. Click **Install**.
7. Once installed, open the **CodeRabbit Auto-Reviewer** tile at `http://<casaos-ip>:8765`, then add your GitHub token in **Settings**.

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
GitHub authentication is configured in **Settings** after the dashboard starts. Existing installations with `GITHUB_TOKEN` in `.env` are migrated automatically to the persistent config directory the first time the updated container starts. All other settings including monitored repositories, scan intervals, limits, and approval policies are managed directly from the Web Dashboard.

### 3. Start the Container
```bash
docker compose up -d
```

Access the dashboard at `http://localhost:8765`.

---

## 🔑 Authentication Options

### 1. GitHub Authentication
Open **Settings** in the dashboard and enter a GitHub Personal Access Token with repository access (`repo` for private repositories or `public_repo` for public repositories). The token is saved in the persistent configuration storage with owner-only file permissions and is never returned by the settings API. Existing `GITHUB_TOKEN` environment values are automatically migrated there on startup.

### 2. CodeRabbit Authentication & Multi-Account Support

To maximize review throughput and avoid being stopped by free tier rate limits (e.g. 429 backoffs or quota ceilings), CodeRabbit PR Auto-Reviewer supports **Multiple CodeRabbit Accounts** with automated round-robin rotation and failure-fallback.

You can manage accounts directly in the Web Dashboard by clicking the **🐰 CodeRabbit icon** in the top navigation bar:

#### Method A: CodeRabbit API Key (Recommended & Headless)
1. Log in to [CodeRabbit.ai](https://coderabbit.ai) with your desired account.
2. In user or organization settings, generate a **CLI / Agent API Key**.
3. In the Web Dashboard modal, click **Add Another CodeRabbit Account**, choose **API Key**, specify your region (`US` or `EU`), and paste the key.
4. Click **Test** to immediately verify that the CLI can authenticate with that account.

#### Method B: Direct Docker CLI Login (OAuth / Google / GitHub)
You don't need to copy files between machines or manually enter folder paths. Simply run the login command directly inside the Docker container with an isolated home directory (replace `account2` with whatever profile name you want):
```bash
docker exec -it coderabbit-pr-automator sh -c 'HOME=/app/data/config/accounts/account2 coderabbit auth login'
```
1. Follow the link printed in the terminal to complete Google/GitHub OAuth authentication in your browser.
2. The credentials are saved straight to persistent container storage under `/app/data/config/accounts/account2`.
3. Open the Web Dashboard and click **🔄 Recheck** — the system **auto-discovers** the new profile account and adds it into rotation automatically!

#### Method C: Default Host Mount (Automatic Fallback)
If no accounts are configured in the dashboard, the application automatically uses the mounted host CLI credentials located at `~/.coderabbit` (or `/DATA/AppData/coderabbit/coderabbit-cli` in CasaOS).

---

## ⚙️ Environment Variables Reference

| Variable | Description | Default |
| :--- | :--- | :--- |
| `GITHUB_TOKEN` | Legacy GitHub token automatically migrated to persistent settings; new setups should use the dashboard Settings | Optional |
| `CODERABBIT_CLI_HOME` | Host path containing the authenticated CodeRabbit CLI state mounted by Docker Compose | `${HOME}/.coderabbit` |

> 💡 **Note**: Repository settings, polling intervals, max file limits, auto-approvals, strict approval mode, and CodeRabbit multi-account configurations are persisted in the app's mounted data directory and can be changed live from the Web Dashboard without recreating or restarting the container.

---

## 📡 REST API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/status` | `GET` | Returns aggregated status, active rate limits, strict approval mode, PR list, review queue items, and account rotation statuses |
| `/api/coderabbit-auth` | `GET` | Returns the default CodeRabbit CLI authentication status |
| `/api/coderabbit-accounts` | `GET` | Returns all configured CodeRabbit accounts with masked credentials, active states, and cooldowns |
| `/api/coderabbit-accounts/add` | `POST` | Adds a new CodeRabbit account (`name`, `type`, `api_key` or `profile_dir`, `region`) |
| `/api/coderabbit-accounts/update` | `POST` | Updates an existing account (`id`, `name`, `enabled`, etc.) |
| `/api/coderabbit-accounts/remove` | `POST` | Deletes a CodeRabbit account (`id`) |
| `/api/coderabbit-accounts/test` | `POST` | Probes CodeRabbit CLI authentication for an account |
| `/api/coderabbit-accounts/clear-cooldown` | `POST` | Clears rate limit cooldown for a specific account (`id`) |
| `/api/config` | `GET` | Returns runtime operational settings (`poll_interval_seconds`, `max_files_limit`, etc.) |
| `/api/config` | `POST` | Updates runtime operational settings in `config.json` live |
| `/api/strict-approval/toggle` | `POST` | Toggles strict approval mode on/off |
| `/api/repos` | `GET` | Lists all configured repositories and their active status |
| `/api/repos/toggle` | `POST` | Toggles repository state: `{"full_name": "owner/repo"}` |
| `/api/repos/add` | `POST` | Adds and validates repository: `{"full_name": "owner/repo"}` |
| `/api/repos/remove` | `POST` | Removes repository: `{"full_name": "owner/repo"}` |
| `/api/trigger` | `POST` | Starts a full scan, or queues a selected PR: `{"force": false, "pr_key": "owner/repo#1"}` |
| `/api/clear-rate-limit` | `POST` | Clears active rate limit cooldown globally |
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
