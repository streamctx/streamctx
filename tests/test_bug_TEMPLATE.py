"""
Template for reproduce-first bug fixing.
Copy this file to test_bug_<short_description>.py when a new bug is reported.

Workflow:
1. Write a test below that reproduces the bug (it should FAIL first).
2. Run: pytest tests/test_bug_<name>.py -v
3. Confirm it fails for the right reason.
4. Fix the actual source code.
5. Run the test again — it should PASS now.
6. Run the full suite: pytest
7. Commit both the fix and the test together.
"""

import pytest


def test_bug_reproduction():
    """
    Describe the bug here:
    - What was expected?
    - What actually happened?
    - Steps to reproduce (link to GitHub issue number)
    """
    # Arrange: set up the exact conditions that trigger the bug

    # Act: perform the action that causes the bug

    # Assert: this should currently FAIL (bug present),
    # then PASS after the fix
    assert True  # replace with real assertion


