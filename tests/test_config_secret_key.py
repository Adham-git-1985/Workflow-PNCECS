import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.py"


def load_config_module(name: str):
    spec = importlib.util.spec_from_file_location(name, CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SecretKeyConfigTests(unittest.TestCase):
    def test_empty_environment_value_uses_nonempty_fallback(self):
        with patch.dict(os.environ, {"SECRET_KEY": ""}):
            module = load_config_module("config_empty_secret_test")
        self.assertTrue(module.DevConfig.SECRET_KEY)
        self.assertGreaterEqual(len(module.DevConfig.SECRET_KEY), 32)

    def test_explicit_environment_value_is_preserved(self):
        expected = "local-test-secret-that-is-long-enough"
        with patch.dict(os.environ, {"SECRET_KEY": expected}):
            module = load_config_module("config_explicit_secret_test")
        self.assertEqual(module.DevConfig.SECRET_KEY, expected)
        self.assertEqual(module.ProdConfig.SECRET_KEY, expected)


if __name__ == "__main__":
    unittest.main()
