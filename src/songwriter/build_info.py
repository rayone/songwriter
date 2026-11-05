"""Identify the running build, captured once when the server imports this module."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess
import tomllib


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def collect_build_info(root: Path = SOURCE_ROOT) -> dict[str, str | bool | None]:
    try:
        package_version = version("songwriter")
    except PackageNotFoundError:
        try:
            package_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        except (OSError, ValueError, KeyError):
            package_version = "unknown"

    def git(*args: str) -> str | None:
        # Do not accidentally report a parent project's commit for wheel installs.
        if not (root / ".git").exists():
            return None
        try:
            result = subprocess.run(
                # The systemd service reads a root-owned checkout. Trust only
                # this known source directory, without changing global Git config.
                ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
                capture_output=True, text=True,
                timeout=2, check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    commit = git("rev-parse", "--verify", "HEAD")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD") if commit else None
    tag = git("describe", "--tags", "--exact-match", "HEAD") if commit else None
    # Tracked changes only: runtime databases/configuration are not build changes.
    changes = git("status", "--porcelain", "--untracked-files=no") if commit else None
    dirty = bool(changes)
    label = f"v{package_version}"
    if commit:
        label += f" · {commit[:7]}" + (" (modified)" if dirty else "")
    else:
        label += " · commit unavailable"
    return {"version": package_version, "commit": commit, "branch": branch,
            "tag": tag, "dirty": dirty, "label": label}


# Do not read Git on each request: a pull without a restart must not make the
# old server claim to be running the new commit.
BUILD_INFO = collect_build_info()

if __name__ == "__main__":
    print(BUILD_INFO["label"])
