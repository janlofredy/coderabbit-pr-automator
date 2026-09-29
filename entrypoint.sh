#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  🐰 CodeRabbit PR Auto-Reviewer for CasaOS & Docker"
echo "=========================================================="

# Ensure directories exist
mkdir -p /app/data/config /app/data/reviews /app/data/repos
mkdir -p /app/repos

# In the single-volume CasaOS layout, link CodeRabbit's standard home to its
# dedicated subdirectory. Docker Compose may instead mount ~/.coderabbit here.
if [ -L /root/.coderabbit ]; then
    ln -sfn /app/data/coderabbit-cli /root/.coderabbit
elif [ ! -e /root/.coderabbit ]; then
    mkdir -p /app/data/coderabbit-cli
    ln -s /app/data/coderabbit-cli /root/.coderabbit
fi

# Configure basic git identity for diffing & fetching
git config --global user.name "${GIT_USER_NAME:-CodeRabbit Auto-Reviewer}"
git config --global user.email "${GIT_USER_EMAIL:-coderabbit-bot@users.noreply.github.com}"
git config --global init.defaultBranch main

# Use the user's manually authenticated CodeRabbit CLI state in its configured home.
CLI_AUTH_DIR="/root/.coderabbit"
if [ -f "${CLI_AUTH_DIR}/auth.json" ]; then
    echo "🔑 Found mounted CodeRabbit CLI login at ${CLI_AUTH_DIR}/auth.json"
else
    echo "⚠️ No CodeRabbit CLI login found at ${CLI_AUTH_DIR}/auth.json."
    echo "   Open the dashboard and use 'Login with Google' to authenticate the CLI, or mount an existing CLI login."
fi

# Authenticate GitHub CLI / git credentials if GITHUB_TOKEN is provided
if [ -n "$GITHUB_TOKEN" ]; then
    echo "🔑 Configuring GitHub credentials..."
    echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > /root/.git-credentials
    git config --global credential.helper store
else
    echo "⚠️ Warning: GITHUB_TOKEN is not set. GitHub API requests will be unauthenticated or fail."
fi

echo "🚀 Starting CodeRabbit Auto-Review Web Dashboard on port ${PORT:-8765}..."
exec python3 /app/web_dashboard.py
