"""SillyTavern imports keep activation, execution and compatibility explicit."""

from copy import deepcopy
import random

import pytest

from backend.engine.st_importer import SillyTavernImporter


def prompt(identifier, content="Narrate the scene.", **overrides):
    return {"identifier": identifier, "name": identifier, "content": content, **overrides}


def codes(result):
    return {warning["code"] for warning in result["warnings"]}


def test_uses_active_default_order_without_enabling_unordered_templates():
    raw = {
        "prompts": [prompt("first"), prompt("second"), prompt("orphan"), prompt("missing_flag")],
        "prompt_order": [
            {"character_id": 100001, "order": [
                {"identifier": "second", "enabled": True},
                {"identifier": "first", "enabled": False},
                {"identifier": "missing_flag"},
            ]},
            {"character_id": 999, "order": [
                {"identifier": "first", "enabled": True},
                {"identifier": "orphan", "enabled": True},
            ]},
        ],
    }
    original = deepcopy(raw)
    result = SillyTavernImporter().import_preset(raw)

    assert [(b["id"], b["enabled"]) for b in result["blocks"]] == [
        ("second", True), ("first", False), ("missing_flag", False), ("orphan", False),
    ]
    assert "multiple_order_configurations" in codes(result)
    assert "unordered_prompts_disabled" in codes(result)
    assert raw == original


def test_last_valid_order_is_fallback_and_an_empty_order_disables_every_prompt():
    importer = SillyTavernImporter()
    raw = {
        "prompts": [prompt("first"), prompt("second")],
        "prompt_order": [
            {"character_id": 4, "order": [{"identifier": "first", "enabled": True}]},
            {"character_id": 5, "order": [{"identifier": "second", "enabled": True}]},
            {"character_id": 6},
        ],
    }
    result = importer.import_preset(raw)
    assert [(b["id"], b["enabled"]) for b in result["blocks"]] == [("second", True), ("first", False)]

    raw["prompt_order"] = [{"order": []}]
    assert not any(b["enabled"] for b in importer.import_preset(raw)["blocks"])


def test_no_order_retains_explicit_prompt_activation_and_duplicate_order_is_first_wins():
    importer = SillyTavernImporter()
    raw = {"prompts": [prompt("first", enabled=False), prompt("second")]}
    assert [b["enabled"] for b in importer.import_preset(raw)["blocks"]] == [False, True]

    raw["prompt_order"] = [{"order": [
        {"identifier": "first", "enabled": False},
        {"identifier": "first", "enabled": True},
    ]}]
    result = importer.import_preset(raw)
    assert [b["enabled"] for b in result["blocks"]] == [False, False]
    assert "duplicate_order_entry" in codes(result)


def test_action_restrictions_are_normalized_and_unknown_only_does_not_activate():
    result = SillyTavernImporter().import_preset({"prompts": [
        prompt("player_turn", injection_trigger=[" Impersonate ", "IMPERSONATE"]),
        prompt("story", injection_trigger=["normal", "CONTINUE", "regenerate", "future_action"]),
        prompt("unsupported", injection_trigger=["future_action"]),
        prompt("all", injection_trigger=[]),
    ]})
    blocks = {b["id"]: b for b in result["blocks"]}

    assert blocks["player_turn"]["generation_types"] == ["impersonate"]
    assert blocks["story"]["generation_types"] == ["normal", "continue", "regenerate"]
    assert blocks["unsupported"]["enabled"] is False
    assert blocks["unsupported"]["config"]["st_unsupported_triggers"] == ["future_action"]
    assert "st_unsupported_triggers" not in blocks["story"]["config"]
    assert blocks["all"]["generation_types"] is None
    warnings = {w["block_id"]: w for w in result["warnings"] if "block_id" in w}
    assert warnings["player_turn"]["code"] == "generation_action_unavailable"
    assert warnings["unsupported"]["code"] == "unsupported_generation_triggers"
    assert "disabled" in warnings["unsupported"]["message"]


def test_macro_import_preserves_runtime_values_and_removes_balanced_comments_and_unsupported_commands():
    result = SillyTavernImporter().import_preset({"prompts": [prompt(
        "rules",
        "{{// Notes mention {{setvar::ignored::bad}} and must all disappear.}}"
        "Hello {{user}}. {{setvar::dice::{{roll::1d20}}}}Result: {{getvar::dice}}."
        "{{setglobalvar::secret::do not emit this}} {{trim}}",
    )]})
    block = result["blocks"][0]
    text = block["config"]["text"]

    assert block["config"]["st_macros"] is True
    assert "Hello ${player_name}." in text
    assert "{{setvar::dice::{{roll::1d20}}}}" in text
    assert "{{getvar::dice}}" in text
    assert "ignored" not in text
    assert "Notes mention" not in text
    assert "secret" not in text
    assert "{{trim}}" not in text
    assert {"unsupported_macros_removed", "variables_not_persistent"} <= codes(result)


def test_compatibility_warnings_are_scoped_to_final_unique_block_ids():
    result = SillyTavernImporter().import_preset({
        "temperature": 0.7,
        "reasoning_effort": "medium",
        "extensions": {"regex_scripts": [{"scriptName": "custom cleanup"}]},
        "prompts": [
            prompt("first-long-identifier-0000000000000000000001", "<!-- IMG_PROMPT: a forest -->", name="Same"),
            prompt("second-long-identifier-0000000000000000000002", '<div style="color:red">Notice</div>', name="Same"),
            {"identifier": "charDescription", "marker": True},
            prompt("display", '<details><summary>Notes</summary><cyan:whisper>"Hello"</cyan:whisper></details>'),
        ],
    })
    warnings = result["warnings"]
    assert {"preset_settings_ignored", "extensions_ignored", "character_assets_not_imported"} <= codes(result)
    assert next(w for w in warnings if w["code"] == "image_prompt_unsupported")["block_id"] == "st_same"
    assert next(w for w in warnings if w["code"] == "inline_styles_unsupported")["block_id"] == "st_same_2"
    assert not any(w.get("block_id") == "display" for w in warnings)


def test_skipped_statistics_count_every_entry_once_including_comment_only_blocks():
    result = SillyTavernImporter().import_preset({"prompts": [
        prompt("separator", name="===== Section ====="),
        prompt("comment", "{{// A note with {{getvar::nested}} inside.}}"),
        {"identifier": "chatHistory", "marker": True},
        prompt("content", "Keep this."),
    ]})
    assert result["stats"]["total"] == 4
    assert result["stats"]["imported"] == 1
    assert result["stats"]["skipped"] == 3
    assert len(result["stats"]["skipped_ids"]) == 3


def test_macro_validation_uses_active_order_without_rewriting_values_or_global_random_state():
    source = "{{setvar::roll::{{roll::2d20+1}}}}"
    raw = {
        "prompts": [
            prompt("read", "Result {{getvar::roll}}; optional {{getvar::optional}}."),
            prompt("setup", source),
            prompt("disabled", "{{setvar::optional::not active}}", enabled=False),
            prompt("unavailable", "{{getvar::no_impersonation_value}}", injection_trigger=["impersonate"]),
        ],
        "prompt_order": [{"order": [
            {"identifier": "setup", "enabled": True},
            {"identifier": "disabled", "enabled": False},
            {"identifier": "read", "enabled": True},
            {"identifier": "unavailable", "enabled": True},
        ]}],
    }
    original = deepcopy(raw)
    random_state = random.getstate()
    result = SillyTavernImporter().import_preset(raw)
    diagnostics = [w for w in result["warnings"] if w["code"] == "macro_validation"]

    assert len(diagnostics) == 1
    assert diagnostics[0]["block_id"] == "read"
    assert "'optional' is unset" in diagnostics[0]["message"]
    assert "normal, continue, regenerate" in diagnostics[0]["message"]
    assert "optional value" in diagnostics[0]["message"]
    assert "does not disable the block" in diagnostics[0]["message"]
    assert next(b for b in result["blocks"] if b["id"] == "setup")["config"]["text"] == source
    assert next(b for b in result["blocks"] if b["id"] == "read")["enabled"] is True
    assert raw == original
    assert random.getstate() == random_state


def test_macro_validation_checks_each_action_in_an_independent_context():
    result = SillyTavernImporter().import_preset({"prompts": [
        prompt("normal_setup", "{{setvar::normal_only::value}}", injection_trigger=["normal"]),
        prompt("read", "{{getvar::normal_only}}"),
        prompt("optional_fallback", "{{getvar::missing::fallback}}"),
    ]})
    diagnostics = [w for w in result["warnings"] if w["code"] == "macro_validation"]

    assert len(diagnostics) == 1
    assert diagnostics[0]["block_id"] == "read"
    assert "Checked for: continue, regenerate." in diagnostics[0]["message"]


def test_macro_validation_reports_invalid_dice_and_arguments_once_per_block():
    source = "{{roll::not-dice}} {{getvar}} {{roll::not-dice}}"
    result = SillyTavernImporter().import_preset({"prompts": [prompt("invalid", source)]})
    diagnostics = [w for w in result["warnings"] if w["code"] == "macro_validation"]

    assert len(diagnostics) == 2
    assert all(w["block_id"] == "invalid" for w in diagnostics)
    assert any("dice expression" in w["message"] for w in diagnostics)
    assert any("invalid arguments" in w["message"] for w in diagnostics)
    assert result["blocks"][0]["config"]["text"] == source
    assert result["blocks"][0]["enabled"] is True


@pytest.mark.parametrize("text", [
    "{{setvar amount::4}}{{getvar amount}}",
    "{{setvar:amount::4}}{{getvar:amount}}",
])
def test_variable_scope_notice_covers_supported_legacy_argument_syntax(text):
    result = SillyTavernImporter().import_preset({"prompts": [prompt("legacy", text)]})

    assert "variables_not_persistent" in codes(result)
    assert "macro_validation" not in codes(result)


@pytest.mark.parametrize("raw", [
    None, [], {}, {"prompts": {}}, {"prompts": [None]},
    {"prompts": [prompt("id", content={})]},
    {"prompts": [prompt(None)]},
    {"prompts": [prompt("id", enabled="false")]},
    {"prompts": [prompt("id", role="tool")]},
    {"prompts": [prompt("id", role=[])]},
    {"prompts": [prompt("id", injection_position=2)]},
    {"prompts": [prompt("id", injection_position=1, injection_depth=-1)]},
    {"prompts": [prompt("id", injection_trigger="normal")]},
    {"prompts": [prompt("id", injection_trigger=[None])]},
    {"prompts": [], "prompt_order": {}},
    {"prompts": [], "prompt_order": [None]},
    {"prompts": [], "prompt_order": [{"order": "invalid"}]},
    {"prompts": [], "prompt_order": [{"order": [None]}]},
    {"prompts": [], "prompt_order": [{"order": [{"identifier": "id", "enabled": "yes"}]}]},
    {"prompts": [], "extensions": []},
])
def test_malformed_imports_raise_client_errors(raw):
    with pytest.raises(ValueError):
        SillyTavernImporter().import_preset(raw)
