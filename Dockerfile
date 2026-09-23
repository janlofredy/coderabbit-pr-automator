# Multi-arch Linux Dockerfile for CodeRabbit PR Auto-Reviewer
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/root/.coderabbit \
    REVIEWS_DIR=/root/.coderabbit/reviews \
    REPOS_DIR=/app/repos \
    PORT=8765 \
    PATH="/root/.local/bin:/usr/local/bin:$PATH"

# Install system dependencies & CodeRabbit prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    unzip \
    ca-certificates \
    libsecret-1-0 \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install official CodeRabbit CLI to /usr/local/bin (with CI=1 to skip interactive prompts)
RUN CI=1 CODERABBIT_INSTALL_DIR=/usr/local/bin curl -fsSL https://cli.coderabbit.ai/install.sh | sh

# Set up application workspace
WORKDIR /app

# Copy application files
COPY config_manager.py state_manager.py github_client.py auto_review_prs.py web_dashboard.py dashboard.html entrypoint.sh /app/

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Create required volume directories
RUN mkdir -p /root/.coderabbit/reviews /app/repos

# Expose Web Dashboard Port
EXPOSE 8765

ENTRYPOINT ["/app/entrypoint.sh"]
