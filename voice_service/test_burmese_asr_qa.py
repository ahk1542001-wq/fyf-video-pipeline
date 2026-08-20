import unittest
from voice_service.burmese_asr_qa import compare_asr, clean_text, segment_burmese


class TestBurmeseASRQA(unittest.TestCase):
    def test_clean_text(self):
        # Zero-width spaces, punctuation, spaces
        text = "အေ အိုင်\u200B (AI)"
        self.assertEqual(clean_text(text), "အေအိုင်AI")

    def test_segment_burmese(self):
        # Test virama linkage and combining marks
        text = clean_text("တက္ကသိုလ်")
        segments = segment_burmese(text)
        self.assertEqual(segments, ['တ', 'က္က', 'သို', 'လ်'])

    def test_segment_burmese_keeps_multi_virama_stack(self):
        self.assertEqual(segment_burmese("က္က္က"), ["က္က္က"])

    def test_exact_match(self):
        result = compare_asr("မင်္ဂလာပါ", "မင်္ဂလာပါ")
        self.assertEqual(result.coverage_ratio, 1.0)
        self.assertFalse(result.review_required)
        self.assertEqual(result.missing_spans, [])
        self.assertEqual(result.extra_spans, [])

    def test_punctuation_spacing_equivalence(self):
        # Should be treated as exactly the same
        result = compare_asr("မင်္ဂလာပါ၊ အေ အိုင် (AI)", "မင်္ဂလာပါ အေအိုင် AI")
        self.assertEqual(result.coverage_ratio, 1.0)
        self.assertFalse(result.review_required)

    def test_missing_burmese_phrase(self):
        result = compare_asr("မင်္ဂလာပါ ခင်ဗျာ", "မင်္ဂလာပါ")
        self.assertLess(result.coverage_ratio, 1.0)
        self.assertTrue(result.review_required)
        self.assertEqual("".join(result.missing_spans), "ခင်ဗျာ")

    def test_substitution(self):
        result = compare_asr("ကောင်းတယ်", "ကောင်းဘူး")
        self.assertLess(result.coverage_ratio, 1.0)
        self.assertTrue(result.review_required)
        self.assertTrue(result.missing_spans)
        self.assertTrue(result.extra_spans)

    def test_zero_width_chars(self):
        result = compare_asr("အေ\u200Bအိုင်", "အေအိုင်")
        self.assertEqual(result.coverage_ratio, 1.0)
        self.assertFalse(result.review_required)

    def test_protected_term(self):
        # Expected contains the term, ASR missed it
        result = compare_asr("အေ အိုင် အကြောင်း", "အကြောင်း", protected_terms=["အေ အိုင်"])
        self.assertLess(result.coverage_ratio, 1.0)
        self.assertTrue(result.review_required)
        self.assertFalse(result.protected_term_presence["အေ အိုင်"])

        # ASR got it
        result2 = compare_asr("အေ အိုင် အကြောင်း", "အေ အိုင် အကြောင်း", protected_terms=["အေ အိုင်"])
        self.assertTrue(result2.protected_term_presence["အေ အိုင်"])
        self.assertFalse(result2.review_required)

    def test_invalid_threshold(self):
        with self.assertRaises(ValueError):
            compare_asr("က", "က", threshold=1.01)

    def test_extra_transcription_requires_review(self):
        result = compare_asr("မင်္ဂလာပါ", "မင်္ဂလာပါ ခင်ဗျာ")
        self.assertEqual(result.coverage_ratio, 1.0)
        self.assertTrue(result.extra_spans)
        self.assertTrue(result.review_required)

if __name__ == '__main__':
    unittest.main()
