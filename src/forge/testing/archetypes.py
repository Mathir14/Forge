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

        # 3. Check HTML static files
        if (project_root / "index.html").exists() or (project_root / "public" / "index.html").exists():
            return ProjectArchetype.WEB_SPA

        # 4. Check Python markers
        pyproject = project_root / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text(encoding="utf-8").lower()
                if "fastapi" in content or "uvicorn" in content or "flask" in content or "django" in content or "starlette" in content:
                    # If html templates or static dir exist, could be WEB_SPA / SSR, otherwise API
                    if (project_root / "templates").exists() or (project_root / "static").exists():
                        return ProjectArchetype.WEB_SPA
                    return ProjectArchetype.API
                if "click" in content or "typer" in content or "project.scripts" in content or "console_scripts" in content:
                    return ProjectArchetype.CLI
                if "build-backend" in content or "flit" in content or "setuptools" in content or "poetry" in content:
                    return ProjectArchetype.LIBRARY
            except Exception as e:
                logger.debug("Failed reading pyproject.toml: %s", e)

        # 5. Check requirements.txt
        req_file = project_root / "requirements.txt"
        if req_file.exists():
            content = req_file.read_text(encoding="utf-8").lower()
            if "fastapi" in content or "uvicorn" in content or "flask" in content or "django" in content:
                return ProjectArchetype.API
            if "click" in content or "typer" in content:
                return ProjectArchetype.CLI

        # 6. Check common CLI entry points
        if (project_root / "src" / "forge" / "cli.py").exists() or (project_root / "cli.py").exists() or (project_root / "main.py").exists():
            return ProjectArchetype.CLI

        return ProjectArchetype.UNKNOWN
