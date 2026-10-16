from pathlib import Path
import tempfile
import unittest

from excel_match_agent.environment import load_environment


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_config_preserving_equals_and_hash(self):
        self.path.write_text('# config\nDEEPSEEK_API_KEY="test=value#literal"\nDEEPSEEK_MODEL=dc\n', encoding="utf-8-sig")
        env = {}
        load_environment(self.path, env)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "test=value#literal")
        self.assertEqual(env["DEEPSEEK_MODEL"], "dc")

    def test_process_environment_has_priority(self):
        self.path.write_text("DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
        env = {"DEEPSEEK_API_KEY": "from-process"}
        load_environment(self.path, env)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "from-process")

    def test_empty_template_does_not_set_key(self):
        self.path.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
        env = {}
        load_environment(self.path, env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)

    def test_internal_gateway_uses_separate_token_and_profile(self):
        self.path.write_text('DEEPSEEK_API_KEY=official-test-key\nCUSTOM_LLM_API_TOKEN=internal-test-token\nDEEPSEEK_API_KEY_ENV=CUSTOM_LLM_API_TOKEN\n', encoding='utf-8')
        env = {}
        load_environment(self.path, env)
        self.assertEqual(env['CUSTOM_LLM_API_TOKEN'], 'internal-test-token')
        self.assertEqual(env['DEEPSEEK_API_KEY_ENV'], 'CUSTOM_LLM_API_TOKEN')
        self.assertNotEqual(env['DEEPSEEK_API_KEY'], env['CUSTOM_LLM_API_TOKEN'])

    def test_unrelated_environment_is_not_modified(self):
        self.path.write_text("PATH=unexpected\nHOME=unexpected\nDEEPSEEK_MODEL=dc\n", encoding="utf-8")
        env = {}
        load_environment(self.path, env)
        self.assertEqual(env, {"DEEPSEEK_MODEL": "dc"})

    def test_error_never_includes_line_contents(self):
        self.path.write_text("secret-placeholder-without-equals", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_environment(self.path, {})
        self.assertNotIn("secret-placeholder", str(caught.exception))
