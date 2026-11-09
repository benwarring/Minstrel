"""Tests for the IR in ``minstrel.ir``: JSON round-trips and call-site walking.

M1 acceptance requires the IR to round-trip through JSON without loss, and
``program_from_json`` to report every bad document as a ValueError.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from minstrel.ir import (
    SCHEMA_VERSION,
    Arm,
    BranchPoint,
    CallSite,
    Chunk,
    Diagnostic,
    Page,
    PageCall,
    Program,
    Story,
    Terminator,
    iter_call_sites,
    program_from_json,
    program_to_json,
    story_to_json,
)

MAIN = "app:main"

# Far past any recursion or stack limit the JSON scanner and decoders allow.
TOO_DEEP_JSON = 100_000
# Each branch level costs the decoders three Python frames, so this is well
# past the default recursion limit of 1000.
TOO_DEEP_BRANCHES = 2_000


def _sample_program() -> Program:
    """Return a Program using every node type, nested two branches deep.

    ``app:main`` walks, via ``iter_call_sites``, as call1 to call5 in order;
    call2 sits in a try body, so it appears in both try arms.
    """
    load = CallSite(f"{MAIN}#call2", 4, 8, "load", ["app:load"], "resolved")
    inner_if = BranchPoint(
        id=f"{MAIN}#branch2",
        kind="if",
        line=5,
        arms=[
            Arm(
                id=f"{MAIN}#branch2.arm1",
                label="if ready",
                start_line=5,
                end_line=6,
                body=[CallSite(f"{MAIN}#call3", 6, 12, "go", ["app:go"], "resolved")],
            ),
            Arm(
                id=f"{MAIN}#branch2.arm2",
                label="else",
                start_line=7,
                end_line=8,
                body=[Terminator("return", 8)],
                terminates=True,
            ),
        ],
        default_arm=f"{MAIN}#branch2.arm1",
    )
    try_branch = BranchPoint(
        id=f"{MAIN}#branch1",
        kind="try",
        line=3,
        arms=[
            Arm(
                id=f"{MAIN}#branch1.arm1",
                label="no exception",
                start_line=3,
                end_line=8,
                body=[load, inner_if],
            ),
            Arm(
                id=f"{MAIN}#branch1.arm2",
                label="except OSError",
                start_line=9,
                end_line=11,
                partial=[replace(load)],
                body=[
                    CallSite(f"{MAIN}#call4", 10, 8, "log.warn", [], "external", True),
                    Terminator("raise", 11),
                ],
                terminates=True,
            ),
        ],
        default_arm=f"{MAIN}#branch1.arm1",
    )
    run_targets = ["app:A.run", "app:B.run"]
    main_body = [
        CallSite(f"{MAIN}#call1", 2, 4, "self.run", run_targets, "ambiguous"),
        try_branch,
        CallSite(f"{MAIN}#call5", 12, 4, "done"),
    ]
    return Program(
        language="python",
        files=["app.py"],
        chunks=[
            Chunk(
                id="app:<module>",
                kind="module_toplevel",
                name="<module>",
                file="app.py",
                start_line=14,
                end_line=14,
                source="main()\n",
                body=[
                    CallSite("app:<module>#call1", 14, 0, "main", [MAIN], "resolved")
                ],
            ),
            Chunk(
                id=MAIN,
                kind="function",
                name="main",
                file="app.py",
                start_line=1,
                end_line=12,
                source='def main():\n    # café, "quoted"\n',
                body=main_body,
            ),
        ],
        entry_candidates=[["app:<module>"], [MAIN]],
        diagnostics=[
            Diagnostic("unresolved_call", "done is not defined", "app.py", 12),
            Diagnostic("no_location", "a problem with no place"),
        ],
    )


def _main_body(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ``app:main`` body from the sample Program's decoded JSON."""
    return doc["chunks"][1]["body"]


def _edited(edit: Callable[[dict[str, Any]], object]) -> str:
    """Return the sample Program's JSON after ``edit`` mutates its decoded dict."""
    doc = json.loads(program_to_json(_sample_program()))
    edit(doc)
    return json.dumps(doc)


def _deep_branches(depth: int) -> str:
    """Return a valid-looking IR document with ``depth`` nested branch points.

    Built as a string: ``json.dumps`` of a structure this deep overflows the
    stack on Python 3.14.
    """
    branch_open = (
        '{"node": "branch", "id": "m:f#branch1", "kind": "if", "line": 1,'
        ' "default_arm": "m:f#branch1.arm1", "arms": [{"id": "m:f#branch1.arm1",'
        ' "label": "if x", "start_line": 1, "end_line": 1, "partial": [],'
        ' "terminates": false, "body": ['
    )
    body = branch_open * depth + "]}]}" * depth
    chunk = (
        '{"id": "m:f", "kind": "function", "name": "f", "file": "m.py",'
        f' "start_line": 1, "end_line": 1, "source": "", "body": [{body}]}}'
    )
    return (
        f'{{"schema_version": {SCHEMA_VERSION}, "language": "python", "files": [],'
        f' "entry_candidates": [], "diagnostics": [], "chunks": [{chunk}]}}'
    )


@pytest.mark.parametrize(
    "program",
    [Program(language="python"), _sample_program()],
    ids=["empty", "nested"],
)
def test_program_round_trips_without_loss(program: Program) -> None:
    text = program_to_json(program)

    decoded = program_from_json(text)

    assert decoded == program
    assert program_to_json(decoded) == text


def test_program_to_json_tags_block_nodes_only() -> None:
    doc = json.loads(program_to_json(_sample_program()))
    body = _main_body(doc)
    except_arm = body[1]["arms"][1]

    assert [node["node"] for node in body] == ["call", "branch", "call"]
    assert except_arm["partial"][0]["node"] == "call"
    assert [node["node"] for node in except_arm["body"]] == ["call", "terminator"]
    assert "node" not in doc
    assert "node" not in doc["chunks"][1]
    assert "node" not in except_arm
    assert "node" not in doc["diagnostics"][0]


BAD_DOCUMENTS = {
    "not_json": "{not json",
    "empty_text": "",
    "top_level_list": "[]",
    "top_level_string": '"program"',
    "version_unknown": _edited(
        lambda doc: doc.update(schema_version=SCHEMA_VERSION + 1)
    ),
    "version_bool": _edited(lambda doc: doc.update(schema_version=True)),
    "version_float": _edited(lambda doc: doc.update(schema_version=1.0)),
    "version_string": _edited(lambda doc: doc.update(schema_version="1")),
    "version_missing": _edited(lambda doc: doc.pop("schema_version")),
    "version_huge_int": '{"schema_version": ' + "9" * 5_000 + "}",
    "unknown_node_tag": _edited(lambda doc: _main_body(doc)[0].update(node="loop")),
    "missing_node_tag": _edited(lambda doc: _main_body(doc)[0].pop("node")),
    "missing_chunks": _edited(lambda doc: doc.pop("chunks")),
    "missing_language": _edited(lambda doc: doc.pop("language")),
    "missing_call_line": _edited(lambda doc: _main_body(doc)[0].pop("line")),
    "missing_arm_partial": _edited(
        lambda doc: _main_body(doc)[1]["arms"][1].pop("partial")
    ),
    "unknown_key": _edited(lambda doc: doc.update(author="someone")),
    "chunks_not_list": _edited(lambda doc: doc.update(chunks=5)),
    "deep_json": (
        f'{{"schema_version": {SCHEMA_VERSION}, "chunks": '
        + "[" * TOO_DEEP_JSON
        + "]" * TOO_DEEP_JSON
        + "}"
    ),
    "deep_branches": _deep_branches(TOO_DEEP_BRANCHES),
}


@pytest.mark.parametrize("text", BAD_DOCUMENTS.values(), ids=BAD_DOCUMENTS.keys())
def test_program_from_json_rejects_bad_document_with_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        program_from_json(text)


def test_iter_call_sites_walks_nested_arms_once_each_in_order() -> None:
    body = _sample_program().chunks[1].body

    calls = list(iter_call_sites(body))

    assert [call.id for call in calls] == [f"{MAIN}#call{n}" for n in range(1, 6)]
    # call2 is yielded at its first occurrence (the try arm's body), not the
    # equal copy in the except arm's partial.
    assert calls[1] is body[1].arms[0].body[0]


def test_iter_call_sites_yields_partial_before_body() -> None:
    arm = Arm(
        id="m:f#branch1.arm1",
        label="except",
        start_line=1,
        end_line=3,
        partial=[CallSite("m:f#call1", 2, 4, "risky")],
        body=[CallSite("m:f#call2", 3, 4, "handle")],
    )
    branch = BranchPoint("m:f#branch1", "try", 1, [arm], arm.id)

    assert [call.id for call in iter_call_sites([branch])] == [
        "m:f#call1",
        "m:f#call2",
    ]


def test_iter_call_sites_of_block_without_calls_is_empty() -> None:
    assert list(iter_call_sites([])) == []
    assert list(iter_call_sites([Terminator("return", 1)])) == []


def test_story_to_json_emits_pages_and_calls() -> None:
    page = Page(
        index=1,
        chunk_id=MAIN,
        depth=0,
        breadcrumb=[MAIN],
        active_ranges=[(3, 8)],
        inactive_ranges=[(9, 11)],
        calls=[
            PageCall(f"{MAIN}#call1", 2, "placed"),
            PageCall(f"{MAIN}#call4", None, "external", True, True),
        ],
    )
    story = Story(
        entry=MAIN,
        mode="follow",
        pages=[page],
        not_on_path=["app:B.run"],
        never_reached=["app:unused"],
        diagnostics=[Diagnostic("unresolved_call", "done is not defined")],
    )

    data = json.loads(story_to_json(story))

    assert data["entry"] == MAIN
    assert data["mode"] == "follow"
    assert data["not_on_path"] == ["app:B.run"]
    assert data["never_reached"] == ["app:unused"]
    assert data["diagnostics"] == [
        {
            "code": "unresolved_call",
            "message": "done is not defined",
            "file": None,
            "line": None,
        }
    ]
    assert data["pages"] == [
        {
            "index": 1,
            "chunk_id": MAIN,
            "depth": 0,
            "breadcrumb": [MAIN],
            "reached_from": None,
            "active_ranges": [[3, 8]],
            "inactive_ranges": [[9, 11]],
            "via_arm": None,
            "calls": [
                {
                    "call_site_id": f"{MAIN}#call1",
                    "target_page": 2,
                    "note": "placed",
                    "conditional": False,
                    "maybe_interrupted": False,
                },
                {
                    "call_site_id": f"{MAIN}#call4",
                    "target_page": None,
                    "note": "external",
                    "conditional": True,
                    "maybe_interrupted": True,
                },
            ],
        }
    ]
