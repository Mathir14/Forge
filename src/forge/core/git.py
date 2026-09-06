"""Git integration service for Forge."""

import subprocess
from pathlib import Path
from typing import List, Optional


class GitService:
    def __init__(self, repo_path: Optional[Path] = None):
        self.repo_path = repo_path or Path.cwd()

    def _run(self, args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git"] + args,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=["git"] + args,
                returncode=124,
                stdout="",
                stderr="Git command timed out",
            )

    def is_git_repo(self) -> bool:
        res = self._run(["rev-parse", "--is-inside-work-tree"])
        return res.returncode == 0 and res.stdout.strip() == "true"

    def status(self) -> str:
        res = self._run(["status", "--short"])
        return res.stdout.strip()

    def diff(self, cached: bool = False) -> str:
        args = ["diff"]
        if cached:
            args.append("--cached")
        res = self._run(args)
        return res.stdout.strip()

    def changed_files(self) -> List[str]:
        """List untracked and modified files relative to repo root."""
        res = self._run(["-c", "core.quotepath=false", "status", "--porcelain"])
        if res.returncode != 0 or not res.stdout.strip():
            return []
        files = []
        for line in res.stdout.splitlines():
            if len(line) >= 4:
                raw_path = line[3:]
                if " -> " in raw_path:
                    path = raw_path.split(" -> ", 1)[1].strip()
                else:
                    path = raw_path.strip()
                path = path.strip('"')
                if path:
                    files.append(path)
        return files

    def current_branch(self) -> str:
        res = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        return res.stdout.strip() if res.returncode == 0 else ""

    def commit(self, message: str) -> bool:
        if not self.changed_files():
            return False
        self._run(["add", "-A"])
        res = self._run(["commit", "-m", message])
        return res.returncode == 0

    def restore(self, paths: Optional[List[str]] = None) -> bool:
        if paths:
            res = self._run(["restore"] + paths)
        else:
            res = self._run(["restore", "."])
        return res.returncode == 0
