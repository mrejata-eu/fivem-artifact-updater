#!/usr/bin/env python3
"""FXServer Linux artifact updater: staged install, binary-only backup, rollback.

Requires Python 3.13 and requests. Stop FXServer before running.
Only artifact-owned top-level paths are replaced. User data stays in place.
"""
import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

SERVER = Path('/home/fivem/FXServer/server')
CACHE = Path('/home/fivem/fx-server/txData/QBCore/cache')
BACKUPS = Path('/home/fivem/artifact-backups')
API = 'https://changelogs-live.fivem.net/api/changelog/versions/linux/server'
MAX_MEMBERS = 100000
MAX_UNPACKED = 8 * 1024**3
PROTECTED = {'server.cfg', 'resources', 'txData'}


def info(message):
    print(message, flush=True)


def artifact(channel):
    response = requests.get(API, timeout=25)
    response.raise_for_status()
    data = response.json()
    version = str(data.get(channel, ''))
    url = data.get(channel + '_download')
    if not version.isdecimal() or not isinstance(url, str):
        raise ValueError('Invalid FiveM version API response')
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'runtime.fivem.net'
            or not re.fullmatch(r'/artifacts/fivem/build_proot_linux/master/[^/]+/fx\.tar\.xz', parsed.path)
            or parsed.query or parsed.fragment):
        raise ValueError('Unexpected artifact URL')
    return version, url


def download(url, path):
    info('Downloading artifact...')
    with requests.get(url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        if urlparse(response.url).hostname != 'runtime.fivem.net' or urlparse(response.url).scheme != 'https':
            raise ValueError('Unexpected download redirect')
        expected = int(response.headers.get('Content-Length', 0))
        total = 0
        with path.open('wb') as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    total += len(chunk)
                    output.write(chunk)
                    if expected:
                        print(f'\r{total * 100 / expected:.1f}%', end='', flush=True)
        print()
        if expected and total != expected:
            raise IOError('Incomplete download')


def normalize(name):
    # Archive member names must be canonical relative POSIX paths.
    if not name or '\\' in name or '\x00' in name or name.startswith('/'):
        raise ValueError(f'Unsafe archive name: {name!r}')
    p = PurePosixPath(name)
    if '..' in p.parts or not p.parts or p.parts[0] in ('.', '..'):
        raise ValueError(f'Unsafe archive path: {name!r}')
    return p


def extract(archive_path, root):
    """Never write through archive symlinks; create symlinks only after files.

    Absolute symlinks are allowed ONLY within alpine/, and preserved verbatim.
    They are never followed during extraction. This matches the bundled
    proot/Alpine runtime without allowing links to alter host files.
    """
    info('Validating archive...')
    with tarfile.open(archive_path, 'r:xz') as tf:
        members = tf.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError('Archive contains too many entries')
        entries = {}
        total_size = 0
        for m in members:
            p = normalize(m.name)
            key = str(p)
            if key in entries:
                raise ValueError(f'Duplicate archive path: {key}')
            if not (m.isdir() or m.isfile() or m.issym()):
                raise ValueError(f'Unsupported archive entry: {key} ({m.type!r})')
            if m.isfile():
                total_size += m.size
                if total_size > MAX_UNPACKED:
                    raise ValueError('Archive exceeds unpacked size limit')
            if m.issym():
                if '\x00' in m.linkname or not m.linkname:
                    raise ValueError(f'Invalid symlink: {key}')
                if m.linkname.startswith('/'):
                    if p.parts[0] != 'alpine':
                        raise ValueError(f'Absolute symlink outside alpine: {key} -> {m.linkname}')
                else:
                    # Relative targets must remain inside staging tree.
                    parts = list(p.parts[:-1])
                    for component in PurePosixPath(m.linkname).parts:
                        if component == '..':
                            if not parts:
                                raise ValueError(f'Escaping symlink: {key} -> {m.linkname}')
                            parts.pop()
                        elif component not in ('', '.'):
                            parts.append(component)
            entries[key] = m
        # No archive file/directory may have a symlink ancestor.
        for key in entries:
            parents = PurePosixPath(key).parents
            for parent in parents:
                if str(parent) != '.' and str(parent) in entries and entries[str(parent)].issym():
                    raise ValueError(f'Archive entry under symlink: {key}')
        if 'run.sh' not in entries or not entries['run.sh'].isfile():
            raise ValueError('Artifact does not contain run.sh')
        info(f'Extracting {len(entries)} entries safely...')
        # First create directories, then regular files, then symlinks.
        for key, m in entries.items():
            if m.isdir():
                (root / key).mkdir(parents=True, exist_ok=True)
        for key, m in entries.items():
            if m.isfile():
                dst = root / key
                dst.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    raise IOError(f'Cannot read {key}')
                with src, dst.open('xb') as output:
                    shutil.copyfileobj(src, output, length=1024 * 1024)
                dst.chmod(m.mode & 0o755)
        for key, m in entries.items():
            if m.issym():
                dst = root / key
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(m.linkname)
        # Restore directory execute/read bits; never honor ownership or special bits.
        for key, m in entries.items():
            if m.isdir():
                (root / key).chmod((m.mode & 0o755) | 0o500)
    info('Extraction complete')



def install_binaries(staged, version):
    """Replace only artifact-owned top-level entries; never copy resources/txData.

    Previous artifact entries are recorded in a manifest. If no manifest exists,
    existing paths matching the new artifact are treated as artifact-owned, except
    for protected user paths, which cause an abort.
    """
    manifest = SERVER / '.artifact-paths'
    previous = set()
    if manifest.is_file():
        previous = set(manifest.read_text().splitlines())
    incoming = {p.name for p in staged.iterdir()}
    protected = PROTECTED | {'.artifact-version', '.artifact-paths'}
    if incoming & protected:
        raise RuntimeError(f'Artifact conflicts with protected names: {incoming & protected}')
    if previous & protected:
        raise RuntimeError('Unsafe artifact manifest: protected path')
    for name in previous:
        if name != Path(name).name or name in ('.', '..'):
            raise RuntimeError('Unsafe artifact manifest entry')
    # On initial migration, refuse unrecognized top-level directories that
    # coincide with artifact names only when they are protected by policy.
    for name in incoming:
        if name in PROTECTED:
            raise RuntimeError(f'Protected path collision: {name}')

    SERVER.mkdir(parents=True, exist_ok=True)
    BACKUPS.mkdir(parents=True, exist_ok=True)
    backup = BACKUPS / ('binaries-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir()
    touched = sorted(incoming | previous)
    moved_old = []
    installed = []
    try:
        # Backup via rename: fast and no copying of resources or txData.
        for name in touched:
            src = SERVER / name
            if src.exists() or src.is_symlink():
                src.rename(backup / name)
                moved_old.append(name)
        for name in sorted(incoming):
            (staged / name).rename(SERVER / name)
            installed.append(name)
        for name in ('.artifact-version', '.artifact-paths'):
            src = SERVER / name
            if src.exists() or src.is_symlink():
                src.rename(backup / name)
                moved_old.append(name)
        (SERVER / '.artifact-version').write_text(version + '\n')
        (SERVER / '.artifact-paths').write_text('\n'.join(sorted(incoming)) + '\n')
    except BaseException:
        info('Install failed; restoring previous artifact files...')
        for name in installed + ['.artifact-version', '.artifact-paths']:
            target = SERVER / name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        for name in reversed(moved_old):
            (backup / name).rename(SERVER / name)
        raise
    info(f'Previous artifact files saved in: {backup}')
    info('User resources/configuration were not copied or moved.')


def running():
    result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-f', 'FXServer|run.sh'],
                            capture_output=True, text=True, check=False)
    # pgrep can match an unrelated process; fail closed when it reports a match.
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description='Safe FiveM artifact updater')
    parser.add_argument('--channel', choices=('recommended', 'latest'), default='recommended')
    parser.add_argument('--clear-cache', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if SERVER.is_symlink() or (SERVER.exists() and not SERVER.is_dir()):
        raise RuntimeError('Server directory must be a real directory')
    if running():
        raise RuntimeError('FXServer appears to be running. Stop it first.')
    version, url = artifact(args.channel)
    info(f'{args.channel}: build {version}')
    installed = (SERVER / '.artifact-version').read_text().strip() if (SERVER / '.artifact-version').is_file() else None
    info(f'Installed build: {installed or "unknown"}')
    if installed == version and not args.force:
        info('Already up to date')
        if args.clear_cache:
            clear_cache()
        return
    SERVER.parent.mkdir(parents=True, exist_ok=True)
    BACKUPS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.fxupdate-', dir=SERVER.parent) as tmp:
        tmp = Path(tmp)
        archive = tmp / 'fx.tar.xz'
        staged = tmp / 'staged'
        staged.mkdir()
        download(url, archive)
        extract(archive, staged)
        if running():
            raise RuntimeError('FXServer started during update; refusing install')
        install_binaries(staged, version)
        info('Installed build ' + version)
    if args.clear_cache:
        clear_cache()
    info('Update complete. Start FXServer and verify it works before deleting the backup.')


def clear_cache():
    if CACHE.is_symlink():
        raise RuntimeError('Cache path is a symlink')
    if CACHE.is_dir():
        info(f'Clearing cache: {CACHE}')
        shutil.rmtree(CACHE)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterrupted', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
