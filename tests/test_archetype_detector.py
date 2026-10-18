"""Regression tests for project archetype detection and runtime supervisor target resolution.

Validates:
1. Streamlit applications with requirements.txt or pyproject.toml are classified as WEB_SPA.
2. Streamlit applications with auxiliary CLI entry points (main.py, click, typer) are NOT misclassified as CLI.
3. Streamlit applications with app.py or streamlit_app.py importing streamlit are classified as WEB_SPA.
4. Pure CLI applications (main.py, click, typer) without Streamlit remain classified as CLI.
5. FastAPI, Flask, and Django projects without templates remain classified as API.
6. Python projects with templates or static directories are classified as WEB_SPA.
7. Node frontend applications (React, Vite, Next.js) remain classified as WEB_SPA.
8. Library projects (poetry, flit, build-backend) remain classified as LIBRARY.
9. Explicit configuration override in forge.yaml takes precedence over filesystem markers.
10. Runtime supervisor detect_runtime_target resolves Streamlit start commands and ports correctly.
"""

from pathlib import Path
import pytest

from forge.core.config import Config, StageConfig
from forge.testing.archetypes import ArchetypeDetector
from forge.testing.models import ProjectArchetype
from forge.testing.supervisor import detect_runtime_target


# ==============================================================================
# 1. Streamlit Archetype Detection
# ==============================================================================

def test_streamlit_in_requirements_detected_as_web_spa(tmp_path: Path):
    """Verify Streamlit in requirements.txt classifies project as WEB_SPA."""
    (tmp_path / "requirements.txt").write_text("streamlit==1.54.0\npandas==2.3.3\n", encoding="utf-8")
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.WEB_SPA


def test_streamlit_with_auxiliary_cli_entry_point_detected_as_web_spa(tmp_path: Path):
    """Verify Streamlit app with auxiliary main.py and click in requirements is NOT misclassified as CLI."""
    (tmp_path / "requirements.txt").write_text(
        "click==8.3.1\nstreamlit==1.54.0\npandas==2.3.3\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text("import click\nprint('CLI Entry')\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('App')\n", encoding="utf-8")

    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.WEB_SPA


def test_streamlit_in_pyproject_toml_detected_as_web_spa(tmp_path: Path):
    """Verify Streamlit in pyproject.toml classifies project as WEB_SPA."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-app"\ndependencies = ["streamlit>=1.50.0"]\n', encoding="utf-8"
    )
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.WEB_SPA


def test_streamlit_entry_point_import_detected_as_web_spa(tmp_path: Path):
    """Verify app.py or streamlit_app.py importing streamlit classifies as WEB_SPA without req files."""
    (tmp_path / "app.py").write_text("import streamlit as st\nst.write('Hello')\n", encoding="utf-8")
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.WEB_SPA


# ==============================================================================
# 2. Pure CLI Projects Preserved
# ==============================================================================

def test_pure_cli_with_main_py_remains_cli(tmp_path: Path):
    """Verify ordinary Python script with main.py remains CLI."""
    (tmp_path / "main.py").write_text("import sys\nprint('Running CLI')\n", encoding="utf-8")
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.CLI


def test_pure_cli_with_click_in_requirements_remains_cli(tmp_path: Path):
    """Verify Python project with click in requirements remains CLI."""
    (tmp_path / "requirements.txt").write_text("click==8.3.1\nrich==13.0.0\n", encoding="utf-8")
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.CLI


def test_pure_cli_with_typer_in_pyproject_remains_cli(tmp_path: Path):
    """Verify project with typer in pyproject.toml remains CLI."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "cli-tool"\ndependencies = ["typer"]\n', encoding="utf-8"
    )
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.CLI


# ==============================================================================
# 3. API, Web SPA, and Library Archetypes Preserved
# ==============================================================================

def test_fastapi_without_templates_detected_as_api(tmp_path: Path):
    """Verify FastAPI backend is detected as API."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.110.0\nuvicorn==0.28.0\n", encoding="utf-8")
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.API


def test_fastapi_with_templates_detected_as_web_spa(tmp_path: Path):
    """Verify FastAPI with templates directory is detected as WEB_SPA."""
    (tmp_path / "requirements.txt").write_text("fastapi==0.110.0\nuvicorn==0.28.0\n", encoding="utf-8")
    (tmp_path / "templates").mkdir()
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.WEB_SPA


def test_flask_without_templates_detected_as_api(tmp_path: Path):
    """Verify Flask backend without templates is detected as API."""
    (tmp_path / "pyproject.toml").write_text('dependencies = ["flask"]\n', encoding="utf-8")
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.API


def test_node_react_detected_as_web_spa(tmp_path: Path):
    """Verify package.json with React is detected as WEB_SPA."""
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"}}', encoding="utf-8"
    )
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.WEB_SPA


def test_node_api_detected_as_api(tmp_path: Path):
    """Verify package.json with express is detected as API."""
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"express": "^4.18.0"}}', encoding="utf-8"
    )
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.API


def test_python_library_detected_as_library(tmp_path: Path):
    """Verify poetry / build-backend without web or cli markers is detected as LIBRARY."""
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["poetry-core"]\nbuild-backend = "poetry.core.masonry.api"\n', encoding="utf-8"
    )
    assert ArchetypeDetector.detect(tmp_path) == ProjectArchetype.LIBRARY


# ==============================================================================
# 4. Explicit Configuration Overrides
# ==============================================================================

def test_explicit_configuration_override(tmp_path: Path):
    """Verify forge.yaml tester extra_flags archetype override takes absolute precedence."""
    # Filesystem indicates CLI
    (tmp_path / "main.py").write_text("print('CLI')\n", encoding="utf-8")

    # Config forces WEB_SPA
    cfg = Config.default()
    cfg.stages["tester"] = StageConfig(
        adapter="opencode",
        extra_flags={"archetype": "WEB_SPA"},
    )

    assert ArchetypeDetector.detect(tmp_path, config=cfg) == ProjectArchetype.WEB_SPA


# ==============================================================================
# 5. Runtime Supervisor Target Detection
# ==============================================================================

def test_detect_runtime_target_for_streamlit_in_requirements(tmp_path: Path):
    """Verify detect_runtime_target resolves Streamlit start command from requirements.txt."""
    (tmp_path / "requirements.txt").write_text("streamlit==1.54.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("import streamlit as st\n", encoding="utf-8")

    cmd, port, arch = detect_runtime_target(tmp_path)
    assert cmd == "streamlit run app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false"
    assert port == 8501
    assert arch == "WEB_SPA"


def test_detect_runtime_target_for_streamlit_candidate_entry_point(tmp_path: Path):
    """Verify detect_runtime_target resolves Streamlit start command from candidate file import."""
    (tmp_path / "streamlit_app.py").write_text("from streamlit import session_state\n", encoding="utf-8")

    cmd, port, arch = detect_runtime_target(tmp_path)
    assert cmd == "streamlit run streamlit_app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false"
    assert port == 8501
    assert arch == "WEB_SPA"
