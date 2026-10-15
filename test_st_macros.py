"""SillyTavern compatibility exercises, independent of providers and saves."""

from backend.engine.st_macros import (
    MAX_DICE_PER_ROLL,
    STMacroContext,
    clean_st_import_text,
    clean_st_text,
    inspect_st_macros,
)


class SequenceRng:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def randint(self, low, high):
        self.calls.append((low, high))
        value = next(self.values)
        assert low <= value <= high
        return value


def test_nested_comments_do_not_leak_or_execute_their_contents():
    text = "Before {{// author {{user}} note {{setvar::x::bad}} tail}} after."
    assert clean_st_text(text) == "Before  after."
    context = STMacroContext()
    assert context.render(text) == "Before  after."
    assert context.variables == {}
    assert context.warnings == []


def test_scoped_comments_hide_all_contents_and_can_nest():
    text = "A{{ // }}hidden {{//}}nested{{///}} {{unknown::x}} {{setvar::a::x}}{{ /// }}B"
    assert clean_st_import_text(text) == ("AB", [])
    context = STMacroContext()
    assert context.render(text) == "AB"
    assert context.variables == {}
    assert context.warnings == []


def test_import_preserves_supported_nested_macros_without_rolling():
    text = "{{// note}}{{ SETVAR :: roll :: {{roll 1d20}} }}{{trim}}\n{{getvar::roll}} {{ USER }}"
    cleaned, issues = clean_st_import_text(text)
    assert cleaned == "{{ SETVAR :: roll :: {{roll 1d20}} }}\n{{getvar::roll}} ${player_name}"
    assert issues == []


def test_unsupported_commands_are_removed_as_units_without_nested_side_effects():
    text = "A{{getglobalvar::x}}B{{run::{{setvar::side_effect::bad}}}}C"
    assert clean_st_import_text(text) == ("ABC", ["getglobalvar", "run"])
    context = STMacroContext()
    assert context.render(text) == "ABC"
    assert context.variables == {}
    assert len(context.warnings) == 2


def test_unsupported_scoped_command_body_does_not_become_unconditional_prose():
    text = "Before {{if::condition}}Conditional {{setvar::x::bad}} text{{/if}} after."
    assert clean_st_import_text(text) == ("Before  after.", ["if"])
    context = STMacroContext()
    assert context.render(text) == "Before  after."
    assert context.variables == {}


def test_spaced_closing_flags_and_scoped_preserve_space_flag_are_recognized():
    assert clean_st_import_text("A{{ if condition }}hidden{{ / if }}B") == ("AB", ["if"])
    text = "A{{ # setvar key }}hidden {{getglobalvar::x}}{{ / setvar }}B"
    assert clean_st_import_text(text) == ("AB", ["#setvar"])


def test_data_values_are_not_reparsed_when_retrieved():
    context = STMacroContext()
    context.variables["literal"] = "{{setvar::injected::yes}} ${player_name}"
    assert context.render("{{getvar literal}}") == "{{setvar::injected::yes}} ${player_name}"
    assert "injected" not in context.variables


def test_cross_block_variables_roll_once_and_reset_between_contexts():
    rng = SequenceRng([4])
    context = STMacroContext(rng=rng)
    assert context.render("{{setvar::roll::{{roll::1d6}}}}") == ""
    assert context.render("Roll {{getvar::roll}} and again {{getvar::roll}}.") == "Roll 4 and again 4."
    assert rng.calls == [(1, 6)]
    other = STMacroContext()
    assert other.render("{{getvar::roll}}") == ""
    assert any("unset" in warning for warning in other.warnings)


def test_nested_arguments_have_source_order_and_case_insensitive_macro_names():
    context = STMacroContext(player_name="Alex")
    text = "{{ SETVAR :: key :: greeting }}{{setvar::{{GETVAR key}}::Hi {{USER}}!}}{{getvar greeting}}"
    assert context.render(text) == "Hi Alex!"
    assert context.variables == {"key": "greeting", "greeting": "Hi Alex!"}
    assert context.warnings == []


def test_numeric_updates_and_string_add_follow_their_return_contracts():
    context = STMacroContext()
    assert context.render("{{incvar score}} / {{decvar::score}}") == "1 / 0"
    assert context.render("{{setvar::score::1.25}}{{addvar::score::2.5}}{{getvar score}}") == "3.75"
    assert context.render("{{setvar::words::hello}}{{addvar::words::world}}{{getvar words}}") == "helloworld"
    assert context.warnings == []


def test_invalid_numeric_update_warns_and_preserves_variable():
    context = STMacroContext()
    context.variables["counter"] = "not a number"
    assert context.render("{{incvar counter}}") == ""
    assert context.variables["counter"] == "not a number"
    assert len(context.warnings) == 1


def test_getvar_fallback_only_applies_to_unset_variables():
    context = STMacroContext()
    assert context.render("{{getvar::unknown::None}}") == "None"
    assert context.render("{{setvar::empty::}}{{getvar::empty::None}}") == ""
    assert context.warnings == []


def test_supported_dice_formats_use_injected_rng():
    rng = SequenceRng([2, 5, 3, 6])
    context = STMacroContext(rng=rng)
    assert context.render("{{roll::2d6+3}}, {{roll: d6 - 2}}, {{roll D6}}") == "10, 1, 6"
    assert rng.calls == [(1, 6)] * 4


def test_invalid_and_pathological_rolls_warn_instead_of_fudging_or_capping():
    rng = SequenceRng([])
    context = STMacroContext(rng=rng)
    text = "{{roll::0d6}}{{roll::1d0}}{{roll::2d6*3}}{{roll::%sd6}}" % (MAX_DICE_PER_ROLL + 1)
    assert context.render(text) == ""
    assert rng.calls == []
    assert any("rejected, not capped" in warning for warning in context.warnings)
    assert any("Unsupported" in warning for warning in context.warnings)


def test_escaped_macros_html_urls_and_ordinary_prose_stay_literal():
    text = r'<details><!-- marker -->https://example.com/a // prose \{{setvar::x::{{roll::1d6}}}} \{\{user\}\}</details>'
    assert clean_st_import_text(text) == (text, [])
    context = STMacroContext()
    assert context.render(text) == text
    assert context.variables == {}
    assert context.warnings == []


def test_unclosed_macros_and_comments_do_not_leave_brace_or_command_fragments():
    for text in ("before {{setvar::key::{{roll::d6}}", "before {{// note {{user}}", "before {{//}} comment"):
        context = STMacroContext()
        assert context.render(text) == "before "
        assert context.warnings
        assert context.variables == {}


def test_warnings_are_available_for_each_affected_render_call():
    context = STMacroContext()
    context.render("{{getglobalvar::x}}{{getglobalvar::x}}")
    assert len(context.warnings) == 1
    context.render("{{getglobalvar::x}}")
    assert len(context.warnings) == 2


def test_deep_input_is_rejected_with_a_warning_not_a_recursion_error():
    context = STMacroContext()
    nested = "{{getvar::" * 2000 + "name" + "}}" * 2000
    assert context.render(nested) == ""
    assert any("nesting" in warning for warning in context.warnings)


def test_large_plain_text_and_stored_templates_are_not_truncated():
    text = "Useful prompt text.\n" * 10000
    context = STMacroContext()
    assert context.render("{{setvar::template::" + text + "}}{{getvar template}}") == text.strip()
    assert context.warnings == []


def test_inspection_does_not_report_macros_hidden_in_comments_or_execute_rolls():
    text = "{{// note {{getglobalvar::x}}}}{{setvar::x::{{roll::d6}}}}{{unknown::value}}"
    assert inspect_st_macros(text) == ["unknown"]
