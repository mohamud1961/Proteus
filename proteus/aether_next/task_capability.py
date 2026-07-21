"""Generic task-surface capability classification for EnvMap/audit.

This module intentionally does not recognize benchmark task names.  It uses
visible instructions and visible file/material surfaces as a high-recall,
evidence-backed index of likely task capability requirements for the architect.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
import re


@dataclass(frozen=True)
class CapabilityNeed:
    capability: str
    confidence: str
    source: str
    required_tools: tuple[str, ...] = ()
    verifier_needs: tuple[str, ...] = ()
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["inferred_not_fact"] = True
        return data


_TOOL_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    "long_running_command": ("bash", "make", "cmake", "gcc", "g++", "cargo", "python", "python3"),
    "background_service": ("curl", "wget", "ss", "netstat", "lsof"),
    "http_service": ("curl", "wget", "nginx"),
    "ssh_or_telnet_service": ("ssh", "sshd", "telnet"),
    "qemu_vm": ("qemu-system-i386", "qemu-system-x86_64", "qemu-img", "expect"),
    "image_processing": ("python", "python3", "ffmpeg", "ffprobe", "tesseract", "pdftotext", "convert", "magick"),
    "video_processing": ("ffmpeg", "ffprobe", "python", "python3"),
    "ocr_pdf_document": ("tesseract", "pdftotext", "python", "python3", "convert", "magick"),
    "ml_training_or_inference": ("python", "python3", "pip", "uv", "R", "Rscript"),
    "scientific_computing": ("python", "python3", "R", "Rscript", "julia", "octave"),
    "database": ("sqlite3", "psql", "mysql"),
    "compiler_build": ("make", "cmake", "gcc", "g++", "clang", "clang++", "pkg-config"),
    "rust_build": ("cargo", "rustc"),
    "ocaml_coq_build": ("ocaml", "opam", "dune", "coqtop", "coqc"),
    "binary_reverse_engineering": ("file", "strings", "readelf", "objdump", "gdb", "xxd", "hexdump"),
    "crypto_security": ("openssl", "python", "python3", "john", "hashcat"),
    "network_download": ("curl", "wget", "git", "pip", "uv"),
    "text_log_data_transformation": ("python", "python3", "grep", "awk", "sed"),
    "query_semantic_data": ("python", "python3", "sqlite3"),
    "git_repository_repair": ("git", "python", "python3"),
    "web_security_sanitization": ("python", "python3", "node", "npm"),
    "password_hash_secret_recovery": ("python", "python3", "john", "hashcat", "7z"),
    "code_security_repair": ("python", "python3", "node", "npm", "grep"),
    "geometry_toolpath_extraction": ("python", "python3"),
}
_VERIFIER_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    "long_running_command": ("sandboxed_execution", "log_tail_handles"),
    "background_service": ("process_probe", "service_log_tail", "sandboxed_execution"),
    "http_service": ("http_probe", "port_probe", "sandboxed_execution"),
    "ssh_or_telnet_service": ("port_probe", "interactive_probe"),
    "qemu_vm": ("background_process_probe", "port_probe", "interactive_or_vnc_probe"),
    "image_processing": ("artifact_preview", "image_metadata", "sandboxed_execution"),
    "video_processing": ("sample_video_frames", "media_metadata", "sandboxed_execution"),
    "ocr_pdf_document": ("ocr_or_pdf_text_probe", "artifact_preview"),
    "ml_training_or_inference": ("sandboxed_execution", "metric_artifact_inspection"),
    "scientific_computing": ("sandboxed_execution", "numeric_output_check"),
    "database": ("database_file_or_service_probe", "sandboxed_execution"),
    "compiler_build": ("sandboxed_execution", "build_log_handles"),
    "rust_build": ("sandboxed_execution", "build_log_handles"),
    "ocaml_coq_build": ("sandboxed_execution", "build_log_handles"),
    "binary_reverse_engineering": ("binary_artifact_probe", "sandboxed_execution"),
    "crypto_security": ("sandboxed_execution", "artifact_probe"),
    "network_download": ("network_fact_probe",),
    "text_log_data_transformation": ("sandboxed_execution", "raw_input_sample_inspection"),
    "query_semantic_data": ("query_output_inspection", "sandboxed_execution"),
    "git_repository_repair": ("repository_state_inspection", "sandboxed_execution"),
    "web_security_sanitization": ("content_security_inspection", "fixture_replay"),
    "password_hash_secret_recovery": ("artifact_probe", "sandboxed_execution"),
    "code_security_repair": ("source_diff_inspection", "fixture_replay"),
    "geometry_toolpath_extraction": ("content_derivation_inspection", "artifact_preview"),
}

_KEYWORDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("video_processing", ("video", "mp4", "ffmpeg", "frame", "fps"), ("video-processing", "video")),
    ("image_processing", ("image", "png", "jpeg", "jpg", "render", "screenshot", "pixel", "segmentation"), ("images", "image")),
    ("ocr_pdf_document", ("ocr", "pdf", "document", "tesseract", "invoice", "receipt"), ("ocr", "pdf")),
    ("qemu_vm", ("qemu", "vnc", "virtual machine", "vm", "boot", "bios"), ("qemu", "vm")),
    ("background_service", ("daemon", "listen", "listening", "background process", "background service", "long-running service", "start a server", "run a server", "server running", "running on port", "runs on port", "serve requests", "grpc server", "grpc", "rpc calls", "pypi server"), ()),
    ("http_service", ("http", "https", "nginx", "webserver", "web server", "curl", "localhost", "port 80", "http endpoint", "web endpoint"), ()),
    ("ssh_or_telnet_service", ("ssh", "telnet", "login prompt"), ("ssh", "telnet")),
    ("ml_training_or_inference", ("train", "model", "neural", "torch", "tensorflow", "sklearn", "fasttext", "caffe", "inference"), ("machine-learning", "ml", "model")),
    ("scientific_computing", ("stan", "mcmc", "eigenvalue", "eigen value", "raman", "simulation", "numeric", "numerical", "numpy", "scipy", "biology", "dna", "protein", "assembly", "primer", "peak fitting", "curve fit", "least squares", "optimize", "optimization", "adaptive-rejection sampler", "rejection sampler", "density", "distribution", "stochastic", "bayesian network", "dag", "causal intervention"), ()),
    ("database", ("sqlite", "database", "sql", ".db"), ("database", "sql")),
    ("compiler_build", ("compile", "build", "make", "cmake", "gcc", "g++", "clang", "coverage", "gcov", "source code", "implement", "compiled program"), ("compiler", "build")),
    ("rust_build", ("cargo", "rustc", "rust"), ("rust",)),
    ("ocaml_coq_build", ("ocaml", "opam", "coq", "compcert", "dune"), ("ocaml", "coq")),
    ("binary_reverse_engineering", ("elf", "binary", "reverse", "disassemble", "objdump", "readelf", "mips"), ("binary", "reverse-engineering", "security")),
    ("crypto_security", ("openssl", "certificate", "crypto", "hash", "cipher", "cryptanalysis", "feal"), ("security", "crypto")),
    ("network_download", ("download", "pip install", "apt-get", "apt install", "git clone", "fetch external", "download from", "install package", "install packages", "install grpcio", "python packages", "--index-url"), ()),
    ("text_log_data_transformation", ("log", "logs", "csv", "tsv", "json", "jsonl", "summary", "summarize", "aggregate", "count", "counts", "date range", "parse text", "transform data", "extract rows"), ()),
    ("query_semantic_data", ("sparql", "rdf", "turtle", ".ttl", "knowledge graph", "semantic query", "query language"), ()),
    ("git_repository_repair", ("git", "repository", "commit", "branch", "merge conflict", "rebase", "cherry-pick", "restore changes", "checked out master", "merge them into master", "lost changes"), ()),
    ("web_security_sanitization", ("xss", "cross-site scripting", "sanitize html", "sanitizer", "remove javascript", "filter javascript", "script tag", "onclick", "javascript:"), ()),
    ("password_hash_secret_recovery", ("password", "hash", "secret", "credential", "leak", "7z hash", "7z archive", ".7z", "secret_file", "shadow file", "recover password", "secret key"), ()),
    ("code_security_repair", ("vulnerability", "vulnerable", "security bug", "injection", "exploit", "unsafe", "sanitize input", "analyze program", "secret key"), ()),
    ("geometry_toolpath_extraction", ("gcode", "g-code", "toolpath", "cnc", "extrusion path", "motion commands", "printer moves"), ()),
)


def _contains_phrase(text: str, phrase: str) -> bool:
    """Return true when *phrase* appears as tokens, not as a substring.

    Capability hints are model-facing substrate.  False positives are worse than
    missed weak hints, so matching is intentionally conservative.
    """
    clean = phrase.strip().lower()
    if not clean:
        return False
    if re.search(r"\s", clean):
        pattern = r"(?<![a-z0-9_])" + re.escape(clean).replace(r"\ ", r"\s+") + r"(?![a-z0-9_])"
    elif clean.startswith("."):
        pattern = re.escape(clean) + r"(?![a-z0-9_])"
    else:
        pattern = r"(?<![a-z0-9_])" + re.escape(clean) + r"(?![a-z0-9_])"
    return re.search(pattern, text.lower()) is not None

_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "image_processing": (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".ppm"),
    "video_processing": (".mp4", ".mov", ".avi", ".mkv", ".webm"),
    "ocr_pdf_document": (".pdf",),
    "database": (".db", ".sqlite", ".sqlite3", ".sql"),
    "compiler_build": (".c", ".cc", ".cpp", ".h", ".hpp", ".cmake", ".mk"),
    "rust_build": (".rs",),
    "ocaml_coq_build": (".ml", ".mli", ".v"),
    "scientific_computing": (".R", ".r", ".stan", ".npy", ".npz", ".mat"),
    "binary_reverse_engineering": (".elf", ".bin", ".o", ".so", ".a"),
    "geometry_toolpath_extraction": (".gcode", ".gco", ".nc"),
    "text_log_data_transformation": (".log", ".csv", ".tsv", ".jsonl"),
    "query_semantic_data": (".ttl", ".rdf", ".sparql"),
}


def flatten_task_toml(task_toml: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(task_toml or {})
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    agent = data.get("agent") if isinstance(data.get("agent"), Mapping) else {}
    verifier = data.get("verifier") if isinstance(data.get("verifier"), Mapping) else {}
    environment = data.get("environment") if isinstance(data.get("environment"), Mapping) else {}
    tags = tuple(str(tag) for tag in metadata.get("tags", ()) if str(tag).strip())
    return {
        "task_version": data.get("version", ""),
        "category": str(metadata.get("category", "")),
        "difficulty": str(metadata.get("difficulty", "")),
        "tags": tags,
        "expert_time_estimate_min": metadata.get("expert_time_estimate_min"),
        "junior_time_estimate_min": metadata.get("junior_time_estimate_min"),
        "agent_timeout_sec": agent.get("timeout_sec"),
        "verifier_timeout_sec": verifier.get("timeout_sec"),
        "environment": dict(environment),
        "resource_budget": {
            "agent_timeout_sec": agent.get("timeout_sec"),
            "verifier_timeout_sec": verifier.get("timeout_sec"),
            "build_timeout_sec": environment.get("build_timeout_sec"),
            "cpus": environment.get("cpus"),
            "memory": environment.get("memory"),
            "storage": environment.get("storage"),
            "docker_image": environment.get("docker_image"),
        },
    }


def classify_capability_needs(
    instruction_text: str,
    *,
    task_metadata: Mapping[str, Any] | None = None,
    visible_files: Iterable[str] = (),
) -> tuple[CapabilityNeed, ...]:
    metadata = task_metadata or {}
    # Model-facing capability needs must be inferred from visible task/workspace
    # material, not benchmark category/tags. Resource budgets below remain useful
    # because they are runtime constraints rather than semantic task labels.
    tags: tuple[str, ...] = ()
    haystack = instruction_text.lower()
    by_capability: dict[str, CapabilityNeed] = {}

    def add(capability: str, confidence: str, source: str, notes: str = "") -> None:
        existing = by_capability.get(capability)
        if existing is not None and existing.confidence == "high":
            return
        by_capability[capability] = CapabilityNeed(
            capability=capability,
            confidence=confidence,
            source=source if existing is None else existing.source + "+" + source,
            required_tools=_TOOL_BY_CAPABILITY.get(capability, ()),
            verifier_needs=_VERIFIER_BY_CAPABILITY.get(capability, ()),
            notes=notes,
        )

    for capability, words, tag_words in _KEYWORDS:
        # Token/phrase-aware matching prevents substring pollution such as
        # report→port, instance→stan, or self-signed→elf.
        if any(_contains_phrase(haystack, word) for word in words):
            add(capability, "medium", "visible_instruction")

    lower_files = [str(path).lower() for path in visible_files]
    for capability, extensions in _EXTENSIONS.items():
        if any(path.endswith(extensions) or Path(path).name.lower() in {"makefile", "cmakelists.txt", "cargo.toml", "dune", "coq_makefile"} for path in lower_files):
            add(capability, "medium", "visible_file_extensions")

    return tuple(sorted(by_capability.values(), key=lambda item: item.capability))


def required_tool_hints(needs: Iterable[CapabilityNeed], existing_tool_hints: Iterable[str] = ()) -> tuple[str, ...]:
    tools: list[str] = []
    seen: set[str] = set()
    for tool in existing_tool_hints:
        name = str(tool).strip()
        if name and name not in seen:
            seen.add(name); tools.append(name)
    for need in needs:
        for tool in need.required_tools:
            if tool and tool not in seen:
                seen.add(tool); tools.append(tool)
    return tuple(tools)
