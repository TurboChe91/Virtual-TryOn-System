"""The advisory judge must be told what it is judging.

Real failures these cover, taken from qa_results on the production database:

  "Layout is a hand photo, not a matrix_cell image as required"
  "Incorrect total nail count: 7 total nails instead of the required 10 for a
   2-row 5-column matrix"

Both are the judge inventing a contract. `matrix_cell` was interpolated into the
prompt as a bare enum name, so the model assumed the grid's 2x5 layout and scored
correct hand photos as defects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lunelle.llm import auto_qa_verdict
from lunelle.prompts import MATRIX_VIEWS, matrix_visible_nails


class RecordingChat:
    """Captures the prompt so a test can assert what the judge was actually told."""

    def __init__(self, reply: str = '{"passed": true, "issues": [], "correction": ""}'):
        self.reply = reply
        self.system: str | None = None
        self.user: str | None = None
        self.images: list[Path] = []

    def __call__(self, system: str, user: str, images: list[Path]) -> str:
        self.system, self.user, self.images = system, user, list(images)
        return self.reply


class TestLayoutContract:
    def test_matrix_cell_prompt_says_hands_are_expected(self):
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "matrix_cell",
                        visible_nails=["nail-01"])
        assert chat.user is not None
        assert "REAL HANDS" in chat.user
        # The exact misjudgement seen in production must be pre-empted.
        assert "not a defect" in chat.user.lower() or "is correct" in chat.user.lower()

    def test_matrix_cell_prompt_forbids_demanding_a_grid(self):
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "matrix_cell",
                        visible_nails=["nail-01"])
        assert "NOT a grid" in chat.user
        assert "never require a 2x5" in chat.user

    def test_grid_prompt_still_forbids_hands(self):
        # The opposite contract must survive: a grid with hands IS a defect.
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "grid")
        assert "NO hands" in chat.user
        assert "2-row x 5-column" in chat.user

    def test_hero_prompt_expects_hands(self):
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "hero")
        assert "Hands ARE expected" in chat.user

    def test_unknown_output_type_does_not_crash_or_invent_a_layout(self):
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "something_new")
        assert "internal coherence" in chat.user


class TestVisibleNailCount:
    def test_single_hand_view_is_judged_against_five_not_ten(self):
        nails = matrix_visible_nails("p5_left_hand")
        assert nails is not None and len(nails) == 5
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "matrix_cell", visible_nails=nails)
        assert "exactly 5 nails" in chat.user
        assert "required 10" not in chat.user

    def test_both_hands_view_is_judged_against_ten(self):
        nails = matrix_visible_nails("p2_open_hands")
        assert nails is not None and len(nails) == 10
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "matrix_cell", visible_nails=nails)
        assert "exactly 10 nails" in chat.user

    def test_out_of_frame_nails_are_declared_not_a_defect(self):
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "matrix_cell",
                        visible_nails=matrix_visible_nails("p3_right_hand"))
        assert "out of frame" in chat.user

    def test_the_named_slots_reach_the_prompt(self):
        nails = matrix_visible_nails("p3_right_hand")
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "matrix_cell", visible_nails=nails)
        for nail in nails:
            assert nail in chat.user

    def test_grid_without_visible_nails_keeps_the_ten_nail_rule(self):
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png")], "", "grid")
        assert "Exactly 10 nails" in chat.user

    @pytest.mark.parametrize("view", MATRIX_VIEWS)
    def test_every_contract_view_has_a_visible_nail_list(self, view):
        nails = matrix_visible_nails(view)
        assert nails, f"{view} has no visible_nails; the judge would guess"
        assert len(nails) in (5, 10)

    def test_unknown_view_yields_none_rather_than_a_wrong_list(self):
        assert matrix_visible_nails("p9_nonexistent") is None


class TestDistortionCheck:
    def test_prompt_asks_about_proportions_when_a_base_photo_is_supplied(self):
        chat = RecordingChat()
        auto_qa_verdict(chat, [Path("cand.png"), Path("plan.png"), Path("hand.png")],
                        "", "matrix_cell", visible_nails=["nail-01"])
        assert "Image 3" in chat.user
        assert "proportions" in chat.user
        # The defect a human spots instantly must be named explicitly.
        assert "elongated" in chat.user

    def test_all_three_images_are_forwarded_to_the_judge(self):
        chat = RecordingChat()
        images = [Path("cand.png"), Path("plan.png"), Path("hand.png")]
        auto_qa_verdict(chat, images, "", "matrix_cell", visible_nails=["nail-01"])
        assert chat.images == images


class TestVerdictParsing:
    def test_failure_verdict_is_passed_through(self):
        chat = RecordingChat(
            '{"passed": false, "issues": ["slot 3 wrong"], "correction": "fix slot 3"}')
        verdict = auto_qa_verdict(chat, [Path("c.png")], "", "matrix_cell",
                                  visible_nails=["nail-01"])
        assert verdict["passed"] is False
        assert verdict["issues"] == ["slot 3 wrong"]
        assert verdict["correction"] == "fix slot 3"
