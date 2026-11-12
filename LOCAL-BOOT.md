# Local Linux boot integration (WindustH fork)

This branch is based on upstream **v2.1.11**, commit
`a06aecf1bfdf9ded45e6c3e0658c6e4d1daa6bc3`. It adds opt-in local ESP
installation, automatic zero-countdown unlock, and a one-time unlock question
before GRUB. GPU transaction, firmware payload and readback logic are unchanged.

## Behavior

- Normal boot: run the unlock with a `0` countdown marker, then chainload GRUB.
  GRUB starts the configured Linux entry with a hidden, zero-timeout menu.
- `reboot-with-grub-menu`: reboot once to a five-second unlock question.
  Enter confirms immediately, Escape skips **this boot's unlock transaction**,
  and timeout confirms. Other keys do not cancel or reset the timer.
  Both choices continue to GRUB, which waits for the user's OS selection.
- GRUB consumes `windy_show_grub_once` from its environment block, restoring
  fast boot on subsequent boots. The legacy key name is retained for compatibility.
- `--prepare-only`, `--cancel`, and `--status` manage the one-time request without
  requesting a reboot. A rejected reboot restores the previous request state.
- `tools/diag-unlock.py` maps BAR0 a page at a time and opens read-only unless
  `--reapply` is explicitly supplied.

This is a decision to execute the unlock, not proof of its resulting state.
Verify in the running OS with the diagnostic script after reboot. Escape does
not actively relock registers. Sleep or power events may erase volatile state.
The firmware's display routing still determines which screen shows the prompt.

## Supported local installation

`tools/linux-local/manage.py` intentionally supports a narrow layout:

- Linux with a dedicated FAT ESP mounted at `/boot`;
- existing GRUB at `/boot/EFI/GRUB/grubx64.efi`, with `/boot/grub/grub.cfg`;
- Ada GPU and locally generated `ad102` assets;
- Secure Boot disabled for this unsigned local build;
- `efibootmgr`, `findmnt`, `lsblk`, `grub-mkconfig`, `grub-script-check`,
  `grub-editenv`, Python 3, Bash, sudo and systemd available.

The original upstream USB deployment remains available. Only the `local-esp`
build feature changes storage paths; `cargo efi-tx` retains the USB-root layout.
Read upstream `AGENTS.deploy.md` and compatibility documentation for GPU support
and locally generated firmware prerequisites. This installer does not generate
or distribute NVIDIA firmware or change GPU transaction safety checks.

## Build and install

```sh
rustup target add x86_64-unknown-uefi
cargo xt --locked
python3 -m unittest discover -s tools/linux-local -p 'test_*.py' -v
cargo efi-local --locked
python3 check_efi.py target/x86_64-unknown-uefi/release/loader.efi
```

Copy `tools/linux-local/profile.example.json` outside the repository and fill
in the actual ESP UUID, whole disk, partition number, GPU BDF, existing Linux
menuentry ID, asset directory, EFI build path and existing user bin directory.
The `auto_unlock: true` setting is an explicit opt-in to automatic GPU writes
on each boot. `iomem_relaxed` is optional and defaults to false: it relaxes
kernel MMIO restrictions for diagnostics, and does not preserve unlock state.

Generate the seven Ada asset files from the upstream pinned firmware inputs,
then put their verified SHA-256 checksums in `SHA256SUMS` alongside them
(`HASH  filename`, relative basenames). Do not copy arbitrary firmware versions.

```sh
sudo python3 tools/linux-local/manage.py install --profile /path/profile.json --dry-run
sudo python3 tools/linux-local/manage.py install --profile /path/profile.json
sudo python3 tools/linux-local/manage.py status
```

Install updates an existing managed installation while retaining its original
uninstall baseline. It refuses conflicting unmanaged destinations or modified
managed files. It validates the mounted ESP against the disk and partition,
asset checksums, the selected GPU family, and the generated GRUB default ID.
It never formats a volume or requests a reboot.

## Installed layout

```text
/boot/EFI/nvunlock/
  BOOTX64.EFI
  booter_load_ad102.bin
  gsp_boot_desc_ad102.bin
  gsp_boot_ad102.bin
  fwimage_ad102.bin
  fwsignature_ad102.bin
  scrubber_ad10x.bin
  v67-ad102-four-plm.bin
  chain-target.txt
  tx-select-bdf.txt
  i_know_what_i_am_doing.txt
  tx-quiet.txt
  loader.log                 # produced on the next boot
```

All optional unlocker marker files also belong in that directory for the
`local-esp` build; optional ACPI dumps go in its `acpi/` subdirectory. The GRUB
environment and OS EFI paths remain at their real locations on the ESP.
The local build does not fall back to stale root or `usbroot/` asset files.

Outside this directory, installation manages four GRUB defaults plus the
optional diagnostic kernel parameter, `/etc/grub.d/99_local_menu_once`,
`/usr/local/libexec/reboot-with-grub-menu`, the user-bin command, a firmware
boot entry, and its position in BootOrder. It regenerates GRUB from the current
installed kernels. The root-owned ownership record and transaction backups are
under `/var/lib/nv-uefi-unlocker/`.

## Uninstall and recovery

```sh
sudo python3 tools/linux-local/manage.py uninstall --dry-run
sudo python3 tools/linux-local/manage.py uninstall
```

Uninstall verifies ownership before changing anything. It removes the recorded
EFI entry and managed files, restores the GRUB settings it owns, preserves
unrelated later edits and firmware entries, clears the one-time menu request,
and regenerates GRUB for the **currently installed** kernels. It never restores
an obsolete generated menu containing removed kernels. The last loader log is
archived in the uninstall backup. Source, asset caches and historical backups
remain available for reinstall. Optional diagnostic output directories are
retained if they contain additional files.

Uninstall does not relock a currently running GPU; subsequent normal boot stops
replaying the unlock. If removal is interrupted, rerun `uninstall`: its journal
supports resuming and the boot order already prefers the ordinary OS loaders.
Failed installs restore their file snapshots and original boot order. If a
managed file was independently changed, inspect it before retrying; the tool
refuses to overwrite or delete the modified file automatically.

The original GRUB firmware entry remains available through the firmware boot
menu. Backups contain a file map and `efibootmgr` output. Retain the ownership
record until uninstall is complete; do not remove `/var/lib/nv-uefi-unlocker`
while the installation is active.

The `adopt` command imports the earlier `v2.1.11+local.menu.2` local deployment
only when its receipt, installed bytes and original GRUB backup match. This is
a migration aid, not a general-purpose claim of ownership over existing files.
After adoption, installing the `local-esp` build migrates root-level unlocker
assets, markers and the previous log into the dedicated directory transactionally.

## Tests and scope of evidence

Host tests cover key handling, timeout boundaries, file path selection,
configuration merging, install/update/uninstall/reinstall, changed-file guards,
dry runs, rollback, interrupted-removal recovery and root-layout migration.
They use temporary files and simulated firmware variables, not the live ESP.
Passing them and building the EFI do not establish hardware unlock success;
that requires a fresh boot log and in-OS register readback.

Firmware, generated payloads, credentials, machine profiles, and private
diagnostic logs are not part of this fork's commits. Keep upstream as a separate
remote and rebase/cherry-pick local changes deliberately when updating releases.
