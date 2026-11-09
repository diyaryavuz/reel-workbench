# Reel Workbench

Turn a local recording into vertical clips you can review before sharing. Reel Workbench transcribes the audio, selects excerpts, and exports a captioned 1080 × 1920 MP4, an editable SRT, and a text excerpt for each clip. It also saves the source timestamps in `plan.json`.

Install Python 3, a recent FFmpeg build with `ffprobe`, `libx264` and the `subtitles` filter, and the speech recognition package:

```sh
python -m pip install faster-whisper
```

Run the complete workflow without a Claude Code account:

```sh
python .claude/skills/reel-kit/scripts/reel_kit.py auto "recording.mp4" exports --language tr
```

Omit `--language` to detect the spoken language. The default speech model downloads on first use. Pass `--model "/path/to/model"` to use an existing model directory. Once the model is available, transcription, clip selection and rendering run locally.

Automatic selection favors continuous speech, sentence endings and pauses near cuts. It exports up to three non-overlapping clips by default. Use `--count` to request one to five, and `--framing fill` to center-crop the frame. The default `fit` preserves the whole picture. Start with a source without embedded captions and review the exports before posting.

In a local test, a [17-minute NASA speech recording](https://ntrs.nasa.gov/citations/19940010921) produced three captioned clips of about 28 seconds each with the base speech model. A clip can still start mid-sentence when the speaker pauses there.

## Optional Claude Code workflow

Open this folder in Claude Code, or copy `.claude/skills/reel-kit` into another project's `.claude/skills/` directory. Invoke the skill with a local recording:

```text
/reel-kit "recording.mp4"
```

In this mode, Claude Code reads the transcript to choose clips and draft social copy.

You can also run the script directly with a clip plan in the format shown in [SKILL.md](.claude/skills/reel-kit/SKILL.md):

```sh
python .claude/skills/reel-kit/scripts/reel_kit.py transcribe "recording.mp4" transcript.json --language tr
python .claude/skills/reel-kit/scripts/reel_kit.py render "recording.mp4" transcript.json plan.json exports
```
