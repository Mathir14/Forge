"""Unit tests for forge knowledge CLI commands."""

from pathlib import Path
from click.testing import CliRunner
import pytest

from forge.cli import main
from forge.core.knowledge import KnowledgeFact
from forge.storage.knowledge import KnowledgeStore


def test_cli_knowledge_list_empty(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["knowledge", "list"])
    assert result.exit_code == 0
    assert "No knowledge facts found." in result.output


def test_cli_knowledge_list_and_filters(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KnowledgeStore(tmp_path)
    store.save_all([
        KnowledgeFact(id="auth-jwt", type="architecture", title="JWT Auth", status="VERIFIED"),
        KnowledgeFact(id="login-feat", type="feature", title="User Login", status="PROVISIONAL"),
        KnowledgeFact(id="adr-001", type="decision", title="ADR 001", status="HUMAN_LOCKED"),
    ])

    runner = CliRunner()
    # List all
    res_all = runner.invoke(main, ["knowledge", "list"])
    assert res_all.exit_code == 0
    assert "auth-jwt" in res_all.output
    assert "login-feat" in res_all.output
    assert "adr-001" in res_all.output

    # Filter by type
    res_feat = runner.invoke(main, ["knowledge", "list", "--type", "feature"])
    assert res_feat.exit_code == 0
    assert "login-feat" in res_feat.output
    assert "auth-jwt" not in res_feat.output

    # Filter by status
    res_locked = runner.invoke(main, ["knowledge", "list", "--status", "HUMAN_LOCKED"])
    assert res_locked.exit_code == 0
    assert "adr-001" in res_locked.output
    assert "login-feat" not in res_locked.output


def test_cli_knowledge_show(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KnowledgeStore(tmp_path)
    store.save_fact(KnowledgeFact(
        id="auth-jwt",
        type="architecture",
        title="JWT Auth",
        status="VERIFIED",
        summary="Token-based auth system.",
        evidence=["src/auth/jwt.py"],
    ))

    runner = CliRunner()
    # Show existing
    res = runner.invoke(main, ["knowledge", "show", "auth-jwt"])
    assert res.exit_code == 0
    assert "JWT Auth" in res.output
    assert "Token-based auth system." in res.output
    assert "src/auth/jwt.py" in res.output

    # Show non-existent
    res_404 = runner.invoke(main, ["knowledge", "show", "nonexistent"])
    assert res_404.exit_code != 0
    assert "not found" in res_404.output


def test_cli_knowledge_lock_and_unlock(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KnowledgeStore(tmp_path)
    store.save_fact(KnowledgeFact(
        id="auth-jwt",
        type="architecture",
        title="JWT Auth",
        status="PROVISIONAL",
    ))

    runner = CliRunner()
    # Lock fact
    res_lock = runner.invoke(main, ["knowledge", "lock", "auth-jwt"])
    assert res_lock.exit_code == 0
    assert "is now HUMAN_LOCKED" in res_lock.output

    fact_locked = store.get_fact("auth-jwt")
    assert fact_locked.is_locked
    assert fact_locked.source == "human"

    # Unlock fact
    res_unlock = runner.invoke(main, ["knowledge", "unlock", "auth-jwt"])
    assert res_unlock.exit_code == 0
    assert "is now unlocked (VERIFIED)" in res_unlock.output

    fact_unlocked = store.get_fact("auth-jwt")
    assert fact_unlocked.is_verified
