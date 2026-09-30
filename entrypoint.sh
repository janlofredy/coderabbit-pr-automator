#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  🐰 CodeRabbit PR Auto-Reviewer for CasaOS & Docker"
echo "=========================================================="

# Ensure directories exist
mkdir -p /app/data/config/accounts /app/data/reviews /app/data/repos
mkdir -p /app/repos

# Keep CodeRabbit's home on persistent storage. CasaOS mounts /app/data and
# stores CLI state under /app/data/coderabbit-cli; standard Compose mounts the
# host CLI home directly at /root/.coderabbit.
mkdir -p /app/data/coderabbit-cli
if [ -L /root/.coderabbit ]; then
    ln -sfn /app/data/coderabbit-cli /root/.coderabbit
elif awk '$5 == "/root/.coderabbit" { mounted=1 } END { exit !mounted }' /proc/self/mountinfo; then
    echo "🔒 Using mounted CodeRabbit CLI home at /root/.coderabbit"
else
    # The CLI installer can create this directory in the image. Preserve any
    # existing credentials before replacing that ephemeral directory with the
    # link into the persistent app-data volume.
    if [ -d /root/.coderabbit ]; then
        cp -an /root/.coderabbit/. /app/data/coderabbit-cli/
        rm -rf /root/.coderabbit
    elif [ -e /root/.coderabbit ]; then
        rm -f /root/.coderabbit
    fi
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
    echo "   Browser OAuth is unavailable in Docker. Run 'coderabbit auth login' on the Docker host, or mount an existing CLI login."
fi

# GitHub access now uses the token loaded from persistent dashboard settings.
# Remove legacy credential-helper copies so a replaced/cleared token is not
# retained separately from the persistent token file.
git config --global --unset-all credential.helper || true
rm -f /root/.git-credentials

echo "🚀 Starting CodeRabbit Auto-Review Web Dashboard on port ${PORT:-8765}..."
exec python3 /app/web_dashboard.py
