import unittest
import os
import tempfile
from unittest.mock import patch, MagicMock

from voice_service.gemini_tts import generate_gemini_tts

class TestGeminiTTSVertex(unittest.TestCase):
    @patch.dict(os.environ, {"GEMINI_API_KEY": "fake", "GOOGLE_CLOUD_PROJECT": "test-project", "GOOGLE_CLOUD_LOCATION": "asia-southeast1"}, clear=True)
    @patch("voice_service.gemini_tts.genai.Client")
    def test_gemini_tts_vertex_initialization(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        mock_response = MagicMock()
        mock_part = MagicMock()
        mock_part.inline_data.data = b"fake_audio_bytes"
        mock_part.inline_data.mime_type = "audio/mpeg"
        mock_response.candidates = [MagicMock(content=MagicMock(parts=[mock_part]))]

        mock_client.models.generate_content.return_value = mock_response

        with tempfile.TemporaryDirectory() as temp_dir:
            out_file = os.path.join(temp_dir, "test.wav")

            result_path = generate_gemini_tts("Hello world", output_path=out_file)

            self.assertEqual(result_path, out_file)

            with open(out_file, "rb") as f:
                result_bytes = f.read()
            self.assertEqual(result_bytes, b"fake_audio_bytes")

        mock_client_class.assert_called_once()
        _, kwargs = mock_client_class.call_args

        self.assertTrue(kwargs.get("vertexai", False))
        self.assertEqual(kwargs.get("project"), "test-project")
        self.assertEqual(kwargs.get("location"), "asia-southeast1")
        self.assertNotIn("api_key", kwargs)
