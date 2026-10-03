# Forge Production Staged Release Guide

This document details the production-grade, staged release process for `forge-orchestrator`.

---

## 1. Core Principles

- **Zero Local Publishing**: No developer publishes from local machines.
- **Zero API Tokens**: No API tokens or passwords are stored in GitHub Secrets.
- **Trusted Publishing (OIDC) Only**: Cryptographic, short-lived OpenID Connect token minting.
- **Single Build Guarantee**: Wheel and sdist are built once, validated once, uploaded to TestPyPI, and subsequently deployed to production PyPI. **Artifacts are never rebuilt between staging and production.**
- **Automatic Smoke Testing**: The pipeline automatically installs the freshly staged package from TestPyPI into a clean container and executes import and CLI diagnostics before requesting production approval.
- **Environment Gatekeeper**: Production PyPI publication requires explicit manual approval in GitHub by designated maintainers.
- **Automated GitHub Release**: Once PyPI publication succeeds, a GitHub Release is automatically published with release notes and distribution assets attached.

---

## 2. Release Architecture & Flow

```
Developer
  │
  ├─ 1. Bump version in pyproject.toml & src/forge/__init__.py
  ├─ 2. Commit changes
  ├─ 3. Tag (e.g., git tag -a v0.1.0b2 -m "Release v0.1.0b2")
  └─ 4. Push tag to GitHub
        │
GitHub Actions (.github/workflows/release.yml)
  │
  ├─ Job 1: test-and-build
  │    ├─ Validate version tag format (PEP 440)
  │    ├─ Run full pytest suite (540+ tests)
  │    ├─ Single Build: build wheel and sdist (python -m build)
  │    ├─ Validate with twine check --strict dist/*
  │    └─ Upload dist/ artifacts to workflow run storage
  │
  ├─ Job 2: publish-testpypi (Environment: testpypi)
  │    ├─ Download dist/ artifacts
  │    └─ Publish to TestPyPI via pypa/gh-action-pypi-publish (OIDC)
  │
  ├─ Job 3: smoke-test-testpypi
  │    ├─ Poll and install forge-orchestrator FROM TestPyPI (20 attempts x 10s)
  │    ├─ Verify import: python -c "import forge; print(forge.__version__)"
  │    ├─ Verify: forge --version
  │    ├─ Verify: forge --help
  │    └─ Verify: forge doctor
  │
  ├─ Job 4: publish-pypi (Environment: pypi)
  │    ├─ 🛑 PAUSES: Waits for maintainer review & approval
  │    ├─ Download EXACT SAME dist/ artifacts (No Rebuild)
  │    └─ Publish to Production PyPI via pypa/gh-action-pypi-publish (OIDC)
  │
  └─ Job 5: create-github-release
       ├─ Download EXACT SAME dist/ artifacts
       └─ Auto-publish GitHub Release with assets via gh release create
```

---

## 3. One-Time Setup: PyPI & TestPyPI Trusted Publishing (OIDC)

### A. Production PyPI Setup

1. Log into your account at [pypi.org](https://pypi.org).
2. If `forge-orchestrator` is not yet published:
   - Go to [PyPI Publishing (Add Pending Publisher)](https://pypi.org/manage/account/publishing/).
   - Select **GitHub**.
   - **PyPI Project Name**: `forge-orchestrator`
   - **Owner**: `Mathir14`
   - **Repository name**: `Forge`
   - **Workflow name**: `release.yml`
   - **Environment name**: `pypi`
3. If already registered:
   - Go to **Project Settings** ➔ **Publishing** ➔ **Add a new publisher** with the above details.

### B. TestPyPI Setup

1. Log into your account at [test.pypi.org](https://test.pypi.org).
2. Go to [TestPyPI Publishing (Add Pending Publisher)](https://test.pypi.org/manage/account/publishing/).
   - Select **GitHub**.
   - **PyPI Project Name**: `forge-orchestrator`
   - **Owner**: `Mathir14`
   - **Repository name**: `Forge`
   - **Workflow name**: `release.yml`
   - **Environment name**: `testpypi`

### C. GitHub Repository Environments Setup

In the GitHub repository (`https://github.com/Mathir14/Forge`):

1. Go to **Settings** ➔ **Environments**.
2. Create environment `testpypi`:
   - No approval restrictions needed (automated staging).
3. Create environment `pypi`:
   - Enable **Required reviewers**.
   - Select authorized maintainers who must sign off before any production PyPI deployment.
   - *(Optional)* Set **Deployment branches/tags** to `v*`.

---

## 4. Maintainer Release Process

To cut a new release:

### Step 1: Bump Version & Validate Locally

Update the version in:
- `pyproject.toml` (e.g. `version = "0.1.0b2"`)
- `src/forge/__init__.py` (e.g. `__version__ = "0.1.0b2"`)

Run local tests to confirm:
```bash
pytest
```

### Step 2: Commit and Tag

```bash
git commit -am "chore: release v0.1.0b2"
git push origin master

git tag -a v0.1.0b2 -m "Release v0.1.0b2"
git push origin v0.1.0b2
```

### Step 3: Monitor Staged Release in GitHub Actions

1. The `Staged Release` workflow triggers on the tag push.
2. It validates the tag format, runs the full test suite, builds distributions, uploads to TestPyPI, and runs automated smoke tests.
3. Once smoke tests succeed, the workflow pauses at `publish-pypi` with status `Waiting for review`.

### Step 4: Approve Production Release

1. In the GitHub Actions run page, review the TestPyPI smoke test logs.
2. Click **Review deployments** ➔ Select `pypi` ➔ Click **Approve and deploy**.
3. The exact same wheel and sdist files are immediately deployed to [pypi.org/p/forge-orchestrator](https://pypi.org/p/forge-orchestrator).
4. GitHub Actions then automatically creates and publishes the GitHub Release with generated release notes and attached distribution assets.

---

## 5. PEP 440 Versioning Reference

| Release Phase | PEP 440 Version Format | Git Tag | Target |
| :--- | :--- | :--- | :--- |
| **Development** | `0.2.0.dev1` | `v0.2.0-dev1` | TestPyPI |
| **Alpha** | `0.2.0a1` | `v0.2.0a1` | TestPyPI / PyPI |
| **Beta** | `0.2.0b1` | `v0.2.0b1` | TestPyPI / PyPI |
| **Release Candidate** | `0.2.0rc1` | `v0.2.0rc1` | TestPyPI / PyPI |
| **Final / Stable** | `0.2.0` | `v0.2.0` | PyPI |
