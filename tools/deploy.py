"""Plan, apply, verify or roll back explicitly owned source files in an installation.

Plans record every source and destination hash. Apply refuses drift, backs up every
changed file, and records the exact revisions. It never starts services or copies
secrets, game assets, databases, state files, or files outside the manifest.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def digest(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("Expected file: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def within(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or '..' in rel.parts or '.git' in rel.parts:
        raise ValueError("Unsafe manifest path: " + str(relative))
    result = (root / rel).resolve()
    if result == root or root not in result.parents:
        raise ValueError("Path escapes root: " + str(relative))
    return result


def revision(root):
    r = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2) + '\n').encode('utf-8')
    atomic_bytes(path, data)


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.deploy-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def plan(source, target, manifest):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or source in target.parents or target in source.parents:
        # Existing installations may contain checkouts below them; files are
        # nevertheless constrained individually below. Only identical roots fail.
        if source == target:
            raise ValueError('Source and installation must be different directories')
    spec = json.loads(Path(manifest).read_text(encoding='utf-8-sig'))
    if spec.get('schema') != 1:
        raise ValueError('Unsupported deployment manifest schema')
    entries, seen = [], set()
    for item in spec['files']:
        src = within(source, item['source']); dst = within(target, item['destination'])
        if source == dst or source in dst.parents:
            raise ValueError('Deployment destination overlaps source checkout')
        key = str(dst).casefold()
        if key in seen:
            raise ValueError('Duplicate destination: ' + str(dst))
        seen.add(key)
        sha = digest(src)
        if sha is None:
            raise ValueError('Missing source: ' + str(src))
        entries.append(dict(item, source_sha256=sha, previous_sha256=digest(dst)))
    return {'schema': 1, 'component': spec['component'], 'source': str(source),
            'target': str(target), 'revision': revision(source), 'files': entries}


def check(plan, installed=False):
    source, target = Path(plan['source']).resolve(), Path(plan['target']).resolve()
    if not installed and revision(source) != plan['revision']:
        raise ValueError('Source revision changed; create a new plan')
    for item in plan['files']:
        src, dst = within(source, item['source']), within(target, item['destination'])
        if not installed and digest(src) != item['source_sha256']:
            raise ValueError('Source changed: ' + item['source'])
        expected = item['source_sha256'] if installed else item['previous_sha256']
        if digest(dst) != expected:
            raise ValueError('Installed file changed: ' + item['destination'])


def apply(plan):
    check(plan)
    source, target = Path(plan['source']).resolve(), Path(plan['target']).resolve()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    record = target / 'deployments' / (plan['component'] + '-' + stamp)
    record.mkdir(parents=True, exist_ok=False)
    changed = [x for x in plan['files'] if x['source_sha256'] != x['previous_sha256']]
    written = []
    lock = target / 'deployments' / '.deploy.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError('Another deployment holds ' + str(lock))
    try:
        os.close(fd)
        check(plan)
        for item in changed:
            dst = within(target, item['destination'])
            if item['previous_sha256'] is not None:
                backup = within(record / 'backup', item['destination'])
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup)
        save_json(record / 'plan.json', plan)
        try:
            for item in changed:
                src, dst = within(source, item['source']), within(target, item['destination'])
                if digest(dst) != item['previous_sha256']:
                    raise ValueError('Concurrent edit: ' + item['destination'])
                data = src.read_bytes()
                if hashlib.sha256(data).hexdigest() != item['source_sha256']:
                    raise ValueError('Concurrent source edit: ' + item['source'])
                atomic_bytes(dst, data)
                written.append(item)
            check(plan, installed=True)
        except Exception:
            for item in reversed(written):
                dst = within(target, item['destination'])
                if digest(dst) != item['source_sha256']:
                    continue  # Preserve a concurrent edit instead of overwriting it.
                if item['previous_sha256'] is None:
                    dst.unlink()
                else:
                    atomic_bytes(dst, within(record / 'backup', item['destination']).read_bytes())
            raise
        save_json(record / 'receipt.json', dict(plan, applied_utc=stamp, changed_files=len(changed)))
        return record / 'receipt.json'
    finally:
        lock.unlink(missing_ok=True)


def rollback(receipt):
    receipt = Path(receipt).resolve()
    data = json.loads(receipt.read_text(encoding='utf-8'))
    target = Path(data['target']).resolve()
    check(data, installed=True)  # Never overwrite edits made after deployment.
    changed = [x for x in data['files'] if x['source_sha256'] != x['previous_sha256']]
    for item in changed:
        if item['previous_sha256'] is not None:
            backup = within(receipt.parent / 'backup', item['destination'])
            if digest(backup) != item['previous_sha256']:
                raise ValueError('Backup missing or changed: ' + item['destination'])
    for item in reversed(changed):
        dst = within(target, item['destination'])
        if item['previous_sha256'] is None:
            dst.unlink()
        else:
            atomic_bytes(dst, within(receipt.parent / 'backup', item['destination']).read_bytes())
    save_json(receipt.parent / 'rolled-back.json', {'receipt': str(receipt)})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    modes = ap.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan', metavar='FILE')
    modes.add_argument('--apply', metavar='PLAN')
    modes.add_argument('--verify', metavar='RECEIPT')
    modes.add_argument('--rollback', metavar='RECEIPT')
    ap.add_argument('--source'); ap.add_argument('--target'); ap.add_argument('--manifest')
    a = ap.parse_args()
    if a.plan:
        if not all((a.source, a.target, a.manifest)):
            ap.error('--plan requires --source, --target, and --manifest')
        data = plan(a.source, a.target, a.manifest)
        save_json(Path(a.plan), data)
        print('Plan:', a.plan, '| changed files:', sum(x['source_sha256'] != x['previous_sha256'] for x in data['files']))
    elif a.apply:
        print('Receipt:', apply(json.loads(Path(a.apply).read_text(encoding='utf-8'))))
    elif a.verify:
        check(json.loads(Path(a.verify).read_text(encoding='utf-8')), installed=True)
        print('All deployed file hashes match the receipt.')
    else:
        rollback(a.rollback)
        print('Deployment rolled back.')

if __name__ == '__main__':
    main()
