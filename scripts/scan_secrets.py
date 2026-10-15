"""扫描准备提交的文件，发现疑似密钥时只报告位置和规则，不打印值。"""
from __future__ import annotations

import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = {".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".db"}
FORBIDDEN_NAMES = {".env", "secrets.toml", "credentials.json", "id_rsa", "id_ed25519"}
PATTERNS = {
    "OpenAI 风格密钥": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "私钥内容": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "带凭据数据库 URL": re.compile(r"(?:mysql|postgres(?:ql)?)://[^\s:/]+:[^\s@]{8,}@", re.I),
    "高熵敏感变量": re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|admin[_-]?token|password|secret)\s*[:=]\s*[\"']([^\"']{16,})[\"']"),
}
PLACEHOLDER = re.compile(r"(?i)^(?:test|fake|example|placeholder|change[-_ ]?me|your[-_ ]?|x+$|\$\{|<)")


def entropy(value: str) -> float:
    counts = Counter(value)
    size = len(value)
    return -sum((n / size) * math.log2(n / size) for n in counts.values())


def candidates() -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    )
    return [ROOT / p.decode("utf-8") for p in proc.stdout.split(b"\0") if p]


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in candidates():
        rel = path.relative_to(ROOT).as_posix()
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            findings.append((rel, 0, "禁止提交的凭据文件"))
            continue
        if path.suffix.lower() in BINARY or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    value = match.group(1) if match.lastindex else match.group(0)
                    if PLACEHOLDER.search(value) or (rule == "高熵敏感变量" and entropy(value) < 3.5):
                        continue
                    findings.append((rel, line_no, rule))
    if findings:
        print("疑似密钥扫描失败：")
        for rel, line_no, rule in findings:
            print(f"- {rel}:{line_no or '-'} [{rule}]")
        return 1
    print(f"密钥扫描通过：{len(candidates())} 个待提交文件，未发现疑似凭据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
