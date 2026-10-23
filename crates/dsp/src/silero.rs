//! Silero v6.2 ONNX recurrent state and 64-sample context, independently per source.
use super::*;
use ndarray::{Array0, Array2, Array3};
use ort::{session::Session, value::TensorRef};
pub struct Silero {
    session: Session,
    state: Array3<f32>,
    context: [f32; 64],
    input: Array2<f32>,
    sample_rate: Array0<i64>,
}
impl Silero {
    pub fn load(model: &std::path::Path) -> Result<Self> {
        let directory = model
            .parent()
            .ok_or_else(|| anyhow::anyhow!("bundled DSP directory missing"))?;
        let library = directory.join(if cfg!(windows) {
            "onnxruntime.dll"
        } else if cfg!(target_os = "macos") {
            "libonnxruntime.dylib"
        } else {
            "libonnxruntime.so"
        });
        ensure!(
            model.is_file() && library.is_absolute() && library.is_file(),
            "bundled Silero model/ONNX Runtime unavailable"
        );
        static RUNTIME: std::sync::OnceLock<std::result::Result<(), String>> =
            std::sync::OnceLock::new();
        let initialized = RUNTIME.get_or_init(|| {
            ort::init_from(library.to_string_lossy())
                .with_name("Meeting Assistant DSP")
                .commit()
                .map(|_| ())
                .map_err(|e| e.to_string())
        });
        if let Err(message) = initialized {
            return Err(anyhow::anyhow!("ONNX Runtime: {message}"));
        }
        let session = Session::builder()?
            .with_intra_threads(1)?
            .commit_from_file(model)?;
        Ok(Self {
            session,
            state: Array3::zeros((2, 1, 128)),
            context: [0.; 64],
            input: Array2::zeros((1, 576)),
            sample_rate: Array0::from_elem((), 16000),
        })
    }
}
impl Probability for Silero {
    fn predict(&mut self, samples: &[f32; 512]) -> Result<f32> {
        for (i, x) in self.context.iter().chain(samples).enumerate() {
            self.input[[0, i]] = *x;
        }
        let output=self.session.run(ort::inputs!["input"=>TensorRef::from_array_view(&self.input)?,"state"=>TensorRef::from_array_view(&self.state)?,"sr"=>TensorRef::from_array_view(&self.sample_rate)?])?;
        let (_, prob) = output["output"].try_extract_tensor::<f32>()?;
        let p = *prob
            .first()
            .ok_or_else(|| anyhow::anyhow!("Silero empty probability"))?;
        let (_, state) = output["stateN"].try_extract_tensor::<f32>()?;
        ensure!(state.len() == 256 && p.is_finite(), "Silero invalid state");
        self.state.as_slice_mut().unwrap().copy_from_slice(state);
        self.context.copy_from_slice(&samples[448..]);
        Ok(p)
    }
    fn reset(&mut self) {
        self.state.fill(0.);
        self.context.fill(0.);
    }
    fn mode(&self) -> &str {
        "silero_v6_2_onnx"
    }
}
