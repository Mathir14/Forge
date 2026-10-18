"""Project archetype detection for Tester v2.

Classifies a repository into one of four testing archetypes:
- WEB_SPA: Browser-driven web applications (React, Next.js, Vue, Vite, etc.)
- API: HTTP/REST/GraphQL service daemons (FastAPI, Express, Flask, Django REST, etc.)
- CLI: Interactive and non-interactive command-line applications
- LIBRARY: Packaged reusable libraries / SDKs validated via consumer sandbox
"""

import json
import logging
from pathlib import Path
from typing import Optional

from forge.core.config import Config
from forge.testing.models import ProjectArchetype

logger = logging.getLogger(__name__)


class ArchetypeDetector:
    """Detects the project archetype from project configuration and filesystem markers."""

    @classmethod
    def detect(cls, project_root: Path, config: Optional[Config] = None) -> ProjectArchetype:
        # 1. Check explicit configuration override in forge.yaml
        if config:
            tester_cfg = config.get_stage_config("tester")
            if tester_cfg and hasattr(tester_cfg, "extra_flags") and tester_cfg.extra_flags:
                explicit = tester_cfg.extra_flags.get("archetype")
                if explicit:
                    val = str(explicit).upper()
                    if val in ProjectArchetype.__members__:
                        return ProjectArchetype[val]

        # 2. Check Node / Frontend markers
        pkg_json = project_root / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                
                # Web frameworks
                web_markers = [
                    "react", "next", "vue", "nuxt", "svelte", "sveltekit",
                    "vite", "@angular/core", "astro", "solid-js", "remix",
                    "gatsby", "tailwindcss",
                ]
                if any(marker in deps for marker in web_markers):
                    return ProjectArchetype.WEB_SPA

                # Node API markers
                api_markers = ["express", "koa", "fastify", "nest", "@nestjs/core", "hapi"]
                if any(marker in deps for marker in api_markers):
                    return ProjectArchetype.API

                # Node CLI / Library markers
                if "bin" in data:
                    return ProjectArchetype.CLI
                if "main" in data or "module" in data or "exports" in data:
                    return ProjectArchetype.LIBRARY
            except Exception as e:
                logger.debug("Failed reading package.json for archetype detection: %s", e)

        # 3. Check Python markers (pyproject.toml, requirements.txt)
        py_content = ""
        pyproject = project_root / "pyproject.toml"
        req_file = project_root / "requirements.txt"
        if pyproject.exists():
            try:
                py_content += " " + pyproject.read_text(encoding="utf-8").lower()
            except Exception as e:
                logger.debug("Failed reading pyproject.toml: %s", e)
        if req_file.exists():
            try:
                py_content += " " + req_file.read_text(encoding="utf-8").lower()
            except Exception as e:
                logger.debug("Failed reading requirements.txt: %s", e)

        if py_content:
            has_streamlit = "streamlit" in py_content
            has_api = any(m in py_content for m in ("fastapi", "uvicorn", "flask", "django", "starlette"))
            has_explicit_cli = (project_root / "cli.py").exists() or "project.scripts" in py_content or "console_scripts" in py_content

            # Ambiguity check: conflicting web/api backend frameworks without entrypoint disambiguation
            if has_streamlit and has_api:
                has_streamlit_file = (project_root / "streamlit_app.py").exists()
                has_api_file = (project_root / "api.py").exists()
                if has_streamlit_file and not has_api_file:
                    return ProjectArchetype.WEB_SPA
                elif has_api_file and not has_streamlit_file:
                    return ProjectArchetype.API
                else:
                    logger.warning("Ambiguous archetype: conflicting frameworks detected in %s", project_root)
                    return ProjectArchetype.UNKNOWN

            if has_streamlit:
                return ProjectArchetype.WEB_SPA
            if has_api:
                if (project_root / "templates").exists() or (project_root / "static").exists():
                    return ProjectArchetype.WEB_SPA
                return ProjectArchetype.API
            if has_explicit_cli or any(m in py_content for m in ("click", "typer")):
                return ProjectArchetype.CLI
            if any(m in py_content for m in ("build-backend", "flit", "setuptools", "poetry")):
                return ProjectArchetype.LIBRARY

        # 4. Check candidate entry files
        if (project_root / "cli.py").exists():
            return ProjectArchetype.CLI

        for candidate in ("app.py", "streamlit_app.py", "main.py"):
            fpath = project_root / candidate
            if fpath.exists():
                try:
                    head = fpath.read_text(encoding="utf-8", errors="ignore")[:2000]
                    if "import streamlit" in head or "from streamlit" in head:
                        return ProjectArchetype.WEB_SPA
                except Exception as e:
                    logger.debug("Failed checking %s for streamlit import: %s", candidate, e)

        # 5. Check HTML static files (Conventional Web) only if no backend framework was found
        if (project_root / "index.html").exists() or (project_root / "public" / "index.html").exists():
            return ProjectArchetype.WEB_SPA

        # 6. Fallback entrypoint check
        if (project_root / "src" / "forge" / "cli.py").exists() or (project_root / "main.py").exists():
            return ProjectArchetype.CLI

        return ProjectArchetype.UNKNOWN
