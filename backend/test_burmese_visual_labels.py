import inspect
import unittest

from writer_agent_vertex import generate_exact_lock, generate_narration_script


class BurmeseVisualLabelPromptTests(unittest.TestCase):
    def test_vertex_prompt_requires_burmese_visible_labels(self):
        source = inspect.getsource(generate_narration_script)
        self.assertIn("narration-first", source)
        self.assertIn("Production visuals are added", source)

    def test_exact_lock_prompt_requires_burmese_visible_labels(self):
        source = inspect.getsource(generate_exact_lock)
        self.assertIn("viewer-visible labels", source)
        self.assertIn("beginner-friendly Burmese", source)


if __name__ == "__main__":
    unittest.main()
