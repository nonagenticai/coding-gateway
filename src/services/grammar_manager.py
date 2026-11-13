"""
Tree-sitter Grammar Manager Service

Manages Tree-sitter grammar installation, updates, and version control
for the multi-language analysis system.

Extracted from the monolith's app/services/grammar_manager.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GrammarInfo:
    """Information about a Tree-sitter grammar."""

    name: str
    language: str
    repository_url: str
    version: str
    installed_version: str | None = None
    last_checked: datetime | None = None
    last_updated: datetime | None = None
    install_path: Path | None = None
    sha256_hash: str | None = None
    is_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        if self.last_checked:
            data["last_checked"] = self.last_checked.isoformat()
        if self.last_updated:
            data["last_updated"] = self.last_updated.isoformat()
        if self.install_path:
            data["install_path"] = str(self.install_path)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrammarInfo:
        """Create from dictionary loaded from JSON."""
        if data.get("last_checked"):
            data["last_checked"] = datetime.fromisoformat(data["last_checked"])
        if data.get("last_updated"):
            data["last_updated"] = datetime.fromisoformat(data["last_updated"])
        if data.get("install_path"):
            data["install_path"] = Path(data["install_path"])
        return cls(**data)


class TreeSitterGrammarManager:
    """Manages Tree-sitter grammars for multiple languages."""

    # Official Tree-sitter grammar repositories
    OFFICIAL_GRAMMARS = {
        "java": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-java",
            "package_name": "tree-sitter-java",
        },
        "rust": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-rust",
            "package_name": "tree-sitter-rust",
        },
        "go": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-go",
            "package_name": "tree-sitter-go",
        },
        "python": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-python",
            "package_name": "tree-sitter-python",
        },
        "javascript": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-javascript",
            "package_name": "tree-sitter-javascript",
        },
        "typescript": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-typescript",
            "package_name": "tree-sitter-typescript",
        },
        "c": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-c",
            "package_name": "tree-sitter-c",
        },
        "cpp": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-cpp",
            "package_name": "tree-sitter-cpp",
        },
        "csharp": {
            "repository_url": "https://github.com/tree-sitter/tree-sitter-c-sharp",
            "package_name": "tree-sitter-c-sharp",
        },
    }

    def __init__(self, cache_dir: Path | None = None) -> None:
        """Initialize the grammar manager.

        Args:
            cache_dir: Directory to store grammars and cache data
        """
        self.cache_dir = (
            cache_dir or Path(os.getenv("TEMP_DIR", "/tmp/code_graph")) / "tree_sitter_grammars"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.grammars_file = self.cache_dir / "grammars.json"
        self.grammars: dict[str, GrammarInfo] = {}

        # Configuration
        self.check_interval_hours = 24
        self.auto_update = False

    async def initialize(self) -> None:
        """Initialize the grammar manager and load existing data."""
        await self._load_grammar_info()

        for language, config in self.OFFICIAL_GRAMMARS.items():
            if language not in self.grammars:
                self.grammars[language] = GrammarInfo(
                    name=config["package_name"],
                    language=language,
                    repository_url=config["repository_url"],
                    version="unknown",
                )

        await self._save_grammar_info()
        logger.info("Grammar manager initialized with %d languages", len(self.grammars))

    async def check_for_updates(self, languages: list[str] | None = None) -> dict[str, bool]:
        """Check for grammar updates.

        Args:
            languages: Specific languages to check, or None for all

        Returns:
            Dictionary mapping language to whether update is available
        """
        if languages is None:
            languages = list(self.grammars.keys())

        update_status: dict[str, bool] = {}

        for language in languages:
            if language not in self.grammars:
                continue

            grammar = self.grammars[language]

            if (
                not self.auto_update
                and grammar.last_checked
                and datetime.now(UTC) - grammar.last_checked
                < timedelta(hours=self.check_interval_hours)
            ):
                update_status[language] = False
                continue

            try:
                latest_version = await self._get_latest_version(grammar)
                grammar.last_checked = datetime.now(UTC)

                if latest_version and latest_version != grammar.installed_version:
                    grammar.version = latest_version
                    update_status[language] = True
                    logger.info(
                        "Update available for %s: %s -> %s",
                        language,
                        grammar.installed_version,
                        latest_version,
                    )
                else:
                    update_status[language] = False

            except Exception as e:
                logger.error("Failed to check updates for %s: %s", language, e)
                update_status[language] = False

        await self._save_grammar_info()
        return update_status

    async def install_grammar(self, language: str, force: bool = False) -> bool:
        """Install or update a Tree-sitter grammar.

        Args:
            language: Language to install grammar for
            force: Force reinstallation even if already installed

        Returns:
            True if installation succeeded
        """
        if language not in self.grammars:
            logger.error("Unknown language: %s", language)
            return False

        grammar = self.grammars[language]

        if not force and grammar.is_available and grammar.installed_version == grammar.version:
            logger.info("Grammar for %s is already up to date", language)
            return True

        logger.info("Installing Tree-sitter grammar for %s...", language)

        try:
            language_dir = self.cache_dir / language
            language_dir.mkdir(exist_ok=True)

            success = await self._download_and_install_grammar(grammar, language_dir)

            if success:
                grammar.installed_version = grammar.version
                grammar.last_updated = datetime.now(UTC)
                grammar.install_path = language_dir
                grammar.is_available = True
                logger.info(
                    "Successfully installed %s grammar version %s",
                    language,
                    grammar.version,
                )
            else:
                grammar.is_available = False
                logger.error("Failed to install %s grammar", language)

            await self._save_grammar_info()
            return success

        except (OSError, shutil.Error) as e:
            logger.error("Error installing %s grammar: %s", language, e)
            return False

    async def install_all_grammars(self, force: bool = False) -> dict[str, bool]:
        """Install grammars for all supported languages.

        Args:
            force: Force reinstallation of all grammars

        Returns:
            Dictionary mapping language to installation success
        """
        results: dict[str, bool] = {}

        for language in self.grammars:
            try:
                results[language] = await self.install_grammar(language, force)
            except (OSError, shutil.Error) as e:
                logger.error("Error installing %s: %s", language, e)
                results[language] = False

        success_count = sum(1 for success in results.values() if success)
        logger.info("Installed %d/%d grammars successfully", success_count, len(results))
        return results

    async def get_grammar_status(self) -> dict[str, dict[str, Any]]:
        """Get status information for all grammars."""
        status: dict[str, dict[str, Any]] = {}

        for language, grammar in self.grammars.items():
            status[language] = {
                "language": grammar.language,
                "installed": grammar.is_available,
                "installed_version": grammar.installed_version,
                "latest_version": grammar.version,
                "needs_update": (
                    grammar.version != grammar.installed_version
                    if grammar.installed_version
                    else False
                ),
                "last_checked": (
                    grammar.last_checked.isoformat() if grammar.last_checked else None
                ),
                "last_updated": (
                    grammar.last_updated.isoformat() if grammar.last_updated else None
                ),
                "repository_url": grammar.repository_url,
            }

        return status

    async def cleanup_old_versions(self) -> dict[str, int]:
        """Clean up old grammar versions to save space.

        Returns:
            Dictionary mapping language to number of old versions removed
        """
        cleanup_results: dict[str, int] = {}

        for language, grammar in self.grammars.items():
            if not grammar.install_path or not grammar.install_path.exists():
                cleanup_results[language] = 0
                continue

            try:
                version_dirs = [
                    d
                    for d in grammar.install_path.iterdir()
                    if d.is_dir() and d.name != grammar.installed_version
                ]

                removed_count = 0
                for version_dir in version_dirs:
                    shutil.rmtree(version_dir)
                    removed_count += 1

                cleanup_results[language] = removed_count

                if removed_count > 0:
                    logger.info("Cleaned up %d old versions for %s", removed_count, language)

            except (OSError, shutil.Error) as e:
                logger.error("Error cleaning up %s: %s", language, e)
                cleanup_results[language] = 0

        return cleanup_results

    async def _get_latest_version(self, grammar: GrammarInfo) -> str | None:
        """Get the latest version from GitHub API."""
        try:
            import aiohttp

            parts = grammar.repository_url.rstrip("/").split("/")
            owner, repo = parts[-2], parts[-1]

            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"

            async with aiohttp.ClientSession() as session:
                async with session.get(api_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return str(data.get("tag_name", "unknown"))
                    else:
                        commits_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
                        async with session.get(commits_url) as commits_response:
                            if commits_response.status == 200:
                                commits = await commits_response.json()
                                if commits:
                                    return str(commits[0]["sha"][:8])

        except Exception as e:
            logger.debug("Failed to get version for %s: %s", grammar.name, e)

        return None

    async def _download_and_install_grammar(self, grammar: GrammarInfo, install_dir: Path) -> bool:
        """Download and install a grammar from GitHub."""
        try:
            clone_url = grammar.repository_url + ".git"

            if install_dir.exists():
                shutil.rmtree(install_dir)

            process = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "1",
                clone_url,
                str(install_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            _, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error("Git clone failed: %s", stderr.decode())
                return False

            grammar_js = install_dir / "grammar.js"

            if not grammar_js.exists():
                if grammar.language == "typescript":
                    typescript_grammar = install_dir / "typescript" / "grammar.js"
                    tsx_grammar = install_dir / "tsx" / "grammar.js"

                    if typescript_grammar.exists() or tsx_grammar.exists():
                        logger.info("Found TypeScript grammar in subdirectory")
                        grammar.sha256_hash = await self._calculate_directory_hash(install_dir)
                        return True
                    else:
                        logger.error("No grammar.js found in TypeScript subdirectories")
                        return False
                else:
                    logger.error("No grammar.js found in %s", install_dir)
                    return False

            grammar.sha256_hash = await self._calculate_directory_hash(install_dir)
            return True

        except (TimeoutError, OSError) as e:
            logger.error("Error downloading grammar: %s", e)
            return False

    async def _calculate_directory_hash(self, directory: Path) -> str:
        """Calculate SHA-256 hash of directory contents."""
        hasher = hashlib.sha256()

        for file_path in sorted(directory.rglob("*")):
            if file_path.is_file():
                try:
                    with open(file_path, "rb") as f:
                        while True:
                            chunk = f.read(8192)
                            if not chunk:
                                break
                            hasher.update(chunk)
                except OSError:
                    continue

        return hasher.hexdigest()

    async def _load_grammar_info(self) -> None:
        """Load grammar information from JSON file."""
        if not self.grammars_file.exists():
            return

        try:
            with open(self.grammars_file) as f:
                data = json.load(f)

            for language, grammar_data in data.items():
                self.grammars[language] = GrammarInfo.from_dict(grammar_data)

            logger.debug("Loaded grammar info for %d languages", len(self.grammars))

        except (OSError, json.JSONDecodeError) as e:
            logger.error("Error loading grammar info: %s", e)

    async def _save_grammar_info(self) -> None:
        """Save grammar information to JSON file."""
        try:
            data = {language: grammar.to_dict() for language, grammar in self.grammars.items()}

            with open(self.grammars_file, "w") as f:
                json.dump(data, f, indent=2)

        except OSError as e:
            logger.error("Error saving grammar info: %s", e)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_grammar_manager_instance: TreeSitterGrammarManager | None = None


def get_grammar_manager() -> TreeSitterGrammarManager:
    """Get or create the singleton grammar manager."""
    global _grammar_manager_instance
    if _grammar_manager_instance is None:
        _grammar_manager_instance = TreeSitterGrammarManager()
        logger.info("Initialized Tree-sitter grammar manager")
    return _grammar_manager_instance
