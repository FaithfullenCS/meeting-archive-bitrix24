"""Local, read-only discovery of portable archives; SQLite is a replaceable index."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .settings import portal_domain


def safe_files(root: Path, name: str):
    root = root.resolve()
    if not root.is_dir():
        return
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith('.') and
                   not (Path(base) / d).is_symlink() and not (Path(base) / d).is_junction()]
        path = Path(base) / name
        if name in files and not path.is_symlink() and path.resolve().is_relative_to(root):
            yield path


def local_accounts(root):
    """Discover identities from directory names, without reading message contents."""
    root = Path(root).resolve()
    result = []
    if not root.is_dir():
        return result
    for portal in sorted(root.iterdir()):
        if not portal.is_dir() or portal.is_symlink() or portal.is_junction():
            continue
        try:
            if portal_domain(portal.name) != portal.name:
                continue
        except ValueError:
            continue
        for user in sorted(portal.glob('user-*')):
            number = user.name.removeprefix('user-')
            if (number.isdecimal() and int(number) > 0 and user.is_dir() and
                    not user.is_symlink() and not user.is_junction()):
                result.append({'portal': portal.name, 'user_id': int(number)})
    return result


def backup_index(service):
    target = service.home / 'recovery-backups'
    target.mkdir(parents=True, exist_ok=True)
    path = target / f'index-{time.time_ns()}.sqlite'
    with service.db.lock, closing(sqlite3.connect(path)) as copy:
        service.db.connection.backup(copy)
    return path.name


def ensure_backup(service, report):
    if 'backup' not in report:
        report['backup'] = backup_index(service)


def restore_meetings(service, report):
    archive = service.archive
    for marker in safe_files(archive.root, 'meeting.json'):
        report['found'] += 1
        report['current'] = report['found']
        try:
            data = json.loads(marker.read_text('utf-8'))
            if data.get('schemaVersion') != 1 or data.get('source') not in {'bitrix', 'import'}:
                raise ValueError('Неизвестный формат совещания')
            portal = str(data['portal'])
            metadata = data['metadata']
            if not isinstance(metadata, dict) or str(metadata.get('callId')) != str(data['callId']):
                raise ValueError('Идентичность совещания не совпадает с манифестом')
            if str(metadata.get('uuid') or '') != str(data.get('uuid') or ''):
                raise ValueError('UUID совещания не совпадает с манифестом')
            folder = marker.parent
            entry = {'portal': portal, 'call_id': str(data['callId']), 'uuid': data.get('uuid') or '',
                     'metadata': json.dumps(metadata), 'source': data['source'], 'folder': ''}
            if archive.folder(entry) != folder.resolve():
                raise ValueError('Идентичность папки совещания не совпадает с манифестом')
            existing = service.db.rows('SELECT * FROM meetings WHERE portal=? AND call_id=? AND uuid=?',
                                       (portal, entry['call_id'], entry['uuid']))
            if existing and service.db.rows("SELECT id FROM jobs WHERE meeting_id=? AND state='running' LIMIT 1", (existing[0]['id'],)):
                report['known'] += 1
                continue
            if existing and existing[0]['folder'] and Path(existing[0]['folder']).resolve() != folder.resolve():
                raise ValueError('Совещание уже связано с другой папкой')
            present = set()
            for relative, details in data.get('files', {}).items():
                path = archive.contained(folder / relative, within=folder)
                if path.is_symlink() or any(p.is_symlink() or p.is_junction() for p in path.parents if p != archive.root):
                    raise ValueError('Ссылки в архиве не поддерживаются')
                if path.is_file() and path.stat().st_size == details.get('size'):
                    present.add(path.relative_to(folder).as_posix())
                else:
                    report['missing'] += 1
            audio = any(p.startswith('audio/') for p in present)
            bitrix = any(p in present for p in ('bitrix/transcript.json', 'bitrix/transcript.txt', 'bitrix/transcript.md'))
            runs = []
            for marker_run in (folder / 'local').glob('*/run.json'):
                archive.contained(marker_run, within=folder)
                if marker_run.is_symlink() or marker_run.parent.is_symlink() or marker_run.parent.is_junction():
                    raise ValueError('Ссылки в архиве не поддерживаются')
                run = json.loads(marker_run.read_text('utf-8'))
                if any((marker_run.parent / f'transcript.{ext}').relative_to(folder).as_posix() in present
                       for ext in ('txt', 'json', 'md')) and type(run.get('sample')) is bool:
                    runs.append(run['sample'])
            states = {'audio': 'saved' if audio else 'not_saved', 'bitrix': 'saved' if bitrix else 'not_saved',
                      'local': 'saved' if False in runs else 'tested' if runs else 'not_saved'}
            if existing and existing[0]['folder'] and all(existing[0][key] == value for key, value in states.items()):
                report['known'] += 1
                continue
            with service.db.lock:
                ensure_backup(service, report)
                record = service.db.upsert(portal, metadata, source=data['source'])
                service.db.update_meeting(record['id'], folder=str(folder), **states,
                                          requested=existing[0]['requested'] if existing else 0)
            report['restored'] += 1
        except (ValueError, OSError, KeyError, TypeError) as exc:
            report['errors'] += 1
            if len(report['issues']) < 30:
                report['issues'].append({'kind': 'meeting', 'path': marker.relative_to(archive.root).as_posix(),
                                         'error': service.vault.redact(str(exc))})


def restore_local(service):
    report = {'running': True, 'found': 0, 'current': 0, 'restored': 0, 'known': 0,
              'missing': 0, 'errors': 0, 'issues': [], 'started_at': time.time()}
    service.recovery_status = report
    try:
        restore_meetings(service, report)
        engine = service.chat_archive
        engine.local_accounts = None
        store = engine.store()
        if store.portal and store.user_id:
            result = store.recover(tolerant=True, hold_work=True, before_insert=lambda: ensure_backup(service, report))
            for key in ('found', 'restored', 'known', 'errors'):
                report[key] += result[key]
            report['issues'].extend(result['issues'][:max(0, 30 - len(report['issues']))])
            engine.recovered.add(store.account)
        report['accounts'] = local_accounts(service.settings.chat_archive_root)
    except (OSError, ValueError, sqlite3.Error) as exc:
        report['errors'] += 1
        report['issues'].append({'kind': 'index', 'error': service.vault.redact(str(exc))})
    finally:
        report.update(running=False, completed_at=time.time())
    return report


def verify_local(service):
    """Explicit full checksum verification, separated from quick startup discovery."""
    from .archive import sha256
    report = {"running": True, "checked": 0, "errors": 0, "issues": []}
    service.integrity_status = report
    try:
        for marker in safe_files(service.archive.root, 'meeting.json'):
            try:
                data = json.loads(marker.read_text('utf-8'))
                for relative, details in data.get('files', {}).items():
                    path = service.archive.contained(marker.parent / relative, within=marker.parent)
                    report['checked'] += 1
                    if path.is_symlink() or any(p.is_symlink() or p.is_junction() for p in path.parents if p != service.archive.root):
                        raise ValueError('Ссылки в архиве не поддерживаются')
                    if not path.is_file() or sha256(path) != details.get('sha256'):
                        report['errors'] += 1
            except (ValueError, OSError, KeyError, TypeError):
                report['errors'] += 1
        store = service.chat_archive.store()
        for marker in safe_files(store.folder / 'chats', 'chat.json'):
            try:
                data = json.loads(marker.read_text('utf-8'))
                for item in data.get('reading', []) + data.get('files', []):
                    relative = item.get('jsonl') or item.get('path')
                    if not relative or not item.get('sha256'):
                        continue
                    path = (marker.parent / relative).resolve()
                    report['checked'] += 1
                    if not path.is_relative_to(marker.parent.resolve()) or path.is_symlink() or not path.is_file() or sha256(path) != item['sha256']:
                        report['errors'] += 1
            except (ValueError, OSError, KeyError, TypeError):
                report['errors'] += 1
    finally:
        report.update(running=False, completed_at=time.time())
    return report
