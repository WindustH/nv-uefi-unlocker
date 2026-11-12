"""No real EFI/NVRAM mutations: lifecycle tests use temporary files and a fake firmware."""
import importlib.util
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('manage', Path(__file__).with_name('manage.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class GrubTests(unittest.TestCase):
    def test_owned_changes_restore_without_overwriting_later_user_settings(self):
        before = 'GRUB_THEME="theme"\nGRUB_CMDLINE_LINUX_DEFAULT="quiet iomem=strict"\n'
        installed = m.configure_grub(before, {'default_id': 'linux-id', 'iomem_relaxed': True})
        self.assertIn('iomem=relaxed', installed)
        changed = installed.replace('quiet iomem=relaxed', 'quiet iomem=relaxed debug')
        changed += 'GRUB_GFXMODE=auto\n'
        restored = m.restore_grub(changed, before, installed)
        self.assertEqual(m.value(m.assignments(restored)[m.PARAM]), 'quiet debug iomem=strict')
        self.assertIn('GRUB_GFXMODE=auto', restored)
        self.assertNotIn('GRUB_TIMEOUT=', restored)

    def test_later_user_default_is_kept(self):
        before = 'GRUB_DEFAULT=0\n'
        installed = m.configure_grub(before, {'default_id': 'linux-id'})
        changed = m.set_lines(installed, {'GRUB_DEFAULT': 'GRUB_DEFAULT=windows-id'})
        self.assertIn('GRUB_DEFAULT=windows-id', m.restore_grub(changed, before, installed))

    def test_existing_relaxed_parameter_is_retained(self):
        before = 'GRUB_CMDLINE_LINUX_DEFAULT="quiet iomem=relaxed"\n'
        installed = m.configure_grub(before, {'default_id': 'linux-id', 'iomem_relaxed': True})
        self.assertEqual(m.restore_grub(installed, before, installed), before)

    def test_duplicate_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            m.configure_grub('GRUB_DEFAULT=0\nGRUB_DEFAULT=1\n', {'default_id': 'x'})

    def test_uninstall_preserves_new_entries_and_user_boot_order(self):
        self.assertEqual(m.without_unlock(['0003', '0000', '0009', '0004'], ['0004', '0003'], '0000'),
                         ['0003', '0009', '0004'])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {name: self.root / path for name, path in {
            'STATE': 'state/installation.json', 'ESP': 'boot', 'GRUB': 'etc/default/grub',
            'BOOTCFG': 'boot/grub/grub.cfg', 'ENV': 'boot/grub/grubenv',
            'HOOK': 'etc/grub.d/99_local_menu_once', 'EFI_DIR': 'boot/EFI/nvunlock',
        }.items()}
        for name, p in self.paths.items():
            p.parent.mkdir(parents=True, exist_ok=True)
        self.patchers = [patch.object(m, name, p) for name, p in self.paths.items()]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        m.GRUB.write_text('GRUB_THEME="my-theme"\nGRUB_CMDLINE_LINUX_DEFAULT="quiet"\n')
        m.BOOTCFG.write_text('old config with a now-removed zen kernel\n')
        m.ENV.write_text('# GRUB Environment Block\n')
        self.before = m.GRUB.read_text()
        self.profile = {'default_id': 'linux-id', 'iomem_relaxed': True,
                        'disk': '/dev/fake', 'partition': 1}
        self.files = {str(m.EFI_DIR / 'BOOTX64.EFI'): b'fake EFI',
                      str(m.EFI_DIR / 'fwimage_ad102.bin'): b'fake firmware',
                      str(m.HOOK): b'fake hook', str(self.root / 'bin/menu'): b'fake helper'}
        self.entries = {'0004': 'GRUB', '0003': 'Windows'}
        self.order = ['0004', '0003']
        self.next = None
        self.fail_generation = False
        for patcher in [patch.object(m, 'run', self.fake_run),
                        patch.object(m, 'desired_files', lambda profile, **kwargs: self.files),
                        patch.object(m, 'validate_layout', lambda profile: None),
                        patch.object(m.os, 'sync', lambda: None),
                        patch.object(m.subprocess, 'run', self.command)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_run(self, *args):
        args = tuple(map(str, args))
        if args == ('efibootmgr',):
            return ('BootOrder: ' + ','.join(self.order) + '\n' +
                    (f'BootNext: {self.next}\n' if self.next else '') +
                    ''.join(f'Boot{k}* {v}\n' for k, v in self.entries.items()))
        if args[0] == 'findmnt':
            return 'part-id\n'
        if args[0] == 'grub-editenv':
            m.ENV.write_text('# GRUB Environment Block\n')
            return ''
        self.assertEqual(args[0], 'efibootmgr')
        if '--create-only' in args:
            self.entries['0000'] = 'NVIDIA Unlock HD(part-id)/' + m.EFI_PATH
        elif '--bootorder' in args:
            self.order = args[-1].split(',')
        elif '--delete-bootnum' in args:
            self.entries.pop(args[args.index('--bootnum') + 1])
        elif '--delete-bootnext' in args:
            self.next = None
        else:
            self.fail('Unexpected command: ' + repr(args))
        return ''

    def command(self, args, **kwargs):
        if args[0] == 'grub-mkconfig':
            if self.fail_generation:
                raise subprocess.CalledProcessError(1, args)
            Path(args[-1]).write_text("menuentry 'Linux' --id linux-id {\n linux /vmlinuz-current\n}\n")
        else:
            self.assertEqual(args[0], 'grub-script-check')
        return subprocess.CompletedProcess(args, 0)

    def test_install_update_uninstall_roundtrip(self):
        m.install(self.profile, False)
        self.assertEqual(self.order[0], '0000')
        self.files[str(m.EFI_DIR / 'BOOTX64.EFI')] = b'updated EFI'
        m.install(self.profile, False)
        self.assertEqual(len(self.entries), 3)
        m.uninstall(False)
        self.assertEqual(m.GRUB.read_text(), self.before)
        self.assertEqual(self.order, ['0004', '0003'])
        self.assertNotIn('0000', self.entries)
        self.assertNotIn('zen', m.BOOTCFG.read_text())
        self.assertFalse(any(Path(p).exists() for p in self.files))
        self.assertEqual(json.loads(m.STATE.read_text())['phase'], 'uninstalled')
        m.uninstall(False)
        m.install(self.profile, False)
        self.assertEqual(self.order[0], '0000')

    def test_failed_install_restores_files_and_boot_order(self):
        self.fail_generation = True
        with self.assertRaises(subprocess.CalledProcessError):
            m.install(self.profile, False)
        self.assertEqual(m.GRUB.read_text(), self.before)
        self.assertEqual(self.order, ['0004', '0003'])
        self.assertFalse(any(Path(p).exists() for p in self.files))
        self.assertFalse(m.STATE.exists())

    def test_changed_managed_file_blocks_uninstall_before_mutations(self):
        m.install(self.profile, False)
        (m.EFI_DIR / 'BOOTX64.EFI').write_bytes(b'user replacement')
        with self.assertRaises(ValueError):
            m.uninstall(False)
        self.assertEqual(self.order[0], '0000')
        self.assertEqual(json.loads(m.STATE.read_text())['phase'], 'installed')

    def test_interrupted_uninstall_can_resume(self):
        m.install(self.profile, False)
        self.fail_generation = True
        with self.assertRaises(subprocess.CalledProcessError):
            m.uninstall(False)
        self.assertEqual(self.order, ['0004', '0003'])
        self.assertEqual(json.loads(m.STATE.read_text())['phase'], 'uninstalling')
        self.fail_generation = False
        m.uninstall(False)
        self.assertEqual(json.loads(m.STATE.read_text())['phase'], 'uninstalled')

    def test_dry_runs_do_not_change_files_or_variables(self):
        m.install(self.profile, True)
        self.assertFalse(m.STATE.exists())
        self.assertEqual(self.order, ['0004', '0003'])
        m.install(self.profile, False)
        prior = m.STATE.read_bytes(), m.GRUB.read_bytes(), list(self.order)
        m.uninstall(True)
        self.assertEqual(prior, (m.STATE.read_bytes(), m.GRUB.read_bytes(), self.order))

    def test_reused_boot_number_is_never_deleted(self):
        m.install(self.profile, False)
        self.entries['0000'] = 'Some other OS'
        with self.assertRaises(ValueError):
            m.uninstall(False)
        self.assertIn('0000', self.entries)

    def test_root_layout_migration_moves_assets_and_log(self):
        self.files[str(m.EFI_DIR / 'chain-target.txt')] = b'GRUB path'
        m.install(self.profile, False)
        state = json.loads(m.STATE.read_text())
        for name in ['chain-target.txt', 'fwimage_ad102.bin']:
            src, dst = m.EFI_DIR / name, m.ESP / name
            src.rename(dst)
            state['files'][str(dst)] = state['files'].pop(str(src))
        (m.ESP / 'loader.log').write_text('last boot evidence')
        m.save_state(state)
        m.install(self.profile, False)
        self.assertFalse((m.ESP / 'chain-target.txt').exists())
        self.assertFalse((m.ESP / 'fwimage_ad102.bin').exists())
        self.assertFalse((m.ESP / 'loader.log').exists())
        self.assertEqual((m.EFI_DIR / 'loader.log').read_text(), 'last boot evidence')
        m.uninstall(False)
        self.assertFalse(m.EFI_DIR.exists())


if __name__ == '__main__':
    unittest.main()
