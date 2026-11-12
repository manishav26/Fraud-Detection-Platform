"""CI's own supply chain (.github/workflows; docs/supply-chain.md)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_every_action_is_pinned_to_a_full_commit():
    """A tag (@v4) can be moved to different code; a 40-character commit can't. The repository
    also refuses unpinned actions, so an unpinned one would fail every CI run."""
    uses = [m.group(1) for f in (ROOT / ".github" / "workflows").glob("*.yml")
            for m in re.finditer(r"^\s*-?\s*uses:\s*(\S+)", f.read_text(), re.MULTILINE)]
    assert uses, "no actions found"
    unpinned = [u for u in uses if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u)]
    assert unpinned == []
