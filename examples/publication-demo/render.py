"""Render the two reviewed repository papers with Pandoc and XeLaTeX.

Requires local TeX Live (ctexart, Fandol, xurl) and Pandoc. Does not accept
uploaded source or run as a web service. PDF byte reproducibility across tool
versions is not promised; rerendering requires explicit manifest renewal.
"""
import os
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    pandoc, xelatex = shutil.which('pandoc'), shutil.which('xelatex')
    if not pandoc or not xelatex:
        raise SystemExit('Install Pandoc and XeLaTeX locally before optional PDF rebuilding.')
    with tempfile.TemporaryDirectory(prefix='oic-demo-render-') as directory:
        work = Path(directory)
        env = {'PATH': str(Path(xelatex).parent) + os.pathsep + os.defpath,
               'HOME': str(work), 'TMPDIR': str(work), 'SOURCE_DATE_EPOCH': '1790121600',
               'openin_any': 'p', 'openout_any': 'p', 'shell_escape': 'f'}
        for version in ('v1', 'v2'):
            source = work / 'paper.md'; source.write_bytes((ROOT / version / 'paper.md').read_bytes())
            result = subprocess.run([pandoc, '--sandbox', 'paper.md', '-f', 'markdown', '-t', 'latex',
                    '--standalone', '--syntax-highlighting=none', '-V', 'documentclass=ctexart',
                    '-V', 'classoption=fontset=fandol', '-V', 'geometry:margin=25mm',
                    '-V', 'fontsize=11pt', '-V', 'colorlinks=true',
                    '-V', 'header-includes=\\emergencystretch=2em', '-o', 'paper.tex'],
                    cwd=work, env=env, capture_output=True, timeout=60, preexec_fn=limits)
            if result.returncode:
                raise RuntimeError(result.stderr.decode())
            for _ in range(2):
                result = subprocess.run([xelatex, '-no-shell-escape', '-halt-on-error',
                                         '-interaction=nonstopmode', 'paper.tex'], cwd=work, env=env,
                                        capture_output=True, timeout=90, preexec_fn=limits)
                if result.returncode:
                    raise RuntimeError(result.stdout.decode(errors='replace')[-5000:])
            log = (work / 'paper.log').read_text(errors='replace')
            if 'Missing character:' in log or 'Overfull \\hbox' in log:
                warnings = [line for line in log.splitlines() if 'Missing character:' in line or 'Overfull' in line]
                raise RuntimeError('PDF layout warnings: ' + '; '.join(warnings))
            for suffix in ('tex', 'pdf'):
                shutil.copyfile(work / ('paper.' + suffix), ROOT / version / ('paper.' + suffix))
            print(f'{version}: PDF and TeX rendered from paper.md')


if __name__ == '__main__':
    main()
