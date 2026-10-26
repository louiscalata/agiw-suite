"""Freeze build inputs without following links or reopening checked source paths."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import sys


def freeze_inputs(source_fd, destination, inputs):
    """Copy retained regular-file descriptors into a new, private source tree."""
    destination = Path(destination)
    with ExitStack() as retained:
        directories = {'': source_fd}
        files = {}

        def open_input(name):
            parts = Path(name).parts
            if not parts or Path(name).is_absolute() or '..' in parts:
                raise ValueError('Unsafe build input path')
            parent = ''
            for component in parts[:-1]:
                child = '/'.join(filter(None, (parent, component)))
                if child not in directories:
                    descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY |
                                         os.O_NOFOLLOW, dir_fd=directories[parent])
                    retained.callback(os.close, descriptor)
                    directories[child] = descriptor
                parent = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW |
                                 os.O_NONBLOCK, dir_fd=directories[parent])
            retained.callback(os.close, descriptor)
            details = os.fstat(descriptor)
            if stat.S_ISDIR(details.st_mode):
                directories[name] = descriptor
                for child in sorted(os.listdir(descriptor)):
                    open_input(name + '/' + child)
            elif stat.S_ISREG(details.st_mode):
                files[name] = (descriptor, details)
            else:
                raise ValueError('Nonregular build input: ' + name)

        # Hold all inputs before copying any bytes. A renamed source directory or
        # replaced filename cannot redirect a later copy outside this set.
        for name in inputs:
            open_input(name)
        if 'vendor/nisi' in inputs:
            descriptor = files['vendor/nisi/manifest.json'][0]
            with os.fdopen(os.dup(descriptor), 'rb') as manifest_file:
                manifest = json.load(manifest_file)
            os.lseek(descriptor, 0, os.SEEK_SET)
            expected = {'vendor/nisi/package/' + name for name in manifest['files']}
            expected.add('vendor/nisi/manifest.json')
            expected_dirs = {str(parent) for name in expected for parent in Path(name).parents
                             if str(parent).startswith('vendor/nisi')}
            if ({name for name in files if name.startswith('vendor/nisi/')} != expected or
                    {name for name in directories if name.startswith('vendor/nisi')} != expected_dirs):
                raise ValueError('Nisi payload file set mismatch')
        destination.mkdir(mode=0o700)
        for name, (descriptor, before) in files.items():
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as output:
                while chunk := os.read(descriptor, 1024 * 1024):
                    output.write(chunk)
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Build input changed while freezing: ' + name)
            target.chmod(0o644)


if __name__ == '__main__':
    root = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        freeze_inputs(root, sys.argv[2], sys.argv[3:])
    finally:
        os.close(root)
