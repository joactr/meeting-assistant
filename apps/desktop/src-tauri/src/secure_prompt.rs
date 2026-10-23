//! Credential entry uses an OS-native secure prompt, never React state.
use anyhow::Result;
use zeroize::Zeroizing;
#[cfg(windows)]
pub fn prompt(label: &str) -> Result<Option<Zeroizing<String>>> {
    use windows_sys::Win32::Security::Credentials::*;
    let caption: Vec<u16> = "Meeting Assistant credential"
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let message: Vec<u16> = label.encode_utf16().chain(Some(0)).collect();
    let target: Vec<u16> = "Meeting Assistant".encode_utf16().chain(Some(0)).collect();
    let mut user = [0u16; 514];
    user[0] = 'x' as u16;
    let mut password = Zeroizing::new(vec![0u16; 4096]);
    let info = CREDUI_INFOW {
        cbSize: std::mem::size_of::<CREDUI_INFOW>() as u32,
        hwndParent: std::ptr::null_mut(),
        pszMessageText: message.as_ptr(),
        pszCaptionText: caption.as_ptr(),
        hbmBanner: std::ptr::null_mut(),
    };
    let mut save = 0;
    let result = unsafe {
        CredUIPromptForCredentialsW(
            &info,
            target.as_ptr(),
            std::ptr::null(),
            0,
            user.as_mut_ptr(),
            user.len() as u32,
            password.as_mut_ptr(),
            password.len() as u32,
            &mut save,
            CREDUI_FLAGS_GENERIC_CREDENTIALS
                | CREDUI_FLAGS_ALWAYS_SHOW_UI
                | CREDUI_FLAGS_DO_NOT_PERSIST,
        )
    };
    if result == 1223 {
        return Ok(None);
    }
    anyhow::ensure!(result == 0, "Windows credential prompt failed ({result})");
    let len = password
        .iter()
        .position(|x| *x == 0)
        .unwrap_or(password.len());
    Ok(Some(Zeroizing::new(String::from_utf16(&password[..len])?)))
}
#[cfg(target_os = "macos")]
pub fn prompt(label: &str) -> Result<Option<Zeroizing<String>>> {
    anyhow::ensure!(label.len() <= 256, "credential label too long");
    let output=std::process::Command::new("/usr/bin/osascript").args(["-e","on run argv\nset r to display dialog (item 1 of argv) default answer \"\" with hidden answer with title \"Meeting Assistant credential\"\nreturn text returned of r\nend run",label]).output()?;
    if !output.status.success() {
        return Ok(None);
    }
    Ok(Some(Zeroizing::new(
        String::from_utf8(output.stdout)?
            .trim_end_matches(['\r', '\n'])
            .to_owned(),
    )))
}
#[cfg(target_os = "linux")]
pub fn prompt(label: &str) -> Result<Option<Zeroizing<String>>> {
    let output = std::process::Command::new("/usr/bin/zenity")
        .args([
            "--password",
            "--title=Meeting Assistant credential",
            &format!("--text={label}"),
        ])
        .output()?;
    if !output.status.success() {
        return Ok(None);
    }
    Ok(Some(Zeroizing::new(
        String::from_utf8(output.stdout)?
            .trim_end_matches(['\r', '\n'])
            .to_owned(),
    )))
}
