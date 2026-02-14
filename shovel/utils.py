"""Shared utilities: data loading, repo prep, and patch parsing."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def load_instances(dataset: str, split: str | None = None) -> dict[str, dict]:
    """Load instances from JSON, JSONL, or a HuggingFace dataset."""
    path = Path(dataset)
    if path.suffix == ".json":
        with path.open() as f:
            data = json.load(f)
        if isinstance(data, list):
            return {item["instance_id"]: item for item in data}
        return data

    if path.suffix == ".jsonl":
        instances: dict[str, dict] = {}
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                instances[item["instance_id"]] = item
        return instances

    from datasets import load_dataset

    ds = load_dataset(dataset, split=split)
    return {item["instance_id"]: item for item in ds}


def clone_repo(instance: dict, repo_root_dir: str) -> str | None:
    """Clone and checkout the repo for an instance."""
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    repo_dir = os.path.join(repo_root_dir, instance_id)

    if os.path.isdir(repo_dir):
        logger.info("[%s] Repo dir exists, resetting to %s", instance_id, base_commit[:8])
        try:
            subprocess.run(
                ["git", "reset", "--hard", base_commit],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                timeout=120,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                timeout=60,
            )
            return repo_dir
        except Exception as exc:
            logger.error("[%s] Reset failed: %s", instance_id, exc)
            shutil.rmtree(repo_dir, ignore_errors=True)

    logger.info("[%s] Cloning %s@%s", instance_id, repo, base_commit[:8])
    try:
        subprocess.run(
            ["git", "clone", "-o", "origin", f"https://github.com/{repo}", repo_dir],
            check=True,
            capture_output=True,
            timeout=300,
        )
        subprocess.run(
            ["git", "reset", "--hard", base_commit],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            timeout=120,
        )
        return repo_dir
    except Exception as exc:
        logger.error("[%s] Clone failed: %s", instance_id, exc)
        return None


def get_modified_files(patch: str) -> list[str]:
    """Extract modified file paths from a unified diff patch."""
    if not patch or not patch.strip():
        return []
    try:
        from unidiff import PatchSet

        source_files = []
        for file_obj in PatchSet(patch):
            if file_obj.source_file != "/dev/null":
                source_files.append(file_obj.source_file)
        return [x[2:] for x in source_files if x.startswith("a/")]
    except Exception:
        return []


EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
}


def detect_language(files: list[str]) -> str:
    """Detect the primary language from file extensions."""
    lang_counts: dict[str, int] = {}
    for file_path in files:
        for ext, lang in EXTENSION_TO_LANGUAGE.items():
            if file_path.endswith(ext):
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
                break
    if not lang_counts:
        return "python"
    return max(lang_counts, key=lang_counts.get)
