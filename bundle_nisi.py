"""Verify and copy the exact public Nisi 0.2.0 release, entirely offline."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

SOURCE = Path(__file__).resolve().parent / 'vendor' / 'nisi'
MANIFEST_SHA256 = 'd7cd9f31365ad47f01ccfbfaf2cf29923830abf642c60518cbeb1b9776d8cd23'


def _no_symlink_components(path: Path) -> None:
    if '..' in path.parts:
        raise ValueError('Traversal path refused')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Symlink path refused')


def verify(source: Path = SOURCE) -> dict:
    source = Path(source).absolute()
    _no_symlink_components(source)
    if not source.is_dir():
        raise ValueError('Nisi source directory missing')
    manifest_path = source / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Nisi provenance missing or unsafe')
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        raise ValueError('Nisi provenance hash mismatch')
    manifest = json.loads(manifest_bytes)
    expected = {'package/' + name for name in manifest['files']} | {'manifest.json'}
    actual = set()
    expected_dirs = {str(p) for name in expected for p in Path(name).parents if str(p) != '.'}
    for base, directories, filenames in os.walk(source, followlinks=False):
        for name in directories:
            path = Path(base) / name
            if path.is_symlink() or str(path.relative_to(source)) not in expected_dirs:
                raise ValueError('Unexpected or symlink directory')
        for name in filenames:
            path = Path(base) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('Nonregular payload file')
            actual.add(str(path.relative_to(source)))
    if actual != expected:
        raise ValueError('Nisi payload file set mismatch')
    for name, digest in manifest['files'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe manifest path')
        if hashlib.sha256((source / 'package' / relative).read_bytes()).hexdigest() != digest:
            raise ValueError('Nisi payload hash mismatch: ' + name)
    return manifest


verify_payload = verify


def resource_inputs() -> list[Path]:
    manifest = verify()
    return [SOURCE / 'package' / name for name in sorted(manifest['files'])] + [SOURCE / 'manifest.json']


def copy_bundle(destination: Path, source: Path = SOURCE) -> dict:
    manifest = verify(source)
    destination = Path(destination).absolute()
    _no_symlink_components(destination)
    if os.path.lexists(destination):
        raise ValueError('Destination must not exist')
    if not destination.parent.is_dir():
        raise ValueError('Destination parent must exist')
    temporary = Path(tempfile.mkdtemp(prefix='.nisi-stage-', dir=destination.parent))
    try:
        for name in ['package/' + name for name in manifest['files']] + ['manifest.json']:
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((Path(source) / name).read_bytes())
            target.chmod(0o644)
        verify(temporary)
        # Exclusive mkdir prevents replacing a destination created during copying.
        destination.mkdir()
        try:
            for child in temporary.iterdir():
                shutil.move(str(child), str(destination / child.name))
            verify(destination)
        except Exception:
            shutil.rmtree(destination)
            raise
    finally:
        shutil.rmtree(temporary)
    return {'status': 'PASS', 'version': manifest['version'], 'files': len(manifest['files']), 'destination': str(destination)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--list-inputs', action='store_true')
    args = parser.parse_args()
    if not args.verify and args.destination is None and not args.list_inputs:
        parser.error('Specify --verify or --destination')
    try:
        if args.list_inputs:
            for path in resource_inputs():
                print(path.relative_to(Path(__file__).resolve().parent).as_posix())
            return 0
        result = copy_bundle(args.destination) if args.destination else {'status': 'PASS', 'version': verify()['version'], 'files': 18}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({'status': 'FAIL', 'error': str(exc)}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
