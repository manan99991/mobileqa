"""The learned-defect playbook — how mobileqa compounds.

Every time a real defect is found in one tool and fixed, we add the *general pattern* here as
one entry. From then on the agent probes EVERY tool for that pattern on every run: the entries are
injected into both the action brain (what to try + what counts as a bug) and the vision brain (what
to look for in the screenshot). This is the mechanism by which the tester gets better over time
instead of re-finding the same class of bug by luck.

HOW TO ADD ONE (do this whenever a defect is fixed):
  1. Generalise the specific bug to the class it belongs to (not "Sign&fill Add is disabled" but
     "a control that looks actionable but does nothing / is disabled with no reason").
  2. Append a Pattern below with a probe (what to try on each tool), a tell (how to recognise it),
     a severity, and the tool it was learned from + date.
  3. If it can be caught deterministically, also add a check in mobileqa/checks/ and reference it.
Keep each entry short and tool-agnostic. Order: most impactful first.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pattern:
    id: str
    title: str
    defect_class: str              # crash|functional|value|ui|ux|a11y|content|perf|security
    severity: str                  # critical|high|medium|low
    probe: str                     # what to DO on each tool to surface it
    tell: str                      # how to RECOGNISE it (expected vs actual)
    visual: str = ""               # what it looks like in a screenshot (for the vision brain); "" = not visual
    learned_from: str = ""         # tool id + short note
    since: str = ""                # YYYY-MM-DD


# ── Learned patterns ───────────────────────────────────────────────────────────────────────────
# Seeded from the 2026-10-31 Tenzy review (dead Sign&fill Add, blank Long-Screenshot output,
# Format Fixer's 2-format/no-preview gaps, Fit It's missing result preview).
PATTERNS: list[Pattern] = [
    Pattern(
        id="dead-control",
        title="Control looks actionable but does nothing",
        defect_class="functional", severity="high",
        probe="On every tool, tap each visible button/chip/row. For any text field, type a value then "
              "tap its adjacent Add/Save/Apply/Submit. Try the primary action with the input the tool preloads.",
        tell="A visible, actionable-looking control that produces NO change (no navigation, no new item, "
             "no feedback, no error) is a dead control — report it. A control that is disabled while it "
             "looks tappable, with nothing on screen explaining why, is the same defect. (Exempt only the "
             "already-selected tab and genuine no-ops like Cancel/Close.)",
        learned_from="scan — Sign & fill 'Add' was disabled while drawing, so typing a name + Add did nothing",
        since="2026-10-31",
    ),
    Pattern(
        id="blank-output",
        title="Produced result or preview is blank",
        defect_class="functional", severity="critical",
        probe="Run each tool's main action to completion (convert, stitch, shrink, scan, generate) and look "
              "at the result it shows.",
        tell="The output/preview must actually contain content. A result area that is uniformly white, black, "
             "grey or transparent — an empty image, a blank PDF page, a video thumbnail that never renders — is "
             "a critical defect even when the surrounding stats say 'done'. Expected: the processed media; actual: "
             "a blank rectangle.",
        visual="a large flat white / grey / empty rectangle where a photo, video frame, document page or "
               "generated image should be — especially a tall or full-width blank area under a 'done'/'Fits'/'Saved' label.",
        learned_from="stitch — Long Screenshot exported one long white image (tall GPU render came back blank)",
        since="2026-10-31",
    ),
    Pattern(
        id="result-no-preview",
        title="Result is described but not shown",
        defect_class="ux", severity="medium",
        probe="After a tool finishes, check whether it SHOWS the result, not only facts about it.",
        tell="A results card with stats (size, %, count) and a Save/Share button but NO visible preview of the "
             "output is a defect: the user can't verify before sharing. Expected: a thumbnail for photos/PDFs and "
             "inline playback for video; actual: text + a share button only.",
        visual="a 'done'/'Fits — N KB'/'Saved' card that has a Save or Share button but no image/video/document "
               "thumbnail anywhere in it.",
        learned_from="fitit & formatfix — showed size/percentage + Save-or-share but no preview of the output",
        since="2026-10-31",
    ),
    Pattern(
        id="promised-auto-is-manual",
        title="A behavior the tool promises as automatic is actually off / manual",
        defect_class="functional", severity="high",
        probe="Read what the tool/How-to says it does ('automatically', 'auto', 'hides X for you', 'detects and …'). "
              "Then run it on the sample WITHOUT touching any toggle and check the promised thing actually happened.",
        tell="If the copy says something happens automatically but the user must flip a switch / select items for it "
             "to happen, that's a defect — expected: it's on by default; actual: detected-but-off. (e.g. a privacy "
             "scrubber that finds plates/numbers but leaves them unchecked.)",
        learned_from="scrub — detected text (plates, numbers) was off by default despite 'auto-hide personal info'",
        since="2026-10-31",
    ),
    Pattern(
        id="thin-options",
        title="Fewer options offered than the tool can support",
        defect_class="ux", severity="medium",
        probe="On converters/exporters/pickers, open the format/quality/target choices. For a mixed input "
              "(photos AND videos), check each type can be targeted separately.",
        tell="Offering only 1–2 outputs when the platform supports more (e.g. photos JPG/PNG only, no HEIC/TIFF/"
             "GIF/PDF; video with no chooseable format at all), or forcing one choice across different input types, "
             "is an incompleteness defect. Expected: all genuinely supported options, chooseable per file type.",
        learned_from="formatfix — only JPG/PNG for photos and no per-type choice for a photos+videos batch",
        since="2026-10-31",
    ),
]


def _lines(for_vision: bool) -> list[str]:
    out: list[str] = []
    for i, p in enumerate(PATTERNS, 1):
        if for_vision:
            if not p.visual:
                continue
            out.append(f"{i}. {p.title} ({p.severity}): {p.visual}")
        else:
            out.append(f"{i}. {p.title} [{p.defect_class}/{p.severity}]\n"
                       f"   PROBE: {p.probe}\n"
                       f"   DEFECT IF: {p.tell}")
    return out


def action_block() -> str:
    """Learned patterns for the action/judgement brain (prompts.SYSTEM)."""
    if not PATTERNS:
        return ""
    return ("LEARNED DEFECT PATTERNS — actively probe for EACH of these on EVERY tool/screen, and report a "
            "finding when the 'DEFECT IF' condition holds. These are real bugs found and fixed in past runs:\n"
            + "\n".join(_lines(for_vision=False)))


def vision_block() -> str:
    """Learned patterns that are visible in a screenshot (prompts.VISION_SYSTEM)."""
    vis = [p for p in PATTERNS if p.visual]
    if not vis:
        return ""
    return ("Also specifically check for these known visual defects:\n" + "\n".join(_lines(for_vision=True)))
