import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set


@dataclass
class FileSnapshot:
    """Snapshot of a file's state in Git index and working tree."""

    path: str
    staged_code: str
    unstaged_code: str
    index_sha: Optional[str]
    worktree_hash: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "staged_code": self.staged_code,
            "unstaged_code": self.unstaged_code,
            "index_sha": self.index_sha,
            "worktree_hash": self.worktree_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileSnapshot":
        return cls(
            path=data["path"],
            staged_code=data.get("staged_code", ""),
            unstaged_code=data.get("unstaged_code", ""),
            index_sha=data.get("index_sha"),
            worktree_hash=data.get("worktree_hash"),
        )


@dataclass
class GitBaseline:
    """Repository state snapshot captured at the beginning of a Forge run."""

    head_commit: Optional[str]
    dirty_files: Dict[str, FileSnapshot]
    captured_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "head_commit": self.head_commit,
            "dirty_files": {k: v.to_dict() for k, v in self.dirty_files.items()},
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GitBaseline":
        return cls(
            head_commit=data.get("head_commit"),
            dirty_files={
                k: FileSnapshot.from_dict(v)
                for k, v in data.get("dirty_files", {}).items()
            },
            captured_at=data.get("captured_at", ""),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "GitBaseline":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


@dataclass
class ChangeAttribution:
    """Attribution of workspace changes relative to a Git baseline."""

    pure_forge_changes: List[str]
    mixed_ownership_changes: List[str]
    preexisting_unchanged: List[str]

    @property
    def all_run_relevant_changes(self) -> List[str]:
        """All files touched during the Forge run (pure Forge + mixed)."""
        res = list(self.pure_forge_changes)
        for f in self.mixed_ownership_changes:
            if f not in res:
                res.append(f)
        return res


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
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=["git"] + args,
                returncode=124,
                stdout="",
                stderr="Git command timed out",
            )
        except (FileNotFoundError, OSError, ValueError) as e:
            return subprocess.CompletedProcess(
                args=["git"] + args,
                returncode=127,
                stdout="",
                stderr=str(e),
            )

    PROTECTED_PATH_PATTERNS: List[str] = [
        ":(exclude).forge",
        ":(exclude).forge/**",
        ":(exclude).env",
        ":(exclude).env*",
        ":(exclude)**/.env*",
        ":(exclude)*credential*",
        ":(exclude)**/*credential*",
        ":(exclude)*token*",
        ":(exclude)**/*token*",
        ":(exclude)*.key",
        ":(exclude)**/*.key",
        ":(exclude)*.pem",
        ":(exclude)**/*.pem",
        ":(exclude)*id_rsa*",
        ":(exclude)**/*id_rsa*",
        ":(exclude)*id_ed25519*",
        ":(exclude)**/*id_ed25519*",
        ":(exclude)*client_secret*",
        ":(exclude)**/*client_secret*",
        ":(exclude)*secrets.*",
        ":(exclude)**/*secrets.*",
        ":(exclude)secrets/**",
        ":(exclude)**/secrets/**",
        ":(exclude).secrets/**",
        ":(exclude)**/.secrets/**",
        ":(exclude)*secret_*",
        ":(exclude)**/*secret_*",
        ":(exclude)*secret-*",
        ":(exclude)**/*secret-*",
    ]

    @classmethod
    def is_protected_path(cls, path_str: str) -> bool:
        """Check whether a path corresponds to a protected internal or sensitive path."""
        if not path_str:
            return False
        normalized = str(path_str).replace("\\", "/").strip()
        if normalized.startswith('"') and normalized.endswith('"') and len(normalized) >= 2:
            normalized = normalized[1:-1]
        parts = Path(normalized).parts
        if not parts:
            return False
        if parts[0] == ".forge" or parts[0].startswith(".forge"):
            return True
        # Protect directories named secrets or .secrets
        if any(part.lower() in ("secrets", ".secrets") for part in parts[:-1]):
            return True
        if parts[-1].lower() in ("secrets", ".secrets"):
            return True
        filename = parts[-1].lower()
        # Protect .env files
        if any(part.startswith(".env") for part in parts):
            return True
        # Protect credential and secret files
        sensitive_substrings = ["credential", "token", "client_secret"]
        if any(sub in filename for sub in sensitive_substrings):
            return True
        # Protect SSH / SSL keys and certs
        if any(filename.startswith(p) for p in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
            return True
        if filename.endswith((".key", ".pem", ".p12", ".pfx", ".pkcs12")):
            return True
        if filename.startswith(("secrets.", "secret.", "secret_", "secrets_", "secret-", "secrets-")):
            return True
        return False

    def toplevel(self) -> Path:
        """Get the absolute path to the Git repository root."""
        res = self._run(["rev-parse", "--show-toplevel"])
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip())
        return self.repo_path

    def is_git_repo(self) -> bool:
        res = self._run(["rev-parse", "--is-inside-work-tree"])
        return res.returncode == 0 and res.stdout.strip() == "true"

    def _parse_status_z(self) -> List[Tuple[str, str, str, Optional[str]]]:
        """Parse 'git status --porcelain=v1 -z -uall' output into (staged_code, unstaged_code, path, orig_path).

        Using -z guarantees filenames with spaces, quotes, newlines, or special characters
        are emitted unquoted and bit-exact without C-style escaping.
        """
        res = self._run(["-c", "core.quotepath=false", "status", "--porcelain=v1", "-z", "-uall"])
        if res.returncode != 0 or not res.stdout:
            return []

        entries: List[Tuple[str, str, str, Optional[str]]] = []
        if "\0" in res.stdout:
            raw_tokens = res.stdout.split("\0")
            i = 0
            while i < len(raw_tokens):
                token = raw_tokens[i]
                if not token or len(token) < 3:
                    i += 1
                    continue
                staged_code = token[0]
                unstaged_code = token[1]
                path = token[3:]  # format is 'XY <path>' (target/new path for renames/copies)
                orig_path: Optional[str] = None
                if staged_code in ("R", "C") or unstaged_code in ("R", "C"):
                    # For renames/copies, next token in git status -z is orig/source path
                    i += 1
                    if i < len(raw_tokens):
                        orig_path = raw_tokens[i]
                entries.append((staged_code, unstaged_code, path, orig_path))
                i += 1
        else:
            # Fallback for mock environments returning newline-delimited output
            for line in res.stdout.splitlines():
                line_clean = line.strip("\r\n")
                if not line_clean or len(line_clean) < 3:
                    continue
                staged_code = line_clean[0]
                unstaged_code = line_clean[1]
                rest = line_clean[3:].strip()
                orig_path = None
                if " -> " in rest:
                    parts = rest.split(" -> ", 1)
                    orig_path = parts[0].strip().strip('"')
                    path = parts[1].strip().strip('"')
                else:
                    path = rest.strip('"')
                entries.append((staged_code, unstaged_code, path, orig_path))

        return entries

    def status(self) -> str:
        entries = self._parse_status_z()
        if not entries:
            return ""
        lines = []
        for staged, unstaged, path, orig in entries:
            if self.is_protected_path(path) or (orig and self.is_protected_path(orig)):
                continue
            if orig:
                lines.append(f"{staged}{unstaged} {orig} -> {path}")
            else:
                lines.append(f"{staged}{unstaged} {path}")
        return "\n".join(lines)

    def diff(self, cached: bool = False, include_untracked: bool = True) -> str:
        pathspec = ["--", "."] + self.PROTECTED_PATH_PATTERNS
        if cached:
            args = ["diff", "--cached"] + pathspec
            res = self._run(args)
            diff_text = res.stdout.strip()
        else:
            res_unstaged = self._run(["diff"] + pathspec)
            unstaged_text = res_unstaged.stdout.strip()
            res_cached = self._run(["diff", "--cached"] + pathspec)
            cached_text = res_cached.stdout.strip()
            if unstaged_text and cached_text:
                diff_text = f"{cached_text}\n{unstaged_text}"
            else:
                diff_text = unstaged_text or cached_text

        if include_untracked:
            # Capture newly created untracked files as unified diffs against /dev/null
            entries = self._parse_status_z()
            if entries:
                untracked_diffs = []
                count = 0
                MAX_UNTRACKED_FILES = 20
                MAX_FILE_SIZE_BYTES = 1_000_000  # 1MB
                toplevel = self.toplevel().resolve()
                repo_resolved = self.repo_path.resolve()
                for staged, unstaged, raw_path, _ in entries:
                    if staged == "?" and unstaged == "?":
                        if self.is_protected_path(raw_path):
                            continue

                        cand = repo_resolved / raw_path
                        if not cand.exists() and not cand.is_symlink():
                            cand = toplevel / raw_path

                        is_sym = cand.is_symlink()
                        if is_sym:
                            rel_posix = raw_path
                            abs_file = cand
                        else:
                            abs_file = cand.resolve()
                            if not abs_file.is_relative_to(repo_resolved):
                                continue
                            rel_path = abs_file.relative_to(repo_resolved)
                            rel_posix = str(rel_path.as_posix())

                        if self.is_protected_path(rel_posix):
                            continue
                        if not is_sym and not abs_file.is_file():
                            continue

                        if count >= MAX_UNTRACKED_FILES:
                            untracked_diffs.append("[... Additional untracked files truncated ...]")
                            break
                        try:
                            if not is_sym and abs_file.stat().st_size > MAX_FILE_SIZE_BYTES:
                                untracked_diffs.append(f"[File {rel_posix} exceeds size limit (1MB), skipped diff]")
                                count += 1
                                continue
                            diff_res = self._run(["diff", "--no-index", "--", "/dev/null", rel_posix])
                            if diff_res.stdout.strip():
                                untracked_diffs.append(diff_res.stdout.strip())
                                count += 1
                        except Exception:
                            pass
                if untracked_diffs:
                    if diff_text:
                        diff_text += "\n" + "\n".join(untracked_diffs)
                    else:
                        diff_text = "\n".join(untracked_diffs)

        return diff_text

    def changed_files(self) -> List[str]:
        """List untracked and modified files relative to repo root."""
        entries = self._parse_status_z()
        if not entries:
            return []
        toplevel = self.toplevel().resolve()
        repo_resolved = self.repo_path.resolve()
        files = []
        for _, _, path, orig in entries:
            cand_paths = [path]
            if orig:
                cand_paths.append(orig)

            for p in cand_paths:
                if not p or self.is_protected_path(p):
                    continue

                cand = repo_resolved / p
                if not cand.exists() and not cand.is_symlink():
                    cand = toplevel / p

                if cand.is_symlink():
                    if p not in files:
                        files.append(p)
                else:
                    abs_file = cand.resolve()
                    if abs_file.is_relative_to(repo_resolved):
                        rel = str(abs_file.relative_to(repo_resolved).as_posix())
                        if rel not in files:
                            files.append(rel)
                    else:
                        if p not in files:
                            files.append(p)
        return files

    def changed_files_summary(self) -> List[str]:
        """Return concise summary lines of changed files, e.g. ['M src/forge/stage.py', 'A tests/test_reviewer.py']."""
        entries = self._parse_status_z()
        if not entries:
            return []
        toplevel = self.toplevel().resolve()
        repo_resolved = self.repo_path.resolve()
        summary = []
        for staged, unstaged, path, orig in entries:
            if self.is_protected_path(path) or (orig and self.is_protected_path(orig)):
                continue

            cand = repo_resolved / path
            if not cand.exists() and not cand.is_symlink():
                cand = toplevel / path

            if cand.is_symlink():
                rel = path
            else:
                abs_file = cand.resolve()
                if abs_file.is_relative_to(repo_resolved):
                    rel = str(abs_file.relative_to(repo_resolved).as_posix())
                else:
                    rel = path

            status_code = staged + unstaged
            if status_code.strip() == "??":
                code_letter = "A"
            elif "M" in status_code:
                code_letter = "M"
            elif "A" in status_code:
                code_letter = "A"
            elif "D" in status_code:
                code_letter = "D"
            elif "R" in status_code:
                code_letter = "R"
            else:
                code_letter = status_code.strip() or "M"

            if orig:
                summary.append(f"{code_letter} {orig} -> {rel}")
            else:
                summary.append(f"{code_letter} {rel}")
        return summary

    def current_branch(self) -> str:
        res = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        return res.stdout.strip() if res.returncode == 0 else ""

    def resolve_relative_path(self, path_str: str) -> str:
        """Resolve path relative to repo root (or repo_path)."""
        if not path_str:
            return ""
        clean = path_str.strip()
        if clean.startswith('"') and clean.endswith('"') and len(clean) >= 2:
            clean = clean[1:-1]
        toplevel = self.toplevel().resolve()
        repo_resolved = self.repo_path.resolve()
        cand = repo_resolved / clean
        if not cand.exists() and not cand.is_symlink():
            cand = toplevel / clean
        if cand.is_symlink():
            return clean
        if cand.exists():
            abs_file = cand.resolve()
            if abs_file.is_relative_to(repo_resolved):
                return str(abs_file.relative_to(repo_resolved).as_posix())
            return clean
        return clean

    def _hash_worktree_file(self, rel_path: str) -> Optional[str]:
        """Compute content hash for file in worktree, or None if deleted/missing."""
        if not rel_path:
            return None
        clean = rel_path.strip()
        if clean.startswith('"') and clean.endswith('"') and len(clean) >= 2:
            clean = clean[1:-1]
        toplevel = self.toplevel().resolve()
        repo_resolved = self.repo_path.resolve()
        cand = repo_resolved / clean
        if not cand.exists() and not cand.is_symlink():
            cand = toplevel / clean
        if cand.is_symlink():
            try:
                return "symlink:" + str(os.readlink(cand))
            except Exception:
                return "symlink:broken"
        if cand.exists() and cand.is_file():
            try:
                return hashlib.sha256(cand.read_bytes()).hexdigest()
            except Exception:
                return None
        return None

    def _get_index_shas(self) -> Dict[str, str]:
        """Return mapping of normalized relative paths to git blob SHAs in the index."""
        res = self._run(["ls-files", "-z", "-s"])
        if res.returncode != 0 or not res.stdout:
            return {}
        shas: Dict[str, str] = {}
        records = res.stdout.split("\0") if "\0" in res.stdout else res.stdout.splitlines()
        for rec in records:
            if not rec:
                continue
            if "\t" in rec:
                meta, raw_path = rec.split("\t", 1)
                meta_parts = meta.split()
                if len(meta_parts) >= 2:
                    sha = meta_parts[1]
                    rel = self.resolve_relative_path(raw_path)
                    if rel:
                        shas[rel] = sha
        return shas

    def capture_baseline(self) -> GitBaseline:
        """Capture a baseline snapshot of current repository state before a Forge run."""
        head_res = self._run(["rev-parse", "HEAD"])
        head_commit = head_res.stdout.strip() if head_res.returncode == 0 else None

        dirty_files: Dict[str, FileSnapshot] = {}
        entries = self._parse_status_z()
        if entries:
            idx_shas = self._get_index_shas()
            for staged_code, unstaged_code, raw_path, orig_path in entries:
                cand_paths = [raw_path]
                if orig_path:
                    cand_paths.append(orig_path)

                for p in cand_paths:
                    if not p or self.is_protected_path(p):
                        continue
                    rel = self.resolve_relative_path(p)
                    if not rel or self.is_protected_path(rel):
                        continue
                    dirty_files[rel] = FileSnapshot(
                        path=rel,
                        staged_code=staged_code,
                        unstaged_code=unstaged_code,
                        index_sha=idx_shas.get(rel),
                        worktree_hash=self._hash_worktree_file(rel),
                    )

        return GitBaseline(
            head_commit=head_commit,
            dirty_files=dirty_files,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    def attribute_changes(self, baseline: Optional[GitBaseline] = None) -> ChangeAttribution:
        """Classify repository changes relative to baseline into pure Forge, mixed ownership, and pre-existing unchanged."""
        if not self.is_git_repo():
            return ChangeAttribution([], [], [])

        if baseline is None:
            all_changed = self.changed_files()
            return ChangeAttribution(
                pure_forge_changes=all_changed,
                mixed_ownership_changes=[],
                preexisting_unchanged=[],
            )

        entries = self._parse_status_z()
        current_dirty_status: Dict[str, Tuple[str, str]] = {}
        if entries:
            for staged_code, unstaged_code, raw_path, orig_path in entries:
                cand_paths = [raw_path]
                if orig_path:
                    cand_paths.append(orig_path)
                for p in cand_paths:
                    if not p or self.is_protected_path(p):
                        continue
                    rel = self.resolve_relative_path(p)
                    if not rel or self.is_protected_path(rel):
                        continue
                    current_dirty_status[rel] = (staged_code, unstaged_code)

        idx_shas = self._get_index_shas()
        pure_forge: List[str] = []
        mixed_ownership: List[str] = []
        preexisting_unchanged: List[str] = []

        # 1. Process all currently dirty files
        for rel, (staged_code, unstaged_code) in current_dirty_status.items():
            if rel not in baseline.dirty_files:
                # File was clean or non-existent at baseline -> pure Forge change
                if rel not in pure_forge:
                    pure_forge.append(rel)
            else:
                # File was already dirty at baseline -> check if state changed during Forge run
                base = baseline.dirty_files[rel]
                cur_idx = idx_shas.get(rel)
                cur_worktree = self._hash_worktree_file(rel)
                state_changed = (
                    staged_code != base.staged_code
                    or unstaged_code != base.unstaged_code
                    or cur_idx != base.index_sha
                    or cur_worktree != base.worktree_hash
                )
                if state_changed:
                    if rel not in mixed_ownership:
                        mixed_ownership.append(rel)
                else:
                    if rel not in preexisting_unchanged:
                        preexisting_unchanged.append(rel)

        # 2. Check for files that were dirty at baseline, but are no longer in current status
        for base_rel, base_snap in baseline.dirty_files.items():
            if base_rel not in current_dirty_status:
                cur_idx = idx_shas.get(base_rel)
                cur_worktree = self._hash_worktree_file(base_rel)
                state_changed = (
                    cur_idx != base_snap.index_sha
                    or cur_worktree != base_snap.worktree_hash
                )
                if state_changed:
                    if base_rel not in mixed_ownership and base_rel not in pure_forge:
                        mixed_ownership.append(base_rel)
                else:
                    if base_rel not in preexisting_unchanged:
                        preexisting_unchanged.append(base_rel)

        return ChangeAttribution(
            pure_forge_changes=pure_forge,
            mixed_ownership_changes=mixed_ownership,
            preexisting_unchanged=preexisting_unchanged,
        )

    def pure_changed_files_since(self, baseline: Optional[GitBaseline] = None) -> List[str]:
        """Return list of pure Forge changed files relative to baseline."""
        return self.attribute_changes(baseline).pure_forge_changes

    def mixed_changed_files_since(self, baseline: Optional[GitBaseline] = None) -> List[str]:
        """Return list of mixed-ownership changed files relative to baseline."""
        return self.attribute_changes(baseline).mixed_ownership_changes

    def changed_files_since(
        self,
        baseline: Optional[GitBaseline] = None,
        include_mixed: bool = False,
    ) -> List[str]:
        """Return list of files changed specifically relative to baseline, excluding protected files.

        By default (include_mixed=False), returns only pure Forge changes eligible for commit.
        If include_mixed=True, returns all run-relevant changed files (pure + mixed).
        If baseline is None, falls back to self.changed_files().
        """
        if baseline is None:
            logging.warning("changed_files_since called without baseline; falling back to repository-wide changed_files()")
            return self.changed_files()
        attr = self.attribute_changes(baseline)
        if include_mixed:
            return attr.all_run_relevant_changes
        return attr.pure_forge_changes

    def status_since(
        self,
        baseline: Optional[GitBaseline] = None,
        paths: Optional[List[str]] = None,
    ) -> str:
        """Return git status lines strictly for run-relevant paths."""
        if baseline is None and paths is None:
            return self.status()

        if paths is not None:
            target_set = {self.resolve_relative_path(f) for f in paths if not self.is_protected_path(f)}
        else:
            attr = self.attribute_changes(baseline)
            target_set = {self.resolve_relative_path(f) for f in attr.all_run_relevant_changes if not self.is_protected_path(f)}

        if not target_set:
            return ""

        raw_status = self.status()
        if not raw_status:
            return ""

        lines = []
        for line in raw_status.splitlines():
            if len(line) < 4:
                continue
            raw_path = line[3:].strip()
            if raw_path.startswith('"') and raw_path.endswith('"') and len(raw_path) >= 2:
                raw_path = raw_path[1:-1]
            cand_paths = [p.strip() for p in raw_path.split(" -> ")] if " -> " in raw_path else [raw_path]
            for p in cand_paths:
                if p.startswith('"') and p.endswith('"') and len(p) >= 2:
                    p = p[1:-1]
                rel = self.resolve_relative_path(p)
                if rel in target_set and not self.is_protected_path(rel):
                    lines.append(line)
                    break
        return "\n".join(lines)

    def diff_since(
        self,
        baseline: Optional[GitBaseline] = None,
        paths: Optional[List[str]] = None,
        include_untracked: bool = True,
    ) -> str:
        """Return diff only for run-relevant paths relative to baseline."""
        if baseline is None and paths is None:
            return self.diff(include_untracked=include_untracked)

        if paths is not None:
            target_files = [f for f in paths if not self.is_protected_path(f)]
        else:
            attr = self.attribute_changes(baseline)
            target_files = [f for f in attr.all_run_relevant_changes if not self.is_protected_path(f)]

        if not target_files:
            return ""

        toplevel = self.toplevel().resolve()
        repo_resolved = self.repo_path.resolve()
        idx_shas = self._get_index_shas()

        tracked_candidates: List[str] = []
        untracked_candidates: List[str] = []

        for f in target_files:
            if f in idx_shas:
                tracked_candidates.append(f)
            else:
                cand = repo_resolved / f
                if not cand.exists() and not cand.is_symlink():
                    cand = toplevel / f
                if cand.exists() or cand.is_symlink():
                    untracked_candidates.append(f)
                else:
                    tracked_candidates.append(f)

        diff_parts: List[str] = []

        if tracked_candidates:
            pathspec = ["--"] + tracked_candidates + self.PROTECTED_PATH_PATTERNS
            res_cached = self._run(["diff", "--cached"] + pathspec)
            cached_text = res_cached.stdout.strip()
            res_unstaged = self._run(["diff"] + pathspec)
            unstaged_text = res_unstaged.stdout.strip()
            if cached_text:
                diff_parts.append(cached_text)
            if unstaged_text:
                diff_parts.append(unstaged_text)

        if include_untracked and untracked_candidates:
            MAX_UNTRACKED_FILES = 20
            MAX_FILE_SIZE_BYTES = 1_000_000  # 1MB
            count = 0
            for raw_path in untracked_candidates:
                if self.is_protected_path(raw_path):
                    continue
                cand = repo_resolved / raw_path
                if not cand.exists() and not cand.is_symlink():
                    cand = toplevel / raw_path

                is_sym = cand.is_symlink()
                if is_sym:
                    rel_posix = raw_path
                    abs_file = cand
                else:
                    abs_file = cand.resolve()
                    if not abs_file.is_relative_to(repo_resolved):
                        continue
                    rel_path = abs_file.relative_to(repo_resolved)
                    rel_posix = str(rel_path.as_posix())

                if self.is_protected_path(rel_posix):
                    continue
                if not is_sym and not abs_file.is_file():
                    continue

                if count >= MAX_UNTRACKED_FILES:
                    diff_parts.append("[... Additional untracked files truncated ...]")
                    break
                try:
                    if not is_sym and abs_file.stat().st_size > MAX_FILE_SIZE_BYTES:
                        diff_parts.append(f"[File {rel_posix} exceeds size limit (1MB), skipped diff]")
                        count += 1
                        continue
                    diff_res = self._run(["diff", "--no-index", "--", "/dev/null", rel_posix])
                    if diff_res.stdout.strip():
                        diff_parts.append(diff_res.stdout.strip())
                        count += 1
                except Exception:
                    pass

        return "\n".join(diff_parts).strip()

    def changed_files_summary_since(
        self,
        baseline: Optional[GitBaseline] = None,
        paths: Optional[List[str]] = None,
    ) -> List[str]:
        """Return concise summary lines for run-relevant changed files."""
        if baseline is None and paths is None:
            return self.changed_files_summary()

        if paths is not None:
            target_set = {self.resolve_relative_path(f) for f in paths if not self.is_protected_path(f)}
        else:
            attr = self.attribute_changes(baseline)
            target_set = {self.resolve_relative_path(f) for f in attr.all_run_relevant_changes if not self.is_protected_path(f)}

        if not target_set:
            return []

        summary = self.changed_files_summary()
        filtered = []
        for item in summary:
            parts = item.split(" ", 1)
            if len(parts) == 2:
                p = parts[1].strip()
                if p.startswith('"') and p.endswith('"') and len(p) >= 2:
                    p = p[1:-1]
                if " -> " in p:
                    p = p.split(" -> ", 1)[1].strip()
                    if p.startswith('"') and p.endswith('"') and len(p) >= 2:
                        p = p[1:-1]
                rel = self.resolve_relative_path(p)
                if rel in target_set and not self.is_protected_path(rel):
                    filtered.append(item)
        return filtered

    def commit_run(self, message: str, baseline: Optional[GitBaseline] = None) -> bool:
        """Commit changes attributable specifically to the current Forge run relative to baseline."""
        attr = self.attribute_changes(baseline)
        if attr.mixed_ownership_changes:
            logging.warning(
                "Skipping %d mixed-ownership file(s) from commit_run: %s",
                len(attr.mixed_ownership_changes),
                attr.mixed_ownership_changes,
            )
        if not attr.pure_forge_changes:
            return False
        return self.commit(message, paths=attr.pure_forge_changes)


    def commit(self, message: str, paths: Optional[List[str]] = None) -> bool:
        """Commit files to Git.

        NOTE: If `paths` is explicitly provided, only those specified paths (excluding
        protected paths) will be staged and committed. Unrelated staged or unstaged files
        are preserved untouched in the index/working tree.
        If `paths` is None, this commits all changed files across the repository (excluding
        protected paths). In Forge autonomous workflows, always supply run-scoped paths
        (e.g. from `changed_files_since`) to prevent committing unrelated user work.
        """
        target_files = paths if paths is not None else self.changed_files()
        safe_files = list(dict.fromkeys([f for f in target_files if not self.is_protected_path(f)]))
        if not safe_files:
            return False

        toplevel = self.toplevel().resolve()
        repo_resolved = self.repo_path.resolve()
        is_real_repo = (repo_resolved / ".git").exists() or (toplevel / ".git").exists()

        if is_real_repo:
            existing_files: List[str] = []
            missing_files: List[str] = []
            for f in safe_files:
                cand = repo_resolved / f
                if not cand.exists() and not cand.is_symlink():
                    cand = toplevel / f
                if cand.exists() or cand.is_symlink():
                    existing_files.append(f)
                else:
                    missing_files.append(f)

            if existing_files:
                self._run(["add", "-A", "--"] + existing_files)
            if missing_files:
                self._run(["rm", "--cached", "--ignore-unmatch", "--"] + missing_files)
        else:
            self._run(["add", "--"] + safe_files)

        res = self._run(["commit", "-m", message, "--"] + safe_files)
        if res.returncode != 0:
            logging.warning("Git commit failed: %s", res.stderr.strip() or res.stdout.strip())
            return False
        return True

    def restore(self, paths: Optional[List[str]] = None) -> bool:
        if paths:
            res = self._run(["restore"] + paths)
        else:
            res = self._run(["restore", "."])
        return res.returncode == 0
