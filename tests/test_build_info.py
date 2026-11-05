"""Run with: uv run python -m unittest discover -s tests -p test_build_info.py"""
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from songwriter import build_info


class BuildInfoTests(unittest.TestCase):
    def test_checkout_reports_exact_commit_and_modified_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            outputs = ['a' * 40, 'main', 'v0.3.0', ' M src/example.py']
            results = [subprocess.CompletedProcess([], 0, value, '') for value in outputs]
            with patch.object(build_info, 'version', return_value='0.1.0'), patch.object(
                build_info.subprocess, 'run', side_effect=results
            ):
                info = build_info.collect_build_info(root)
        self.assertEqual(info['commit'], 'a' * 40)
        self.assertEqual(info['branch'], 'main')
        self.assertEqual(info['tag'], 'v0.3.0')
        self.assertTrue(info['dirty'])
        self.assertEqual(info['label'], 'v0.1.0 · aaaaaaa (modified)')

    def test_wheel_install_does_not_pick_up_parent_checkout(self):
        with TemporaryDirectory() as directory:
            with patch.object(build_info, 'version', return_value='0.1.0'), patch.object(
                build_info.subprocess, 'run'
            ) as run:
                info = build_info.collect_build_info(Path(directory))
            run.assert_not_called()
        self.assertIsNone(info['commit'])
        self.assertIn('commit unavailable', info['label'])

    def test_missing_git_is_nonfatal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            with patch.object(build_info, 'version', return_value='0.1.0'), patch.object(
                build_info.subprocess, 'run', side_effect=FileNotFoundError
            ):
                info = build_info.collect_build_info(root)
        self.assertIsNone(info['commit'])

    def test_git_timeout_is_nonfatal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            with patch.object(build_info, 'version', return_value='0.1.0'), patch.object(
                build_info.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 2)
            ):
                info = build_info.collect_build_info(root)
        self.assertIsNone(info['commit'])

    def test_uninstalled_source_uses_project_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pyproject.toml').write_text('[project]\nversion = "0.4.0"\n')
            with patch.object(build_info, 'version', side_effect=build_info.PackageNotFoundError):
                info = build_info.collect_build_info(root)
        self.assertEqual(info['version'], '0.4.0')

    def test_running_build_is_a_snapshot_not_a_live_git_lookup(self):
        original = dict(build_info.BUILD_INFO)
        with patch.object(build_info, 'collect_build_info', return_value={'commit': 'new'}):
            self.assertEqual(build_info.BUILD_INFO, original)


if __name__ == '__main__':
    unittest.main()
