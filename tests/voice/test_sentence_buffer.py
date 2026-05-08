"""Tests for the TTS sentence buffer."""

from gobby.voice.sentence_buffer import SentenceBuffer


class TestSentenceBuffer:
    def test_no_boundary_returns_empty(self):
        buf = SentenceBuffer()
        assert buf.feed("Hello world") == []

    def test_single_sentence(self):
        buf = SentenceBuffer()
        result = buf.feed("Hello world. ")
        assert result == ["Hello world."]

    def test_multiple_sentences(self):
        buf = SentenceBuffer()
        result = buf.feed("First sentence. Second sentence. ")
        assert result == ["First sentence.", "Second sentence."]

    def test_incremental_chunks(self):
        buf = SentenceBuffer()
        assert buf.feed("Hello ") == []
        assert buf.feed("world. ") == ["Hello world."]
        assert buf.feed("Next one") == []

    def test_question_mark_boundary(self):
        buf = SentenceBuffer()
        result = buf.feed("Really? Yes. ")
        assert result == ["Really?", "Yes."]

    def test_exclamation_mark_boundary(self):
        buf = SentenceBuffer()
        result = buf.feed("Wow! That's great. ")
        assert result == ["Wow!", "That's great."]

    def test_flush_remaining(self):
        buf = SentenceBuffer()
        buf.feed("Some partial text")
        assert buf.flush() == ["Some partial text"]

    def test_flush_empty(self):
        buf = SentenceBuffer()
        assert buf.flush() == []

    def test_flush_after_complete_sentence(self):
        buf = SentenceBuffer()
        result = buf.feed("Complete. Partial")
        assert result == ["Complete."]
        assert buf.flush() == ["Partial"]

    def test_clear_discards_buffer(self):
        buf = SentenceBuffer()
        buf.feed("Some text")
        buf.clear()
        assert buf.flush() == []

    def test_short_fragment_merged(self):
        """Short fragments like 'Dr.' should be merged with next sentence."""
        buf = SentenceBuffer(min_length=10)
        result = buf.feed("Dr. Smith is here. ")
        # "Dr." is too short (3 chars), gets merged with "Smith is here."
        assert len(result) == 1
        assert "Dr." in result[0]
        assert "Smith is here." in result[0]

    def test_short_fragment_pushed_to_buffer(self):
        """If only short fragments exist, they go back to buffer."""
        buf = SentenceBuffer(min_length=20)
        result = buf.feed("Hi. Ok. ")
        # Both are too short, pushed back to buffer
        assert result == []
        flushed = buf.flush()
        assert "Hi." in " ".join(flushed)

    def test_mixed_punctuation(self):
        buf = SentenceBuffer()
        result = buf.feed("What?! Yes, I think so. Really! ")
        assert len(result) == 3
        assert result == ["What?!", "Yes, I think so.", "Really!"]

    def test_no_split_on_abbreviations(self):
        """Periods not followed by whitespace shouldn't split."""
        buf = SentenceBuffer()
        result = buf.feed("Visit example.com for details. ")
        assert len(result) == 1
        assert "example.com" in result[0]

    def test_multiline_text(self):
        buf = SentenceBuffer()
        result = buf.feed("First line.\nSecond line. ")
        # \n is not a sentence boundary (only whitespace after .!? matters)
        # But "First line.\n" contains ".\n" which our regex matches as ". " equivalent
        assert len(result) >= 1

    def test_default_max_chunk_chars_splits_after_180_chars(self):
        first_clause = f"{'a' * 170},"
        second_clause = "short tail"
        buf = SentenceBuffer()

        assert buf.flush() == []
        buf.feed(f"{first_clause} {second_clause}")

        assert buf.flush() == [first_clause, second_clause]

    def test_splits_long_sentence_on_clause_boundaries(self):
        buf = SentenceBuffer(max_chunk_chars=32)

        result = buf.feed(
            "Alpha beta gamma, Delta epsilon zeta; Eta theta iota: "
            "Kappa lambda — Mu nu xi – Omicron pi rho. "
        )

        assert result == [
            "Alpha beta gamma,",
            "Delta epsilon zeta;",
            "Eta theta iota: Kappa lambda —",
            "Mu nu xi – Omicron pi rho.",
        ]

    def test_whitespace_fallback_splits_clause_that_exceeds_limit(self):
        buf = SentenceBuffer(max_chunk_chars=20)

        buf.feed("alpha beta gamma delta epsilon zeta")

        assert buf.flush() == ["alpha beta gamma", "delta epsilon zeta"]

    def test_unsplittable_token_can_exceed_limit(self):
        token = "x" * 25
        buf = SentenceBuffer(max_chunk_chars=10)

        buf.feed(token)

        assert buf.flush() == [token]

    def test_flush_splits_remaining_text_into_ordered_chunks(self):
        buf = SentenceBuffer(max_chunk_chars=18)

        buf.feed("alpha beta, gamma delta epsilon")

        assert buf.flush() == ["alpha beta,", "gamma delta", "epsilon"]
