"""ToolDescriptor -> MCP Tool definition: hints follow side_effect_class, schemas are closed like
the kernel's validator, and WakeCore guarantees ride along in _meta (they are hints, not grants)."""
import dataclasses
import json

import pytest

from wakecore.adapters.sources.offline_grades import OfflineGradesSource
from wakecore.adapters.tools.browser_cua import BrowserCuaTool
from wakecore.adapters.tools.fake_external import FakeEmailProvider
from wakecore.adapters.tools.inbox import InboxTool
from wakecore.kernel.domain.enums import SideEffectClass
from wakecore.kernel.ports.annotations import META, _closed, connector_view, mcp_annotations, to_mcp_tool, tool_view

EMAIL = FakeEmailProvider.descriptor
INBOX = InboxTool.descriptor
BROWSER = BrowserCuaTool.descriptor


def test_external_write_is_destructive_open_world_and_needs_approval():
    tool = to_mcp_tool(EMAIL, title="Send e-mail", description="Sends once per effect key")
    assert tool["name"] == "email.send" and tool["title"] == "Send e-mail"
    assert tool["annotations"] == {"title": "Send e-mail", "readOnlyHint": False, "destructiveHint": True,
                                   "idempotentHint": True, "openWorldHint": True}
    assert tool["_meta"][META + "approval_default"] == "required"
    assert tool["_meta"][META + "confirmation_semantics"] == "provider_message_id"


def test_local_write_is_additive_and_closed_world():
    assert mcp_annotations(INBOX) == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                                      "openWorldHint": False}
    assert to_mcp_tool(INBOX)["_meta"][META + "approval_default"] == "not_required"


def test_read_only_tool():
    d = dataclasses.replace(INBOX, side_effect_class=SideEffectClass.READ, supports_idempotency=False)
    assert mcp_annotations(d) == {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                  "openWorldHint": False}


def test_url_fields_or_egress_make_a_tool_open_world():
    d = dataclasses.replace(INBOX, required_egress=("model:x",))
    assert mcp_annotations(d)["openWorldHint"] is True
    assert to_mcp_tool(BROWSER)["_meta"][META + "url_fields"] == list(BROWSER.url_fields)
    assert mcp_annotations(BROWSER)["openWorldHint"] is True


def test_non_idempotent_writer_is_not_hinted_idempotent():
    d = dataclasses.replace(EMAIL, supports_idempotency=False, idempotency_retention_seconds=0)
    assert mcp_annotations(d)["idempotentHint"] is False


def test_input_schema_is_closed_like_the_kernel_validator():
    schema = to_mcp_tool(EMAIL)["inputSchema"]
    assert schema["additionalProperties"] is False
    inbox = to_mcp_tool(INBOX)["inputSchema"]
    assert inbox["properties"]["data"]["additionalProperties"] is True     # explicit openness is kept
    nested = _closed({"type": "object", "properties": {"a": {"type": "object", "properties": {}}},
                      "items": {"type": "object"}})
    assert nested["properties"]["a"]["additionalProperties"] is False
    assert nested["items"]["additionalProperties"] is False


def test_descriptor_is_not_mutated():
    before = json.dumps(EMAIL.input_schema, sort_keys=True)
    to_mcp_tool(EMAIL)["inputSchema"]["properties"]["x"] = {}
    assert json.dumps(EMAIL.input_schema, sort_keys=True) == before
    assert "additionalProperties" not in EMAIL.input_schema


@pytest.mark.parametrize("d", [EMAIL, INBOX, BROWSER])
def test_output_is_json_and_only_object_output_schemas_are_exported(d):
    tool = to_mcp_tool(d)
    json.dumps(tool)
    assert ("outputSchema" in tool) == (d.output_schema.get("type") == "object")
    assert all(k.startswith(META) for k in tool["_meta"])
    assert set(tool) <= {"name", "title", "description", "inputSchema", "outputSchema", "annotations", "_meta"}


def test_views_are_json():
    json.dumps(tool_view(BROWSER))
    view = connector_view(OfflineGradesSource.descriptor)
    assert view["observation_mode"] == "snapshot" and view["required_scopes"] == ["grades.read"]
    json.dumps(view)
