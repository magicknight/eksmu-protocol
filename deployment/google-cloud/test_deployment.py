import hashlib
from unittest.mock import patch
import importlib.util
import tempfile
import unittest
import socket
import shutil
import subprocess
import zipfile
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from checkout import read_checkout, stage_checkout, allowed_path
spec=importlib.util.spec_from_file_location('eksmu_install', ROOT/'install.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = str(ROOT.parents[1])
        sys.path.insert(0, cls.app)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.app)

    def test_archive_digest_and_paths(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'fixture.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('open-inquiry-community/README.md','fixture')
            with patch.object(module,'SOURCE_SHA256',hashlib.sha256(p.read_bytes()).hexdigest()):
                module.validate_archive(p)

    def test_modified_archive_refused(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'source.zip';p.write_bytes(b'wrong bytes')
            with self.assertRaises(ValueError):module.validate_archive(p)
    def test_existing_directory_refused(self):
        with tempfile.TemporaryDirectory() as t:
            h=Path(t);target=h/'existing';target.mkdir()
            with self.assertRaises(ValueError):module.safe_root(target,h)
    def test_home_itself_refused(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):module.safe_root(Path(t),Path(t))
    def test_outside_home_refused(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):module.safe_root(Path('/outside-eksmu-test'),Path(t))
    def test_new_private_subdirectory_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(module.safe_root(Path(t)/'new',Path(t)),Path(t)/'new')
    def test_symlink_escape_refused(self):
        with tempfile.TemporaryDirectory() as t:
            h=Path(t);(h/'out').symlink_to('/tmp')
            with self.assertRaises(ValueError):module.safe_root(h/'out'/'not-existing',h)
    def test_unit_control_characters_refused(self):
        with tempfile.TemporaryDirectory() as t:
            for name in ('bad\npath','bad%path','bad$path','bad"path'):
                with self.subTest(name=name),self.assertRaises(ValueError):module.safe_root(Path(t)/name,Path(t))
    def test_unit_has_dedicated_data_and_resources(self):
        unit=module.service_text(Path('/home/alice/.local/share/eksmu-test'),8765)
        self.assertIn('/data/community.sqlite',unit)
        for line in ('UMask=0077','CPUQuota=50%','MemoryMax=1G','Restart=on-failure','Nice=10'):
            self.assertIn(line,unit)
        self.assertNotIn('0.0.0.0',unit)
        self.assertNotIn('sudo',unit)
    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze is required')
    def test_generated_unit_is_accepted_by_systemd(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/'install with spaces'
            (root/'app').mkdir(parents=True)
            (root/'venv/bin').mkdir(parents=True)
            (root/'venv/bin/python').symlink_to(sys.executable)
            unit=Path(t)/'eksmu-install-test.service'
            unit.write_text(module.service_text(root,8765))
            result=subprocess.run(['systemd-analyze','--user','verify',str(unit)],
                                  capture_output=True,text=True,timeout=15)
            if result.returncode and 'Failed to initialize manager' in result.stderr:
                # CI containers may have the parser but no user systemd manager.
                # System-scope verification still checks the generated unit syntax.
                result=subprocess.run(['systemd-analyze','verify',str(unit)],
                                      capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_chosen_occupied_port_refused(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));s.listen()
            with self.assertRaises(OSError):module.check_port(s.getsockname()[1])
    def test_app_health_in_private_local_instance(self):
        import sys,threading
        from eksmu.server import make_server
        with tempfile.TemporaryDirectory() as t:
            server=make_server(Path(t)/'test.sqlite',0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:self.assertTrue(module.health(server.server_port,timeout=2))
            finally:server.shutdown();server.server_close();thread.join()

class CheckoutTests(unittest.TestCase):
    def manifest(self, root, data=b'print(1)'):
        import json
        (root/'eksmu').mkdir(exist_ok=True)
        (root/'eksmu/example.py').write_bytes(data)
        p=root/'manifest.json'
        p.write_text(json.dumps({'format':'eksmu.deploy-source.v1','files':{'eksmu/example.py':hashlib.sha256(data).hexdigest()}}))
        return p

    def test_release_manifest_matches_checkout(self):
        files=read_checkout(ROOT.parents[1], ROOT/'source-manifest.json')
        self.assertIn('eksmu/community.py',files)
        self.assertIn('eksmu/research.py',files)
        self.assertIn('requirements-research.txt',files)

    def test_copy_is_identical_and_ignores_unlisted_files(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);m=self.manifest(root)
            (root/'private.key').write_bytes(b'not a real key')
            files=read_checkout(root,m);stage_checkout(files,root/'snapshot')
            self.assertEqual((root/'snapshot/eksmu/example.py').read_bytes(),b'print(1)')
            self.assertFalse((root/'snapshot/private.key').exists())

    def test_changed_source_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);m=self.manifest(root);(root/'eksmu/example.py').write_bytes(b'changed')
            with self.assertRaises(ValueError):read_checkout(root,m)

    def test_file_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);m=self.manifest(root);(root/'eksmu/example.py').unlink()
            (root/'other.py').write_bytes(b'print(1)');(root/'eksmu/example.py').symlink_to(root/'other.py')
            with self.assertRaises(ValueError):read_checkout(root,m)

    def test_directory_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);m=self.manifest(root);(root/'eksmu').rename(root/'other')
            (root/'eksmu').symlink_to(root/'other',target_is_directory=True)
            with self.assertRaises(ValueError):read_checkout(root,m)

    def test_unsafe_manifest_paths_are_refused(self):
        for name in ['../secret.py','/absolute.py','eksmu/../x.py','.git/config','data/a.sqlite','eksmu/a.key','eksmu/.env','eksmu//a.py']:
            with self.subTest(name=name),self.assertRaises(ValueError):allowed_path(name)

    def test_existing_destination_is_not_modified(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/'sentinel').write_text('preserve')
            with self.assertRaises(FileExistsError):stage_checkout({'eksmu/a.py':b'a'},p)
            self.assertEqual((p/'sentinel').read_text(),'preserve')

    def test_missing_runtime_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);m=self.manifest(root);(root/'eksmu/example.py').unlink()
            with self.assertRaises(ValueError):read_checkout(root,m)

if __name__=='__main__':unittest.main(verbosity=2)
