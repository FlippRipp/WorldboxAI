"""A deliberately small, local-only SillyTavern macro compatibility layer.

The parser never executes scripts. A context belongs to one prompt compilation;
stored values are data and are never parsed again when read. Worldbox additionally
accepts ``getvar::name::fallback`` for presets that use an unset-value fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
import random
import re
from typing import Any


SUPPORTED_ST_MACROS = frozenset({
    "setvar", "getvar", "addvar", "incvar", "decvar", "roll", "user", "trim", "noop",
})
MAX_MACRO_DEPTH = 128
MAX_DICE_PER_ROLL = 1000
MAX_DIE_SIDES = 2_147_483_647
_DICE = re.compile(r"(?:(\d+)\s*)?[dD]\s*(\d+)(?:\s*([+-])\s*(\d+))?\Z")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")


@dataclass
class _Macro:
    parts: list[Any] = field(default_factory=list)
    closed: bool = True


def _escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return bool(backslashes % 2)


def _literal_end(text: str, start: int) -> int:
    """Keep an escaped outer macro and all of its contents literal."""
    depth = 1
    index = start + 2
    while index < len(text) - 1:
        token = text[index:index + 2]
        if token == "{{":
            depth += 1
            index += 2
        elif token == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return len(text)


def _parse(text: str) -> list[Any]:
    """Build balanced nodes iteratively, including malformed trailing nodes."""
    root: list[Any] = []
    stack: list[tuple[list[Any], _Macro]] = []
    current = root
    start = index = 0
    while index < len(text) - 1:
        token = text[index:index + 2]
        if token == "{{" and _escaped(text, index):
            index = _literal_end(text, index)
            continue
        if token == "{{":
            current.append(text[start:index])
            macro = _Macro()
            current.append(macro)
            stack.append((current, macro))
            current = macro.parts
            index += 2
            start = index
        elif token == "}}" and stack and not _escaped(text, index):
            current.append(text[start:index])
            current, _ = stack.pop()
            index += 2
            start = index
        else:
            index += 1
    current.append(text[start:])
    for _, macro in stack:
        macro.closed = False
    return root


def _signature(macro: _Macro) -> tuple[str, list[list[Any]]]:
    """Split arguments at top-level separators, never inside nested macros."""
    first = macro.parts[0] if macro.parts and isinstance(macro.parts[0], str) else ""
    leading = first.lstrip()
    if leading.startswith("///"):
        return "///", []
    if leading.startswith("//"):
        return "//", [[leading[2:], *macro.parts[1:]]]
    # ST permits spaces after a scoped tag's closing/preserve-space flag.
    leading = re.sub(r"^([/#])\s+", r"\1", leading)
    match = re.match(r"([^\s:]+)(.*)\Z", leading, re.DOTALL)
    if not match:
        return "<empty>", []
    name = match.group(1).lower()
    remainder = match.group(2).lstrip()
    if not remainder and len(macro.parts) == 1:
        return name, []
    if remainder.startswith("::"):
        remainder = remainder[2:]
    elif remainder.startswith(":"):
        remainder = remainder[1:]
    pieces: list[list[Any]] = [[]]
    for part in [remainder, *macro.parts[1:]]:
        if isinstance(part, _Macro):
            pieces[-1].append(part)
            continue
        split = part.split("::")
        pieces[-1].append(split[0])
        for value in split[1:]:
            pieces.append([value])
    return name, pieces


def _scope_pairs(parts: list[Any]) -> dict[int, int]:
    """Pair scoped tags so unsupported bodies cannot become active prose."""
    openings: dict[str, list[int]] = {}
    pairs: dict[int, int] = {}
    for index, part in enumerate(parts):
        if not isinstance(part, _Macro):
            continue
        name, args = _signature(part)
        if name == "///":
            closing = "//"
        elif name.startswith("/") and name != "//":
            closing = name[1:]
        else:
            if name == "//" and _plain(args[0]).strip():
                continue
            openings.setdefault(name.lstrip("#"), []).append(index)
            continue
        if openings.get(closing):
            pairs[openings[closing].pop()] = index
    return pairs


def _plain(parts: list[Any]) -> str:
    # Used only to distinguish the empty scoped-comment opener; nested macros
    # make it nonempty, without having to recurse through their contents.
    return "".join(part if isinstance(part, str) else "{{macro}}" for part in parts)


class _Processor:
    def __init__(self, mode: str, context: STMacroContext | None = None):
        self.mode = mode
        self.context = context
        self.diagnostics: set[str] = set()

    def warning(self, name: str, message: str) -> None:
        self.diagnostics.add(name)
        if self.context is not None:
            self.context._warn(message)

    def process(self, parts: list[Any], depth: int = 0) -> str:
        pairs = _scope_pairs(parts)
        output: list[str] = []
        index = 0
        while index < len(parts):
            part = parts[index]
            if isinstance(part, str):
                output.append(part)
                index += 1
                continue
            name, args = _signature(part)
            end = pairs.get(index)
            if name == "//":
                if not part.closed:
                    self.warning("<unclosed comment>", "An unclosed SillyTavern comment was removed.")
                elif not _plain(args[0]).strip() and end is None:
                    self.warning("<unclosed comment>", "An unclosed scoped SillyTavern comment was removed.")
                    break
                index = end + 1 if end is not None else index + 1
                continue
            if name == "///":
                index += 1
                continue
            if not part.closed:
                self.warning(name, f"Unclosed SillyTavern macro '{name}' was removed.")
                index += 1
                continue
            if depth >= MAX_MACRO_DEPTH:
                self.warning("<nesting limit>", f"SillyTavern macro nesting exceeds {MAX_MACRO_DEPTH}; the expression was removed.")
                index = end + 1 if end is not None else index + 1
                continue
            if end is not None:
                # Scoped comments are supported; other scoped commands are not
                # equivalent to the inline subset implemented here.
                self.warning(name, f"Scoped SillyTavern macro '{name}' is unsupported and was removed.")
                if self.mode == "clean":
                    output.append("{{" + self.process(part.parts, depth + 1) + "}}")
                    output.append(self.process(parts[index + 1:end], depth + 1))
                    output.append("{{" + self.process(parts[end].parts, depth + 1) + "}}")
                index = end + 1
                continue
            if name not in SUPPORTED_ST_MACROS:
                self.warning(name, f"SillyTavern macro '{name}' is unsupported and was removed.")
                if self.mode == "clean":
                    output.append("{{" + self.process(part.parts, depth + 1) + "}}")
                index += 1
                continue
            if name in {"trim", "noop"}:
                index += 1
                continue
            if name == "user":
                output.append(self.context.player_name if self.context and self.context.player_name is not None else "${player_name}")
            elif self.mode != "render":
                output.append("{{" + self.process(part.parts, depth + 1) + "}}")
            else:
                assert self.context is not None
                values = [self.process(arg, depth + 1).strip() for arg in args]
                output.append(self.context._evaluate(name, values))
            index += 1
        return "".join(output)


def clean_st_text(text: str) -> str:
    """Remove comments/no-op controls, map the user, retain other macros."""
    return _Processor("clean").process(_parse(text))


def clean_st_import_text(text: str) -> tuple[str, list[str]]:
    """Clean an import without running macros; report removed incompatibilities."""
    processor = _Processor("import")
    result = processor.process(_parse(text))
    return result, sorted(processor.diagnostics)


def inspect_st_macros(text: str) -> list[str]:
    """Return unsupported/malformed macro names without executing any macros."""
    return clean_st_import_text(text)[1]


class STMacroContext:
    """One ordered prompt build's variables, random source, and diagnostics.

    Warnings accumulate, with duplicates suppressed within one render call only,
    so callers can attribute newly appended warnings to each individual block.
    """

    def __init__(self, rng: Any = None, player_name: str | None = None):
        self.variables: dict[str, str] = {}
        self.warnings: list[str] = []
        self.rng = rng if rng is not None else random.SystemRandom()
        self.player_name = player_name
        self._seen_warnings: set[str] = set()

    def _warn(self, message: str) -> None:
        if message not in self._seen_warnings:
            self._seen_warnings.add(message)
            self.warnings.append(message)

    def render(self, text: str) -> str:
        self._seen_warnings = set()
        return _Processor("render", self).process(_parse(text))

    def _evaluate(self, name: str, args: list[str]) -> str:
        required = 2 if name in {"setvar", "addvar"} else 1
        maximum = 2 if name in {"setvar", "addvar", "getvar"} else 1
        if not required <= len(args) <= maximum or (name != "roll" and not args[0]):
            self._warn(f"SillyTavern macro '{name}' has invalid arguments and was removed.")
            return ""
        if name == "roll":
            return self._roll(args[0])
        key = args[0]
        if name == "setvar":
            self.variables[key] = args[1]
            return ""
        if name == "getvar":
            if key in self.variables:
                return self.variables[key]
            if len(args) == 2:
                return args[1]
            self._warn(f"SillyTavern variable '{key}' is unset in this prompt build; persistent chat variables are not supported.")
            return ""
        current = self.variables.get(key, "0")
        increment = args[1] if name == "addvar" else ("1" if name == "incvar" else "-1")
        if not _NUMBER.fullmatch(current) or not _NUMBER.fullmatch(increment):
            if name == "addvar" and not _NUMBER.fullmatch(current) and not _NUMBER.fullmatch(increment):
                self.variables[key] = current + increment
            else:
                self._warn(f"SillyTavern macro '{name}' needs finite numeric values for '{key}'; the variable was left unchanged.")
            return ""
        try:
            # Inputs are ordinary decimal literals, so this precision preserves
            # all their digits without scientific-notation expansion hazards.
            with localcontext() as decimal_context:
                decimal_context.prec = len(current) + len(increment) + 2
                value = Decimal(current) + Decimal(increment)
            rendered = format(value, "f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            if rendered == "-0":
                rendered = "0"
        except (InvalidOperation, ValueError):
            self._warn(f"SillyTavern macro '{name}' could not update '{key}'; the variable was left unchanged.")
            return ""
        self.variables[key] = rendered
        return rendered if name in {"incvar", "decvar"} else ""

    def _roll(self, expression: str) -> str:
        match = _DICE.fullmatch(expression.strip())
        if match is None:
            self._warn(f"Unsupported SillyTavern dice expression '{expression}'; use NdM with an optional +N or -N modifier.")
            return ""
        count_text, sides_text, sign, modifier_text = match.groups()
        try:
            count = int(count_text or "1")
            sides = int(sides_text)
            modifier = int(modifier_text or "0")
        except ValueError:
            self._warn("SillyTavern dice expression contains an invalid or oversized integer and was removed.")
            return ""
        if not 1 <= count <= MAX_DICE_PER_ROLL or not 1 <= sides <= MAX_DIE_SIDES:
            self._warn(f"SillyTavern dice require 1–{MAX_DICE_PER_ROLL} dice and 1–{MAX_DIE_SIDES} sides; the roll was rejected, not capped.")
            return ""
        total = sum(self.rng.randint(1, sides) for _ in range(count))
        return str(total + (-modifier if sign == "-" else modifier))
