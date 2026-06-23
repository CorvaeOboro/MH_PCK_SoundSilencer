"""
pkg_bnk_wwise_tools - Python toolkit for Wwise .pck / .bnk audio research.

Modules:
    UTIL_logger              - Colored terminal logging
    PARSE_pck_bnk            - Parse .pck -> BNK -> DIDX -> WEM with step logging
    CONVERT_wem              - WEM -> OGG via ww2ogg + revorb
    MODIFY_repackager        - Replace / silence WEMs + round-trip verification
    PROCESS_batch_pipeline   - Incremental pipeline (extract / classify / transcribe)
    MODIFY_batch_silencer    - Batch silence designated WEMs in-place
    TRANSCRIBE_sfx_classifier - YAMNet/PANNs audio classification
    TRANSCRIBE_dialogue     - Vosk speech-to-text for .ogg files
    UI_transcription_dashboard - tkinter GUI for review & silence designation
    DATA_silence_manager     - JSON persistence for silence designations
    PCK_extract_named        - HIRC/STID named audio extraction
    DATA_project_pck_index   - Build merged project PCK index
    CONFIG_tools             - External tool path configuration
    CLI_main                 - Unified command-line interface
"""

from pathlib import Path

__all__ = ["PACKAGE_DIR", "__version__"]

PACKAGE_DIR = Path(__file__).parent.resolve()

__version__ = "1.0.0"
