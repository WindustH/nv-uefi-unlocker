#!/usr/bin/env python3
"""Opt-in Linux /boot ESP + GRUB deployment for this fork. Standard library only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

STATE = Path('/var/lib/nv-uefi-unlocker/installation.json')
ESP = Path('/boot')
GRUB = Path('/etc/default/grub')
BOOTCFG = Path('/boot/grub/grub.cfg')
ENV = Path('/boot/grub/grubenv')
HOOK = Path('/etc/grub.d/99_local_menu_once')
EFI_DIR = Path('/boot/EFI/nvunlock')
EFI_PATH = r'\EFI\nvunlock\BOOTX64.EFI'
KEYS = ('GRUB_DEFAULT', 'GRUB_TIMEOUT', 'GRUB_TIMEOUT_STYLE', 'GRUB_DISABLE_BOOTNEXT')
PARAM = 'GRUB_CMDLINE_LINUX_DEFAULT'
HERE = Path(__file__).resolve().parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(*args):
    return subprocess.check_output([str(a) for a in args], text=True)


def assignments(text):
    result = {}
    for line in text.splitlines():
        m = re.match(r'^([A-Z_]+)=(.*)$', line)
        if m:
            if m[1] in result:
                raise ValueError('Duplicate GRUB assignment: ' + m[1])
            result[m[1]] = line
    return result


def value(line):
    if line is None:
        return ''
    words = shlex.split(line.split('=', 1)[1])
    if len(words) != 1:
        raise ValueError('Expected one quoted GRUB value: ' + line)
    return words[0]


def set_lines(text, changes):
    lines = text.splitlines()
    for key, replacement in changes.items():
        matches = [i for i, line in enumerate(lines) if line.startswith(key + '=')]
        if len(matches) > 1:
            raise ValueError('Duplicate setting: ' + key)
        if matches:
            if replacement is None:
                lines.pop(matches[0])
            else:
                lines[matches[0]] = replacement
        elif replacement is not None:
            lines.append(replacement)
    return '\n'.join(lines) + '\n'


def quoted(key, val):
    # GRUB defaults are shell source: quote values, never evaluate profiles.
    return key + '=' + shlex.quote(val)


def configure_grub(text, profile):
    old = assignments(text)
    changes = {k: quoted(k, v) for k, v in {
        'GRUB_DEFAULT': profile['default_id'], 'GRUB_TIMEOUT': '0',
        'GRUB_TIMEOUT_STYLE': 'hidden', 'GRUB_DISABLE_BOOTNEXT': 'true',
    }.items()}
    if profile.get('iomem_relaxed', False):
        tokens = [t for t in value(old.get(PARAM)).split() if not t.startswith('iomem=')]
        changes[PARAM] = quoted(PARAM, ' '.join(tokens + ['iomem=relaxed']))
    return set_lines(text, changes)


def restore_grub(current, before, installed):
    """Restore owned keys only; retain subsequent unrelated user changes."""
    now, old, ours = map(assignments, (current, before, installed))
    changes = {}
    for key in KEYS + (PARAM,):
        if old.get(key) == ours.get(key):
            continue
        if now.get(key) == ours.get(key):
            changes[key] = old.get(key)
        elif key == PARAM:
            original = value(old.get(key)).split()
            tokens = value(now.get(key)).split()
            if 'iomem=relaxed' not in original and 'iomem=relaxed' in tokens:
                tokens.remove('iomem=relaxed')
                if not any(t.startswith('iomem=') for t in tokens):
                    tokens += [t for t in original if t.startswith('iomem=')]
                changes[key] = quoted(key, ' '.join(tokens))
    return set_lines(current, changes)


def boot_state(text):
    order = re.search(r'^BootOrder: (.*)$', text, re.M)
    entries = dict(re.findall(r'^Boot([0-9A-Fa-f]{4})\*?\s+(.*)$', text, re.M))
    nxt = re.search(r'^BootNext: (\w+)$', text, re.M)
    return {'order': order[1].upper().split(',') if order else [],
            'entries': {k.upper(): v for k, v in entries.items()},
            'next': nxt[1].upper() if nxt else None}


def without_unlock(current, original, bootid):
    # Preserve new firmware entries and user reordering; remove only our entry.
    order = [x for x in current if x != bootid]
    return order or [x for x in original if x != bootid]


def atomic_write(path, data, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode if mode is not None else ((previous.st_mode & 0o777) if previous else 0o644))
        if previous:
            os.chown(name, previous.st_uid, previous.st_gid)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_state(state):
    atomic_write(STATE, (json.dumps(state, indent=2) + '\n').encode(), 0o600)


def snapshot(paths, label):
    directory = STATE.parent / 'history' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + label)
    directory.mkdir(parents=True, mode=0o700)
    records = []
    for i, path in enumerate(dict.fromkeys(map(Path, paths))):
        entry = {'path': str(path), 'existed': path.exists()}
        if path.exists():
            dest = directory / str(i)
            shutil.copy2(path, dest)
            s = path.stat()
            entry.update(backup=str(dest), mode=s.st_mode & 0o777, uid=s.st_uid, gid=s.st_gid)
        records.append(entry)
    (directory / 'files.json').write_text(json.dumps(records, indent=2) + '\n')
    (directory / 'efibootmgr.txt').write_text(run('efibootmgr'))
    return directory, records


def rollback(records):
    for item in reversed(records):
        p = Path(item['path'])
        if item['existed']:
            atomic_write(p, Path(item['backup']).read_bytes(), item['mode'])
            os.chown(p, item['uid'], item['gid'])
        else:
            p.unlink(missing_ok=True)
    os.sync()


def validate_layout(profile):
    for key in ['esp_uuid', 'disk', 'partition', 'bdf', 'default_id', 'assets', 'efi', 'user_bin']:
        if key not in profile:
            raise ValueError('Missing profile field: ' + key)
    if not profile.get('auto_unlock', False):
        raise ValueError('This fast-boot installer requires explicit auto_unlock: true in the profile')
    if run('findmnt', '-n', '-o', 'TARGET', '--target', '/boot').strip() != '/boot':
        raise ValueError('Only a dedicated /boot ESP is supported')
    if run('findmnt', '-n', '-o', 'FSTYPE', '--target', '/boot').strip() != 'vfat':
        raise ValueError('/boot must be the FAT ESP')
    if run('findmnt', '-n', '-o', 'UUID', '--target', '/boot').strip() != profile['esp_uuid']:
        raise ValueError('ESP UUID mismatch')
    partition = run('findmnt', '-n', '-o', 'SOURCE', '--target', '/boot').strip()
    disk = '/dev/' + run('lsblk', '-n', '-o', 'PKNAME', partition).strip()
    partno = int((Path('/sys/class/block') / Path(partition).name / 'partition').read_text())
    if disk != profile['disk'] or partno != profile['partition']:
        raise ValueError('Profile disk/partition does not match mounted ESP')
    if not Path('/boot/EFI/GRUB/grubx64.efi').is_file():
        raise ValueError('Missing existing GRUB EFI loader')


def desired_files(profile, legacy=False):
    if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', profile['bdf']):
        raise ValueError('Invalid PCI BDF')
    dev = Path('/sys/bus/pci/devices') / profile['bdf']
    if dev.joinpath('vendor').read_text().strip() != '0x10de':
        raise ValueError('Selected PCI device is not NVIDIA')
    if not 0x2600 <= int(dev.joinpath('device').read_text(), 16) <= 0x28ff:
        raise ValueError('This installer currently supports Ada/ad102 assets only')
    for p in Path('/sys/firmware/efi/efivars').glob('SecureBoot-*'):
        data = p.read_bytes()
        if len(data) >= 5 and data[4]:
            raise ValueError('Unsigned local EFI: Secure Boot must be disabled')
    assets = Path(profile['assets'])
    names = [n + '_ad102.bin' for n in ('booter_load', 'gsp_boot_desc', 'gsp_boot', 'fwimage', 'fwsignature')]
    names += ['scrubber_ad10x.bin', 'v67-ad102-four-plm.bin']
    hashes = {}
    for line in (assets / 'SHA256SUMS').read_text().splitlines():
        h, name = line.split(maxsplit=1)
        hashes[name] = h
    files = {}
    for name in names:
        data = (assets / name).read_bytes()
        if digest(data) != hashes.get(name):
            raise ValueError('Asset hash mismatch: ' + name)
        files[('/boot/' if legacy else str(EFI_DIR) + '/') + name] = data
    efi = Path(profile['efi']).read_bytes()
    version = b'2.1.11+local.menu.2' if legacy else b'2.1.11+local.esp.3'
    if not efi.startswith(b'MZ') or version not in efi:
        raise ValueError('Build this fork\'s local-esp EFI before installing')
    user_bin = Path(profile['user_bin'])
    if not user_bin.is_absolute() or not user_bin.is_dir():
        raise ValueError('user_bin must be an existing absolute directory')
    data_dir = '/boot/' if legacy else str(EFI_DIR) + '/'
    files.update({
        '/boot/EFI/nvunlock/BOOTX64.EFI': efi,
        data_dir + 'chain-target.txt': b'\\EFI\\GRUB\\grubx64.efi\n',
        data_dir + 'tx-select-bdf.txt': (profile['bdf'] + '\n').encode(),
        data_dir + 'i_know_what_i_am_doing.txt': b'0\n', data_dir + 'tx-quiet.txt': b'',
        '/usr/local/libexec/reboot-with-grub-menu': (HERE / 'reboot-with-grub-menu').read_bytes(),
        '/etc/grub.d/99_local_menu_once': (HERE / '99_local_menu_once').read_bytes(),
        str(user_bin / 'reboot-with-grub-menu'): b'#!/usr/bin/bash\nexec /usr/local/libexec/reboot-with-grub-menu "$@"\n',
    })
    return files


def check_owned(state, allow_missing=False):
    for name, h in state['files'].items():
        p = Path(name)
        if allow_missing and not p.exists():
            continue
        if not p.is_file() or digest(p.read_bytes()) != h:
            raise ValueError('Managed file changed; preserved, inspect before continuing: ' + name)


def validate_entry(state, current):
    entry = current['entries'].get(state['bootid'])
    if entry is not None and (EFI_PATH.lower() not in entry.lower() or
                              state['esp_partuuid'].lower() not in entry.lower()):
        raise ValueError('Boot number now belongs to another entry; refusing to change it')


def regenerate(default_id=None):
    fd, name = tempfile.mkstemp(prefix='.grub-nvunlock-', dir=BOOTCFG.parent)
    os.close(fd)
    try:
        subprocess.run(['grub-mkconfig', '-o', name], check=True)
        subprocess.run(['grub-script-check', name], check=True)
        text = Path(name).read_text()
        if not re.search(r'^\s*linux\s+.*vmlinuz', text, re.M):
            raise ValueError('Generated GRUB has no Linux kernel entry')
        if default_id and not any(default_id in shlex.split(line) for line in text.splitlines()
                                  if line.startswith('menuentry ')):
            raise ValueError('Default GRUB ID is absent from the generated menu')
        atomic_write(BOOTCFG, Path(name).read_bytes())
    finally:
        Path(name).unlink(missing_ok=True)


def install(profile, dry_run):
    validate_layout(profile)
    files = desired_files(profile)
    existing = json.loads(STATE.read_text()) if STATE.exists() else None
    if existing and existing['phase'] == 'uninstalling':
        raise ValueError('Finish the interrupted uninstall before installing again')
    state = existing if existing and existing['phase'] == 'installed' else None
    current = boot_state(run('efibootmgr'))
    if current['next']:
        raise ValueError('A BootNext is pending; finish or cancel it before installing')
    if state:
        validate_entry(state, current)
        check_owned(state)
        if state['bootid'] not in current['entries']:
            raise ValueError('Managed firmware entry is missing')
        for name in files:
            if name not in state['files'] and Path(name).exists():
                raise ValueError('New destination already exists: ' + name)
    else:
        for name in files:
            if Path(name).exists():
                raise ValueError('Destination already exists; adopt the recorded installation first: ' + name)
        if any(EFI_PATH.lower() in line.lower() for line in current['entries'].values()):
            raise ValueError('An unrecorded unlock firmware entry exists')
    print('Install/update: local Ada EFI, seven verified assets, fast GRUB and one-time menu.')
    if dry_run:
        print('Dry run: no files or firmware variables changed.')
        return
    before = GRUB.read_text()
    configured = configure_grub(before, profile)
    obsolete = set(state['files']) - set(files) if state else set()
    migrating = str(ESP / 'chain-target.txt') in obsolete
    old_log, new_log = ESP / 'loader.log', EFI_DIR / 'loader.log'
    if migrating and old_log.exists() and new_log.exists():
        raise ValueError('Both old/new log files exist; archive the new one before migration')
    log_paths = [old_log, new_log] if migrating else []
    backup, records = snapshot([*files, *obsolete, *log_paths, GRUB, BOOTCFG, STATE], 'install')
    created = None
    try:
        # Commit assets and marker files before changing the executable that
        # reads them, especially when migrating from the old root layout.
        efi_name = str(EFI_DIR / 'BOOTX64.EFI')
        for name in sorted(files, key=lambda n: n == efi_name):
            data = files[name]
            if name == efi_name:
                os.sync()
            executable = name.endswith('reboot-with-grub-menu') or name.endswith('99_local_menu_once')
            atomic_write(Path(name), data, 0o755 if executable else 0o644)
        atomic_write(GRUB, configured.encode())
        regenerate(profile['default_id'])
        if state is None:
            run('efibootmgr', '--create-only', '--disk', profile['disk'], '--part', profile['partition'],
                '--label', 'NVIDIA Unlock', '--loader', EFI_PATH)
            after = boot_state(run('efibootmgr'))
            added = set(after['entries']) - set(current['entries'])
            if len(added) != 1:
                raise ValueError('Could not identify the new firmware entry')
            created = added.pop()
            state = {'schema': 1, 'bootid': created, 'original_order': current['order'],
                     'grub_before': before, 'initial_backup': str(backup)}
        bootid = state['bootid']
        order = [bootid] + [x for x in current['order'] if x != bootid]
        run('efibootmgr', '--bootorder', ','.join(order))
        if boot_state(run('efibootmgr'))['order'] != order:
            raise ValueError('BootOrder readback mismatch')
        state.update(phase='installed', profile=profile, grub_installed=configured,
                     files={k: digest(v) for k, v in files.items()}, last_backup=str(backup),
                     esp_partuuid=run('findmnt', '-n', '-o', 'PARTUUID', '--target', '/boot').strip())
        check_owned(state)
        if migrating and old_log.exists():
            atomic_write(new_log, old_log.read_bytes())
            if digest(new_log.read_bytes()) != digest(old_log.read_bytes()):
                raise ValueError('Log migration readback mismatch')
            old_log.unlink()
        for name in obsolete:
            Path(name).unlink()
        state['log_path'] = str(new_log)
        save_state(state)
        os.sync()
        print('Installed. No reboot requested. Backup:', backup)
    except BaseException:
        if created:
            run('efibootmgr', '--bootnum', created, '--delete-bootnum')
        run('efibootmgr', '--bootorder', ','.join(current['order']))
        rollback(records)
        raise


def uninstall(dry_run):
    if not STATE.exists():
        raise ValueError('No managed installation record')
    state = json.loads(STATE.read_text())
    if state['phase'] == 'uninstalled':
        print('Already uninstalled; historical backups retained.')
        return
    validate_layout(state['profile'])
    current = boot_state(run('efibootmgr'))
    validate_entry(state, current)
    check_owned(state, allow_missing=state['phase'] == 'uninstalling')
    order = without_unlock(current['order'], state['original_order'], state['bootid'])
    if not order:
        raise ValueError('No alternative boot order available')
    print('Uninstall: remove only recorded files/EFI entry; restore owned GRUB settings; regenerate for current kernels.')
    if dry_run:
        print('Would keep firmware boot order:', ','.join(order))
        print('Dry run: no files or firmware variables changed.')
        return
    if state['phase'] != 'uninstalling':
        log_paths = [Path(state['log_path'])] if state.get('log_path') else []
        backup, _ = snapshot([*state['files'], *log_paths, GRUB, BOOTCFG, ENV, STATE], 'uninstall')
        state.update(phase='uninstalling', uninstall_backup=str(backup))
        save_state(state)  # Journal first: an interrupted removal can be resumed.
    run('efibootmgr', '--bootorder', ','.join(order))
    if current['next'] == state['bootid']:
        run('efibootmgr', '--delete-bootnext')
    restored = restore_grub(GRUB.read_text(), state['grub_before'], state['grub_installed'])
    atomic_write(GRUB, restored.encode())
    HOOK.unlink(missing_ok=True)
    run('grub-editenv', ENV, 'unset', 'windy_show_grub_once')
    regenerate()
    # Remove our entry only after a valid normal GRUB config is available.
    if state['bootid'] in current['entries']:
        run('efibootmgr', '--bootnum', state['bootid'], '--delete-bootnum')
    for name in state['files']:
        Path(name).unlink(missing_ok=True)
    if state.get('log_path'):
        Path(state['log_path']).unlink(missing_ok=True)
    directory = EFI_DIR
    if directory.exists() and not any(directory.iterdir()):
        directory.rmdir()
    state['phase'] = 'uninstalled'
    save_state(state)
    os.sync()
    print('Uninstalled; source, cached assets, logs and backups retained. Reboot normally to stop replaying the unlock.')


def adopt(profile, receipt_path, baseline_path, dry_run):
    """Import the earlier local installation, never guess ownership by filename."""
    if STATE.exists():
        raise ValueError('A managed record already exists')
    validate_layout(profile)
    receipt = json.loads(Path(receipt_path).read_text())
    if receipt.get('version') != 'v2.1.11+local.menu.2':
        raise ValueError('Unsupported legacy receipt version')
    wanted = desired_files(profile, legacy=True)
    for rel, h in receipt['files'].items():
        name = str(Path('/boot') / rel)
        if name not in wanted or digest(Path(name).read_bytes()) != h:
            raise ValueError('Legacy file mismatch: ' + name)
    for name, data in wanted.items():
        if Path(name).read_bytes() != data:
            raise ValueError('Template/build differs from legacy installation: ' + name)
    before = Path(baseline_path).read_text()
    state = {'schema': 1, 'phase': 'installed', 'profile': profile,
             'bootid': receipt['bootid'].upper(), 'original_order': receipt['original_bootorder'].split(','),
             'grub_before': before, 'grub_installed': GRUB.read_text(),
             'esp_partuuid': run('findmnt', '-n', '-o', 'PARTUUID', '--target', '/boot').strip(),
             'files': {k: digest(v) for k, v in wanted.items()}, 'legacy_receipt': str(receipt_path)}
    current = boot_state(run('efibootmgr'))
    validate_entry(state, current)
    if state['bootid'] not in current['entries']:
        raise ValueError('Legacy firmware entry missing')
    check_owned(state)
    if dry_run:
        print('Legacy adoption checks passed; no changes made.')
        return
    backup, _ = snapshot([GRUB, BOOTCFG, ENV], 'adopt')
    state['initial_backup'] = str(backup)
    shutil.copy2(baseline_path, backup / 'grub-before-original-install')
    shutil.copy2(receipt_path, backup / 'legacy-receipt.json')
    save_state(state)
    print('Existing installation adopted without changing boot files or firmware variables.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'install', 'uninstall', 'adopt'])
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--grub-before', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run through sudo/pkexec, including status (the installation record is root-owned)')
    if args.command == 'status':
        if not STATE.exists():
            print('Not managed by this installer.')
            return
        state = json.loads(STATE.read_text())
        print('State:', state['phase'])
        if state['phase'] == 'installed':
            validate_layout(state['profile'])
            check_owned(state)
            current = boot_state(run('efibootmgr'))
            validate_entry(state, current)
            if state['bootid'] not in current['entries']:
                raise ValueError('Managed EFI entry is absent')
            print('Managed files verified. EFI entry:', state['bootid'])
            print('BootOrder:', ','.join(current['order']))
            print('This verifies installation, not live GPU unlock; use tools/diag-unlock.py for that.')
    elif args.command == 'uninstall':
        uninstall(args.dry_run)
    else:
        if not args.profile:
            parser.error('--profile is required')
        profile = json.loads(args.profile.read_text())
        if args.command == 'install':
            install(profile, args.dry_run)
        else:
            if not args.receipt or not args.grub_before:
                parser.error('adopt requires --receipt and --grub-before')
            adopt(profile, args.receipt, args.grub_before, args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print('ERROR:', e, file=sys.stderr)
        sys.exit(1)
