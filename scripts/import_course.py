#!/usr/bin/env python3
"""Import the owner's own exported course files into inputs/ so the app can answer from them with citations.

  import_course.py <source-folder> --rights owner|licensed [--dest inputs] [--product NAME]
                   [--model small] [--language en] [--no-transcribe] [--force] [--dry-run]

  --rights      required: "owner" if you made the course, "licensed" if its owner gave you permission.
                Material you only bought access to is not yours to import.
  --dest        where the corpus goes (default: inputs/ in the repo, or $OPERATOR_CORPUS_DIR)
  --product     product name for lessons directly in the source folder (default: the folder's name);
                lessons in sub-folders take the sub-folder's name
  --model       faster-whisper model for videos without captions: tiny, base, small (default), medium, large-v3
  --language    spoken language code, e.g. en; default: detect per file
  --no-transcribe  only use caption files; list videos without captions as waiting
  --force       re-import files that have not changed
  --dry-run     show what would happen, write nothing

Captions (.vtt/.srt) beside a video are used instead of transcribing it. Transcription runs on this machine
(pip install faster-whisper). Nothing is uploaded anywhere. Re-running only processes new or changed files.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_ai import course_import  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", type=Path)
    ap.add_argument("--rights", choices=sorted(course_import.RIGHTS), required=True)
    ap.add_argument("--dest", type=Path, default=Path(os.environ.get("OPERATOR_CORPUS_DIR", str(ROOT / "inputs"))))
    ap.add_argument("--product")
    ap.add_argument("--model", default="small")
    ap.add_argument("--language")
    ap.add_argument("--no-transcribe", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    transcriber = None
    if not a.no_transcribe:
        transcriber = course_import.whisper_transcriber(a.model, a.language)
        if transcriber is None:
            print("faster-whisper is not installed: videos without captions will be listed as waiting.\n"
                  "  Install it with: .venv/bin/pip install faster-whisper", file=sys.stderr)
    try:
        report = course_import.run(a.source, a.dest, rights=a.rights, product=a.product, transcriber=transcriber,
                                   force=a.force, dry_run=a.dry_run, progress=lambda m: print(f"... {m}", flush=True))
    except course_import.ImportRefused as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    for line in course_import.format_report(report):
        print(line)
    if not a.dry_run and (report.count("imported") or report.count("updated")):
        print(f"\nWritten to {a.dest}. Restart the app (scripts/run.sh) and the knowledge base rebuilds from it.")
    return 1 if report.count("error") else 0


if __name__ == "__main__":
    sys.exit(main())
