"""Tests for Forge's Provider Capability System, Registry, Stage Requirements, and Extensibility."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from forge.core.capabilities import (
    Capability,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityValidationError,
)
from forge.stages.requirements import StageRequirementsRegistry
from forge.stages.stage import Stage
from forge.core.role import Role
from forge.core.context import Context
from forge.core.config import Config, StageConfig
from forge.core.git import GitService
from forge.storage.run_manager import RunManager
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.adapters.opencode import OpenCodeAdapter
from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.codex import CodexAdapter
from forge.adapters.registry import AdapterRegistry
from forge.cli import _get_adapter


# ---------------------------------------------------------------------------
# 1. Capability Enum & Registry Tests
# ---------------------------------------------------------------------------

def test_capability_enum_values():
    """Verify all standard capabilities exist and have correct string values."""
    expected = [
        "code_read",
        "code_edit",
        "shell",
        "git",
        "session_resume",
        "structured_output",
        "streaming",
        "image_input",
        "browser",
        "playwright",
        "screenshots",
        "trace_collection",
        "tool_calling",
        "long_running",
        "custom_flags",
    ]
    for cap_str in expected:
        cap = Capability(cap_str)
        assert str(cap) == cap_str
        assert cap.value == cap_str


def test_capability_registry_defaults():
    """Verify CapabilityRegistry is pre-populated with standard Forge capabilities."""
    all_caps = CapabilityRegistry.all_capabilities()
    assert len(all_caps) >= 15
    for cap in Capability:
        assert str(cap) in all_caps
        defn = CapabilityRegistry.get(cap)
        assert defn is not None
        assert defn.name == str(cap)
        assert len(defn.description) > 0


def test_capability_registry_custom_registration():
    """Verify custom capabilities can be registered dynamically at runtime."""
    custom_name = "audio_input"
    custom_desc = "Process audio signals or speech input"
    defn = CapabilityRegistry.register(custom_name, custom_desc)

    assert defn.name == custom_name
    assert defn.description == custom_desc
    assert CapabilityRegistry.is_valid(custom_name)
    assert CapabilityRegistry.get(custom_name).description == custom_desc

    # Clean up
    CapabilityRegistry._CAPABILITIES.pop(custom_name, None)


def test_capability_registry_empty_name_rejection():
    """Verify registering an empty capability name raises ValueError."""
    with pytest.raises(ValueError, match="cannot be empty"):
        CapabilityRegistry.register("   ")


# ---------------------------------------------------------------------------
# 2. Adapter Capabilities & Query Helpers Tests
# ---------------------------------------------------------------------------

def test_opencode_adapter_capabilities():
    """Verify OpenCodeAdapter declares the exact expected capabilities."""
    adapter = OpenCodeAdapter()
    caps = adapter.capabilities()
    expected = {
        "code_read",
        "code_edit",
        "shell",
        "git",
        "structured_output",
        "custom_flags",
    }
    assert caps == expected
    assert adapter.has_capability("shell")
    assert adapter.has_capability("code_read")
    assert adapter.has_capability("git")
    assert adapter.has_capability("custom_flags")
    assert adapter.has_capability(Capability.CUSTOM_FLAGS)
    assert adapter.has_capability(Capability.CODE_EDIT)
    assert adapter.has_capability(Capability.STRUCTURED_OUTPUT)
    assert not adapter.has_capability("browser")
    assert not adapter.has_capability("playwright")
    assert not adapter.has_capability("session_resume")
    assert not adapter.has_capability(Capability.SESSION_RESUME)
    assert not adapter.has_capability("streaming")
    assert not adapter.has_capability(Capability.STREAMING)

    # Feature properties
    assert adapter.supports_session_resume is False
    assert adapter.supports_streaming is False
    assert adapter.supports_structured_output is True
    assert adapter.supports_browser is False
    assert adapter.supports_playwright is False


def test_opencode_session_resume_is_truthfully_absent():
    """Verify session_resume is truthfully absent because OpenCodeAdapter has no operational resume contract."""
    adapter = OpenCodeAdapter()
    assert "session_resume" not in adapter.capabilities()
    assert adapter.supports_session_resume is False
    assert not adapter.has_capability(Capability.SESSION_RESUME)

    # Verify OpenCode CLI execution path does not capture, persist, or pass session identifiers
    with patch("shutil.which", return_value="/mock/opencode"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="done", stderr="", returncode=0)
        res = adapter.execute(prompt="test task")
        assert res.exit_code == 0
        cmd = mock_run.call_args[0][0]
        # Verify no session resumption parameter is passed to opencode run
        assert "resume" not in cmd
        assert "--session" not in cmd
        assert "-s" not in cmd


def test_opencode_streaming_is_truthfully_absent():
    """Verify streaming is truthfully absent because Forge consumes OpenCode synchronously via buffered subprocess."""
    adapter = OpenCodeAdapter()
    assert "streaming" not in adapter.capabilities()
    assert adapter.supports_streaming is False
    assert not adapter.has_capability(Capability.STREAMING)


def test_all_first_party_adapters_truthful_session_resume_contract():
    """Verify that no first-party adapter advertises session_resume without a verified Forge-level continuation mechanism."""
    for adapter_name in ("opencode", "antigravity", "codex"):
        adapter = AdapterRegistry.get(adapter_name)
        assert "session_resume" not in adapter.capabilities(), (
            f"Adapter '{adapter_name}' advertised session_resume but Forge has no adapter-level resume contract."
        )
        assert adapter.supports_session_resume is False
        assert not adapter.has_capability(Capability.SESSION_RESUME)


def test_all_first_party_adapters_satisfy_standard_lifecycle_stages():
    """Verify OpenCode, Antigravity, and Codex all satisfy the 5 active lifecycle stages (critic, architect, planner, executor, reviewer)."""
    stages = ["critic", "architect", "planner", "executor", "reviewer"]
    for adapter_name in ("opencode", "antigravity", "codex"):
        adapter = AdapterRegistry.get(adapter_name)
        for stage_name in stages:
            role = Role(name=stage_name, sequence_number=1, template_content="", protocol_content="")
            stage = Stage(role=role, adapter=adapter)
            # Must not raise CapabilityValidationError
            stage.validate_compatibility()


def test_all_first_party_adapters_rejected_for_synthetic_browser_stage():
    """Verify OpenCode, Antigravity, and Codex are all rejected for synthetic_browser_stage due to missing playwright/screenshots."""
    for adapter_name in ("opencode", "antigravity", "codex"):
        adapter = AdapterRegistry.get(adapter_name)
        role = Role(name="synthetic_browser_stage", sequence_number=99, template_content="", protocol_content="")
        stage = Stage(role=role, adapter=adapter)
        with pytest.raises(CapabilityValidationError) as exc_info:
            stage.validate_compatibility()
        err = exc_info.value
        assert "playwright" in err.missing_capabilities
        assert "screenshots" in err.missing_capabilities


def test_all_first_party_adapters_satisfy_tester_stage():
    """Verify OpenCode, Antigravity, and Codex all satisfy the real Tester stage requirements (code_read, shell)."""
    for adapter_name in ("opencode", "antigravity", "codex"):
        adapter = AdapterRegistry.get(adapter_name)
        role = Role(name="tester", sequence_number=4, template_content="", protocol_content="")
        stage = Stage(role=role, adapter=adapter)
        # Must pass capability compatibility check
        stage.validate_compatibility()




def test_antigravity_adapter_capabilities():
    """Verify AntigravityAdapter declares the exact expected capabilities."""
    adapter = AntigravityAdapter()
    caps = adapter.capabilities()
    expected = {
        "code_read",
        "code_edit",
        "shell",
        "git",
        "structured_output",
        "long_running",
        "custom_flags",
    }
    assert caps == expected
    assert adapter.has_capability("shell")
    assert adapter.has_capability("long_running")
    assert adapter.has_capability("custom_flags")
    assert adapter.has_capability(Capability.CUSTOM_FLAGS)
    assert not adapter.has_capability("browser")
    assert not adapter.has_capability("streaming")
    assert not adapter.has_capability("session_resume")

    # Feature properties
    assert adapter.supports_session_resume is False
    assert adapter.supports_streaming is False
    assert adapter.supports_structured_output is True
    assert adapter.supports_browser is False
    assert adapter.supports_playwright is False


def test_codex_adapter_capabilities():
    """Verify CodexAdapter declares the exact expected capabilities."""
    adapter = CodexAdapter()
    caps = adapter.capabilities()
    expected = {
        "code_read",
        "code_edit",
        "shell",
        "git",
        "structured_output",
        "tool_calling",
        "long_running",
        "custom_flags",
    }
    assert caps == expected
    assert adapter.has_capability("shell")
    assert adapter.has_capability("tool_calling")
    assert adapter.has_capability(Capability.CODE_EDIT)
    assert not adapter.has_capability("browser")
    assert not adapter.has_capability("playwright")
    assert not adapter.has_capability("streaming")
    assert not adapter.has_capability("session_resume")

    # Feature properties
    assert adapter.supports_session_resume is False
    assert adapter.supports_streaming is False
    assert adapter.supports_structured_output is True
    assert adapter.supports_browser is False
    assert adapter.supports_playwright is False


# ---------------------------------------------------------------------------
# 3. Extensibility & Adapter Discovery Tests
# ---------------------------------------------------------------------------

def test_adapter_registration_and_discovery():
    """Verify new adapters can register themselves and be discovered without modifying orchestration code."""
    class FutureTestAdapter(BaseAdapter):
        CAPABILITIES = {
            Capability.CODE_READ,
            Capability.CODE_EDIT,
            Capability.SHELL,
            Capability.GIT,
        }
        DEFAULT_MODEL = "test-beta"

        def is_available(self) -> bool:
            return True

        def execute(self, prompt: str, cwd=None, timeout=None) -> AdapterResponse:
            return AdapterResponse(stdout="done", stderr="", exit_code=0, duration_seconds=0.1, raw_output="done")

    # 1. Register adapter
    AdapterRegistry.register("test_future", FutureTestAdapter, aliases=["test-future-cli"])

    try:
        assert AdapterRegistry.is_registered("test_future")
        assert AdapterRegistry.is_registered("test-future-cli")
        assert AdapterRegistry.get_class("test_future") is FutureTestAdapter
        assert AdapterRegistry.get_class("test-future-cli") is FutureTestAdapter

        # Verify discovery in canonical adapters
        canonical = AdapterRegistry.list_canonical_adapters()
        assert "test_future" in canonical
        assert canonical["test_future"] is FutureTestAdapter

        # Verify capabilities query without instantiating
        caps = AdapterRegistry.get_capabilities("test_future")
        assert caps == {"code_read", "code_edit", "shell", "git"}

        # Instantiation via registry
        instance = AdapterRegistry.get("test_future")
        assert isinstance(instance, FutureTestAdapter)
        assert instance.name == "test_future"
    finally:
        AdapterRegistry.unregister("test_future")
        AdapterRegistry.unregister("test-future-cli")


def test_decorator_registration():
    """Verify @AdapterRegistry.register_adapter decorator works as expected."""
    @AdapterRegistry.register_adapter("decorator_test")
    class DecoratedAdapter(BaseAdapter):
        CAPABILITIES = {"code_read"}
        def is_available(self): return True
        def execute(self, *args, **kwargs): pass

    try:
        assert AdapterRegistry.is_registered("decorator_test")
        assert AdapterRegistry.get_class("decorator_test") is DecoratedAdapter
    finally:
        AdapterRegistry.unregister("decorator_test")


# ---------------------------------------------------------------------------
# 4. Stage Requirements & Role Integration Tests
# ---------------------------------------------------------------------------

def test_default_stage_requirements():
    """Verify standard stage requirements are correctly defined."""
    assert StageRequirementsRegistry.get("critic") == {"code_read"}
    assert StageRequirementsRegistry.get("architect") == {"code_read"}
    assert StageRequirementsRegistry.get("planner") == {"code_read"}
    assert StageRequirementsRegistry.get("executor") == {"code_edit", "shell"}
    assert StageRequirementsRegistry.get("reviewer") == {"code_read"}
    assert StageRequirementsRegistry.get("tester") == {"code_read", "shell"}
    assert StageRequirementsRegistry.get("synthetic_browser_stage") == {"shell", "playwright", "screenshots"}


def test_stage_class_delegates_to_requirements_registry():
    """Verify Stage.get_required_capabilities matches registry."""
    assert Stage.get_required_capabilities("executor") == {"code_edit", "shell"}
    assert Stage.get_required_capabilities("critic") == {"code_read"}
    assert Stage.get_required_capabilities("tester") == {"code_read", "shell"}
    assert Stage.get_required_capabilities("synthetic_browser_stage") == {"shell", "playwright", "screenshots"}


def test_role_required_capabilities_property():
    """Verify Role.required_capabilities returns the correct set."""
    role_exec = Role(name="executor", sequence_number=3, template_content="", protocol_content="")
    assert role_exec.required_capabilities == {"code_edit", "shell"}

    role_critic = Role(name="critic", sequence_number=0, template_content="", protocol_content="")
    assert role_critic.required_capabilities == {"code_read"}

    role_tester = Role(name="tester", sequence_number=4, template_content="", protocol_content="")
    assert role_tester.required_capabilities == {"code_read", "shell"}

    role_browser = Role(name="synthetic_browser_stage", sequence_number=99, template_content="", protocol_content="")
    assert role_browser.required_capabilities == {"shell", "playwright", "screenshots"}


def test_custom_stage_requirement_registration():
    """Verify custom stages can register capability requirements."""
    StageRequirementsRegistry.register("security_auditor", {"code_read", "shell"})
    assert Stage.get_required_capabilities("security_auditor") == {"code_read", "shell"}

    role_sec = Role(name="security_auditor", sequence_number=10, template_content="", protocol_content="")
    assert role_sec.required_capabilities == {"code_read", "shell"}

    # Reset
    StageRequirementsRegistry.reset()


# ---------------------------------------------------------------------------
# 5. Missing Capability Failures & Validation Error Tests
# ---------------------------------------------------------------------------

def test_stage_validate_compatibility_passes_when_satisfied():
    """Verify stage validation passes when adapter satisfies requirements."""
    role = Role(name="critic", sequence_number=0, template_content="", protocol_content="")
    adapter = OpenCodeAdapter()
    stage = Stage(role=role, adapter=adapter)
    # Should not raise
    stage.validate_compatibility()


def test_stage_validate_compatibility_raises_on_missing_capabilities():
    """Verify Stage.validate_compatibility raises CapabilityValidationError when capability is missing."""
    class ReadOnlyAdapter(BaseAdapter):
        CAPABILITIES = {"code_read"}
        def is_available(self): return True
        def execute(self, *args, **kwargs): pass

    role = Role(name="executor", sequence_number=3, template_content="", protocol_content="")
    adapter = ReadOnlyAdapter()
    stage = Stage(role=role, adapter=adapter)

    with pytest.raises(CapabilityValidationError) as exc_info:
        stage.validate_compatibility()

    err = exc_info.value
    assert err.stage_name == "executor"
    assert err.adapter_name == "readonly"
    assert err.required_capabilities == {"code_edit", "shell"}
    assert err.provided_capabilities == {"code_read"}
    assert err.missing_capabilities == {"code_edit", "shell"}
    assert "Stage 'executor' requires capabilities:" in str(err)
    assert "Configured adapter 'readonly' is missing:" in str(err)
    assert "code_edit" in str(err)
    assert "shell" in str(err)


def test_stage_run_aborts_before_execution_on_missing_capability(tmp_path):
    """Verify Stage.run aborts immediately before any execution or prompt compilation if capabilities missing."""
    executed = False

    class PartialAdapter(BaseAdapter):
        CAPABILITIES = {"code_edit"}  # Missing "shell"
        def is_available(self): return True
        def execute(self, *args, **kwargs):
            nonlocal executed
            executed = True
            return AdapterResponse(stdout="", stderr="", exit_code=0, duration_seconds=0.1, raw_output="")

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Test execution")
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))

    role = Role(name="executor", sequence_number=3, template_content="Test", protocol_content="")
    adapter = PartialAdapter()
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    with pytest.raises(CapabilityValidationError) as exc_info:
        stage.run(context)

    # Ensure adapter was never invoked
    assert not executed
    assert exc_info.value.missing_capabilities == {"shell"}


def test_get_adapter_raises_capability_validation_error_when_flagged():
    """Verify _get_adapter raises CapabilityValidationError when validate_capabilities=True and exit_on_error=False."""
    class IncompleteAdapter(BaseAdapter):
        CAPABILITIES = {"code_read"}
        def is_available(self): return True
        def execute(self, *args, **kwargs): pass

    AdapterRegistry.register("incomplete", IncompleteAdapter)
    cfg = Config.default()
    cfg.stages["executor"] = StageConfig(adapter="incomplete")

    try:
        with pytest.raises(CapabilityValidationError) as exc_info:
            _get_adapter(cfg, "executor", exit_on_error=False)
        assert "shell" in exc_info.value.missing_capabilities
        assert "code_edit" in exc_info.value.missing_capabilities
    finally:
        AdapterRegistry.unregister("incomplete")


# ---------------------------------------------------------------------------
# 6. Backwards Compatibility Tests
# ---------------------------------------------------------------------------

def test_backwards_compatibility_legacy_adapter_without_capabilities():
    """Verify legacy adapters that don't declare CAPABILITIES default to all known capabilities."""
    class LegacyThirdPartyAdapter(BaseAdapter):
        # Does NOT specify CAPABILITIES attribute
        def is_available(self): return True
        def execute(self, *args, **kwargs): pass

    adapter = LegacyThirdPartyAdapter()
    caps = adapter.capabilities()
    # For backwards compatibility, legacy adapter returns all known capabilities
    assert "code_read" in caps
    assert "code_edit" in caps
    assert "shell" in caps
    assert "playwright" in caps

    # Must pass validation on any stage
    for stage_name in ("critic", "architect", "planner", "executor", "reviewer", "tester"):
        role = Role(name=stage_name, sequence_number=0, template_content="", protocol_content="")
        stage = Stage(role=role, adapter=adapter)
        # Should not raise CapabilityValidationError
        stage.validate_compatibility()


def test_backwards_compatibility_with_existing_test_mock():
    """Verify that MockAdapter subclassing BaseAdapter works without modification."""
    class MockAdapter(BaseAdapter):
        def is_available(self): return True
        def execute(self, *args, **kwargs):
            return AdapterResponse(stdout="ok", stderr="", exit_code=0, duration_seconds=0.1, raw_output="ok")

    role = Role(name="architect", sequence_number=1, template_content="Design", protocol_content="")
    adapter = MockAdapter()
    stage = Stage(role=role, adapter=adapter)
    # Validate compatibility succeeds
    stage.validate_compatibility()
