"""Exercise the isolated trial's backup/recovery commands without systemd."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from eksmu.server import make_server


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('m5_trial', ROOT / 'deployment/google-cloud/m5_trial.py')
trial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trial)


class TrialTests(unittest.TestCase):
    def test_isolated_backup_restore_and_explicit_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / 'trials'
            venv_python = Path(temporary) / 'python'
            venv_python.symlink_to(sys.executable)
            ports = []

            def fake_start(_unit, _app, _python, database, port):
                ports.append(port)
                server = make_server(database, 0)
                server.server_close()

            with (patch.object(trial, 'PARENT', parent),
                  patch.object(trial, 'free'),
                  patch.object(trial, 'start_unit', side_effect=fake_start),
                  patch.object(trial, 'stop_unit')):
                trial.start(SimpleNamespace(app_root=ROOT, app_python=venv_python))
                directory, = parent.iterdir()
                self.assertTrue((directory / trial.MARKER).exists())
                self.assertEqual(trial.load(directory)[1]['python'], str(venv_python))
                trial.restore(SimpleNamespace(run_dir=directory))
                self.assertEqual(ports, [8767, 8768])
                self.assertTrue((directory / 'backup/manifest.json').is_file())
                self.assertTrue((directory / 'restored/community.sqlite').is_file())
                with self.assertRaises(ValueError):
                    trial.cleanup(SimpleNamespace(run_dir=directory, confirm='wrong-run'))
                with self.assertRaises(ValueError):
                    trial.load(Path(temporary))
                trial.cleanup(SimpleNamespace(run_dir=directory, confirm=directory.name))
                self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
