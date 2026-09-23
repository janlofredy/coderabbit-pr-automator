#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  🐰 CodeRabbit PR Auto-Reviewer for CasaOS & Docker"
echo "=========================================================="

# Ensure directories exist
mkdir -p /root/.coderabbit/reviews
mkdir -p /app/repos

# Configure basic git identity for diffing & fetching
git config --global user.name "${GIT_USER_NAME:-CodeRabbit Auto-Reviewer}"
git config --global user.email "${GIT_USER_EMAIL:-coderabbit-bot@users.noreply.github.com}"
git config --global init.defaultBranch main

# Authenticate CodeRabbit CLI if API key is provided
if [ -n "$CODERABBIT_API_KEY" ]; then
    echo "🔑 Configuring CodeRabbit CLI with CODERABBIT_API_KEY..."
    export CODERABBIT_API_KEY="$CODERABBIT_API_KEY"
elif [ -f "/root/.coderabbit/auth.json" ]; then
    echo "🔑 Found mounted CodeRabbit auth at /root/.coderabbit/auth.json"
else
    echo "⚠️ Warning: No CODERABBIT_API_KEY or mounted auth.json detected."
    echo "   Free Tier rate limits and features will apply."
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
