//! Local one-time unlock question, requested by reboot-with-grub-menu.
//! Shared with the host tests so input/deadline behavior is tested directly.

pub const WAIT_MS: u64 = 5_000;

pub fn requested(env: &[u8]) -> bool {
    env.starts_with(b"# GRUB Environment Block\n")
        && env.split(|&b| b == b'\n').any(|line| line == b"windy_show_grub_once=1")
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision {
    Wait,
    Unlock,
    Skip,
}

/// UEFI scan code 0x17 is Escape; some consoles return ASCII Escape.
/// Read the key before evaluating the deadline, so Escape wins at 5s.
pub fn decide(scan_code: u16, unicode: u16, elapsed_ms: u64) -> Decision {
    if scan_code == 0x17 || (scan_code == 0 && unicode == 0x1b) {
        Decision::Skip
    } else if (scan_code == 0 && matches!(unicode, 0x0d | 0x0a)) || elapsed_ms >= WAIT_MS {
        Decision::Unlock
    } else {
        Decision::Wait
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_an_exact_pending_grub_request_enables_the_question() {
        assert!(requested(b"# GRUB Environment Block\nnext_entry=\nwindy_show_grub_once=1\n####"));
        for env in [
            b"# GRUB Environment Block\nnext_entry=\n####".as_slice(),
            b"# GRUB Environment Block\nwindy_show_grub_once=\n####",
            b"# GRUB Environment Block\nwindy_show_grub_once=0\n####",
            b"# GRUB Environment Block\nwindy_show_grub_once=10\n####",
            b"# GRUB Environment Block\nother_windy_show_grub_once=1\n####",
            b"# GRUB Environment Block\n# windy_show_grub_once=1\n####",
            b"windy_show_grub_once=1\n",
            b"",
        ] {
            assert!(!requested(env));
        }
    }

    #[test]
    fn enter_confirms_immediately() {
        assert_eq!(decide(0, 0x0d, 0), Decision::Unlock);
        assert_eq!(decide(0, 0x0a, 1234), Decision::Unlock);
    }

    #[test]
    fn escape_skips_even_at_the_deadline() {
        for elapsed in [0, 1234, WAIT_MS, WAIT_MS + 50] {
            assert_eq!(decide(0x17, 0, elapsed), Decision::Skip);
            assert_eq!(decide(0, 0x1b, elapsed), Decision::Skip);
        }
    }

    #[test]
    fn no_input_confirms_only_after_five_seconds() {
        assert_eq!(decide(0, 0, 0), Decision::Wait);
        assert_eq!(decide(0, 0, WAIT_MS - 1), Decision::Wait);
        assert_eq!(decide(0, 0, WAIT_MS), Decision::Unlock);
    }

    #[test]
    fn unrelated_keys_do_not_cancel_or_reset_the_timeout() {
        for (scan, unicode) in [(0, 0x20), (0, b'x' as u16), (1, 0), (0x0b, 0)] {
            assert_eq!(decide(scan, unicode, 1000), Decision::Wait);
            assert_eq!(decide(scan, unicode, WAIT_MS), Decision::Unlock);
        }
    }
}
