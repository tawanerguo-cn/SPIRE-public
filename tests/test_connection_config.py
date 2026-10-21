from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer.config import load_connection_config


class ConnectionConfigTests(unittest.TestCase):
    def test_default_load_reads_checkout_configuration(self):
        expected = Path(__file__).resolve().parents[1] / "config" / "nanonis.toml"
        opened = []

        def open_config(path, mode):
            opened.append(path)
            self.assertEqual(path, expected)
            self.assertEqual(mode, "rb")
            return io.BytesIO(b'[connection]\nhost="127.0.0.1"\nport=6517\nbackend="fake"\n')

        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "open", open_config):
            config = load_connection_config()
        self.assertEqual(opened, [expected])
        self.assertEqual(config.port, 6517)
        self.assertEqual(config.backend, "fake")
