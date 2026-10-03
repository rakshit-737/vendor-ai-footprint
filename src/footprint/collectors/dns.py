"""DNS collector: TXT / CNAME / MX (+SPF) via DNS-over-HTTPS from two resolvers that must agree.

Emits one Document (kind ``dns``) per domain with the normalised record text, and an AI verification-token
summary. Tokens prove a relationship only (design 2.3).
"""

from __future__ import annotations

import json
import re

from footprint.collectors.base import CollectContext, make_text_document, vendor_domains
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

RESOLVERS: tuple[str, ...] = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")
RECORD_TYPES: tuple[str, ...] = ("TXT", "CNAME", "MX")
DNS_JSON = "application/dns-json"

# Provider -> TXT verification token regex. Google has no AI-specific token (google-site-verification is generic).
AI_TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "OpenAI": re.compile(r"^openai-domain-verification=", re.I),
    "Anthropic": re.compile(r"^anthropic-domain-verification-[\w-]*=?", re.I),
    "Perplexity": re.compile(r"^perplexity-site-verification=", re.I),
    "Mistral": re.compile(r"^mistral-domain-verification=", re.I),
    "Cohere": re.compile(r"^cohere-domain-verification=", re.I),
    "Cursor": re.compile(r"^cursor-domain-verification=", re.I),
}


def find_ai_tokens(txt_records: list[str]) -> list[tuple[str, str]]:
    """[(provider, record)] for TXT records that are AI-provider domain verification tokens."""
    out = []
    for rec in txt_records:
        r = rec.strip().strip('"')
        for provider, pat in AI_TOKEN_PATTERNS.items():
            if pat.search(r):
                out.append((provider, r))
    return out


_CHAR_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ESCAPE = re.compile(rb"\\(\d{3}|.)", re.S)
_PRESENTATION = re.compile(r'(?:"(?:[^"\\]|\\.)*"\s*)+')


def unescape_txt(presentation: str) -> str:
    """Decode RFC 1035 presentation-format TXT data: ``"part1" "part2"`` joined, ``\\DDD`` decimal byte escapes
    and ``\\X`` literal escapes resolved, bytes read as UTF-8 (Cloudflare's DoH answers look like this)."""
    parts = _CHAR_STRING.findall(presentation)
    raw = "".join(parts).encode("utf-8")
    out = _ESCAPE.sub(lambda m: bytes([int(m.group(1)) & 0xFF]) if m.group(1).isdigit() else m.group(1), raw)
    return out.decode("utf-8", errors="replace")


def parse_doh(raw: bytes, rtype: str) -> list[str] | None:
    """Sorted record data from a DoH JSON answer; None if unparseable. NXDOMAIN/no data -> [].

    TXT data is normalised so both resolvers compare equal: Google answers with decoded, unquoted text; Cloudflare
    with quoted character-strings in presentation format (see ``unescape_txt``).
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    want = {"TXT": 16, "CNAME": 5, "MX": 15}[rtype]
    vals = []
    for ans in data.get("Answer", []) or []:
        if ans.get("type") != want:
            continue
        d = str(ans.get("data", ""))
        if rtype == "TXT":
            if _PRESENTATION.fullmatch(d.strip()):
                d = unescape_txt(d.strip())
        vals.append(d.rstrip(".") if rtype != "TXT" else d)
    return sorted(set(vals))


class DnsCollector:
    name = "dns"
    family = SourceFamily.DNS

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        fp = plan.family(SourceFamily.DNS)
        return bool(fp and fp.cap > 0 and vendor_domains(profile, seeds))

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        domains = vendor_domains(ctx.profile, ctx.seeds)
        if not domains:
            res.coverage.append(ctx.entry(self.family, CoverageStatus.NOT_APPLICABLE, self.name, note="no domain"))
            return res
        for domain in domains:
            self._domain(ctx, domain, res)
        return res

    def _domain(self, ctx: CollectContext, domain: str, res: CollectorResult) -> None:
        records: dict[str, list[str]] = {}
        disagreements: list[str] = []
        failures: list[str] = []
        used = 0
        first_capture = None
        for rtype in RECORD_TYPES:
            answers: list[list[str]] = []
            for base in RESOLVERS:
                url = f"{base}?name={domain}&type={rtype}"
                out = ctx.fetch(url, self.family, self.name, accept=DNS_JSON)
                if out.reason != "cap_reached":
                    used += 1
                if out.capture is not None:
                    res.captures.append(out.capture)
                    first_capture = first_capture or out.capture
                parsed = parse_doh(ctx.raw(out), rtype) if out.ok else None
                if parsed is None:
                    failures.append(f"{rtype}@{base.split('/')[2]}:{out.reason or 'unparseable'}")
                else:
                    answers.append(parsed)
            if len(answers) == 2 and answers[0] != answers[1]:
                disagreements.append(rtype)
                records[rtype] = sorted(set(answers[0]) | set(answers[1]))
            elif answers:
                records[rtype] = answers[0]
        txt = records.get("TXT", [])
        spf = [t for t in txt if t.lower().startswith("v=spf1")]
        tokens = find_ai_tokens(txt)
        lines = [f"DNS records for {domain} (DoH: dns.google, cloudflare-dns.com)"]
        for rtype in RECORD_TYPES:
            for v in records.get(rtype, []):
                lines.append(f"{rtype}\t{v}")
        for s in spf:
            lines.append(f"SPF\t{s}")
        for provider, rec in tokens:
            lines.append(f"AI_TOKEN\t{provider}\t{rec}")
        if disagreements:
            lines.append("RESOLVER_DISAGREEMENT\t" + ",".join(disagreements))
        doc_count = 0
        if first_capture is not None and records:
            res.documents.append(
                make_text_document(ctx, first_capture, "\n".join(lines) + "\n", kind="dns",
                                   title=f"DNS {domain}", extractor="footprint.dns 1")
            )
            doc_count = 1
        if not records:
            status = CoverageStatus.STOPPED if any("cap_reached" in f for f in failures) else CoverageStatus.ERROR
        elif disagreements:
            status = CoverageStatus.ERROR
        else:
            status = CoverageStatus.DONE
        tok = ", ".join(sorted({p for p, _ in tokens})) or "none"
        note = f"AI verification tokens: {tok}; SPF {len(spf)}; MX {len(records.get('MX', []))}"
        if disagreements:
            note += f"; resolvers disagree on {','.join(disagreements)}"
        if failures:
            note += f"; failures: {'; '.join(failures)}"
        res.coverage.append(ctx.entry(self.family, status, self.name, endpoint=f"DoH TXT/CNAME/MX {domain}",
                                      requests_used=used, documents=doc_count, ai_passages=len(tokens), note=note))
