"""Verify all 5 tools are registered on the FastMCP instance without importing ViMax.

This must not import langchain or any ViMax module — the runner does lazy
imports for that reason. If this test starts hanging, the eagerness leaked
back into server.py.
"""

from vimax_mcp.server import mcp


def test_five_tools_registered():
    tools = mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "submit_idea2video",
        "submit_script2video",
        "get_job_status",
        "list_artifacts",
        "cancel_job",
    }


def test_each_tool_has_a_description():
    tools = mcp._tool_manager.list_tools()
    for t in tools:
        assert t.description, f"tool {t.name} has no description"


def test_no_vimax_modules_imported_at_server_import():
    import sys

    forbidden = ("pipelines.idea2video_pipeline", "langchain", "moviepy")
    for f in forbidden:
        assert not any(m.startswith(f) for m in sys.modules), (
            f"{f} should not be eager-imported by server.py"
        )
