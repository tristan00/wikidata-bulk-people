import argparse
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=pathlib.Path)
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    wheel = max((root / 'dist').glob('*.whl'), key=lambda p: p.stat().st_mtime)
    archive = max((root / 'dist').glob('*.tar.gz'), key=lambda p: p.stat().st_mtime)
    env = {k: v for k, v in os.environ.items() if k.upper() not in
           {'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'}}
    env['PYTHONNOUSERSITE'] = '1'
    report = {'wheel': wheel.name, 'sdist': archive.name, 'platform': platform.platform(),
              'python': platform.python_version(), 'system_site_packages': False, 'installs': []}
    with tempfile.TemporaryDirectory(prefix='wbp-distribution-') as directory:
        target = pathlib.Path(directory).resolve()
        with tarfile.open(archive) as handle:
            names = handle.getnames()
            assert any(n.endswith('/scripts/check_consumer.py') for n in names)
            assert any(n.endswith('/tests/support.py') for n in names)
            assert not any('/audit/' in n or '/docs/' in n or '/.runs/' in n for n in names)
            assert not any(n.endswith('.md') and n.split('/', 1)[-1] not in {'README.md', 'CHANGELOG.md'} for n in names)
            assert not any(n.endswith('/AGENTS.md') for n in names)
            assert all((target / 'source' / n).resolve().is_relative_to(target / 'source') for n in names)
            assert not any(m.issym() or m.islnk() for m in handle.getmembers())
            handle.extractall(target / 'source', **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
        source = next((target / 'source').iterdir())
        consumer = target / 'consumer'
        consumer.mkdir()
        shutil.copyfile(source / 'scripts/check_consumer.py', consumer / 'check_consumer.py')

        def run(command, cwd=consumer, timeout=600):
            result = subprocess.run([str(c) for c in command], cwd=cwd, env=env,
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                print(result.stdout + result.stderr)
                raise RuntimeError(f'Command failed ({result.returncode}): {command}')
            return result.stdout

        for name, artifact, extras in [('base', wheel, ''), ('db', wheel, 'db'),
                                       ('bulk', wheel, 'bulk'), ('combined', wheel, 'db,bulk'),
                                       ('sdist', archive, 'dev')]:
            print(f'Checking isolated {name} installation', flush=True)
            environment = target / ('venv-' + name)
            run([sys.executable, '-m', 'venv', environment])
            python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            spec = str(artifact) + (f'[{extras}]' if extras else '')
            run([python, '-m', 'pip', 'install', '--no-cache-dir', spec])
            run([python, '-m', 'pip', 'check'])
            installed = json.loads(run([python, '-I', 'check_consumer.py']))
            assert installed['db_extra'] == (name in {'db', 'combined', 'sdist'})
            assert installed['bulk_extra'] == (name in {'bulk', 'combined', 'sdist'})
            executable = python.parent / ('wikidata-bulk-people.exe' if sys.platform == 'win32' else 'wikidata-bulk-people')
            installed['cli_version'] = run([executable, 'version']).strip()
            installed['installation'] = name
            if name in {'combined', 'sdist'}:
                run([python, '-m', 'pip', 'install', '--no-cache-dir', str(artifact) + '[dev]'])
                assert json.loads(run([python, '-I', 'check_consumer.py']))['installed_path'] == installed['installed_path']
                tests = run([python, '-m', 'pytest', '-q', '--tb=short', '--import-mode=importlib'], cwd=source)
                installed['packaged_tests'] = tests.strip()
                print(tests.strip(), flush=True)
            report['installs'].append(installed)
    report['passed'] = True
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('All isolated wheel/extra/sdist consumer checks passed.')


if __name__ == '__main__':
    main()
