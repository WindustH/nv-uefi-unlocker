//! Keep local ESP assets/config separate from OS boot files. USB builds
//! retain the upstream volume-root layout unless local-esp is enabled.
extern crate alloc;
use alloc::{format, string::String};

pub fn resolve(path: &str, local_esp: bool) -> String {
    if local_esp {
        format!("\\EFI\\nvunlock\\{}", path.trim_start_matches('\\'))
    } else {
        String::from(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn isolates_assets_and_every_config_marker() {
        for name in ["booter_load_ad102.bin", "v67-ad102-four-plm.bin", "chain-target.txt",
                     "tx-select-bdf.txt", "i_know_what_i_am_doing.txt", "tx-quiet.txt"] {
            assert_eq!(resolve(&format!("\\{name}"), true), format!("\\EFI\\nvunlock\\{name}"));
        }
    }

    #[test]
    fn usb_paths_are_unchanged() {
        assert_eq!(resolve("\\fwimage_ad102.bin", false), "\\fwimage_ad102.bin");
        assert_eq!(resolve("\\usbroot\\fwimage_ad102.bin", false), "\\usbroot\\fwimage_ad102.bin");
    }
}
