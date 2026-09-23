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
- **Headless CodeRabbit CLI Execution**: Leverages the official CodeRabbit CLI in agent mode (`--agent --base <ref>`) with 240-second timeout detection.
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

---

## 🏠 CasaOS Setup (1-Click Import)

CasaOS makes deploying Docker apps seamless. Follow these steps:

1. Open your **CasaOS Web UI**.
2. Click the **App Store** icon.
3. In the top-right corner of the App Store, click **Custom Install**.
4. Click **Import** in the top-right corner of the modal.
5. Copy the contents of [`docker-compose.yml`](docker-compose.yml) and paste it into the text area, then click **Submit**.
6. In the configuration form:
   - Enter your `GITHUB_TOKEN` under **Environment Variables**.
   - (Optional) Enter your `CODERABBIT_API_KEY` or leave blank if mounting `~/.coderabbit/auth.json`.
   - Update `REPOSITORIES` to your comma-separated repository list.
7. Click **Install**.
8. Once installed, click the **CodeRabbit Auto-Reviewer** icon on your CasaOS dashboard to open the Web UI at `http://<casaos-ip>:8765`.

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
Edit `.env` with your preferred editor:
```env
GITHUB_TOKEN=ghp_yourPersonalAccessTokenHere
REPOSITORIES=cictd-isds/chrmd-web,cictd-isds/chrmd-api
POLL_INTERVAL_SECONDS=900
MAX_FILES_LIMIT=100
AUTO_APPROVE=true
CODERABBIT_API_KEY=
PORT=8765
```

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
The system supports two headless authentication methods:
- **API Key**: Set `CODERABBIT_API_KEY` in your `.env` or Compose file.
- **Mounted Auth File**: If you have already authenticated CodeRabbit locally on your host machine, mount your `~/.coderabbit/auth.json` into the container volume (`/root/.coderabbit/auth.json`).

---

## ⚙️ Environment Variables Reference

| Variable | Description | Default |
| :--- | :--- | :--- |
| `GITHUB_TOKEN` | GitHub Personal Access Token (`repo` scope) | *Required* |
| `REPOSITORIES` | Comma-separated default repositories to monitor | `cictd-isds/chrmd-web,cictd-isds/chrmd-api` |
| `POLL_INTERVAL_SECONDS` | Interval between background PR scans | `900` (15 minutes) |
| `MAX_FILES_LIMIT` | Maximum modified file threshold for reviews | `100` |
| `AUTO_APPROVE` | Automatically submit `APPROVE` on 0 major issues | `true` |
| `CODERABBIT_API_KEY` | Optional CodeRabbit API key | *Empty* |
| `PORT` | Web dashboard listening port | `8765` |
| `CONFIG_DIR` | Directory holding `config.json` and state files | `/root/.coderabbit` |
| `REVIEWS_DIR` | Directory storing generated HTML reports | `/root/.coderabbit/reviews` |
| `REPOS_DIR` | Directory caching cloned Git repositories | `/app/repos` |

---

## 📡 REST API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/status` | `GET` | Returns aggregated status, active rate limits, and PR list |
| `/api/repos` | `GET` | Lists all configured repositories and their active status |
| `/api/repos/toggle` | `POST` | Toggles repository state: `{"full_name": "owner/repo"}` |
| `/api/repos/add` | `POST` | Adds and validates repository: `{"full_name": "owner/repo"}` |
| `/api/repos/remove` | `POST` | Removes repository: `{"full_name": "owner/repo"}` |
| `/api/trigger` | `POST` | Triggers review: `{"force": false, "pr_key": "owner/repo#1"}` |
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
