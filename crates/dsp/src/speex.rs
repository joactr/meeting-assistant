//! Audited SpeexDSP dynamic ABI. The caller supplies a bundled absolute library path.
use super::*;
use libloading::Library;
use std::ffi::c_void;
type Init = unsafe extern "C" fn(i32, i32) -> *mut c_void;
type Destroy = unsafe extern "C" fn(*mut c_void);
type Ctl = unsafe extern "C" fn(*mut c_void, i32, *mut c_void) -> i32;
type Run = unsafe extern "C" fn(*mut c_void, *mut i16) -> i32;
type Echo = unsafe extern "C" fn(*mut c_void, *const i16, *const i16, *mut i16);
pub struct Speex {
    _library: Library,
    pre: *mut c_void,
    echo: *mut c_void,
    destroy: Destroy,
    echo_destroy: Destroy,
    run: Run,
    cancel: Echo,
    pub calibration: Option<Calibration>,
    mic: [i16; 480],
    reference: [i16; 480],
    output: [i16; 480],
    denoise: bool,
}
// A Speex instance is owned by exactly one DSP thread, never shared.
unsafe impl Send for Speex {}
impl Speex {
    pub fn new(
        path: &std::path::Path,
        denoise: bool,
        calibration: Option<Calibration>,
    ) -> Result<Self> {
        ensure!(
            path.is_absolute(),
            "DSP library must be bundled absolute path"
        );
        unsafe {
            let lib = Library::new(path)?;
            let init: Init = *lib.get(b"speex_preprocess_state_init\0")?;
            let echo_init: Init = *lib.get(b"speex_echo_state_init\0")?;
            let pre = init(480, 48000);
            let echo = echo_init(480, 4800);
            ensure!(!pre.is_null() && !echo.is_null(), "Speex allocation failed");
            let ctl: Ctl = *lib.get(b"speex_preprocess_ctl\0")?;
            let echo_ctl: Ctl = *lib.get(b"speex_echo_ctl\0")?;
            let mut rate = 48000i32;
            echo_ctl(echo, 24, &mut rate as *mut _ as *mut c_void);
            let mut enabled = denoise as i32;
            ctl(pre, 0, &mut enabled as *mut _ as *mut c_void);
            let this = Self {
                destroy: *lib.get(b"speex_preprocess_state_destroy\0")?,
                echo_destroy: *lib.get(b"speex_echo_state_destroy\0")?,
                run: *lib.get(b"speex_preprocess_run\0")?,
                cancel: *lib.get(b"speex_echo_cancellation\0")?,
                _library: lib,
                pre,
                echo,
                calibration,
                mic: [0; 480],
                reference: [0; 480],
                output: [0; 480],
                denoise,
            };
            Ok(this)
        }
    }
    pub fn process(
        &mut self,
        mic: &mut [f32; 480],
        reference: Option<&[f32; 480]>,
        drift_ppm: f32,
    ) -> Result<()> {
        for (i, x) in mic.iter().enumerate() {
            self.mic[i] = (x.clamp(-1., 1.) * 32767.) as i16;
        }
        if let Some(cal) = &self.calibration {
            ensure!(
                cal.passed && drift_ppm.abs() <= 100.,
                "AEC disabled: calibration/reference drift unreliable"
            );
            let reference = reference
                .ok_or_else(|| anyhow::anyhow!("AEC disabled: synchronized reference missing"))?;
            for (i, x) in reference.iter().enumerate() {
                self.reference[i] = (x.clamp(-1., 1.) * 32767.) as i16;
            }
            unsafe {
                (self.cancel)(
                    self.echo,
                    self.mic.as_ptr(),
                    self.reference.as_ptr(),
                    self.output.as_mut_ptr(),
                );
            }
            self.mic = self.output;
        }
        if self.denoise {
            unsafe {
                (self.run)(self.pre, self.mic.as_mut_ptr());
            }
        }
        for (i, x) in mic.iter_mut().enumerate() {
            *x = self.mic[i] as f32 / 32768.;
        }
        Ok(())
    }
}
impl Drop for Speex {
    fn drop(&mut self) {
        unsafe {
            (self.destroy)(self.pre);
            (self.echo_destroy)(self.echo);
        }
    }
}
