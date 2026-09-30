import os
import re
import json
import logging
import tempfile
from typing import List, Dict, Any, Optional

logger = logging.getLogger("config_manager")

DEFAULT_CONFIG_DIR = os.getenv("CONFIG_DIR", os.path.expanduser("~/.coderabbit"))
DEFAULT_CONFIG_FILE = os.getenv("CONFIG_PATH", os.path.join(DEFAULT_CONFIG_DIR, "config.json"))
DEFAULT_REPOS_BASE_DIR = os.getenv("REPOS_DIR", "/app/repos")

def safe_int_env(key: str, default: int) -> int:
    val = os.getenv(key, "")
    if val is not None and str(val).strip():
        try:
            return int(str(val).strip())
        except ValueError:
            return default
    return default

class ConfigManager:
    """Manages persistent repository and service configuration."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_FILE, repos_base_dir: str = DEFAULT_REPOS_BASE_DIR):
        self.config_path = config_path
        self.github_token_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "github_token")
        self.repos_base_dir = repos_base_dir
        self.ensure_config_exists()

    def get_github_token(self) -> str:
        """Load the persisted token, migrating a legacy environment token once."""
        if os.path.exists(self.github_token_path):
            try:
                with open(self.github_token_path, "r", encoding="utf-8") as token_file:
                    return token_file.read().strip()
            except OSError as e:
                logger.error("Could not read persisted GitHub token: %s", e)
                return ""

        legacy_token = os.getenv("GITHUB_TOKEN", "").strip()
        if legacy_token:
            self.set_github_token(legacy_token)
        return legacy_token

    def set_github_token(self, token: str) -> None:
        """Persist a GitHub token with owner-only file permissions."""
        target_dir = os.path.dirname(os.path.abspath(self.github_token_path))
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".github_token.", dir=target_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as token_file:
                token_file.write(token.strip())
                token_file.flush()
                os.fsync(token_file.fileno())
            os.replace(temp_path, self.github_token_path)
            os.chmod(self.github_token_path, 0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _default_config(self) -> Dict[str, Any]:
        env_repos = os.getenv("REPOSITORIES", "")
        repo_list = []
        if env_repos.strip():
            for repo_name in env_repos.split(","):
                repo_clean = repo_name.strip()
                if repo_clean:
                    repo_list.append({
                        "full_name": repo_clean,
                        "enabled": True,
                        "path": os.path.join(self.repos_base_dir, repo_clean)
                    })

        repo_poll_interval = safe_int_env("REPO_POLL_INTERVAL_SECONDS", safe_int_env("POLL_INTERVAL_SECONDS", 900))
        review_poll_interval = safe_int_env("REVIEW_POLL_INTERVAL_SECONDS", 10)
        max_files = safe_int_env("MAX_FILES_LIMIT", 100)
        auto_approve_val = os.getenv("AUTO_APPROVE", "true")
        auto_approve_env = str(auto_approve_val).lower() in ("true", "1", "yes") if auto_approve_val else True
        strict_approval_val = os.getenv("STRICT_APPROVAL", "true")
        strict_approval_env = str(strict_approval_val).lower() in ("true", "1", "yes") if strict_approval_val else True
        return {
            "repositories": repo_list,
            "coderabbit_accounts": [],
            "poll_interval_seconds": repo_poll_interval,
            "repo_poll_interval_seconds": repo_poll_interval,
            "review_poll_interval_seconds": review_poll_interval,
            "max_files_limit": max_files,
            "auto_approve": auto_approve_env,
            "strict_approval": strict_approval_env,
            "service_enabled": True
        }

    def ensure_config_exists(self) -> None:
        """Creates default config.json if it doesn't already exist."""
        os.makedirs(os.path.dirname(os.path.abspath(self.config_path)), exist_ok=True)
        if not os.path.exists(self.config_path):
            initial_data = self._default_config()
            self.save_config(initial_data)
            logger.info("Initialized default config at %s", self.config_path)

    def load_config(self) -> Dict[str, Any]:
        """Loads configuration from disk with fallback to defaults."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    defaults = self._default_config()
                    # Ensure all standard keys exist
                    for key, val in defaults.items():
                        if key not in data:
                            data[key] = val
                    return data
        except Exception as e:
            logger.error("Error reading config at %s: %s", self.config_path, e)

        return self._default_config()

    def save_config(self, config_data: Dict[str, Any]) -> None:
        """Atomically saves configuration to disk."""
        target_dir = os.path.dirname(os.path.abspath(self.config_path))
        os.makedirs(target_dir, exist_ok=True)
        temp_path = f"{self.config_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
        os.replace(temp_path, self.config_path)

    def get_repos(self) -> List[Dict[str, Any]]:
        """Returns all registered repositories."""
        cfg = self.load_config()
        return cfg.get("repositories", [])

    def get_enabled_repos(self) -> List[Dict[str, Any]]:
        """Returns only enabled repositories for processing."""
        return [r for r in self.get_repos() if r.get("enabled", True)]

    def toggle_repo(self, full_name: str, enabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """Toggles or sets the enabled state for a repository."""
        cfg = self.load_config()
        updated_repo = None
        for repo in cfg.get("repositories", []):
            if repo.get("full_name", "").lower() == full_name.lower():
                if enabled is None:
                    repo["enabled"] = not repo.get("enabled", True)
                else:
                    repo["enabled"] = bool(enabled)
                updated_repo = repo
                break

        if updated_repo:
            self.save_config(cfg)
        return updated_repo

    def add_repo(self, full_name: str, enabled: bool = True, custom_path: Optional[str] = None) -> Dict[str, Any]:
        """Adds a repository if it does not already exist."""
        clean_name = full_name.strip()
        cfg = self.load_config()
        repos = cfg.get("repositories", [])

        for repo in repos:
            if repo.get("full_name", "").lower() == clean_name.lower():
                # Repo already exists; update status
                repo["enabled"] = enabled
                self.save_config(cfg)
                return repo

        repo_entry = {
            "full_name": clean_name,
            "enabled": enabled,
            "path": custom_path or os.path.join(self.repos_base_dir, clean_name)
        }
        repos.append(repo_entry)
        cfg["repositories"] = repos
        self.save_config(cfg)
        return repo_entry

    def remove_repo(self, full_name: str) -> bool:
        """Removes a repository from config."""
        clean_name = full_name.strip().lower()
        cfg = self.load_config()
        repos = cfg.get("repositories", [])
        initial_len = len(repos)
        repos = [r for r in repos if r.get("full_name", "").lower() != clean_name]

        if len(repos) != initial_len:
            cfg["repositories"] = repos
            self.save_config(cfg)
            return True
        return False

    def is_service_enabled(self) -> bool:
        """Checks if the background review service is globally enabled."""
        cfg = self.load_config()
        return bool(cfg.get("service_enabled", True))

    def set_service_enabled(self, enabled: bool) -> bool:
        """Enables or pauses the background review service."""
        cfg = self.load_config()
        cfg["service_enabled"] = bool(enabled)
        self.save_config(cfg)
        return cfg["service_enabled"]

    def is_strict_approval(self) -> bool:
        """Checks if strict approval mode is enabled (any minor issue blocks approval)."""
        cfg = self.load_config()
        return bool(cfg.get("strict_approval", True))

    def set_strict_approval(self, strict: bool) -> bool:
        """Sets strict approval mode."""
        cfg = self.load_config()
        cfg["strict_approval"] = bool(strict)
        self.save_config(cfg)
        return cfg["strict_approval"]

    def get_repo_poll_interval(self) -> int:
        """Returns the interval in seconds between repository discovery scans."""
        cfg = self.load_config()
        return int(cfg.get("repo_poll_interval_seconds", cfg.get("poll_interval_seconds", 60)))

    def get_review_poll_interval(self) -> int:
        """Returns the interval in seconds for review queue checking when idle."""
        cfg = self.load_config()
        return int(cfg.get("review_poll_interval_seconds", 10))

    def get_settings(self) -> Dict[str, Any]:
        """Returns runtime service configuration settings."""
        cfg = self.load_config()
        repo_poll = int(cfg.get("repo_poll_interval_seconds", cfg.get("poll_interval_seconds", 60)))
        review_poll = int(cfg.get("review_poll_interval_seconds", 10))
        return {
            "poll_interval_seconds": repo_poll,
            "repo_poll_interval_seconds": repo_poll,
            "review_poll_interval_seconds": review_poll,
            "max_files_limit": int(cfg.get("max_files_limit", 100)),
            "auto_approve": bool(cfg.get("auto_approve", True)),
            "strict_approval": bool(cfg.get("strict_approval", True)),
            "service_enabled": bool(cfg.get("service_enabled", True)),
            "github_token_configured": bool(self.get_github_token())
        }

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically updates operational settings in config.json."""
        cfg = self.load_config()
        if "repo_poll_interval_seconds" in updates or "poll_interval_seconds" in updates:
            raw_val = updates.get("repo_poll_interval_seconds", updates.get("poll_interval_seconds"))
            try:
                val = int(raw_val)
                if val >= 5:
                    cfg["repo_poll_interval_seconds"] = val
                    cfg["poll_interval_seconds"] = val
            except (ValueError, TypeError):
                pass

        if "review_poll_interval_seconds" in updates:
            try:
                val = int(updates["review_poll_interval_seconds"])
                if val >= 1:
                    cfg["review_poll_interval_seconds"] = val
            except (ValueError, TypeError):
                pass

        if "max_files_limit" in updates:
            try:
                val = int(updates["max_files_limit"])
                if val >= 1:
                    cfg["max_files_limit"] = val
            except (ValueError, TypeError):
                pass

        if "auto_approve" in updates:
            cfg["auto_approve"] = bool(updates["auto_approve"])

        if "strict_approval" in updates:
            cfg["strict_approval"] = bool(updates["strict_approval"])

        if "service_enabled" in updates:
            cfg["service_enabled"] = bool(updates["service_enabled"])

        if updates.get("clear_github_token") is True:
            self.set_github_token("")
        elif isinstance(updates.get("github_token"), str) and updates["github_token"].strip():
            self.set_github_token(updates["github_token"])

        self.save_config(cfg)
        return self.get_settings()

    def get_accounts_dir(self) -> str:
        """Returns the persistent directory for CodeRabbit account profiles."""
        base_dir = os.path.dirname(os.path.abspath(self.config_path))
        accounts_dir = os.path.join(base_dir, "accounts")
        os.makedirs(accounts_dir, exist_ok=True)
        return accounts_dir

    def get_coderabbit_accounts(self) -> List[Dict[str, Any]]:
        """
        Returns configured CodeRabbit accounts, auto-discovering any profiles
        created directly via CLI (e.g. docker exec) in the accounts directory.
        """
        cfg = self.load_config()
        accounts = list(cfg.get("coderabbit_accounts", []))
        accounts_dir = self.get_accounts_dir()

        # Discover profile directories containing auth.json
        discovered_modified = False
        existing_ids = {a.get("id") for a in accounts}
        existing_dirs = {os.path.abspath(a.get("profile_dir", "")) for a in accounts if a.get("type") == "profile"}

        try:
            if os.path.isdir(accounts_dir):
                for entry in sorted(os.listdir(accounts_dir)):
                    entry_path = os.path.join(accounts_dir, entry)
                    if os.path.isdir(entry_path):
                        # A profile directory might have auth.json directly or under .coderabbit/auth.json
                        has_auth = os.path.isfile(os.path.join(entry_path, "auth.json")) or os.path.isfile(os.path.join(entry_path, ".coderabbit", "auth.json"))
                        if has_auth and os.path.abspath(entry_path) not in existing_dirs:
                            account_id = f"profile_{entry}"
                            if account_id not in existing_ids:
                                new_acc = {
                                    "id": account_id,
                                    "name": entry.replace("-", " ").replace("_", " ").title(),
                                    "type": "profile",
                                    "api_key": "",
                                    "profile_dir": entry_path,
                                    "region": "us",
                                    "enabled": True,
                                    "discovered": True
                                }
                                accounts.append(new_acc)
                                existing_ids.add(account_id)
                                existing_dirs.add(os.path.abspath(entry_path))
                                discovered_modified = True
        except Exception as e:
            logger.warning("Error discovering CodeRabbit profiles in %s: %s", accounts_dir, e)

        if discovered_modified:
            cfg["coderabbit_accounts"] = accounts
            self.save_config(cfg)

        return accounts

    def add_coderabbit_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """Adds a new CodeRabbit account to the configuration, automatically preparing profile folders."""
        cfg = self.load_config()
        accounts = cfg.get("coderabbit_accounts", [])

        acc_id = account.get("id") or f"acc_{os.urandom(4).hex()}"
        acc_type = account.get("type", "api_key")
        name = account.get("name", "Account").strip() or "Account"
        profile_dir = account.get("profile_dir", "").strip()

        # If profile account and profile_dir not given, automatically construct and create it
        if acc_type == "profile":
            if not profile_dir:
                safe_folder = re.sub(r'[^a-zA-Z0-9_-]', '_', name.lower()) or acc_id
                profile_dir = os.path.join(self.get_accounts_dir(), safe_folder)
            os.makedirs(profile_dir, exist_ok=True)

        new_account = {
            "id": acc_id,
            "name": name,
            "type": acc_type,
            "api_key": account.get("api_key", "").strip(),
            "profile_dir": profile_dir,
            "region": account.get("region", "us").strip().lower() or "us",
            "enabled": bool(account.get("enabled", True)),
        }
        accounts.append(new_account)
        cfg["coderabbit_accounts"] = accounts
        self.save_config(cfg)
        return new_account

    def update_coderabbit_account(self, account_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Updates an existing CodeRabbit account."""
        cfg = self.load_config()
        accounts = cfg.get("coderabbit_accounts", [])
        target = None
        for acc in accounts:
            if acc.get("id") == account_id:
                target = acc
                break

        if not target:
            return None

        if "name" in updates and str(updates["name"]).strip():
            target["name"] = str(updates["name"]).strip()
        if "type" in updates:
            target["type"] = updates["type"]
        if "api_key" in updates:
            target["api_key"] = updates["api_key"].strip()
        if "profile_dir" in updates:
            target["profile_dir"] = updates["profile_dir"].strip()
        if "region" in updates and updates["region"]:
            target["region"] = updates["region"].strip().lower()
        if "enabled" in updates:
            target["enabled"] = bool(updates["enabled"])

        cfg["coderabbit_accounts"] = accounts
        self.save_config(cfg)
        return target

    def remove_coderabbit_account(self, account_id: str) -> bool:
        """Removes a CodeRabbit account by ID."""
        cfg = self.load_config()
        accounts = cfg.get("coderabbit_accounts", [])
        new_accounts = [acc for acc in accounts if acc.get("id") != account_id]
        if len(new_accounts) != len(accounts):
            cfg["coderabbit_accounts"] = new_accounts
            self.save_config(cfg)
            return True
        return False

