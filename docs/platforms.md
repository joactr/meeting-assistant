# Platform setup

## Windows 11 x86-64

Use Visual Studio C++ Desktop build tools, Windows SDK, Rust 1.94.0 MSVC target,
Python 3.12.10, Node 24.22.1 and WebView2. The supported system source is a WASAPI
shared-mode output endpoint loopback. Friendly names are not persistent device
identities. A saved endpoint disappearing requires user re-selection; choosing
`default` resolves once at Start, not continuously during recording.

Microphone privacy denial, device loss and protected/exclusive output content
must be visible failures. AEC needs a calibrated synchronized system reference;
headphones remain the straightforward way to reduce physical speaker echo.
Native codec/DSP bindings require compatible libopus and SpeexDSP binaries and
actual license notices. Model runtimes are optional separate downloads.

Hardware acceptance must exercise microphone, system and both sources,
pause/resume/Stop, unplug/retry, recording off/on and replay. RDP/VDI remains
best-effort; an absent remote endpoint cannot be recovered by a DSP setting.

## macOS — experimental and untested

The implementation targets macOS 15+, arm64 and x86-64 separately. ScreenCaptureKit
is used for audio-only system capture and CoreAudio for microphone. Explain
screen/audio and microphone permissions; do not retain screen frames. Exclude
application replay from system capture. Permission denial/revocation must be
reported with OS-appropriate recovery instructions.

No macOS hardware or installed-surface validation has been performed in the
current environment. A successful compile is not native capture acceptance.
Public binaries additionally need actual Developer ID signing, hardened
runtime, notarization and stapling; updater signatures do not replace these.

## Linux — experimental and untested

The implementation targets Ubuntu 24.04 x86-64, PipeWire and WirePlumber, with
explicit sink-monitor and microphone node selection. Development packages
include libpipewire-0.3-dev, libasound2-dev, libwebkit2gtk-4.1-dev,
libappindicator3-dev, librsvg2-dev, patchelf, libopus-dev and libspeexdsp-dev.
Other distributions/audio configurations are best-effort. Do not assume a
universal desktop system-audio portal exists.

No Linux hardware or installed-surface validation has been performed in the
current environment. Native routing, permissions, replay and AppImage runtime
compatibility require actual target-machine acceptance before promotion out
of experimental status.
