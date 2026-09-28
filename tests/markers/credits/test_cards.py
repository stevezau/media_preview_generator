"""Prose cards at the credits start (spec §5.4, "Prose cards"): the cards, the prose rule and the walk past them."""

from __future__ import annotations

import pytest

from media_preview_generator.markers.credits import cards

LINE_1 = (100, 60, 219, 68)
LINE_2 = (90, 75, 229, 83)
LINE_3 = (110, 90, 209, 98)


def row(t: float, *boxes, luma: float = 16.0):
    return (t, len(boxes), luma, tuple(boxes))


class TestCards:
    def test_seconds_with_text_between_blank_ones_are_one_card(self):
        rows = [row(0.0), row(1.0, LINE_1), row(2.0, LINE_1), row(3.0), row(4.0, LINE_2)]
        found = cards.cards(rows, 0.0)
        assert [(c.first_s, c.last_s) for c in found] == [(1.0, 2.0), (4.0, 4.0)]

    def test_lines_appearing_one_after_another_stay_one_card_read_when_all_are_on_screen(self):
        # A Beautiful Imperfection's epilogue: four sentences appear one at a time over 20 s.
        rows = [row(1.0, LINE_1), row(2.0, LINE_1, LINE_2), row(3.0, LINE_1, LINE_2, LINE_3),
                row(4.0, LINE_1, LINE_2, LINE_3), row(5.0, LINE_1, LINE_2, LINE_3)]  # fmt: skip
        [card] = cards.cards(rows, 0.0)
        assert (card.first_s, card.last_s, card.read_s) == (1.0, 5.0, 4.0)  # the middle of the full seconds

    def test_a_line_that_grows_as_it_fades_in_is_the_same_card(self):
        rows = [row(1.0, (140, 81, 184, 88)), row(2.0, (100, 75, 280, 88))]
        assert len(cards.cards(rows, 0.0)) == 1

    def test_text_fading_out_is_the_same_card(self):
        rows = [row(1.0, LINE_1, LINE_2), row(2.0, LINE_1, LINE_2), row(3.0, (120, 61, 200, 67))]
        [card] = cards.cards(rows, 0.0)
        assert (card.first_s, card.last_s) == (1.0, 3.0)

    def test_text_that_moves_starts_a_card_every_second(self):
        # A crawl: its lines move up between two samples, so no second's text lies inside the one before.
        rows = [row(float(t), (100, 100 - 12 * t, 219, 108 - 12 * t)) for t in range(4)]
        assert [c.first_s for c in cards.cards(rows, 0.0)] == [0.0, 1.0, 2.0, 3.0]

    def test_other_text_elsewhere_with_no_blank_between_is_another_card(self):
        # Accused S03E03: the last epilogue card, then "EXECUTIVE PRODUCERS" low on the next second's footage.
        rows = [row(1.0, LINE_1, LINE_2), row(2.0, (140, 125, 185, 133), luma=68.0)]
        found = cards.cards(rows, 0.0)
        assert [(c.first_s, c.dark) for c in found] == [(1.0, True), (2.0, False)]

    def test_seconds_before_the_start_are_left_out_and_rows_are_read_in_time_order(self):
        rows = [row(5.0, LINE_1), row(0.0, LINE_2), row(4.0, LINE_1)]
        assert [(c.first_s, c.last_s) for c in cards.cards(rows, 3.0)] == [(4.0, 5.0)]

    def test_a_card_is_dark_by_its_read_second(self):
        rows = [row(1.0, LINE_1, luma=45.0), row(2.0, LINE_1, LINE_2, luma=20.0)]
        [card] = cards.cards(rows, 0.0)
        assert card.read_s == 2.0 and card.dark


class TestIsProse:
    @pytest.mark.parametrize(
        "lines",
        [
            ["At the sentencing hearing, the Judge hears testimony", "from Rebecca and her attorney."],
            ["SHE SURVIVED HER HUSBAND BY MORE THAN THIRTY YEARS."],  # capitals: the full stop decides
            ["The investigation is now closed."],
            ["father, brother, and sister."],  # four words
            ['"I WOULD HAVE DONE ANYTHING TO PREVENT THE', "ABUSE THAT HAPPENED.", "DETAILS"],  # across two lines
            ["\"YOU ARE NOT A MONSTER. YOU HAVE VALUE.'"],  # a quote after the full stop
            ["IN 2023,", "APPROXIMATELY 181 GIRLS WERE KIDNAPPED...DAILY."],  # an ellipsis is no initialism
            ["The filmmakers reached out to Hannah on multiple occasions and received no response"],  # 7+ lower
            # Cut short by its boxes, still three words to a line (To Dye For's first epilogue card).
            ["ON OCTOBER 7TH, 2023", "OERNER NEWSOM SIGNED", "THE BIL", "BAN", "ING RED 3 IN CALIFORNIA."],
        ],
    )
    def test_epilogue_sentences_are_prose(self, lines):
        assert cards.is_prose(lines)

    @pytest.mark.parametrize(
        "lines",
        [
            ["directed by", "Billy Ray"],
            ["PRODUCED BY", "HEYMAN, PASCAL, BAUMBACH,", "P.G.A."],  # Jay Kelly: an initialism's full stop
            ["David Heyman, Amy Pascal, Noah Baumbach, p.g.a."],
            ["ROBERT DOWNEY JR."],  # a name's "Jr." ends no sentence of four words
            ["Filmed on location in Washington, D.C."],
            ["IN LOVING MEMORY OF", "ERWIN OLAF", "(1959 - 2023)"],  # a dedication
            ["Developed with the assistance of Ginny Loane and Gaysorn Thavat"],  # 10 words, only 5 lower
            ["AN ADDITIONAL CROWD SAFETY EXPERT WAS CONSULTED", "TO VERIFY THE CONCLUSIONS IN THIS FILM"],
            [],
            # A list whose line ends in a full stop: names and roles don't fill their lines as a sentence does.
            ["CUSTODY SERGEANT", "CRAIG KARPEL", "CLARKE PETERS", "ERIC COLMAN", "ISABELLA", "CHRIS EUBANK JR."],
            ["CAST", "(in order of appearance)", "Travis..", "HARRY DEAN STANTON", "Doctor Ulmer.", "BERNHARD WICKI"],
            ["Productora de Línea", "Mariana Ponisio", "Asistente de Dirección", "Waldo Salgado L."],
        ],
    )
    def test_credit_cards_and_everything_without_a_sentence_are_not(self, lines):
        assert not cards.is_prose(lines)

    def test_a_short_line_ending_a_sentence_starts_the_next_count_afresh(self):
        # "JR." ends the first line's three words: the next line's words don't make it four.
        assert not cards.is_prose(["ROBERT DOWNEY JR.", "CHRIS EVANS"])
        assert cards.is_prose(["ROBERT DOWNEY JR.", "AND CHRIS EVANS WERE THERE."])

    @pytest.mark.parametrize(("last_words", "prose"), [(4, True), (3, False)])
    def test_a_sentence_needs_three_words_to_each_of_its_lines(self, last_words, prose):
        # Nine words over three lines is three to a line; eight isn't.
        lines = ["ONE TWO", "THREE FOUR FIVE", " ".join(["MORE"] * (last_words - 1)) + " END."]
        assert cards.is_prose(lines) is prose

    def test_each_sentence_is_judged_on_its_own_lines(self):
        # A list line ending in a full stop starts the count afresh, so the sentence after it has its own lines.
        assert cards.is_prose(["NAME", "ANOTHER NAME", "A NAME JR.", "THE CASE WAS NEVER SOLVED."])


def card(t: float, *, dark: bool = True, seconds: float = 3.0) -> cards.Card:
    return cards.Card(t, t + seconds, t + seconds / 2, dark)


class TestPastProse:
    def _walk(self, first, rest, texts):
        read, asked = [], []

        def reader(c):
            read.append(c.first_s)
            return texts[c.first_s]

        def later():
            asked.append(True)
            return rest

        return cards.past_prose(first, later, reader), read, asked

    def test_the_start_moves_to_the_first_card_after_the_prose_ones(self):
        texts = {0.0: ["A sentence of five words."], 10.0: ["Another one of five words."], 20.0: ["directed by", "X"],
                 30.0: ["Never read at all."]}  # fmt: skip
        moved, read, _ = self._walk(card(0.0), [card(10.0), card(20.0), card(30.0)], texts)
        assert moved == 20.0 and read == [0.0, 10.0, 20.0]

    def test_a_start_whose_card_is_not_prose_stays_and_nothing_after_it_is_decoded(self):
        moved, read, asked = self._walk(card(0.0), [card(10.0)], {0.0: ["directed by", "X"]})
        assert moved is None and read == [0.0] and asked == []

    def test_a_start_on_a_lit_card_is_never_read(self):
        moved, read, asked = self._walk(card(0.0, dark=False), [card(10.0)], {})
        assert moved is None and read == [] and asked == []

    def test_no_card_at_the_start_keeps_it(self):
        moved, read, _ = self._walk(None, [card(10.0)], {})
        assert moved is None and read == []

    def test_cards_after_the_first_are_read_lit_or_dark(self):
        # Accused S03E03: a newspaper photo between the epilogue cards reads as prose too.
        texts = {0.0: ["Five words in a sentence."], 10.0: ['"YOU HAVE VALUE AND WORTH."'], 20.0: ["EXECUTIVE"]}
        moved, _, _ = self._walk(card(0.0), [card(10.0, dark=False), card(20.0, dark=False)], texts)
        assert moved == 20.0

    def test_a_start_on_a_card_seen_for_one_second_is_read_as_any_other(self):
        # #SKYKING: the disclaimer's fade-in is a card of its own, one second long; the disclaimer follows it.
        texts = {0.0: ["Horizon states that they did not and do not discriminate against individuals"],
                 1.0: ["Horizon states that they did not and do not discriminate against individuals"],
                 21.0: ["Executive Producers", "MICHAEL BERNSTEIN"]}  # fmt: skip
        moved, read, _ = self._walk(card(0.0, seconds=0.0), [card(1.0, seconds=17.0), card(21.0)], texts)
        assert moved == 21.0 and read == [0.0, 1.0, 21.0]

    def test_a_card_seen_for_one_second_after_the_prose_is_where_the_roll_continues(self):
        # Paris, Texas: the dedication, then the cast crawl, whose dot leaders end lines in full stops.
        texts = {0.0: ["For Lotte H. Eisner."], 20.0: ["Never read: the roll is moving."]}
        moved, read, _ = self._walk(card(0.0), [card(5.0, seconds=0.0), card(20.0)], texts)
        assert moved == 5.0 and read == [0.0]

    def test_prose_to_the_end_of_the_window_keeps_the_start(self):
        texts = {0.0: ["Five words in a sentence."], 10.0: ["Five more words in one."]}
        moved, _, _ = self._walk(card(0.0), [card(10.0)], texts)
        assert moved is None
