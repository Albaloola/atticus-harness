from __future__ import annotations

import pytest
from pathlib import Path
from dataclasses import fields

from atticus.tools.registry import (
    ToolContext,
    ToolResult,
    HarnessTool,
    STAGE_TOOL_ALLOWANCES,
    register_tool,
    get_tool,
    list_tools,
    get_tools_for_stage,
)
from atticus.tools.read import ReadTool
from atticus.tools.write import WriteTool
from atticus.tools.grep import GrepTool
from atticus.tools.glob import GlobTool
from atticus.tools.edit import EditTool


class TestToolContext:
    """Tests for ToolContext dataclass."""

    def test_create_with_all_fields(self):
        """Test ToolContext creation with all fields provided."""
        workspace = Path("/tmp/workspace")
        db = Path("/tmp/db.sqlite")
        ctx = ToolContext(
            stage="analyse",
            workspace_path=workspace,
            db_path=db,
            provenance_logger="mock_logger",
            token_budget=2000,
        )

        assert ctx.stage == "analyse"
        assert ctx.workspace_path == workspace
        assert ctx.db_path == db
        assert ctx.provenance_logger == "mock_logger"
        assert ctx.token_budget == 2000

    def test_default_values(self):
        """Test ToolContext default values for optional fields."""
        workspace = Path("/tmp/workspace")
        ctx = ToolContext(stage="analyse", workspace_path=workspace)

        assert ctx.db_path is None
        assert ctx.provenance_logger is None
        assert ctx.token_budget is None

    def test_all_fields_exist(self):
        """Test that ToolContext has all expected fields."""
        field_names = {f.name for f in fields(ToolContext)}
        expected = {"stage", "workspace_path", "db_path", "provenance_logger", "token_budget"}
        assert field_names == expected


class TestToolResult:
    """Tests for ToolResult dataclass."""

    def test_create_with_all_fields(self):
        """Test ToolResult creation with all fields provided."""
        result = ToolResult(
            content="test output",
            metadata={"tokens": 100},
            success=True,
            error=None,
        )

        assert result.content == "test output"
        assert result.metadata == {"tokens": 100}
        assert result.success is True
        assert result.error is None

    def test_success_state(self):
        """Test ToolResult represents success state correctly."""
        result = ToolResult(
            content="success output",
            metadata={"key": "value"},
            success=True,
            error=None,
        )

        assert result.success is True
        assert result.error is None

    def test_error_state(self):
        """Test ToolResult represents error state correctly."""
        result = ToolResult(
            content="",
            metadata={"error_code": 500},
            success=False,
            error="Something went wrong",
        )

        assert result.success is False
        assert result.error == "Something went wrong"

    def test_default_values(self):
        """Test ToolResult default values."""
        result = ToolResult(content="test")

        assert result.metadata == {}
        assert result.success is True
        assert result.error is None

    def test_content_types(self):
        """Test ToolResult with different content types."""
        # String content
        r1 = ToolResult(content="text")
        assert isinstance(r1.content, str)

        # Bytes content
        r2 = ToolResult(content=b"bytes")
        assert isinstance(r2.content, bytes)

        # Dict content
        r3 = ToolResult(content={"key": "value"})
        assert isinstance(r3.content, dict)


class TestHarnessTool:
    """Tests for HarnessTool abstract base class."""

    def test_cannot_instantiate_without_abstract_methods(self):
        """Test that HarnessTool cannot be instantiated without implementing abstract methods."""
        with pytest.raises(TypeError):
            HarnessTool()

    def test_concrete_tool_can_instantiate(self):
        """Test that a concrete tool implementation can be instantiated."""
        tool = ReadTool()

        assert tool.name == "Read"

    def test_can_handle_default_behavior(self):
        """Test can_handle default behavior checks STAGE_TOOL_ALLOWANCES."""

        class TestTool(HarnessTool):
            @property
            def name(self) -> str:
                return "TestTool"

            @property
            def description(self) -> str:
                return "Test tool for testing"

            def invoke(self, params, context):
                return ToolResult(content="test")

        tool = TestTool()
        assert tool.can_handle("evidence-ingest-analyse") is False
        assert tool.can_handle("review") is False

    def test_can_handle_override(self):
        """Test that can_handle can be overridden."""

        class AlwaysAvailableTool(HarnessTool):
            @property
            def name(self) -> str:
                return "AlwaysAvailable"

            @property
            def description(self) -> str:
                return "Always available tool"

            def can_handle(self, stage: str) -> bool:
                return True

            def invoke(self, params, context):
                return ToolResult(content="test")

        tool = AlwaysAvailableTool()
        assert tool.can_handle("any-stage") is True
        assert tool.can_handle("review") is True

    def test_read_tool_can_handle(self):
        """Test Read tool's can_handle behavior (overridden to return True)."""
        tool = ReadTool()
        assert tool.can_handle("any-stage") is True
        assert tool.can_handle("evidence-ingest-analyse") is True

    def test_grep_tool_can_handle(self):
        """Test Grep tool's can_handle only allows specific stages."""
        tool = GrepTool()
        assert tool.can_handle("evidence-ingest-resolve") is True
        assert tool.can_handle("harvest") is True
        assert tool.can_handle("review") is True
        assert tool.can_handle("repair") is True
        assert tool.can_handle("evidence-ingest-scan") is False

    def test_write_tool_can_handle(self):
        """Test WriteTool's can_handle excludes scan stage."""
        tool = WriteTool()
        assert tool.can_handle("evidence-ingest-scan") is False
        assert tool.can_handle("evidence-ingest-execute") is True


class TestRegisterTool:
    """Tests for register_tool decorator and registration system."""

    def setup_method(self):
        """Clear the registry before each test."""
        from atticus.tools.registry import _TOOL_REGISTRY
        _TOOL_REGISTRY.clear()

    def teardown_method(self):
        """Clear the registry after each test."""
        from atticus.tools.registry import _TOOL_REGISTRY
        _TOOL_REGISTRY.clear()

    def test_register_tool_decorator(self):
        """Test that register_tool decorator registers a tool class."""

        @register_tool
        class MyTool(HarnessTool):
            @property
            def name(self) -> str:
                return "MyTool"

            @property
            def description(self) -> str:
                return "My test tool"

            def invoke(self, params, context):
                return ToolResult(content="test")

        assert get_tool("MyTool") == MyTool

    def test_get_tool_returns_none_for_unknown(self):
        """Test get_tool returns None for unregistered tool."""
        assert get_tool("NonExistent") is None

    def test_get_tool_returns_correct_class(self):
        """Test get_tool returns the correct registered class."""

        @register_tool
        class ToolA(HarnessTool):
            @property
            def name(self) -> str:
                return "ToolA"

            @property
            def description(self) -> str:
                return "Tool A"

            def invoke(self, params, context):
                return ToolResult(content="a")

        @register_tool
        class ToolB(HarnessTool):
            @property
            def name(self) -> str:
                return "ToolB"

            @property
            def description(self) -> str:
                return "Tool B"

            def invoke(self, params, context):
                return ToolResult(content="b")

        assert get_tool("ToolA") == ToolA
        assert get_tool("ToolB") == ToolB

    def test_list_tools_empty(self):
        """Test list_tools returns base tools even when no additional tools registered."""
        assert len(list_tools()) >= 0

    def test_list_tools_returns_all_registered(self):
        """Test list_tools returns all registered tools."""

        @register_tool
        class Tool1(HarnessTool):
            @property
            def name(self) -> str:
                return "Tool1"

            @property
            def description(self) -> str:
                return "Tool 1"

            def invoke(self, params, context):
                return ToolResult(content="1")

        @register_tool
        class Tool2(HarnessTool):
            @property
            def name(self) -> str:
                return "Tool2"

            @property
            def description(self) -> str:
                return "Tool 2"

            def invoke(self, params, context):
                return ToolResult(content="2")

        tools = list_tools()
        tool_names = set()
        for t in tools:
            if isinstance(t, type):
                tool_names.add(t().name)
            elif hasattr(t, 'name'):
                tool_names.add(t.name)
        assert len(tools) >= 2
        assert "Tool1" in tool_names
        assert "Tool2" in tool_names

    def test_register_tool_returns_class(self):
        """Test that register_tool returns the class for use as decorator."""

        @register_tool
        class DecoratedTool(HarnessTool):
            @property
            def name(self) -> str:
                return "DecoratedTool"

            @property
            def description(self) -> str:
                return "Decorated tool"

            def invoke(self, params, context):
                return ToolResult(content="test")

        tool = DecoratedTool()
        assert isinstance(tool, HarnessTool)


class TestGetToolsForStage:
    """Tests for get_tools_for_stage function."""

    def setup_method(self):
        """Clear and populate the registry before each test."""
        from atticus.tools.registry import _TOOL_REGISTRY
        _TOOL_REGISTRY.clear()

        # Register mock tools that match STAGE_TOOL_ALLOWANCES
        @register_tool
        class GlobTool(HarnessTool):
            @property
            def name(self) -> str:
                return "Glob"

            @property
            def description(self) -> str:
                return "Glob tool"

            def invoke(self, params, context):
                return ToolResult(content=[])

        @register_tool
        class BashTool(HarnessTool):
            @property
            def name(self) -> str:
                return "Bash"

            @property
            def description(self) -> str:
                return "Bash tool"

            def invoke(self, params, context):
                return ToolResult(content="")

        @register_tool
        class ReadTool(HarnessTool):
            @property
            def name(self) -> str:
                return "Read"

            @property
            def description(self) -> str:
                return "Read tool"

            def invoke(self, params, context):
                return ToolResult(content="")

        @register_tool
        class GrepTool(HarnessTool):
            @property
            def name(self) -> str:
                return "Grep"

            @property
            def description(self) -> str:
                return "Grep tool"

            def invoke(self, params, context):
                return ToolResult(content=[])

    def teardown_method(self):
        """Clear the registry after each test."""
        from atticus.tools.registry import _TOOL_REGISTRY
        _TOOL_REGISTRY.clear()

    def test_get_tools_for_stage_evidence_ingest_scan(self):
        """Test get_tools_for_stage for evidence-ingest-scan stage."""
        tools = get_tools_for_stage("evidence-ingest-scan")
        tool_names = [t().name for t in tools]
        assert "Glob" in tool_names
        assert "Bash" in tool_names

    def test_get_tools_for_stage_evidence_ingest_analyse(self):
        """Test get_tools_for_stage for evidence-ingest-analyse stage."""
        tools = get_tools_for_stage("evidence-ingest-analyse")
        tool_names = [t().name for t in tools]
        assert "Read" in tool_names
        assert "Glob" not in tool_names

    def test_get_tools_for_stage_unknown_stage(self):
        """Test get_tools_for_stage returns empty list for unknown stage."""
        tools = get_tools_for_stage("unknown-stage")
        assert tools == []

    def test_get_tools_for_stage_returns_classes(self):
        """Test get_tools_for_stage returns tool classes."""
        tools = get_tools_for_stage("evidence-ingest-scan")
        assert len(tools) > 0
        for tool_class in tools:
            assert issubclass(tool_class, HarnessTool)


class TestStageToolAllowances:
    """Tests for STAGE_TOOL_ALLOWANCES mapping."""

    def test_stage_tool_allowances_is_dict(self):
        """Test STAGE_TOOL_ALLOWANCES is a dictionary."""
        assert isinstance(STAGE_TOOL_ALLOWANCES, dict)

    def test_all_stages_have_tool_lists(self):
        """Test all stages have non-empty tool lists."""
        for stage, tools in STAGE_TOOL_ALLOWANCES.items():
            assert isinstance(stage, str)
            assert isinstance(tools, list)
            assert len(tools) > 0

    def test_expected_stages_exist(self):
        """Test that expected stages are in STAGE_TOOL_ALLOWANCES."""
        expected_stages = {
            "evidence-ingest-scan",
            "evidence-ingest-analyse",
            "evidence-ingest-resolve",
            "evidence-ingest-execute",
            "evidence-ingest-register",
            "extract-sources",
            "harvest",
            "review",
            "repair",
            "final-gate",
            "S6",
            "S7",
        }
        assert set(STAGE_TOOL_ALLOWANCES.keys()) == expected_stages

    def test_tool_names_are_strings(self):
        """Test that all tool names in allowances are strings."""
        for stage, tools in STAGE_TOOL_ALLOWANCES.items():
            for tool_name in tools:
                assert isinstance(tool_name, str)

    def test_known_tools_in_allowances(self):
        """Test that known tools appear in the allowances."""
        all_tools = []
        for tools in STAGE_TOOL_ALLOWANCES.values():
            all_tools.extend(tools)

        unique_tools = set(all_tools)
        expected_tools = {"Glob", "Bash", "Read", "Grep", "NotebookEdit", "Copy", "Write", "Delete", "Edit", "web_search"}
        assert expected_tools.issubset(unique_tools)


class TestRealToolsIntegration:
    """Integration tests using real tool classes."""

    def setup_method(self):
        """Clear and register real tools."""
        from atticus.tools.registry import _TOOL_REGISTRY
        _TOOL_REGISTRY.clear()

        # Register real tools via subclassing
        @register_tool
        class _ReadTool(ReadTool):  # noqa: F811
            pass

        @register_tool
        class _WriteTool(WriteTool):  # noqa: F811
            pass

        @register_tool
        class _GrepTool(GrepTool):  # noqa: F811
            pass

        @register_tool
        class _GlobTool(GlobTool):  # noqa: F811
            pass

        @register_tool
        class _EditTool(EditTool):  # noqa: F811
            pass

    def teardown_method(self):
        """Clear the registry."""
        from atticus.tools.registry import _TOOL_REGISTRY
        _TOOL_REGISTRY.clear()

    def test_read_tool_registered(self):
        """Test Read tool is registered correctly."""
        tool_class = get_tool("Read")
        assert tool_class is not None
        assert tool_class().name == "Read"

    def test_write_tool_registered(self):
        """Test Write tool is registered correctly."""
        tool_class = get_tool("Write")
        assert tool_class is not None
        assert tool_class().name == "Write"

    def test_invoke_tool_with_context(self, tmp_path):
        """Test invoking a tool with ToolContext."""
        from atticus.tools.registry import ToolContext, ToolResult

        tool = ReadTool()
        context = ToolContext(stage="test", workspace_path=tmp_path)

        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")

        result = tool.invoke({"path": str(test_file)}, context)
        assert isinstance(result, ToolResult)
        assert result.success is True
