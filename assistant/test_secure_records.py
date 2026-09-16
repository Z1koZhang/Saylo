# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secure_records as S


class SecureRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = AESGCM.generate_key(bit_length=256)
        self.other_key = AESGCM.generate_key(bit_length=256)

    def test_blob_round_trip_and_stream_binding(self) -> None:
        source = "一条包含隐私的记录".encode("utf-8")
        blob = S.seal_bytes(source, "journal", self.key)
        self.assertNotIn(source, blob)
        self.assertEqual(S.open_bytes(blob, "journal", self.key), source)
        with self.assertRaises(S.SecureRecordsError):
            S.open_bytes(blob, "reasoning", self.key)

    def test_wrong_key_and_tamper_are_rejected(self) -> None:
        blob = S.seal_bytes(b"private", "journal", self.key)
        with self.assertRaises(S.SecureRecordsError):
            S.open_bytes(blob, "journal", self.other_key)
        damaged = bytearray(blob)
        damaged[-1] ^= 1
        with self.assertRaises(S.SecureRecordsError):
            S.open_bytes(bytes(damaged), "journal", self.key)

    def test_record_and_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records_path = root / "records.senc"
            state_path = root / "state.senc"
            rows = [{"who": "me", "text": "你好"}, {"n": 2}]
            S.write_records(records_path, "journal", rows, self.key)
            self.assertEqual(S.read_records(records_path, "journal", self.key), rows)
            S.append_record(records_path, "journal", {"n": 3}, self.key)
            self.assertEqual(
                S.read_records(records_path, "journal", self.key),
                rows + [{"n": 3}],
            )
            state = {"mood": "warm", "value": 0.7}
            S.write_json(state_path, "emotion_state", state, self.key)
            self.assertEqual(
                S.read_json(state_path, "emotion_state", key=self.key), state
            )

    def test_persisted_dpapi_path_does_not_depend_on_startup_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = root / "actual" / "records.key.dpapi"
            metadata = root / "records.key.json"
            metadata.write_text(
                '{"dpapi_path": "' + str(configured).replace('\\', '\\\\') + '"}',
                encoding="utf-8",
            )
            with (patch.object(S, "KEY_META", metadata),
                  patch.object(S, "DPAPI_KEY", root / "wrong-fallback")):
                self.assertEqual(S.configured_dpapi_path(), configured)


if __name__ == "__main__":
    unittest.main()
