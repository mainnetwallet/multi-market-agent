from __future__ import annotations

import re

from mm_agent.util import redact, redact_text


def test_redacts_private_key_and_tokens() -> None:
    secret = "0x" + "ab" * 32
    pem = "-----BEGIN PRIVATE KEY-----\nMIIBVwIBADANBg\n-----END PRIVATE KEY-----"
    telegram = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    text = f"key={secret} pem={pem} tg={telegram} sk-sk-1234567890abcdef Bearer abcdefghijklmnop"
    scrubbed = redact_text(text)
    assert secret not in scrubbed
    assert "MIIBVwIBADANBg" not in scrubbed
    assert telegram not in scrubbed
    assert "sk-sk-1234567890abcdef" not in scrubbed
    assert "Bearer abcdefghijklmnop" not in scrubbed


def test_redacts_mapping_keys() -> None:
    payload = {
        "password": "hunter2",
        "api_secret": "topsecret",
        "nested": {"authorization": "Bearer xyz", "safe": "hello"},
        "api_key_id": "not-secret-id",
    }
    result = redact(payload)
    assert result["password"] == "[REDACTED]"
    assert result["api_secret"] == "[REDACTED]"
    assert result["nested"]["authorization"] == "[REDACTED]"
    assert result["nested"]["safe"] == "hello"
    assert result["api_key_id"] == "not-secret-id"


def test_repo_has_no_secret_patterns() -> None:
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    allowed_suffixes = {".py", ".toml", ".md", ".example", ".yml", ".yaml", ".json", ".html", ".js", ".ini"}
    # Build output is not source, and its paths can break mid-build, so prune it
    # before walking rather than filtering files afterwards.
    skip_dirs = {".venv", ".git", "build", ".gradle", "node_modules", ".pytest_cache", ".mypy_cache"}
    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        paths.extend(Path(dirpath) / name for name in filenames)
    for path in paths:
        if not path.is_file():
            continue
        if "fixtures" in path.parts:
            continue
        if path.name == ".env":
            # Local, gitignored secrets file; must never be tracked.
            ignored = (root / ".gitignore").read_text(errors="ignore")
            if ".env" not in ignored:
                offenders.append(str(path))
            continue
        if path.suffix not in allowed_suffixes and path.name != ".env.example":
            continue
        text = path.read_text(errors="ignore")
        if re.search(r"0x[a-fA-F0-9]{64}", text):
            offenders.append(str(path))
        if "BEGIN PRIVATE KEY" in text and path.name != "test_security.py":
            offenders.append(str(path))
    assert not offenders, offenders
