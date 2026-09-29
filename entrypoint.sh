#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  🐰 CodeRabbit PR Auto-Reviewer for CasaOS & Docker"
echo "=========================================================="

# Ensure directories exist
mkdir -p /app/data/reviews
mkdir -p /app/repos

# Configure basic git identity for diffing & fetching
git config --global user.name "${GIT_USER_NAME:-CodeRabbit Auto-Reviewer}"
git config --global user.email "${GIT_USER_EMAIL:-coderabbit-bot@users.noreply.github.com}"
git config --global init.defaultBranch main

# Use the user's manually authenticated CodeRabbit CLI state mounted at /root/.coderabbit.
if [ -f "/root/.coderabbit/auth.json" ]; then
    echo "🔑 Found mounted CodeRabbit CLI login at /root/.coderabbit/auth.json"
else
    echo "⚠️ No CodeRabbit CLI login found at /root/.coderabbit/auth.json."
    echo "   Run 'coderabbit auth login' on a machine with a browser, then mount that CLI home here."
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
