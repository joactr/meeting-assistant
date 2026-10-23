//! Native-only edges. Domain state and SQL belong exclusively to the engine.
pub mod acceptance;
pub mod backup;
pub mod broker;
pub mod calibration;
pub mod capture;
#[path = "../../../engine/contracts/contracts.rs"]
pub mod contracts;
pub mod decode;
pub mod export;
pub mod grants;
pub mod oauth;
pub mod preferences;
pub mod supervisor;
pub mod vault;
use anyhow::{ensure, Result};
pub use contracts::{ApplicationCommand, ErrorEnvelope as NativeError};
use serde_json::Value;
use std::sync::Arc;
impl From<anyhow::Error> for NativeError {
    fn from(e: anyhow::Error) -> Self {
        if let Some(error) = e.downcast_ref::<NativeError>() {
            return error.clone();
        }
        Self {
            code: "native_error".into(),
            message: e.to_string(),
            retryable: false,
            details: Value::Null,
            trace_id: uuid::Uuid::new_v4().to_string(),
        }
    }
}
impl std::fmt::Display for NativeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}
impl std::error::Error for NativeError {}
pub type Events = Arc<dyn Fn(&str, Value) + Send + Sync>;
pub fn operation_allowed(operation: &str) -> bool {
    contracts::OPERATION_IDS.binary_search(&operation).is_ok()
}
pub fn validate_external_https(raw: &str) -> Result<url::Url> {
    let url = url::Url::parse(raw)?;
    ensure!(
        url.scheme() == "https"
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none(),
        "only credential-free HTTPS links may open"
    );
    Ok(url)
}
pub fn private_directory(path: &std::path::Path) -> Result<()> {
    std::fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(windows)]
    {
        private_windows_acl(path)?;
    }
    Ok(())
}
pub fn backend(resource_dir: &std::path::Path) -> Arc<dyn audio_core::CaptureBackend> {
    #[cfg(windows)]
    {
        let _ = resource_dir;
        Arc::new(audio_wasapi::WasapiBackend)
    }
    #[cfg(target_os = "macos")]
    {
        Arc::new(audio_macos::MacBackend::new(
            resource_dir.join("bin/meeting-audio-macos"),
        ))
    }
    #[cfg(target_os = "linux")]
    {
        Arc::new(audio_pipewire::PipeWireBackend::new(
            resource_dir.join("bin/meeting-audio-pipewire"),
        ))
    }
}
#[cfg(windows)]
fn private_windows_acl(path: &std::path::Path) -> Result<()> {
    use core::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;
    #[link(name = "advapi32")]
    unsafe extern "system" {
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
            text: *const u16,
            revision: u32,
            descriptor: *mut *mut c_void,
            size: *mut u32,
        ) -> i32;
        fn SetFileSecurityW(path: *const u16, information: u32, descriptor: *mut c_void) -> i32;
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn LocalFree(memory: *mut c_void) -> *mut c_void;
    }
    let text: Vec<u16> = "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let path: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut descriptor = std::ptr::null_mut();
    unsafe {
        ensure!(
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                text.as_ptr(),
                1,
                &mut descriptor,
                std::ptr::null_mut()
            ) != 0,
            "creating private workspace ACL failed"
        );
        let result = SetFileSecurityW(path.as_ptr(), 0x80000004, descriptor);
        LocalFree(descriptor);
        ensure!(
            result != 0,
            "applying private owner-only workspace ACL failed"
        );
    }
    Ok(())
}
