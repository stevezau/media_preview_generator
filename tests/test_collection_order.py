"""Test paths given on the command line are grouped by folder (tests/conftest.py ``pytest_configure``)."""

from pathlib import Path

import pytest

from tests.conftest import _grouped_by_folder


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ["tests/markers/test_a.py", "tests/test_jobs.py", "tests/markers/test_b.py"],
            ["tests/markers/test_a.py", "tests/markers/test_b.py", "tests/test_jobs.py"],
        ),
        (
            ["tests/test_jobs.py", "tests/markers/test_a.py::TestX::test_y[a/b]", "tests/test_utils.py",
             "tests/markers/test_a.py::TestX::test_z"],
            ["tests/test_jobs.py", "tests/markers/test_a.py::TestX::test_y[a/b]", "tests/markers/test_a.py::TestX::test_z",
             "tests/test_utils.py"],
        ),
        (
            ["tests/markers/credits/test_c.py", "tests/markers/test_a.py", "tests/test_jobs.py",
             "tests/markers/credits/test_d.py"],
            ["tests/markers/credits/test_c.py", "tests/markers/credits/test_d.py", "tests/markers/test_a.py",
             "tests/test_jobs.py"],
        ),
        (["tests/test_b.py", "tests/test_a.py"], ["tests/test_b.py", "tests/test_a.py"]),
        (["tests"], ["tests"]),
        ([], []),
    ],
    ids=["folder-given-twice", "node-ids", "nested-folders", "one-folder-keeps-its-order", "one-arg", "none"],
)  # fmt: skip
def test_paths_of_one_folder_run_together_in_first_seen_order(args, expected):
    # pytest 9 gives a folder named twice (with another folder's path between) a second collector, and its conftest's
    # fixtures only reach the first: tests/markers after tests/test_jobs.py lost `client` and every autouse fixture.
    assert _grouped_by_folder(args, Path("/repo")) == expected
