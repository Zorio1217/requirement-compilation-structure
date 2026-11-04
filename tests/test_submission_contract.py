"""Exercise the platform's exact argv against an extracted submission, without model calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from scripts.package import package_directory

ROOT = Path(__file__).resolve().parents[1]


class SubmissionContract(unittest.TestCase):
    def test_config_default_and_explicit_cli_override(self):
        import main
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'requirements.yaml').write_text('id: ROOT\n')
            config = root / 'compiler_config.json'
            config.write_text('{"workflow":"optimized"}')
            with patch.object(main, '__file__', str(root / 'main.py')):
                argv = [str(root), '--output-dir', str(root.parent / (root.name + '-output'))]
                self.assertTrue(main.parse_args(argv).optimize)
                self.assertFalse(main.parse_args([*argv, '--no-optimize']).optimize)
            self.assertFalse(main.parse_args(argv).optimize)

    def test_zip_excludes_credentials_and_old_archives(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            for name in ['main.py', 'requirements.txt', 'infrastructure/compiler.py',
                         'infrastructure/shared/project/templates/deploy.sh', '.env.example',
                         '.env', '.env.local', 'old.zip', 'node_modules/secret.txt',
                         '.venv/secret.txt', 'nested/.env', 'nested/old.zip']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('test fixture')
            archive = root / 'submission.zip'
            package_directory(root, archive, 'optimized')
            with zipfile.ZipFile(archive) as zipped:
                names = zipped.namelist()
                self.assertIn('main.py', names)
                self.assertIn('.env.example', names)
                self.assertEqual(json.loads(zipped.read('compiler_config.json')), {'workflow':'optimized'})
                for name in names:
                    self.assertNotEqual(Path(name).name, '.env')
                    self.assertNotEqual(Path(name).name, '.env.local')
                    self.assertFalse(name.endswith('.zip'))
                    self.assertFalse(name.startswith(('node_modules/', '.venv/')))

    def test_platform_argv_uses_packaged_optimized_mode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, source, output = root / 'submission.zip', root / 'task', root / 'app'
            package_directory(ROOT, archive, 'optimized')
            unpacked = root / 'compiler'
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(unpacked)
            source.mkdir()
            (source / 'requirements.yaml').write_text('id: ROOT\nname: Static\nchildren: []\n')
            # All pipeline/import/argument/dispatch code is real. Only paid model invocation
            # and generated-app runtime stages are replaced inside a subprocess.
            probe = root / 'probe.py'
            probe.write_text('''import json,sys
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,sys.argv.pop(1))
import main
from infrastructure.compiler import Compiler
from infrastructure.shared.agent.optimization import policy
original = Compiler.compile
def compile_probe(self,stage=None):
    assert policy().enabled, 'Platform argv did not enable optimized workflow'
    original(self,stage='00_project_initialization')
    return original(self,stage='01_explore_requirement')
with patch.object(Compiler,'compile',compile_probe): main.main()
''')
            result = subprocess.run([sys.executable, str(probe), str(unpacked), str(source),
                '--output-dir', str(output)], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Compiler workflow: optimized', result.stdout)
            self.assertTrue((output / 'deploy.sh').is_file())
            self.assertTrue((output / 'frontend/package.json').is_file())
            self.assertTrue((output / 'backend/package.json').is_file())
            self.assertEqual(json.loads((output / '.arc/requirements.json').read_text())['id'], 'ROOT')
            self.assertTrue(json.loads((output / '.arc/optimization.json').read_text())['enabled'])


if __name__ == '__main__':
    unittest.main()
