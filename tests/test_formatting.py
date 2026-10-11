"""Markdown -> Google Docs batchUpdate requests. Pure functions: no API calls."""

from agent import _parse_lines, _strip_bold, build_write_requests, find_tab_in, infer_mode


def text_of(requests):
    return next(r["insertText"]["text"] for r in requests if "insertText" in r)


def ranges(requests, kind):
    """(start, end) of every updateTextStyle request that sets `kind`."""
    out = []
    for r in requests:
        u = r.get("updateTextStyle")
        if u and u["fields"] == kind:
            out.append((u["range"]["startIndex"], u["range"]["endIndex"]))
    return out


def doc_slice(requests, start, end):
    """The inserted text between two Docs indices (the doc body starts at index 1)."""
    return text_of(requests)[start - 1 : end - 1]


# _strip_bold


def test_strip_bold_returns_plain_text_and_ranges():
    assert _strip_bold("a **bold** b") == ("a bold b", [(2, 6)])


def test_strip_bold_leaves_unmatched_markers():
    assert _strip_bold("2 ** 3") == ("2 ** 3", [])


# _parse_lines


def test_parse_line_types():
    md = "# Title\n## Section\n### Sub\n- item\n1. step\n> quote\n---\nplain"
    assert [t for _, t, _ in _parse_lines(md)] == ["h1", "h2", "h3", "bullet", "numbered", "normal", "sep", "normal"]


def test_checkbox_is_a_bullet_without_the_box():
    assert _parse_lines("- [ ] Review system design")[0][:2] == ("Review system design", "bullet")


def test_code_block_lines_are_kept_verbatim():
    parsed = _parse_lines("```\n**not bold**\n```")
    assert parsed[1] == ("**not bold**", "code", [])


def test_table_row_becomes_bold_label_and_value():
    (row,) = _parse_lines("| **Salary Range** | TBD |")
    assert row == ("Salary Range: TBD", "normal", [(0, 12)])


def test_table_header_and_divider_become_separators():
    parsed = _parse_lines("| Field | Value |\n|---|---|")
    assert [t for _, t, _ in parsed] == ["sep", "sep"]


# build_write_requests


def test_existing_content_is_cleared_first():
    reqs = build_write_requests("hello", "t1", end_index=50)
    assert reqs[0]["deleteContentRange"]["range"] == {"startIndex": 1, "endIndex": 50, "tabId": "t1"}


def test_empty_tab_is_not_cleared():
    assert "deleteContentRange" not in build_write_requests("hello", "t1", end_index=1)[0]


def test_blank_lines_after_headings_are_collapsed():
    assert text_of(build_write_requests("## A\n\n\nbody\n\n\nmore", "t", 1)) == "A\nbody\n\nmore\n"


def test_bold_ranges_cover_exactly_the_bold_words():
    reqs = build_write_requests("intro\nsay **this** now", "t", 1)
    (start, end), = ranges(reqs, "bold")
    assert doc_slice(reqs, start, end) == "this"


def test_todo_and_gap_highlights_cover_the_marker():
    reqs = build_write_requests("x [TODO — add JD] y\n⚠️ GAP: Kubernetes", "t", 1)
    highlighted = [doc_slice(reqs, s, e) for s, e in ranges(reqs, "backgroundColor")]
    assert "[TODO — add JD]" in highlighted
    assert "⚠️ GAP: Kubernetes" in highlighted


# Tab helpers


def test_find_tab_in_searches_nested_tabs():
    tabs = [{"tabProperties": {"tabId": "a"}, "childTabs": [{"tabProperties": {"tabId": "b"}}]}]
    assert find_tab_in(tabs, "b")["tabProperties"]["tabId"] == "b"


def test_unknown_parent_tab_defaults_to_technical_mode():
    assert infer_mode("Current Interviews ") == "interview"
    assert infer_mode("something else") == "technical"
