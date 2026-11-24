"""CHANGELOG.md, which the notes of a GitHub release are taken from (packaging/release_notes.py)."""
import re
import sys
from pathlib import Path

import pytest

import deephack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import release_notes  # noqa: E402

TEXT = (ROOT / "CHANGELOG.md").read_text(encoding="utf8")


def test_the_version_that_is_to_be_released_has_its_notes():
    """The release workflow asks for them before it builds: without a section here it stops."""
    notes = release_notes.notes(deephack.__version__)
    assert len(notes) > 40 and "\n## " not in notes


def test_every_version_has_notes_and_a_date_newest_first():
    found = release_notes.sections(TEXT)
    assert len(found) >= 14 and "0.1.0" in found
    versions = [tuple(int(n) for n in version.split(".")) for version in found]
    assert versions == sorted(versions, reverse=True) and len(set(versions)) == len(versions)
    dates = [date for date, _ in found.values()]
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) for date in dates) and dates == sorted(dates, reverse=True)
    assert all(body for _, body in found.values())
    assert len(found) == len(re.findall(r"^## ", TEXT, re.MULTILINE))     # no heading the workflow could not read


def test_a_section_is_its_text_and_nothing_of_its_neighbours():
    text = "# Changelog\n\nAbout it.\n\n## 1.2.0 (2030-01-02)\n\n- new thing\n- another\n\n## 1.1.9 (2030-01-01)\n\nOlder.\n"
    assert release_notes.notes("1.2.0", text) == "- new thing\n- another"
    wrapped = ("First words\nand more of them.\n\n- an item that goes\n  over two lines\n- the next\n\n"
               "| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nline one\nline two\n```\nafter the code\nit goes on")
    assert release_notes.unwrap(wrapped) == (                             # GitHub keeps a release's line breaks
        "First words and more of them.\n\n- an item that goes over two lines\n- the next\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n```\nline one\nline two\n```\nafter the code it goes on")
    assert all(len(line) < 400 and not line.startswith("  ") for line in release_notes.notes(deephack.__version__).split("\n"))
    assert release_notes.notes("1.1.9", text) == "Older."
    with pytest.raises(ValueError, match="no section for 1.3.0"):
        release_notes.notes("1.3.0", text)
    with pytest.raises(ValueError, match="empty"):
        release_notes.notes("2.0.0", text + "\n## 2.0.0 (2030-02-01)\n\n## 1.9.9 (2030-01-31)\n\n- x\n")


def test_the_workflow_takes_the_notes_from_the_changelog():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf8")
    assert workflow.count("packaging/release_notes.py") >= 2              # asked early, and written into the release
    assert workflow.index("packaging/release_notes.py") < workflow.index("python -m build")
