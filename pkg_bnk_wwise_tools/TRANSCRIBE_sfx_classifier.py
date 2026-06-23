"""
Audio classification module for tagging .ogg files as speech, SFX, music, etc.

Supports:
    - YAMNet (Google / TensorFlow Hub) - primary classifier
    - PANNs CNN14 (optional secondary) - requires torch + cloned repo

Outputs a structured JSON where every classification is attributed to the model
that produced it, enabling multi-model consensus and dashboard filtering.

Usage:
    from .audio_classifier import AudioClassifier
    clf = AudioClassifier(logger=logger)
    results = clf.classify_folder(Path("extracted/SFX_ScarletWitch_INT"))
    clf.save(results, Path("extracted/SFX_ScarletWitch_INT/classification.json"))

JSON output shape per file:
    {
      "filename": "0003cabf_24.ogg",
      "filepath": ".../0003cabf_24.ogg",
      "classifications": [
        {"model": "yamnet", "label": "Speech", "score": 0.847},
        {"model": "yamnet", "label": "Shout",   "score": 0.120}
      ],
      "is_dialogue": true,
      "dialogue_confidence": 0.847,
      "duration_seconds": 1.23,
      "models_used": ["yamnet"]
    }
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .UTIL_logger import Level, Logger

__all__ = [
    "check_python_deps",
    "check_yamnet",
    "check_panns",
    "download_yamnet_class_map",
    "clone_panns_repo",
    "download_panns_weights",
    "full_check",
    "ClassificationResult",
    "AudioClassifier",
]

# Local reference dir (avoid importing from __init__.py)
_REF_DIR = Path(__file__).parent.parent / "ref"

# ------------------------------------------------------------------
# Setup / dependency checker (merged from setup_audio_models.py)
# ------------------------------------------------------------------

import argparse
import subprocess
import urllib.request

PANN_REPO = _REF_DIR / "audioset_tagging_cnn"
YAMNET_CLASS_MAP = _REF_DIR / "yamnet_class_map.csv"
YAMNET_CLASS_MAP_URL = (
    "https://raw.githubusercontent.com/tensorflow/models/master/"
    "research/audioset/yamnet/yamnet_class_map.csv"
)

PANN_CHECKPOINTS = {
    "cnn14": {
        "url": "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth",
        "filename": "Cnn14_mAP=0.431.pth",
        "size_mb": 79,
    },
    "mobilenetv2": {
        "url": "https://zenodo.org/record/3987831/files/MobileNetV2_mAP%3D0.383.pth",
        "filename": "MobileNetV2_mAP=0.383.pth",
        "size_mb": 17,
    },
}


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def check_python_deps(logger: Optional[Logger] = None) -> dict[str, bool]:
    """Check which Python packages are installed."""
    log = logger or Logger(Level.INFO)
    deps = {
        "tensorflow": _has_module("tensorflow"),
        "tensorflow_hub": _has_module("tensorflow_hub"),
        "librosa": _has_module("librosa"),
        "numpy": _has_module("numpy"),
        "torch": _has_module("torch"),
        "soundfile": _has_module("soundfile"),
    }
    for name, ok in deps.items():
        status = "OK" if ok else "MISSING"
        color = "ok" if ok else "warn"
        getattr(log, color)(f"  {name}: {status}")
    return deps


def _yamnet_model_available() -> bool:
    try:
        import tensorflow_hub as hub  # noqa: F401
        return True
    except ImportError:
        return False


def _yamnet_class_map_available() -> bool:
    return YAMNET_CLASS_MAP.exists() and YAMNET_CLASS_MAP.stat().st_size > 0


def download_yamnet_class_map(logger: Optional[Logger] = None) -> bool:
    """Download YAMNet class map CSV to ref/."""
    log = logger or Logger(Level.INFO)
    log.info(f"Downloading YAMNet class map to {YAMNET_CLASS_MAP}")
    try:
        _REF_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(YAMNET_CLASS_MAP_URL, str(YAMNET_CLASS_MAP))
        log.ok(f"Downloaded {YAMNET_CLASS_MAP.name} ({YAMNET_CLASS_MAP.stat().st_size:,} bytes)")
        return True
    except Exception as e:
        log.error(f"Download failed: {e}")
        return False


def check_yamnet(logger: Optional[Logger] = None) -> dict[str, bool]:
    """Check YAMNet-specific readiness."""
    log = logger or Logger(Level.INFO)
    log.info("YAMNet status:")
    status = {
        "tensorflow_hub": _yamnet_model_available(),
        "class_map_csv": _yamnet_class_map_available(),
    }
    for name, ok in status.items():
        color = "ok" if ok else "warn"
        getattr(log, color)(f"  {name}: {'OK' if ok else 'MISSING'}")
    return status


def _panns_repo_available() -> bool:
    return PANN_REPO.exists() and (PANN_REPO / "pytorch" / "models.py").exists()


def _panns_weights_available(variant: str = "cnn14") -> bool:
    info = PANN_CHECKPOINTS.get(variant)
    if not info:
        return False
    return (PANN_REPO / info["filename"]).exists()


def check_panns(logger: Optional[Logger] = None) -> dict[str, bool]:
    """Check PANNs-specific readiness."""
    log = logger or Logger(Level.INFO)
    log.info("PANNs status:")
    status = {
        "repo_cloned": _panns_repo_available(),
        "torch": _has_module("torch"),
        "cnn14_weights": _panns_weights_available("cnn14"),
        "mobilenetv2_weights": _panns_weights_available("mobilenetv2"),
    }
    for name, ok in status.items():
        color = "ok" if ok else "warn"
        getattr(log, color)(f"  {name}: {'OK' if ok else 'MISSING'}")
    return status


def clone_panns_repo(logger: Optional[Logger] = None) -> bool:
    """Clone PANNs repository into ref/."""
    log = logger or Logger(Level.INFO)
    if _panns_repo_available():
        log.ok("PANNs repo already present")
        return True
    log.info(f"Cloning PANNs repo into {PANN_REPO}")
    try:
        _REF_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git", "clone",
                "https://github.com/qiuqiangkong/audioset_tagging_cnn.git",
                str(PANN_REPO),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        log.ok("PANNs repo cloned")
        return True
    except FileNotFoundError:
        log.error("git not found in PATH. Install Git or clone manually:")
        log.info(f"  git clone https://github.com/qiuqiangkong/audioset_tagging_cnn.git {PANN_REPO}")
        return False
    except subprocess.CalledProcessError as e:
        log.error(f"Clone failed: {e.stderr}")
        return False


def download_panns_weights(variant: str = "cnn14", logger: Optional[Logger] = None) -> bool:
    """Download PANNs pretrained weights."""
    log = logger or Logger(Level.INFO)
    info = PANN_CHECKPOINTS.get(variant)
    if not info:
        log.error(f"Unknown PANNs variant: {variant}")
        return False
    dest = PANN_REPO / info["filename"]
    if dest.exists():
        log.ok(f"{info['filename']} already present")
        return True
    if not _panns_repo_available():
        log.error("PANNs repo not found. Run --setup-panns first.")
        return False
    log.info(f"Downloading {info['filename']} (~{info['size_mb']} MB)...")
    log.info(f"  URL: {info['url']}")
    try:
        urllib.request.urlretrieve(info["url"], str(dest))
        log.ok(f"Downloaded {info['filename']} ({dest.stat().st_size:,} bytes)")
        return True
    except Exception as e:
        log.error(f"Download failed: {e}")
        log.info("Download manually and place in:")
        log.info(f"  {dest}")
        return False


def full_check(logger: Optional[Logger] = None) -> bool:
    """Run all checks and report missing pieces."""
    log = logger or Logger(Level.INFO)
    log.info("=" * 50)
    log.info("Audio Classification Setup Check")
    log.info("=" * 50)

    log.info("\n[1] Python dependencies")
    py_deps = check_python_deps(log)

    log.info("\n[2] YAMNet")
    yamnet_ok = check_yamnet(log)

    log.info("\n[3] PANNs (optional)")
    panns_ok = check_panns(log)

    # Summary
    all_ok = True
    log.info("\n" + "=" * 50)
    log.info("Summary")
    log.info("=" * 50)

    optional_deps = {"torch"}
    missing_py = [k for k, v in py_deps.items() if not v and k not in optional_deps]
    missing_optional = [k for k, v in py_deps.items() if not v and k in optional_deps]
    if missing_py:
        all_ok = False
        log.warn(f"Missing Python packages: {', '.join(missing_py)}")
        log.info("Install with:")
        log.info(f"  pip install {' '.join(missing_py)}")
    else:
        log.ok("All required Python dependencies present")
    if missing_optional:
        log.info(f"Optional packages missing: {', '.join(missing_optional)}")

    if not yamnet_ok["class_map_csv"]:
        all_ok = False
        log.warn("YAMNet class map missing")
        log.info("Download with:")
        log.info("  python -m pkg_bnk_wwise_tools.setup_audio_models --download-yamnet-map")

    if not yamnet_ok["tensorflow_hub"]:
        all_ok = False
        log.warn("TensorFlow Hub not available - YAMNet cannot load")
        log.info("Install with:")
        log.info("  pip install tensorflow tensorflow-hub")

    if not panns_ok["repo_cloned"]:
        log.info("PANNs repo not cloned (optional - YAMNet is sufficient alone)")
        log.info("Setup with:")
        log.info("  python -m pkg_bnk_wwise_tools.setup_audio_models --setup-panns")

    if all_ok:
        log.ok("YAMNet is ready to use. PANNs is optional.")

    log.info("=" * 50)
    return all_ok

# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------

@dataclass
class ClassificationResult:
    filename: str
    filepath: str
    classifications: list[dict[str, Any]] = field(default_factory=list)
    is_dialogue: bool = False
    dialogue_confidence: float = 0.0
    duration_seconds: float = 0.0
    models_used: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "filepath": self.filepath,
            "classifications": self.classifications,
            "is_dialogue": self.is_dialogue,
            "dialogue_confidence": round(self.dialogue_confidence, 4),
            "duration_seconds": round(self.duration_seconds, 3),
            "models_used": self.models_used,
            "error": self.error,
        }


# ------------------------------------------------------------------
# YAMNet backend
# ------------------------------------------------------------------

class _YamnetBackend:
    """Lazy-loaded YAMNet classifier."""

    DIALOGUE_LABELS = {
        "Speech",
        "Male speech, man speaking",
        "Female speech, woman speaking",
        "Narration, monologue",
        "Conversation",
        "Shout",
        "Whispering",
        "Laughter",
        "Crying, sobbing",
        "Child speech, kid speaking",
    }

    def __init__(self, logger: Logger) -> None:
        self.log = logger
        self._model: Any = None
        self._class_names: list[str] = []
        self._ready = False
        self._init()

    def _init(self) -> None:
        # 1. Import deps
        try:
            import tensorflow as tf  # noqa: F401
            import tensorflow_hub as hub  # noqa: F401
            import librosa  # noqa: F401
        except ImportError as e:
            self.log.error(f"YAMNet import failed: {e}")
            self.log.info("Install dependencies: pip install tensorflow tensorflow-hub librosa")
            return

        # 2. Load class map
        if not YAMNET_CLASS_MAP.exists():
            self.log.error(f"YAMNet class map not found: {YAMNET_CLASS_MAP}")
            self.log.info("Download with:")
            self.log.info("  python -m pkg_bnk_wwise_tools.setup_audio_models --download-yamnet-map")
            return
        self._class_names = YAMNET_CLASS_MAP.read_text(encoding="utf-8").strip().splitlines()
        if not self._class_names:
            self.log.error("YAMNet class map is empty")
            return

        # 3. Load model from TF Hub (auto-downloads on first use)
        self.log.info("Loading YAMNet model from TensorFlow Hub...")
        try:
            self._model = hub.load("https://tfhub.dev/google/yamnet/1")
            self._ready = True
            self.log.ok("YAMNet loaded")
        except Exception as e:
            self.log.error(f"YAMNet load failed: {e}")

    @property
    def ready(self) -> bool:
        return self._ready

    def classify(self, audio_path: Path, top_k: int = 5) -> list[dict[str, Any]]:
        import librosa
        waveform, sr = librosa.load(str(audio_path), sr=16000, mono=True)
        scores, _embeddings, _spectrogram = self._model(waveform)
        mean_scores = np.mean(scores.numpy(), axis=0)
        top_indices = np.argsort(mean_scores)[-top_k:][::-1]
        return [
            {
                "model": "yamnet",
                "label": self._class_names[int(i)],
                "score": round(float(mean_scores[int(i)]), 4),
            }
            for i in top_indices
        ]

    def is_dialogue(self, classifications: list[dict[str, Any]], threshold: float = 0.3) -> tuple[bool, float]:
        best = 0.0
        for c in classifications:
            if c["label"] in self.DIALOGUE_LABELS and c["score"] > best:
                best = c["score"]
        return best >= threshold, round(best, 4)


# ------------------------------------------------------------------
# PANNs backend (optional)
# ------------------------------------------------------------------

class _PannsBackend:
    """Lazy-loaded PANNs CNN14 classifier (optional)."""

    def __init__(self, logger: Logger, variant: str = "cnn14") -> None:
        self.log = logger
        self.variant = variant
        self._model: Any = None
        self._labels: list[str] = []
        self._ready = False
        self._init()

    def _init(self) -> None:
        if not _panns_repo_available():
            self.log.info("PANNs repo not found - skipping")
            return
        if not _panns_weights_available(self.variant):
            self.log.info(f"PANNs {self.variant} weights not found - skipping")
            return
        try:
            import torch  # noqa: F401
        except ImportError:
            self.log.info("PyTorch not installed - PANNs disabled")
            return
        self.log.info(f"Loading PANNs {self.variant}...")
        try:
            self._load()
            self._ready = True
            self.log.ok(f"PANNs {self.variant} loaded")
        except Exception as e:
            self.log.error(f"PANNs load failed: {e}")

    def _load(self) -> None:
        import torch
        sys.path.insert(0, str(PANN_REPO))
        try:
            from audioset_tagging_cnn.pytorch.models import Cnn14
            from audioset_tagging_cnn.utils.config import parse_config
            checkpoint_path = PANN_REPO / PANN_CHECKPOINTS[self.variant]["filename"]  # noqa: F405
            checkpoint = torch.load(str(checkpoint_path), map_location="cpu")
            self._model = Cnn14(
                sample_rate=32000, window_size=1024,
                hop_size=320, mel_bins=64, fmin=50, fmax=14000,
                classes_num=527,
            )
            self._model.load_state_dict(checkpoint["model"])
            self._model.eval()
            # Load labels
            labels_path = PANN_REPO / "audioset_tagging_cnn" / "utils" / "labels.csv"
            if labels_path.exists():
                self._labels = [line.strip() for line in labels_path.read_text().splitlines()]
        finally:
            sys.path.pop(0)

    @property
    def ready(self) -> bool:
        return self._ready

    def classify(self, audio_path: Path, top_k: int = 5) -> list[dict[str, Any]]:
        import librosa
        import torch
        waveform, sr = librosa.load(str(audio_path), sr=32000, mono=True)
        # PANNs expects (batch, samples)
        x = torch.from_numpy(waveform).unsqueeze(0).float()
        with torch.no_grad():
            logits = self._model(x)
        probs = torch.sigmoid(logits).squeeze().numpy()
        top_indices = np.argsort(probs)[-top_k:][::-1]
        return [
            {
                "model": f"panns_{self.variant}",
                "label": self._labels[int(i)] if i < len(self._labels) else f"class_{int(i)}",
                "score": round(float(probs[int(i)]), 4),
            }
            for i in top_indices
        ]


# ------------------------------------------------------------------
# Main classifier
# ------------------------------------------------------------------

class AudioClassifier:
    """Unified audio classifier wrapping YAMNet + optional PANNs."""

    def __init__(
        self,
        logger: Optional[Logger] = None,
        use_yamnet: bool = True,
        use_panns: bool = True,
        panns_variant: str = "cnn14",
        dialogue_threshold: float = 0.3,
    ) -> None:
        self.log = logger or Logger(Level.INFO)
        self.dialogue_threshold = dialogue_threshold
        self.backends: list[Any] = []

        if use_yamnet:
            self._yamnet = _YamnetBackend(self.log)
            if self._yamnet.ready:
                self.backends.append(self._yamnet)
            else:
                self.log.warn("YAMNet not available - classification will be limited")
        else:
            self._yamnet = None

        if use_panns:
            self._panns = _PannsBackend(self.log, variant=panns_variant)
            if self._panns.ready:
                self.backends.append(self._panns)
        else:
            self._panns = None

        if not self.backends:
            raise RuntimeError(
                "No audio classification backends available.\n"
                "Run: python -m pkg_bnk_wwise_tools.setup_audio_models --setup-all"
            )

    def classify_file(self, audio_path: Path, top_k: int = 5) -> ClassificationResult:
        """Classify a single .ogg file."""
        if not audio_path.exists():
            return ClassificationResult(
                filename=audio_path.name,
                filepath=str(audio_path),
                error="File not found",
            )

        try:
            import librosa
            duration = librosa.get_duration(path=str(audio_path))
        except Exception:
            duration = 0.0

        all_classifications: list[dict[str, Any]] = []
        models_used: list[str] = []
        is_dialogue = False
        dialogue_confidence = 0.0

        for backend in self.backends:
            try:
                tags = backend.classify(audio_path, top_k=top_k)
                all_classifications.extend(tags)
                models_used.append(tags[0]["model"] if tags else backend.__class__.__name__)

                # Check for dialogue using YAMNet labels
                if hasattr(backend, "is_dialogue"):
                    dlg, conf = backend.is_dialogue(tags, self.dialogue_threshold)
                    if dlg and conf > dialogue_confidence:
                        is_dialogue = True
                        dialogue_confidence = conf
            except Exception as e:
                self.log.warn(f"Classification failed for {audio_path.name} with {backend}: {e}")

        return ClassificationResult(
            filename=audio_path.name,
            filepath=str(audio_path),
            classifications=all_classifications,
            is_dialogue=is_dialogue,
            dialogue_confidence=dialogue_confidence,
            duration_seconds=duration,
            models_used=models_used,
        )

    def classify_folder(
        self,
        folder: Path,
        pattern: str = "*.ogg",
        top_k: int = 5,
    ) -> list[ClassificationResult]:
        """Classify all matching audio files in a folder."""
        files = sorted(folder.glob(pattern))
        if not files:
            self.log.warn(f"No {pattern} files found in {folder}")
            return []

        self.log.info(f"Classifying {len(files)} file(s) in {folder.name}")
        results: list[ClassificationResult] = []
        dialogue_count = 0

        for i, fp in enumerate(files, 1):
            result = self.classify_file(fp, top_k=top_k)
            results.append(result)
            if result.is_dialogue:
                dialogue_count += 1
            # Progress every 50 files
            if i % 50 == 0 or i == len(files):
                self.log.info(f"  {i}/{len(files)} done ({dialogue_count} dialogue)")

        self.log.ok(f"Classification complete: {dialogue_count}/{len(files)} dialogue")
        return results

    def save(self, results: list[ClassificationResult], output_path: Path) -> None:
        """Save classification results to JSON."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "classifier_version": "1.0.0",
            "models_used": list({m for r in results for m in r.models_used}),
            "files": [r.to_dict() for r in results],
        }
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.log.ok(f"Saved classification to {output_path.name}")

    def load(self, json_path: Path) -> list[ClassificationResult]:
        """Load previously saved classification results."""
        data = json.loads(json_path.read_text(encoding="utf-8"))
        valid = {f.name for f in fields(ClassificationResult)}
        results: list[ClassificationResult] = []
        for item in data.get("files", []):
            try:
                results.append(ClassificationResult(**{k: v for k, v in item.items() if k in valid}))
            except Exception as e:
                self.log.warn(f"Skipping invalid classification entry: {e}")
        return results
