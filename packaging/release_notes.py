"""The notes for a GitHub release: the version's section of CHANGELOG.md.

    python packaging/release_notes.py 0.12.0 [-o notes.md]

Prints the section's text, without its heading. Ends with an error when the changelog has no section for
that version, or an empty one: the release workflow runs this before it builds anything, so a version
cannot go out without its notes.

The changelog's lines are broken at a hundred characters, and GitHub shows a release's notes with every
line break kept: so the lines of a paragraph, or of one item of a list, are joined into one here.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
HEADING = re.compile(r"^## (\d+\.\d+\.\d+)(?: \((\d{4}-\d{2}-\d{2})\))?\s*$", re.MULTILINE)


def sections(text: str) -> dict[str, tuple[str, str]]:
    """Every version in the changelog, in the file's order: version -> (date, the section's text)."""
    found = list(HEADING.finditer(text))
    out = {}
    for heading, following in zip(found, found[1:] + [None]):
        body = text[heading.end():following.start() if following else len(text)].strip()
        out[heading.group(1)] = (heading.group(2) or "", body)
    return out


STARTS = re.compile(r"\s*([-*+] |\d+[.)] |#|>|\||```|~~~)")        # a line that begins something of its own


def unwrap(text: str) -> str:
    """The same text with each paragraph and each list item on one line. Tables, headings, quotes and
    what is between code fences are left as they are."""
    out, fenced = [], False
    for line in text.split("\n"):
        fence = line.lstrip().startswith(("```", "~~~"))
        joins = (out and out[-1].strip() and line.strip() and not fenced and not fence and not STARTS.match(line)
                 and not out[-1].lstrip().startswith(("#", "|", "```", "~~~")))
        if joins:
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
        fenced ^= fence
    return "\n".join(out)


def notes(version: str, text: str | None = None) -> str:
    """The text of a version's section, as a release shows it. Raises ValueError when there is none."""
    text = CHANGELOG.read_text(encoding="utf8") if text is None else text
    section = sections(text).get(version)
    if section is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}: add '## {version} (the date)' and what changed")
    if not section[1]:
        raise ValueError(f"CHANGELOG.md's section for {version} is empty")
    return unwrap(section[1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("version", help="as in deephack/__init__.py: 0.12.0")
    ap.add_argument("-o", "--output", help="write the notes to this file instead of printing them")
    a = ap.parse_args()
    try:
        text = notes(a.version.removeprefix("v"))
    except ValueError as e:
        sys.exit(str(e))
    if a.output:
        Path(a.output).write_text(text + "\n", encoding="utf8", newline="\n")
    else:
        sys.stdout.reconfigure(encoding="utf8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
