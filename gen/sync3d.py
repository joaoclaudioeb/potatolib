#!/usr/bin/env python3
"""Keep the lib's 3D models in a GitHub Release (tag "3dshapes") instead of git.

    python3 gen/sync3d.py pull             release -> 3dmodels/  (only missing or changed files)
    python3 gen/sync3d.py push             3dmodels/ -> release  (new files + replace changed ones)
    python3 gen/sync3d.py push --prune     ... and delete from the release what no longer exists locally
    python3 gen/sync3d.py status           show the differences, change nothing

Options: --dir <folder> (default 3dmodels, relative to the lib root), --tag <tag> (default 3dshapes),
         --repo owner/name (default: taken from `git remote get-url origin`), --dry-run (push).

pull needs only Python 3 (public repo; a token in GH_TOKEN/GITHUB_TOKEN or a logged-in gh is used
if available, to avoid the 60 requests/hour anonymous API limit).
push needs the GitHub CLI (gh) logged in with write access to the repo.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent
API = os.environ.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
UA = {'User-Agent': 'potatolib-sync3d'}
SAFE_NAME = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9._-]*[A-Za-z0-9_-]$')  # GitHub renames anything else
MAX_ASSETS = 1000                                                        # GitHub limit per release
BATCH = 20                                                               # files per `gh release upload`


# ---------------------------------------------------------------- helpers

def die(msg):
    print(f'error: {msg}', file=sys.stderr)
    sys.exit(1)


def detect_repo():
    r = subprocess.run(['git', '-C', str(LIB), 'remote', 'get-url', 'origin'], capture_output=True, text=True)
    m = re.search(r'github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$', r.stdout.strip())
    if not m:
        die('could not find the GitHub repo from `git remote get-url origin`; use --repo owner/name')
    return f'{m.group(1)}/{m.group(2)}'


def get_token():
    tok = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not tok and shutil.which('gh'):
        r = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True)
        if r.returncode == 0:
            tok = r.stdout.strip()
    return tok or None


def api_get(path, tok):
    req = urllib.request.Request(API + path, headers={**UA, 'Accept': 'application/vnd.github+json',
                                                     'X-GitHub-Api-Version': '2022-11-28'})
    if tok:
        req.add_header('Authorization', f'Bearer {tok}')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and not tok:
            die('GitHub API rate limit reached (anonymous). Log in with `gh auth login` '
                'or set GH_TOKEN, then run again.')
        if e.code == 404:
            raise
        die(f'GitHub API answered {e.code} for {path}')
    except (urllib.error.URLError, OSError) as e:      # no network, proxy, DNS, dropped connection...
        die(f'could not reach the GitHub API ({API}): {getattr(e, "reason", e)}')


def remote_assets(repo, tag, tok):
    """{name: {size, sha, url, api_url}} for the release, or None if the release does not exist."""
    try:
        rel = api_get(f'/repos/{repo}/releases/tags/{tag}', tok)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    out, page = {}, 1
    while True:
        batch = api_get(f'/repos/{repo}/releases/{rel["id"]}/assets?per_page=100&page={page}', tok)
        for a in batch:
            dig = a.get('digest') or ''
            ok = a.get('state', 'uploaded') == 'uploaded'
            out[a['name']] = dict(size=a['size'] if ok else -1,
                                  sha=dig.split(':', 1)[1] if ok and dig.startswith('sha256:') else None,
                                  url=a['browser_download_url'], api_url=a['url'])
        if len(batch) < 100:
            return out
        page += 1


def local_files(folder):
    if not folder.is_dir():
        return {}
    return {p.name: p for p in sorted(folder.iterdir())
            if p.is_file() and not p.name.startswith('.') and not p.name.endswith('.part')}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def same(path, asset):
    """True if the local file matches the asset (size first; sha256 only when sizes match)."""
    if path.stat().st_size != asset['size']:
        return False
    return asset['sha'] is None or sha256(path) == asset['sha']


def compare(local, remote):
    only_local = [n for n in local if n not in remote]
    only_remote = [n for n in remote if n not in local]
    changed = [n for n in local if n in remote and not same(local[n], remote[n])]
    return only_local, changed, only_remote


def gh(*args):
    r = subprocess.run(['gh', *args])
    if r.returncode:
        die(f'`gh {" ".join(args[:3])} ...` failed')


def show(title, names, limit=15):
    if not names:
        return
    print(f'{title} ({len(names)}):')
    for n in names[:limit]:
        print(f'   {n}')
    if len(names) > limit:
        print(f'   ... +{len(names) - limit}')


# ---------------------------------------------------------------- download

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _open_asset(asset, tok):
    if not tok:  # public repo: plain download link, no API call
        return urllib.request.urlopen(urllib.request.Request(asset['url'], headers=UA), timeout=120)
    # with a token go through the API (works for private repos too); the token must not follow the
    # redirect to the storage host, so the redirect is followed by hand without it
    req = urllib.request.Request(asset['api_url'], headers={**UA, 'Accept': 'application/octet-stream',
                                                            'Authorization': f'Bearer {tok}'})
    try:
        return urllib.request.build_opener(_NoRedirect).open(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get('Location'):
            return urllib.request.urlopen(urllib.request.Request(e.headers['Location'], headers=UA), timeout=120)
        raise


def download(name, asset, folder, tok):
    dest, part = folder / name, folder / (name + '.part')
    with _open_asset(asset, tok) as r, open(part, 'wb') as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if part.stat().st_size != asset['size'] or (asset['sha'] and sha256(part) != asset['sha']):
        part.unlink()
        raise RuntimeError(f'{name}: downloaded file does not match the release (size/sha256)')
    os.replace(part, dest)
    return name


# ---------------------------------------------------------------- commands

def cmd_pull(a):
    tok = get_token()
    remote = remote_assets(a.repo, a.tag, tok)
    if remote is None:
        die(f'release "{a.tag}" not found in {a.repo}')
    a.dir.mkdir(parents=True, exist_ok=True)
    local = local_files(a.dir)
    only_local, changed, only_remote = compare(local, remote)
    todo = only_remote + changed
    print(f'{a.repo} @ {a.tag}: {len(remote)} files in the release, {len(local)} in {a.name}')
    show('new', only_remote)
    show('updated', changed)
    if only_local:
        print(f'({len(only_local)} local file(s) not in the release were left untouched)')
    if not todo:
        print('already up to date')
        return
    errors, tty = 0, sys.stdout.isatty()
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(download, n, remote[n], a.dir, tok): n for n in todo}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                f.result()
            except Exception as e:  # keep going, report at the end
                errors += 1
                print(f'   failed: {futs[f]}: {e}', file=sys.stderr)
            if tty:
                print(f'\r   {i}/{len(todo)}', end='', flush=True)
    if tty:
        print()
    if errors:
        die(f'{errors} file(s) failed; run again to retry')
    print('done')


def cmd_push(a):
    if not shutil.which('gh'):
        die('push needs the GitHub CLI: https://cli.github.com  (then `gh auth login`)')
    local = local_files(a.dir)
    if not local:
        die(f'no files in {a.name}')
    bad = [n for n in local if not SAFE_NAME.match(n)]
    if bad:
        show('these names would be renamed by GitHub (use only letters, digits, . _ -)', bad, limit=50)
        die('rename them (and the model path in their footprints) before pushing')
    tok = get_token()
    remote = remote_assets(a.repo, a.tag, tok)
    if remote is None:
        print(f'release "{a.tag}" does not exist yet: it will be created')
        remote = {}
    only_local, changed, only_remote = compare(local, remote)
    total = len(remote) + len(only_local) - (len(only_remote) if a.prune else 0)
    print(f'{a.repo} @ {a.tag}: {len(local)} files in {a.name}, {len(remote)} in the release')
    show('upload (new)', only_local)
    show('replace (changed)', changed)
    show('delete from the release (--prune)' if a.prune else 'only in the release (kept; use --prune to delete)',
         only_remote)
    if total > MAX_ASSETS:
        die(f'the release would have {total} files; GitHub allows {MAX_ASSETS} per release')
    if not (only_local or changed or (a.prune and only_remote)):
        print('nothing to upload')
        return
    if a.dry_run:
        print('(dry run: nothing changed)')
        return
    if not remote and only_local:
        gh('release', 'create', a.tag, '--repo', a.repo, '--title', '3D models',
           '--notes', 'Storage for the lib 3D models. Download with `make 3d` (gen/sync3d.py pull).')
    if a.prune:
        for n in only_remote:
            gh('release', 'delete-asset', a.tag, n, '--repo', a.repo, '--yes')
    for names, extra in ((only_local, []), (changed, ['--clobber'])):
        for i in range(0, len(names), BATCH):
            gh('release', 'upload', a.tag, '--repo', a.repo, *extra, *[str(local[n]) for n in names[i:i + BATCH]])
    print('done')


def cmd_status(a):
    tok = get_token()
    remote = remote_assets(a.repo, a.tag, tok)
    if remote is None:
        print(f'release "{a.tag}" does not exist in {a.repo}')
        remote = {}
    local = local_files(a.dir)
    only_local, changed, only_remote = compare(local, remote)
    print(f'{a.repo} @ {a.tag}: {len(local)} files in {a.name}, {len(remote)} in the release')
    show('only local (push uploads)', only_local)
    show('different (push replaces / pull downloads)', changed)
    show('only in the release (pull downloads)', only_remote)
    if not (only_local or changed or only_remote):
        print('in sync')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('cmd', choices=['pull', 'push', 'status'])
    p.add_argument('--dir', default='3dmodels',
                   help='models folder, relative to the lib root (default: 3dmodels)')
    p.add_argument('--tag', default='3dshapes', help='release tag used as storage (default: 3dshapes)')
    p.add_argument('--repo', help='owner/name (default: from git remote origin)')
    p.add_argument('--prune', action='store_true', help='push: delete release files that no longer exist locally')
    p.add_argument('--dry-run', action='store_true', help='push: only show what would be done')
    p.add_argument('-j', '--jobs', type=int, default=8, help='pull: parallel downloads (default: 8)')
    a = p.parse_args()
    a.dir = (LIB / a.dir) if not Path(a.dir).is_absolute() else Path(a.dir)
    a.name = a.dir.relative_to(LIB).as_posix() if a.dir.is_relative_to(LIB) else str(a.dir)
    a.repo = a.repo or detect_repo()
    {'pull': cmd_pull, 'push': cmd_push, 'status': cmd_status}[a.cmd](a)


if __name__ == '__main__':
    main()
