import pytest

from tools.markers_eval.score import Tally, judge_intro


@pytest.mark.parametrize(
    ("segment", "verdict"),
    [
        (None, "missed"),
        ((68.0, 86.0), "useful"),
        ((53.0, 91.0), "useful"),  # start 14.5 s early, end 4.75 s late: both inside the tolerances
        ((52.0, 86.0), "wrong"),  # start 15.5 s off
        ((68.0, 91.5), "wrong"),  # end 5.25 s off
    ],
)
def test_judge_intro_uses_the_spec_tolerances(segment, verdict):
    assert judge_intro(segment, (67.5, 86.25)) == verdict


def test_tally_counts_and_compares_with_the_spec():
    t = Tally()
    for v in ["useful"] * 91 + ["wrong"] * 13 + ["missed"] * 14:
        t.add(v)
    assert t.as_dict() == {"useful": 91, "wrong": 13, "missed": 14}
    assert t.at_least(91, 13)
    t.add("wrong")
    assert not t.at_least(91, 13)
