#!/usr/bin/env python3
"""
CLI for pkg_bnk_wwise_tools.

Commands:
    inspect     - Show every parsed field with hex dumps
    extract     - Extract WEMs (optionally convert to OGG)
    silence     - Replace a WEM with silence
    replace     - Replace a WEM with another file
    verify      - Round-trip verify a modified .pck
    test        - Run full pipeline test on a sample .pck

Examples:
    python -m pkg_bnk_wwise_tools inspect SFX_AntMan_INT.pck
    python -m pkg_bnk_wwise_tools extract SFX_AntMan_INT.pck -o ./out --to-ogg
    python -m pkg_bnk_wwise_tools silence SFX_AntMan_INT.pck 5 -o silenced.pck --verify
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .UTIL_logger import Level, Logger
from .PARSE_pck_bnk import PckBnkParser, ParseError
from .CONVERT_wem import WemConverter
from .MODIFY_repackager import Repackager, verify_round_trip
from .PCK_extract_named import NamedExtractor
from .TRANSCRIBE_dialogue import OggTranscriber
from .UI_transcription_dashboard import run_dashboard
from .DATA_project_pck_index import build_project_pck_index
from .PROCESS_batch_pipeline import run as run_batch_processor, PlaylistMaker
from .MODIFY_batch_silencer import run_batch_silence

__all__ = ["main", "build_parser"]


def _parser_for(cmd: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"pkg_bnk_wwise_tools {cmd}")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("-v", "--verbose", action="store_true", help="Show DEBUG/HEX logs")
    return p


def cmd_inspect(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    try:
        parser = PckBnkParser.from_file(args.pck, logger=logger)
        parser.parse()
    except ParseError as e:
        logger.error(str(e))
        return 1

    if args.json:
        print(json.dumps(parser.to_dict(), indent=2))
        return 0

    meta = parser.to_dict()
    logger.table(
        ("Field", "Value"),
        [
            ("File", meta["path"]),
            ("Size", f"{meta['file_size']:,}"),
            ("BNK offset", f"0x{meta['bnk_offset']:x}"),
            ("Bank version", str(meta["bank_version"])),
            ("Bank ID", meta["bank_id"]),
            ("WEM count", str(meta["wem_count"])),
        ],
    )
    print()
    rows = [(w["index"], w["wem_id"], w["offset"], w["length"]) for w in meta["wems"]]
    logger.table(("Idx", "WemId", "Offset", "Length"), rows)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    parser = PckBnkParser.from_file(args.pck, logger=logger)
    parser.parse()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Extract all WEMs
    wem_paths: list[Path] = []
    with logger.step("Extract WEMs"):
        for wem in parser.wems:
            name = f"{args.prefix}{wem.descriptor.wem_id:08x}_{wem.index}.wem"
            path = out_dir / name
            path.write_bytes(wem.data)
            wem_paths.append(path)
            logger.debug(f"Wrote {name} ({len(wem.data):,} bytes)")
        logger.ok(f"Extracted {len(wem_paths)} .wem file(s)")

    if args.to_ogg:
        converter = WemConverter(logger=logger)
        converter.convert_batch(wem_paths, out_dir)

    if args.save_mapping:
        mapping = {
            "source_pck": str(Path(args.pck).name),
            "extracted_at": datetime.now().isoformat(),
            "wems": [
                {
                    "index": wem.index,
                    "wem_id": f"{wem.descriptor.wem_id:08x}",
                    "filename_wem": f"{args.prefix}{wem.descriptor.wem_id:08x}_{wem.index}.wem",
                    "filename_ogg": f"{args.prefix}{wem.descriptor.wem_id:08x}_{wem.index}.ogg",
                    "offset": wem.descriptor.offset,
                    "length": wem.descriptor.length,
                    "is_loose": wem.is_loose,
                }
                for wem in parser.wems
            ],
        }
        map_path = out_dir / "mapping.json"
        map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
        logger.ok(f"Saved mapping to {map_path.name}")

    return 0


def cmd_extract_named(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    extractor = NamedExtractor(logger=logger)
    extractor.run(args.pck, args.output, to_ogg=args.to_ogg)
    return 0


def cmd_silence(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    parser = PckBnkParser.from_file(args.pck, logger=logger)
    parser.parse()

    idx = args.index
    if not (0 <= idx < len(parser.wems)):
        logger.error(f"Index {idx} out of range (0-{len(parser.wems)-1})")
        return 1

    wem_id = parser.wems[idx].descriptor.wem_id
    logger.info(f"Target WEM[{idx}]  id=0x{wem_id:08x}")

    repack = Repackager(parser, logger=logger)
    repack.silence_wem(idx)
    repack.write_pck(args.output)

    if args.verify:
        ok = verify_round_trip(
            Path(args.pck),
            Path(args.output),
            expected_mutations=[idx],
            logger=logger,
        )
        if not ok:
            return 1
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    parser = PckBnkParser.from_file(args.pck, logger=logger)
    parser.parse()

    idx = args.index
    new_data = Path(args.new_wem).read_bytes()
    logger.info(f"Replacing WEM[{idx}] with {len(new_data):,} bytes from {args.new_wem}")

    repack = Repackager(parser, logger=logger)
    repack.replace_wem(idx, new_data, reason="user-replace")
    repack.write_pck(args.output)

    if args.verify:
        ok = verify_round_trip(
            Path(args.pck),
            Path(args.output),
            expected_mutations=[idx],
            logger=logger,
        )
        if not ok:
            return 1
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    ok = verify_round_trip(Path(args.original), Path(args.modified), logger=logger)
    return 0 if ok else 1


def cmd_test(args: argparse.Namespace) -> int:
    """End-to-end pipeline test: parse -> extract -> silence -> verify."""
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    with logger.step("FULL PIPELINE TEST"):
        # Parse
        parser = PckBnkParser.from_file(args.pck, logger=logger)
        parser.parse()
        if not parser.wems:
            logger.error("No WEMs found")
            return 1

        # Extract first WEM
        out_dir = Path("test_output")
        out_dir.mkdir(exist_ok=True)
        first = parser.wems[0]
        wem_path = out_dir / f"{first.descriptor.wem_id:08x}_0.wem"
        wem_path.write_bytes(first.data)
        logger.ok(f"Extracted WEM[0] -> {wem_path.name}")

        # Convert to OGG
        if args.to_ogg:
            converter = WemConverter(logger=logger)
            ogg = converter.convert_single(wem_path, out_dir)
            if ogg:
                logger.ok(f"Conversion OK: {ogg.name}")

        # Silence last WEM
        last_idx = len(parser.wems) - 1
        repack = Repackager(parser, logger=logger)
        repack.silence_wem(last_idx)
        modified_path = out_dir / "modified.pck"
        repack.write_pck(modified_path)

        # Verify
        ok = verify_round_trip(
            Path(args.pck),
            modified_path,
            expected_mutations=[last_idx],
            logger=logger,
        )
        return 0 if ok else 1


def cmd_batch(args: argparse.Namespace) -> int:
    """Full pipeline for one .pck: extract -> ogg -> mapping -> transcribe -> playlist."""
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    pck_path = Path(args.pck)
    root_dir = Path(args.output)
    basename = pck_path.stem
    out_dir = root_dir / basename
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Parse & extract WEMs
    parser = PckBnkParser.from_file(str(pck_path), logger=logger)
    parser.parse()
    wem_paths: list[Path] = []
    with logger.step("Extract WEMs"):
        for wem in parser.wems:
            name = f"{wem.descriptor.wem_id:08x}_{wem.index}.wem"
            path = out_dir / name
            path.write_bytes(wem.data)
            wem_paths.append(path)
        logger.ok(f"Extracted {len(wem_paths)} .wem file(s)")

    # 2. Convert to OGG
    converter = WemConverter(logger=logger)
    converter.convert_batch(wem_paths, out_dir)

    # 3. Save mapping
    mapping = {
        "source_pck": basename,
        "extracted_at": datetime.now().isoformat(),
        "wems": [
            {
                "index": wem.index,
                "wem_id": f"{wem.descriptor.wem_id:08x}",
                "filename_wem": f"{wem.descriptor.wem_id:08x}_{wem.index}.wem",
                "filename_ogg": f"{wem.descriptor.wem_id:08x}_{wem.index}.ogg",
                "offset": wem.descriptor.offset,
                "length": wem.descriptor.length,
                "is_loose": wem.is_loose,
            }
            for wem in parser.wems
        ],
    }
    map_path = out_dir / "mapping.json"
    map_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    logger.ok(f"Saved mapping to {map_path.name}")

    # 4. Transcribe
    if not args.skip_transcribe:
        try:
            transcriber = OggTranscriber(args.model, logger=logger)
            transcriber.transcribe_folder(out_dir, out_dir / "transcription.json")
        except RuntimeError as e:
            logger.error(f"Transcription skipped: {e}")

    # 5. Playlist
    if not args.skip_playlist:
        try:
            maker = PlaylistMaker(logger=logger)
            maker.make_playlist(out_dir, out_dir / "review_playlist.wav")
        except RuntimeError as e:
            logger.error(f"Playlist skipped: {e}")

    logger.ok(f"Batch complete for {basename}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    try:
        build_project_pck_index(Path(args.root_dir), Path(args.output), logger=logger)
    except Exception as e:
        logger.error(str(e))
        return 1
    return 0


def cmd_process_all(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    lang_list = [s.strip().upper() for s in args.languages.split(",")]
    pack_list = [s.strip() for s in args.packs.split(",")] if args.packs else None
    return run_batch_processor(
        Path(args.source_dir),
        Path(args.output),
        args.model,
        stage=args.stage,
        force=args.force,
        dry_run=args.dry_run,
        languages=lang_list,
        use_classifier=not args.no_classifier,
        packs=pack_list,
        logger=logger,
    )


def cmd_setup_models(args: argparse.Namespace) -> int:
    from .TRANSCRIBE_sfx_classifier import (
        full_check,
        download_yamnet_class_map,
        clone_panns_repo,
        download_panns_weights,
    )
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    if args.download_yamnet_map or args.setup_all:
        download_yamnet_class_map(logger)
    if args.setup_panns or args.setup_all:
        clone_panns_repo(logger)
        download_panns_weights("cnn14", logger)
        download_panns_weights("mobilenetv2", logger)
    if args.check or args.setup_all or not any([args.download_yamnet_map, args.setup_panns, args.setup_all]):
        ok = full_check(logger)
        return 0 if ok else 1
    return 0


def cmd_playlist(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    try:
        maker = PlaylistMaker(logger=logger)
        maker.make_playlist(Path(args.ogg_dir), Path(args.output))
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    try:
        transcriber = OggTranscriber(args.model, logger=logger)
        transcriber.transcribe_folder(Path(args.ogg_dir), Path(args.output))
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    json_path = Path(args.json)
    if not json_path.exists():
        print(f"[ERR] File not found: {json_path}", file=sys.stderr)
        return 1
    run_dashboard(json_path)
    return 0


def cmd_batch_silence(args: argparse.Namespace) -> int:
    logger = Logger(Level.DEBUG if args.verbose else Level.INFO)
    try:
        run_batch_silence(
            Path(args.source_dir),
            Path(args.silence_json),
            output_dir=Path(args.output) if args.output else None,
            logger=logger,
        )
    except Exception as e:
        logger.error(str(e))
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pkg_bnk_wwise_tools",
        description="Wwise .pck / .bnk audio research toolkit",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # inspect
    p = sub.add_parser("inspect", help="Parse and inspect every field")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_inspect)

    # extract
    p = sub.add_parser("extract", help="Extract WEMs (optionally to OGG)")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("-o", "--output", default=".", help="Output directory")
    p.add_argument("--to-ogg", action="store_true", help="Convert to OGG")
    p.add_argument("--prefix", default="", help="Filename prefix")
    p.add_argument("--save-mapping", action="store_true", help="Write mapping.json for repackaging")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_extract)

    # extract-named
    p = sub.add_parser("extract-named", help="Extract with human-readable names (HIRC/STID)")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("-o", "--output", default=".", help="Output directory")
    p.add_argument("--to-ogg", action="store_true", help="Convert to OGG")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_extract_named)

    # silence
    p = sub.add_parser("silence", help="Silence a specific WEM")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("index", type=int, help="WEM index")
    p.add_argument("-o", "--output", required=True, help="Output .pck path")
    p.add_argument("--verify", action="store_true", help="Round-trip verify")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_silence)

    # replace
    p = sub.add_parser("replace", help="Replace a WEM with another file")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("index", type=int, help="WEM index")
    p.add_argument("new_wem", help="Replacement .wem file")
    p.add_argument("-o", "--output", required=True, help="Output .pck path")
    p.add_argument("--verify", action="store_true", help="Round-trip verify")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_replace)

    # verify
    p = sub.add_parser("verify", help="Round-trip verify two .pck files")
    p.add_argument("original", help="Original .pck")
    p.add_argument("modified", help="Modified .pck")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_verify)

    # process_all
    p = sub.add_parser("process_all", help="Incremental batch process all .pck files in a directory")
    p.add_argument("source_dir", help="Directory containing .pck files")
    p.add_argument("-o", "--output", default="extracted", help="Root output directory")
    p.add_argument("--model", default="./vosk-model-en-us-0.42-gigaspeech", help="Path to Vosk model")
    p.add_argument("--stage", type=int, default=0, choices=[0, 1, 15, 2, 3], help="0=auto, 1=extract, 15=classify, 2=transcribe, 3=index")
    p.add_argument("--languages", default="INT", help="Comma-separated language suffixes for Stage 2 (e.g. INT,DEU,FRA)")
    p.add_argument("--no-classifier", action="store_true", help="Skip Stage 1.5 audio classification and do not filter Stage 2 by it")
    p.add_argument("--packs", default="", help="Comma-separated pack name(s) to process (e.g. SFX_Angela_INT,SFX_ScarletWitch_INT). Default = all.")
    p.add_argument("--force", action="store_true", help="Reprocess existing folders")
    p.add_argument("--dry-run", action="store_true", help="Show what would be processed without running")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_process_all)

    # setup_models
    p = sub.add_parser("setup_models", help="Check and download audio classification model dependencies")
    p.add_argument("--setup-all", action="store_true", help="Download YAMNet map + clone PANNs repo + download weights")
    p.add_argument("--download-yamnet-map", action="store_true", help="Download YAMNet class map CSV")
    p.add_argument("--setup-panns", action="store_true", help="Clone PANNs repo and download weights")
    p.add_argument("--check", action="store_true", help="Run dependency check only")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logs")
    p.set_defaults(func=cmd_setup_models)

    # batch
    p = sub.add_parser("batch", help="Full pipeline: extract -> ogg -> map -> transcribe -> playlist")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("-o", "--output", default="extracted", help="Root output directory")
    p.add_argument("--model", default="./vosk-model-en-us-0.42-gigaspeech", help="Path to Vosk model")
    p.add_argument("--skip-transcribe", action="store_true", help="Skip Vosk transcription step")
    p.add_argument("--skip-playlist", action="store_true", help="Skip playlist creation step")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_batch)

    # index
    p = sub.add_parser("index", help="Build project_pck_index.json from all transcription.json files")
    p.add_argument("root_dir", help="Root directory containing per-.pck folders")
    p.add_argument("-o", "--output", default="project_pck_index.json", help="Output project PCK index path")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_index)

    # playlist
    p = sub.add_parser("playlist", help="Concatenate all .ogg files into one review WAV")
    p.add_argument("ogg_dir", help="Directory containing .ogg files")
    p.add_argument("-o", "--output", default="playlist.wav", help="Output WAV path")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_playlist)

    # transcribe
    p = sub.add_parser("transcribe", help="Transcribe extracted .ogg files with Vosk")
    p.add_argument("ogg_dir", help="Directory containing .ogg files")
    p.add_argument("--model", default="./vosk-model-en-us-0.42-gigaspeech", help="Path to Vosk model")
    p.add_argument("-o", "--output", default="transcription.json", help="Output JSON path")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_transcribe)

    # dashboard
    p = sub.add_parser("dashboard", help="Open tkinter dashboard to search transcriptions")
    p.add_argument("json", help="Transcription JSON file produced by 'transcribe'")
    p.set_defaults(func=cmd_dashboard)

    # batch_silence
    p = sub.add_parser("batch_silence", help="Batch silence designated WEMs in .pck files")
    p.add_argument("source_dir", help="Directory containing original .pck files")
    p.add_argument("silence_json", help="Path to silence_designations.json")
    p.add_argument("-o", "--output", help="Output directory (default: auto-dated)")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logs")
    p.set_defaults(func=cmd_batch_silence)

    # test
    p = sub.add_parser("test", help="Run full pipeline test")
    p.add_argument("pck", help="Input .pck file")
    p.add_argument("--to-ogg", action="store_true", help="Also test OGG conversion")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug/hex logs")
    p.set_defaults(func=cmd_test)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        import traceback
        print(f"[FATAL] {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
