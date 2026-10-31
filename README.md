# Speech Workbench

PCM WAV transcription-job orchestration, timestamp review and subtitle editing/export. Python3.10+, SQLite and standard library only.

```sh
python -m unittest discover -s tests -v
python demo.py --output-dir artifacts
```

The sample generates one second of synthetic silence and uses a fixture adapter that returns explicitly labelled synthetic captions. No speech recognition or external model is performed. GitHub Actions uploads speech-subtitles-python-* artifacts with captions.srt, captions.vtt and transcript.json. Download and inspect these on a phone/computer; the files are sample outputs rather than a live transcription service.

```python
from pathlib import Path
from speech import Workbench, from_srt
work = Workbench('speech.db')
audio = work.ingest('your-local.wav', Path('your-local.wav').read_bytes())
job = work.enqueue(audio, 'your-adapter', 'unique-event')
# A trusted adapter takes PCM WAV bytes and duration_ms, returning segment dictionaries.
# work.process_one({'your-adapter': adapter})
# Review ready results through work.get(job).
# edited = from_srt(Path('reviewed.srt').read_text(), duration_ms)
# work.edit(job, edited, expected_revision=0, actor='reviewer', reason='Timing correction')
# Path('captions.srt').write_text(work.export(job))
```

Audio bytes and metadata are committed together in SQLite, deduplicated by SHA-256. Jobs have an explicit engine label and globally unique event key; identical audio/engine retries return the same job, while conflicting reuse fails. Atomic worker claims use expiring tokens, recover interrupted work, apply bounded exponential backoff and dead-letter after three attempts by default. Use the same attempt limit across workers. Provider errors retain exception type only. Audio hashes are checked before an adapter receives data.

Adapters must return ordered, nonoverlapping start/end millisecond timestamps within the audio duration. Empty segment lists are permitted for no detected speech. Text, total output size and timestamp types are validated before publishing a ready transcript. Every human edit requires the current revision, actor and reason; original adapter output remains intact and before/after history is retained. Actor names are caller supplied, not authenticated identities. Export supports numbered SRT and WebVTT; SRT import is deliberately strict and rejects malformed numbering/times/overlaps. VTT text escapes markup. SRT output preserves text for round trips; treat it as subtitle data, not HTML.

13 tests cover actual WAV parsing, truncated/oversized audio, deduplication, invalid timestamps, concurrent claims/reviews, restart, expired leases/stale acknowledgements, failed adapters, retries/dead letters, audio integrity, original/review history, Unicode/multiline SRT round trips and VTT markup escaping. CI runs tests and the synthetic demo on Python3.10/3.12/3.13. Actions → Tests and demo → Run workflow repeats them.

Limits: local engine without a microphone recorder, hosted UI, audio player, authentication, encryption or bundled recogniser. Supported input is mono/stereo uncompressed PCM WAV,1..4-byte samples,1..192kHz,up to10MiB and at least1ms. Duration uses whole milliseconds rounded down. Transcript limit4MiB/10000 segments; strict SRT input up to8MiB. Worker lease up toone hour; adapters must enforce a shorter timeout. A provider can succeed before a process crashes, so retries may repeat work/charges; no exactly-once guarantee. Audio, transcript and edits are stored in plaintext; no automatic retention/deletion is included. Synthetic tests do not measure real speech accuracy, diarisation, languages or noisy-audio performance. Live provider/device verification remains pending.
