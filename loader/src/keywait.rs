//! Single-key confirmation with timeout (TOTORONG interaction pattern).

use core::{fmt::Write, time::Duration};
use log::info;
use uefi::proto::console::text::{Color, Key};
use uefi::{boot, system};

pub enum KeyOrTimeout {
    Key(Key),
    Timeout,
}

/// Outcome of the CONFIRM phase.
pub enum Confirm {
    /// Run the transaction (TX builds) / continue the read-only phases.
    Proceed,
    /// Abort into chain-load: no GPU writes this boot. (TX builds only -
    /// read-only builds never abort.)
    #[allow(dead_code)]
    Abort,
}

/// One console-input poll. Public so the armed-prep loop (tx_prep) can
/// interleave read-only work with the abort window.
pub fn poll_key() -> Option<Key> {
    let key = system::with_stdin(|stdin| stdin.read_key());
    key.ok().flatten()
}

/// Poll the console input roughly every 50 ms.
///
/// Fallback-picker semantics (chainload.rs): any key proceeds early,
/// timeout proceeds anyway.
pub fn wait_any_key_or_timeout(seconds: u64) -> KeyOrTimeout {
    let ticks = seconds * 20; // 50 ms each
    for _ in 0..ticks {
        if let Some(key) = poll_key() {
            return KeyOrTimeout::Key(key);
        }
        boot::stall(Duration::from_millis(50));
    }
    KeyOrTimeout::Timeout
}

/// CONFIRM-phase wait (issue #4.1). TX builds: ONLY Space or Enter proceeds
/// (Enter after Space-muscle-memory from shell/BIOS menus; before it aborted
/// and the run looked "finished" while nothing happened - easy to misread
/// as a failed attempt); any other key or the 30s timeout aborts into
/// chain-load with zero GPU writes and says so explicitly, so a stick left
/// in a machine that happens to boot from it can never run the transaction
/// unattended. Read-only builds keep the M0 semantics (any key / timeout
/// proceeds; everything is zero-write by design).
pub fn wait_confirm(seconds: u64) -> Confirm {
    let ticks = seconds * 20;
    for _ in 0..ticks {
        if let Some(key) = poll_key() {
            #[cfg(feature = "tx")]
            match key {
                Key::Printable(c) if u16::from(c) == 0x20 => {
                    info!("CONFIRM: Space pressed, proceeding");
                    return Confirm::Proceed;
                }
                Key::Printable(c) if u16::from(c) == 0x0d => {
                    info!("CONFIRM: Enter pressed, proceeding");
                    return Confirm::Proceed;
                }
                other => {
                    info!(
                        "CONFIRM: {other:?} - ABORT to Windows, NOTHING was unlocked this boot (zero GPU writes)"
                    );
                    return Confirm::Abort;
                }
            }
            #[cfg(not(feature = "tx"))]
            {
                info!("CONFIRM: key {key:?} pressed, proceeding (read-only build)");
                return Confirm::Proceed;
            }
        }
        boot::stall(Duration::from_millis(50));
    }
    #[cfg(feature = "tx")]
    {
        info!("CONFIRM: {seconds}s timeout - ABORT to Windows, NOTHING was unlocked this boot (zero GPU writes)");
        Confirm::Abort
    }
    #[cfg(not(feature = "tx"))]
    {
        info!("CONFIRM: timeout, proceeding (read-only default)");
        Confirm::Proceed
    }
}

/// Armed CONFIRM (`\i_know_what_i_am_doing.txt` on the stick root): the
/// stick itself carries the operator's declaration of intent, so the wait
/// collapses to a short countdown that PROCEEDS on timeout; any key during
/// the window is a human saying stop - TX builds abort into chain-load
/// with zero GPU writes, read-only builds have nothing to abort (they
/// proceed; everything is zero-write by design). TX builds route the armed
/// wait through tx_prep (prep interleaved) and only reach this fn in
/// non-tx builds.
#[cfg_attr(feature = "tx", allow(dead_code))]
pub fn wait_confirm_armed(seconds: u64) -> Confirm {
    let ticks = seconds * 20;
    for _ in 0..ticks {
        if let Some(key) = poll_key() {
            #[cfg(feature = "tx")]
            {
                info!(
                    "CONFIRM: key {key:?} during the {seconds}s armed countdown - ABORT (no GPU writes this boot)"
                );
                return Confirm::Abort;
            }
            #[cfg(not(feature = "tx"))]
            {
                info!("CONFIRM: key {key:?} during the armed countdown, proceeding (read-only build)");
                return Confirm::Proceed;
            }
        }
        boot::stall(Duration::from_millis(50));
    }
    info!("CONFIRM: {seconds}s armed countdown elapsed - PROCEED (marker file opted in)");
    Confirm::Proceed
}

/// The explicit one-time menu request takes precedence over normal auto-run.
/// No prep/GPU writes occur while this question is shown; Escape continues
/// to GRUB through the existing Abort path, not an extra reboot.
pub fn wait_confirm_menu() -> Confirm {
    use crate::menu_once::{self, Decision};

    let (column, row, cursor_was_visible) = system::with_stdout(|out| {
        let (cols, rows) = out.current_mode().ok().flatten()
            .map(|m| (m.columns(), m.rows())).unwrap_or((80, 25));
        let column = cols.saturating_sub(58) / 2;
        let row = rows.saturating_sub(9) / 2;
        let visible = out.cursor_visible();
        let _ = out.set_color(Color::LightGray, Color::Black);
        let _ = out.clear();
        let _ = out.enable_cursor(false);
        let lines = [
            "+--------------------------------------------------------+",
            "|                 NVIDIA GPU UNLOCK                      |",
            "|                                                        |",
            "|  Unlock the GPU before opening the GRUB menu?           |",
            "|                                                        |",
            "|  ENTER  Unlock now       ESC  Skip unlock this boot     |",
            "|                                                        |",
            "|  Default: unlock                                       |",
            "+--------------------------------------------------------+",
        ];
        let _ = out.set_color(Color::LightCyan, Color::Black);
        for (offset, line) in lines.iter().enumerate() {
            let _ = out.set_cursor_position(column, row + offset);
            let _ = write!(out, "{line}");
        }
        (column, row, visible)
    });
    // Start the five seconds after drawing, even on a slow firmware console.
    let t0 = crate::timing::ms_since_entry();
    let mut fallback_ms = 0u64;
    let mut shown_seconds = u64::MAX;
    let (decision, enter_pressed) = loop {
        let elapsed = match (t0, crate::timing::ms_since_entry()) {
            (Some(start), Some(now)) => now.saturating_sub(start),
            _ => fallback_ms,
        };
        let (scan, unicode) = match poll_key() {
            Some(Key::Printable(c)) => (0, u16::from(c)),
            Some(Key::Special(code)) => (code.0, 0),
            None => (0, 0),
        };
        let decision = menu_once::decide(scan, unicode, elapsed);
        if decision != Decision::Wait {
            break (decision, scan == 0 && matches!(unicode, 0x0d | 0x0a));
        }
        let remaining = menu_once::WAIT_MS.saturating_sub(elapsed).div_ceil(1000);
        if remaining != shown_seconds {
            system::with_stdout(|out| {
                let _ = out.set_cursor_position(column + 3, row + 7);
                let _ = out.set_color(Color::White, Color::Black);
                let _ = write!(out, "Default: unlock in {remaining}s                     ");
            });
            shown_seconds = remaining;
        }
        boot::stall(Duration::from_millis(50));
        fallback_ms += 50;
    };
    system::with_stdout(|out| {
        let _ = out.set_color(Color::LightGray, Color::Black);
        let _ = out.clear();
        let _ = out.enable_cursor(cursor_was_visible);
    });
    match decision {
        Decision::Skip => {
            info!("CONFIRM: Esc - SKIP unlock this boot; continuing to GRUB (no GPU writes)");
            Confirm::Abort
        }
        Decision::Unlock => {
            let reason = if enter_pressed { "Enter" } else { "5s timeout (default)" };
            info!("CONFIRM: {reason} - PROCEED with unlock, then GRUB");
            Confirm::Proceed
        }
        Decision::Wait => unreachable!(),
    }
}
