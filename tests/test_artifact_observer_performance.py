from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from data_layer.artifact_observer import SessionArtifactObserver


class ArtifactObserverEnumerationTests(unittest.TestCase):
    def test_directory_metadata_reuse_preserves_changes_and_does_not_reopen_each_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            original = root / "scan.SXM"
            original.write_bytes(b"old")
            (root / "ignored.txt").write_bytes(b"ignore")
            (root / "directory.dat").mkdir()
            observer = SessionArtifactObserver(root, lambda _: None)
            first = observer._scan()
            original.write_bytes(b"new content")
            with patch.object(Path, "resolve", side_effect=AssertionError("per-file resolution")), \
                 patch.object(Path, "stat", side_effect=AssertionError("per-file stat")):
                second = observer._scan()
            self.assertEqual(set(second), {original})
            self.assertEqual(second[original].size_bytes, len(b"new content"))
            self.assertNotEqual(first[original], second[original])
            self.assertIs(next(iter(first)), next(iter(second)))
            original.unlink()
            self.assertEqual(observer._scan(), {})
            self.assertEqual(observer._file_paths, {})

    def test_recursive_option_and_existing_file_change_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            nested = root / "nested"
            nested.mkdir()
            path = nested / "result.dat"
            path.write_bytes(b"before")
            emitted = []
            observer = SessionArtifactObserver(root, emitted.append, recursive=True)
            observer._baseline = observer._scan()
            self.assertEqual(SessionArtifactObserver(root, emitted.append)._scan(), {})
            path.write_bytes(b"after content")
            observer._poll_once()
            observer._poll_once()
            observer._poll_once()
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0].source_path, path)

    def test_new_and_deleted_files_do_not_reuse_a_stale_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            observer = SessionArtifactObserver(root, lambda _: None)
            self.assertEqual(observer._scan(), {})
            path = root / "new.dat"
            path.write_bytes(b"first")
            first = observer._scan()[path]
            path.unlink()
            self.assertEqual(observer._scan(), {})
            path.write_bytes(b"replacement data")
            self.assertNotEqual(observer._scan()[path], first)
