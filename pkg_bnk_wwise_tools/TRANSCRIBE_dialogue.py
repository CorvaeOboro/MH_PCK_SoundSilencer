"""
OGG Transcriber - offline speech-to-text for extracted .ogg files using Vosk.

Requires:
    - vosk Python package  (pip install vosk)
    - A Vosk model downloaded from https://alphacephei.com/vosk/models
    - ffmpeg.exe 

Example:
    python -m pkg_bnk_wwise_tools transcribe ./test_extract --model ./vosk-model-en-us-0.42-gigaspeech -o transcription.json
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from vosk import Model, KaldiRecognizer
except ImportError:
    Model = None  # type: ignore[misc,assignment]
    KaldiRecognizer = None  # type: ignore[misc,assignment]

from .UTIL_logger import Logger, log
from .CONFIG_tools import ToolPaths

__all__ = ["OggTranscriber"]


class OggTranscriber:
    """
    Transcribe a folder of .ogg files using a local Vosk model.
    Each .ogg is converted to 16 kHz mono WAV via ffmpeg, then fed
    to Vosk. Results are written as a JSON file.
    """

    def __init__(self, model_path: str | Path, tool_paths: Optional[ToolPaths] = None, logger: Optional[Logger] = None):
        self.model_path = Path(model_path)
        self.log = logger or log
        self.tp = tool_paths or ToolPaths.load()
        self._model: Optional[Any] = None
        self._check_deps()

    def _check_deps(self) -> None:
        if Model is None:
            raise RuntimeError(
                "The 'vosk' package is not installed. "
                "Install it with: pip install vosk"
            )
        if not self.model_path.exists():
            raise RuntimeError(
                f"Vosk model not found at: {self.model_path}\n"
                "Download a model from https://alphacephei.com/vosk/models "
                "(e.g. vosk-model-en-us-0.42-gigaspeech)"
            )
        if not Path(self.tp.ffmpeg).exists():
            raise RuntimeError(f"ffmpeg not found at: {self.tp.ffmpeg}")

    def _load_model(self) -> Any:
        if self._model is None:
            with self.log.step("Load Vosk model"):
                self._model = Model(str(self.model_path))
                self.log.ok(f"Loaded model from {self.model_path}")
        return self._model

    def _ogg_to_wav(self, ogg_path: Path, wav_path: Path) -> bool:
        """Convert OGG to 16 kHz mono 16-bit PCM WAV. Returns True on success."""
        try:
            result = subprocess.run(
                [
                    self.tp.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(ogg_path),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                    str(wav_path),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.log.error(f"ffmpeg failed for {ogg_path.name}: {result.stderr.strip()}")
                return False
            return True
        except Exception as e:
            self.log.error(f"ffmpeg exception for {ogg_path.name}: {e}")
            return False

    def _transcribe_wav(self, wav_path: Path) -> Dict[str, Any]:
        """Run Vosk on a WAV file. Returns dict with transcription and metadata."""
        model = self._load_model()
        recognizer = KaldiRecognizer(model, 16000)

        with wave.open(str(wav_path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
                return {
                    "transcription": "",
                    "word_timings": [],
                    "error": (
                        f"Unexpected WAV format: ch={wf.getnchannels()}, "
                        f"width={wf.getsampwidth()}, rate={wf.getframerate()}"
                    ),
                }
            data = wf.readframes(wf.getnframes())

        chunk_size = 4096
        for i in range(0, len(data), chunk_size):
            recognizer.AcceptWaveform(data[i : i + chunk_size])

        result = json.loads(recognizer.FinalResult())
        text = result.get("text", "").strip()
        word_timings = result.get("result", [])

        return {
            "transcription": text,
            "word_timings": word_timings,
            "error": None,
        }

    def _sidecar_path(self, ogg_path: Path) -> Path:
        """Return the per-OGG sidecar path for Vosk transcripts."""
        return ogg_path.parent / f"{ogg_path.stem}_transcribe_vosk.txt"

    def transcribe_file(self, ogg_path: Path) -> Dict[str, Any]:
        """Transcribe a single .ogg file. Returns entry dict."""
        self.log.info(f"Processing {ogg_path.name}")

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "temp.wav"
            if not self._ogg_to_wav(ogg_path, wav_path):
                return {
                    "filename": ogg_path.name,
                    "filepath": str(ogg_path.resolve()),
                    "transcription": "",
                    "word_timings": [],
                    "error": "ffmpeg conversion failed",
                }

            try:
                result = self._transcribe_wav(wav_path)
            except Exception as e:
                self.log.error(f"Vosk failed for {ogg_path.name}: {e}")
                result = {
                    "transcription": "",
                    "word_timings": [],
                    "error": f"Vosk error: {e}",
                }

        entry: Dict[str, Any] = {
            "filename": ogg_path.name,
            "filepath": str(ogg_path.resolve()),
            "transcription": result["transcription"],
            "word_timings": result["word_timings"],
            "error": result["error"],
        }

        if result["error"]:
            self.log.warn(f"{ogg_path.name}: {result['error']}")
        elif result["transcription"]:
            snippet = result["transcription"][:60]
            self.log.ok(f'{ogg_path.name}: "{snippet}"')
            # Write per-OGG sidecar so resume is incremental
            sidecar = self._sidecar_path(ogg_path)
            try:
                sidecar.write_text(result["transcription"], encoding="utf-8")
            except Exception as e:
                self.log.warn(f"Could not write sidecar {sidecar.name}: {e}")
        else:
            self.log.info(f"{ogg_path.name}: (no speech detected)")
        return entry

    def transcribe_folder(
        self,
        ogg_dir: Path,
        output_json: Path,
        pattern: str = "*.ogg",
        classification_json: Path | None = None,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        """Transcribe all matching .ogg files in a folder and save results.

        If classification_json is provided, only transcribe files flagged
        as dialogue by the audio classifier (Stage 1.5 pre-filter).

        Existing *_transcribe_vosk.txt sidecars are reused unless force=True.
        """
        ogg_dir = Path(ogg_dir)
        output_json = Path(output_json)

        ogg_files = sorted(ogg_dir.glob(pattern))
        if not ogg_files:
            self.log.warn(f"No .ogg files found in {ogg_dir}")
            return []

        # Load classification filter if provided
        dialogue_files: set[str] = set()
        if classification_json and classification_json.exists():
            try:
                clf_data = json.loads(classification_json.read_text(encoding="utf-8"))
                for item in clf_data.get("files", []):
                    if item.get("is_dialogue"):
                        dialogue_files.add(item["filename"])
                skipped = len(ogg_files) - len(dialogue_files)
                self.log.info(
                    f"Classification filter: {len(dialogue_files)} dialogue, "
                    f"{skipped} SFX skipped"
                )
                ogg_files = [p for p in ogg_files if p.name in dialogue_files]
            except Exception as e:
                self.log.warn(f"Could not load classification filter: {e}")

        if not ogg_files:
            self.log.info("No dialogue files to transcribe (all classified as SFX)")
            # Still write empty result for consistency
            result_doc: Dict[str, Any] = {
                "source_dir": str(ogg_dir.resolve()),
                "model_path": str(self.model_path.resolve()),
                "generated_at": datetime.now().isoformat(),
                "entry_count": 0,
                "entries": [],
                "classification_filter_used": str(classification_json) if classification_json else None,
            }
            output_json.parent.mkdir(parents=True, exist_ok=True)
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(result_doc, f, indent=2, ensure_ascii=False)
            self.log.ok(f"Saved empty transcription results to {output_json}")
            return []

        self.log.info(f"Transcribing {len(ogg_files)} .ogg file(s)")

        entries: List[Dict[str, Any]] = []
        with self.log.step("Transcribe .ogg files"):
            for ogg_path in ogg_files:
                sidecar = self._sidecar_path(ogg_path)
                if not force and sidecar.exists():
                    # Reuse cached per-file transcript
                    text = sidecar.read_text(encoding="utf-8").strip()
                    entries.append({
                        "filename": ogg_path.name,
                        "filepath": str(ogg_path.resolve()),
                        "transcription": text,
                        "word_timings": [],
                        "error": None,
                    })
                    self.log.info(f"{ogg_path.name}: (cached)")
                    continue
                try:
                    entry = self.transcribe_file(ogg_path)
                    entries.append(entry)
                except Exception as e:
                    self.log.error(f"Unhandled exception for {ogg_path.name}: {e}")
                    entries.append({
                        "filename": ogg_path.name,
                        "filepath": str(ogg_path.resolve()),
                        "transcription": "",
                        "word_timings": [],
                        "error": f"Unhandled: {e}",
                    })

        result_doc: Dict[str, Any] = {
            "source_dir": str(ogg_dir.resolve()),
            "model_path": str(self.model_path.resolve()),
            "generated_at": datetime.now().isoformat(),
            "entry_count": len(entries),
            "entries": entries,
        }
        output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(result_doc, f, indent=2, ensure_ascii=False)
        self.log.ok(f"Saved transcription results to {output_json}")

        return entries
