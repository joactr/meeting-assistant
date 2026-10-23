# Troubleshooting

## A device is missing or capture is degraded

Check OS microphone privacy and the selected exact endpoint. Re-select vanished
or ambiguous IDs; do not assume a friendly-name match is the same device.
Protected/exclusive playback and RDP/VDI may prevent system capture. A persistent
lost-source warning means some meeting audio was not captured. Retry that source
or explicitly continue microphone-only; noise suppression cannot restore it.

## Transcription/provider unavailable

Check the selected provider/model and explicit connection test. Missing model,
authentication, quota, unsupported options and worker startup failure are
different errors. The app must not silently upload audio elsewhere. Large local
runtimes/models need explicit prior acquisition for offline use. Optional
analysis/embedding/diarization failure should not erase persisted transcript.

## An edit conflicts

Another revision or late inference may have changed the entity. Inspect the
latest returned value and reapply the intended edit. Do not bypass the expected
revision. Manual corrections should remain locked against inference overwrite.
Regeneration/re-transcription produces a candidate for review, not an overwrite.

## Finalization or shutdown needs attention

Review the failed stage and preservation watermark. Retry eligible stages rather
than starting duplicate remote operations. An interrupted meeting preserves
committed transcript but recording-off in-memory audio cannot be recovered.
Do not install an update until safe shutdown is positively acknowledged.

## Delivery status is unknown

A timeout does not prove an issue/email/webhook was not delivered. Reconcile the
GitHub operation marker or review duplicate risk before an explicit resend.
Never fix this by automatically retrying a non-idempotent write.

## Release verification fails

Read the actual failed prerequisite. A local developer build does not supply
publisher signing, public OAuth registration, GitHub identity or credentialed
service acceptance. Populate legitimate inputs and run the failing gate again;
never replace them with dummy keys, fabricated receipts or disabled checks.
