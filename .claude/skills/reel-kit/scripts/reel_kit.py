"""Transcribe a recording and render selected vertical clips."""

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap


def run(*command, cwd=None):
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, encoding="utf-8").stdout


def probe(video):
    data = json.loads(run(
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
        "-of", "json", str(video),
    ))
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The video must have a finite, positive duration.")
    return duration, {stream["codec_type"] for stream in data["streams"]}


def cues_from_words(words, duration):
    group = []
    for word in words:
        if not word.word.strip() or word.start >= duration:
            continue
        candidate = "".join(item.word for item in [*group, word]).strip()
        if group and (len(candidate) > 44 or word.end - group[0].start > 3):
            yield word_group(group, duration)
            group = []
        group.append(word)
    if group:
        yield word_group(group, duration)


def word_group(group, duration):
    return {
        "start": round(max(0, group[0].start), 3),
        "end": round(min(duration, group[-1].end), 3),
        "text": "".join(item.word for item in group).strip(),
    }


def transcribe(args):
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise ValueError("Install faster-whisper to transcribe recordings.") from error

    duration, streams = probe(args.video)
    if not {"video", "audio"} <= streams:
        raise ValueError("Transcription requires a video with an audio track.")
    if args.transcript.exists():
        raise ValueError("The transcript path already exists.")
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    with TemporaryDirectory(prefix="reel-kit-audio-") as temp:
        audio = Path(temp) / "speech.wav"
        run(
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(args.video),
            "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
        )
        segments, info = model.transcribe(
            str(audio), language=args.language, word_timestamps=True, vad_filter=True,
        )
        cues = [
            cue for segment in segments
            for cue in cues_from_words(segment.words or [], duration)
            if cue["end"] > cue["start"]
        ]
    if not cues:
        raise ValueError("No transcribable speech was detected.")
    with args.transcript.open("x", encoding="utf-8") as output:
        json.dump({"duration": duration, "language": info.language, "cues": cues},
                  output, ensure_ascii=False, indent=2)


def number(value):
    if type(value) not in (int, float) or not -math.inf < value < math.inf:
        raise ValueError("Timestamps must be finite numbers in seconds.")
    return value


def read_clips(path, duration):
    plan = json.loads(path.read_text(encoding="utf-8-sig"))
    clips = plan.get("clips") if isinstance(plan, dict) else None
    if not isinstance(clips, list) or not 1 <= len(clips) <= 5:
        raise ValueError("The plan must contain between one and five clips.")
    seen = set()
    for clip in clips:
        if not isinstance(clip, dict):
            raise ValueError("Each clip must be an object.")
        slug = clip.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 64 or slug in seen:
            raise ValueError("Clip slugs must be unique, at most 64 characters, and use lowercase letters, digits and hyphens.")
        seen.add(slug)
        if not 0 <= number(clip.get("start")) < number(clip.get("end")) <= duration:
            raise ValueError(f"Clip {slug} is outside the video or has no duration.")
        if clip.get("framing", "fit") not in ("fit", "fill"):
            raise ValueError(f"Invalid framing for {slug}. Use fit or fill.")
        for key in ("title", "caption"):
            if not isinstance(clip.get(key), str) or not clip[key].strip():
                raise ValueError(f"Clip {slug} needs a non-empty {key}.")
    return clips


def read_cues(path, duration):
    transcript = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(transcript, dict) or not isinstance(transcript.get("cues"), list):
        raise ValueError("The transcript must contain timed cues.")
    if abs(number(transcript.get("duration")) - duration) > 0.25:
        raise ValueError("The transcript duration does not match the source video.")
    for cue in transcript["cues"]:
        if not isinstance(cue, dict) or not isinstance(cue.get("text"), str):
            raise ValueError("Each transcript cue must contain text.")
        if not 0 <= number(cue.get("start")) < number(cue.get("end")) <= duration + 0.001:
            raise ValueError("A transcript cue falls outside the video.")
    return sorted(transcript["cues"], key=lambda cue: cue["start"])


def sentence_end(cue):
    return cue["text"].rstrip("\"'”’)] ").endswith((".", "?", "!", "…"))


def pick_clips(cues, count, framing):
    """Prefer complete, speech-dense excerpts with pauses at their edges."""
    cues = [cue for cue in cues if cue["text"].strip()]
    if not cues:
        raise ValueError("The transcript contains no usable speech.")
    speech_span = max(cue["end"] for cue in cues) - cues[0]["start"]
    minimum = min(18, speech_span * 0.6)
    candidates = []
    for index, first in enumerate(cues):
        previous = cues[index - 1] if index else None
        clean_start = previous is None or sentence_end(previous) or first["start"] - previous["end"] >= 0.65
        spoken_seconds = 0
        speech_end = first["start"]
        for last_index in range(index, len(cues)):
            last = cues[last_index]
            length = last["end"] - first["start"]
            if length > 45:
                break
            spoken_seconds += max(0, last["end"] - max(last["start"], speech_end))
            speech_end = max(speech_end, last["end"])
            if length < minimum:
                continue
            next_index = last_index + 1
            clean_end = (
                sentence_end(last) or next_index == len(cues)
                or cues[next_index]["start"] - last["end"] >= 0.65
            )
            density = spoken_seconds / length
            if density < 0.5:
                continue
            score = 3 * clean_start + 3 * clean_end + 2 * density - abs(length - 28) / 15
            if first["text"].rstrip().endswith("?"):
                score += 0.25
            candidates.append((score, index, last_index))

    selected = []
    for _, index, last_index in sorted(candidates, key=lambda item: (-item[0], item[1], item[2])):
        start, end = cues[index]["start"], cues[last_index]["end"]
        if any(start < clip["end"] + 2 and end > clip["start"] - 2 for clip in selected):
            continue
        words = " ".join(cue["text"] for cue in cues[index:last_index + 1]).split()
        selected.append({
            "slug": f"clip-{len(selected) + 1:02}",
            "start": start,
            "end": end,
            "title": " ".join(words[:8]),
            "caption": " ".join(words[:30]) + ("…" if len(words) > 30 else ""),
            "framing": framing,
        })
        if len(selected) == count:
            break
    if not selected:
        raise ValueError("No sufficiently continuous speech excerpt was found.")
    return selected


def srt_time(milliseconds):
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def clip_subtitles(cues, start, end):
    entries = []
    for cue in cues:
        left = round((max(start, cue["start"]) - start) * 1000)
        right = round((min(end, cue["end"]) - start) * 1000)
        text = textwrap.fill(" ".join(cue["text"].split()), width=28)
        if right > left and text:
            entries.append(f"{len(entries)+1}\n{srt_time(left)} --> {srt_time(right)}\n{text}\n")
    return "\n".join(entries)


def render(args):
    video = args.video.resolve()
    output = args.output.resolve()
    if output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new output folder under an existing directory.")
    duration, streams = probe(video)
    if "video" not in streams:
        raise ValueError("The source has no video track.")
    clips = read_clips(args.plan, duration)
    cues = read_cues(args.transcript, duration)
    fit = "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    fill = "scale=1080:1920:force_original_aspect_ratio=increase:force_divisible_by=2:reset_sar=1,crop=1080:1920"
    with TemporaryDirectory(prefix=".reel-kit-", dir=output.parent) as temp:
        work = Path(temp)
        batch = work / "exports"
        batch.mkdir()
        (batch / "plan.json").write_text(
            json.dumps({"clips": clips}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        for clip in clips:
            slug, start, end = (clip[key] for key in ("slug", "start", "end"))
            subtitles = clip_subtitles(cues, start, end)
            if not subtitles:
                raise ValueError(f"Clip {slug} does not overlap any transcript cues.")
            (work / "captions.srt").write_text(subtitles, encoding="utf-8")
            (batch / f"{slug}.srt").write_text(subtitles, encoding="utf-8")
            (batch / f"{slug}.txt").write_text(
                f"{clip['title']}\n\n{clip['caption']}\n", encoding="utf-8",
            )
            framing = fill if clip.get("framing") == "fill" else fit
            style = "FontSize=17,Alignment=2,MarginV=34,Outline=2,Bold=1"
            run(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", str(start), "-i", str(video), "-t", str(end - start),
                "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
                "-vf", f"{framing},subtitles=captions.srt:force_style='{style}'",
                "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                str(batch / f"{slug}.mp4"), cwd=work,
            )
        if output.exists():
            raise ValueError("The output folder already exists.")
        batch.rename(output)
    print(f"Exported {len(clips)} clip(s) to {output}")


def automatic(args):
    output = args.output.resolve()
    if output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new output folder under an existing directory.")
    with TemporaryDirectory(prefix=".reel-kit-auto-", dir=output.parent) as temp:
        args.transcript = Path(temp) / "transcript.json"
        args.plan = Path(temp) / "plan.json"
        print("Transcribing the source locally.", flush=True)
        transcribe(args)
        duration, _ = probe(args.video)
        clips = pick_clips(read_cues(args.transcript, duration), args.count, args.framing)
        args.plan.write_text(json.dumps({"clips": clips}, ensure_ascii=False), encoding="utf-8")
        print(f"Selected {len(clips)} excerpt(s). Rendering.", flush=True)
        render(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    speech = actions.add_parser("transcribe")
    speech.add_argument("video", type=Path)
    speech.add_argument("transcript", type=Path)
    speech.add_argument("--language")
    speech.add_argument("--model", default="base")
    export = actions.add_parser("render")
    export.add_argument("video", type=Path)
    export.add_argument("transcript", type=Path)
    export.add_argument("plan", type=Path)
    export.add_argument("output", type=Path)
    auto = actions.add_parser("auto", help="Transcribe, select and render without Claude Code.")
    auto.add_argument("video", type=Path)
    auto.add_argument("output", type=Path)
    auto.add_argument("--language")
    auto.add_argument("--model", default="base")
    auto.add_argument("--count", type=int, choices=range(1, 6), default=3)
    auto.add_argument("--framing", choices=("fit", "fill"), default="fit")
    args = parser.parse_args()
    try:
        {"transcribe": transcribe, "render": render, "auto": automatic}[args.action](args)
    except subprocess.CalledProcessError as error:
        print(error.stderr.strip() or "A media command failed.", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Reel Kit: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
