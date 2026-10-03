"""Duplicates (V9), origin clusters and corroboration links (design Appendix A 2.6-2.7; contract:
docs/contracts_p3.md section 6).

- :func:`dedupe` (V9) keeps one item per item key and one per overlapping span of a document. The best item wins:
  a citable item over one that is not *(interpretation: a definition-test trap or a rejected item never displaces
  citable evidence)*, then STRENGTH_ORDER, then the method (rule+llm_agree > adjudicated > rule >
  llm_proposed_accepted), then the longer excerpt, then the smaller item key. It is greedy: an item is dropped only
  when it overlaps an item already kept.
- :func:`same_origin` (design 2.7): rapidfuzz ``token_set_ratio`` of the normalised excerpts is SIMILARITY or more,
  or the titles match: after normalisation and case folding they share the same words, at least TITLE_MIN_WORDS of
  them (shorter titles are too generic). *(Interpretation)* A title also matches on one of its parts split at " | ",
  " - " (any dash), " · ", " • " or " :: ", so a release headline carried by a partner's or a wire's page under its
  own site suffix ("... | Example Wire") still matches. Two excerpts of one document (same ``doc_id``) or one page
  (same URL, ignoring the scheme, "www.", a trailing slash and the fragment) are one origin too. Items of different
  vendors never are.
  *(Interpretation)* A DNS record is its own origin: every TXT record of one DoH answer shares the document, the URL
  and the title "DNS <domain>", but each verification token proves a separate relationship. Two DNS items share an
  origin only when they name the same provider (or, with no provider, carry the same record text), and a DNS item
  never shares an origin with a non-DNS item.
- :func:`assign_clusters` takes the transitive closure (union-find), so partner mirrors of one release form one
  cluster even when only some of their excerpts match. ``cluster_id`` = the smallest member item key, 12 characters.
- :func:`independent` (design 2.7): another cluster AND another publisher (case-insensitive) or another source family.
  *(Interpretation)* A vendor-origin copy (``rules.vendor_origin_types``: a press-release mirror, a partner's
  press-room copy of a joint release) carries the vendor's own words, and its original may be any of the vendor's
  channels (a release is often also filed as an 8-K exhibit), so its origin is the vendor: it is independent only of
  a third-party source (family IND, not itself a vendor-origin copy) of another publisher, never of the vendor's own
  filings, pages, postings, DNS records or executive statements, nor of another vendor release.
- :func:`distinct_sources` counts corroboration once per source: it keeps an item only when it is independent of
  every item kept before it, so syndicated copies, two articles of one outlet and a mirror of the vendor's own
  release beside the vendor's filing each count once.
- :func:`link_corroboration` gives each K candidate the sorted keys of the Q and K candidates independent of it,
  sets IC 1 on every item some K candidate corroborates, and IC 5 on a U1-U4 item contradicted by a citable SR A/B
  U8 at R3 dated the same day or later (5 wins over 1). It recomputes from scratch, so it is idempotent: links of
  items that are no longer K candidates are cleared, and a stale IC 1 or 5 returns to the rules' initial IC.

Q and K are ``rules.is_qualifying`` and ``rules.is_corroborating`` when footprint.rules is installed; otherwise the
contract's definitions (:func:`_is_q`, :func:`_is_k`) are used, so this module also works on its own.
"""

from __future__ import annotations

import importlib
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import NamedTuple
from urllib.parse import urlsplit

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from footprint.models import QUALIFYING_LOCI, STRENGTH_ORDER, EvidenceItem, SourceFamily
from footprint.verify import normalise

SIMILARITY = 90
"""rapidfuzz token_set_ratio at or above which two excerpts share an origin (design 2.7)."""
TITLE_MIN_WORDS = 4
METHOD_RANK: dict[str, int] = {"rule+llm_agree": 0, "adjudicated": 1, "rule": 2, "llm_proposed_accepted": 3}
"""Tie-break between duplicates of equal strength: lower is better."""

Predicate = Callable[[EvidenceItem], bool]

_RULES = "footprint.rules"
_WORD = re.compile(r"\w+")
# Title separators after normalisation (every dash is "-" by then): " | ", " - ", " -- ", " · ", " • ", " :: ".
_TITLE_SEPARATOR = re.compile(r"\s+(?:\||-{1,2}|\u00b7|\u2022|::)\s+")


def _rules() -> ModuleType | None:
    """footprint.rules when it is installed, else None. Errors raised inside an existing rules module propagate."""
    try:
        return importlib.import_module(_RULES)
    except ModuleNotFoundError as exc:
        if exc.name == _RULES:
            return None
        raise


# --------------------------------------------------------------------------- V9


def _rank(item: EvidenceItem) -> tuple[bool, int, int, int, str]:
    return (not item.citable, STRENGTH_ORDER.index(item.strength), METHOD_RANK.get(item.method, len(METHOD_RANK)),
            -len(item.excerpt), item.item_key)


def dedupe(items: Sequence[EvidenceItem], *, log: list[str] | None = None) -> list[EvidenceItem]:
    """V9: drop repeated item keys and overlapping spans of one document, keeping the best item of each.

    Comparisons stay within one vendor. The kept items are returned in input order, and ``log`` gets
    ``"V9:<item_key>"`` for each removed item, in input order.
    """
    items = list(items)
    kept_keys: set[tuple[str, str]] = set()
    kept_spans: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    removed: set[int] = set()
    for i in sorted(range(len(items)), key=lambda n: _rank(items[n])):
        item = items[i]
        spans = kept_spans[(item.vendor_id, item.doc_id)]
        if (item.vendor_id, item.item_key) in kept_keys or any(s < item.end and item.start < e for s, e in spans):
            removed.add(i)
            continue
        kept_keys.add((item.vendor_id, item.item_key))
        spans.append((item.start, item.end))
    if log is not None:
        log.extend(f"V9:{items[i].item_key}" for i in sorted(removed))
    return [item for i, item in enumerate(items) if i not in removed]


# --------------------------------------------------------------------------- origin


class _Origin(NamedTuple):
    vendor: str
    doc: str
    page: str
    text: str
    titles: frozenset[str]
    dns: str = ""
    """Empty for a non-DNS item; else the record's origin key (its providers, or its record text)."""


def _page_key(url: str) -> str:
    """A URL without scheme, "www.", trailing slash or fragment (one page captured twice keeps one key)."""
    url = url.strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return url.casefold()
    host = host.removeprefix("www.")
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{f':{port}' if port else ''}{parts.path.rstrip('/')}{query}"


def title_keys(title: str) -> frozenset[str]:
    """The keys a title matches on: its words (normalised, case-folded) and those of each part between title
    separators, each kept only when it has TITLE_MIN_WORDS words or more."""
    norm = normalise(title)[0].casefold()
    keys: set[str] = set()
    for part in (norm, *_TITLE_SEPARATOR.split(norm)):
        words = _WORD.findall(part)
        if len(words) >= TITLE_MIN_WORDS:
            keys.add(" ".join(words))
    return frozenset(keys)


def _dns_key(item: EvidenceItem) -> str:
    """A DNS record's origin: the providers it verifies (case-folded), else its normalised record text."""
    providers = sorted({p.casefold() for p in item.providers if p.strip()})
    if providers:
        return "provider:" + ";".join(providers)
    return "record:" + " ".join(normalise(item.excerpt)[0].casefold().split())


def _origin(item: EvidenceItem) -> _Origin:
    text = default_process(normalise(item.excerpt)[0].casefold())
    return _Origin(
        vendor=item.vendor_id, doc=item.doc_id, page=_page_key(item.url), text=text, titles=title_keys(item.title),
        dns=_dns_key(item) if item.family == SourceFamily.DNS else "",
    )


def _same(a: _Origin, b: _Origin) -> bool:
    if a.vendor != b.vendor:
        return False
    if a.dns or b.dns:  # one DoH answer holds many records: one origin per provider (or record), never per page
        return a.dns == b.dns
    if (a.doc and a.doc == b.doc) or (a.page and a.page == b.page):
        return True
    if not a.titles.isdisjoint(b.titles):
        return True
    return bool(a.text and b.text) and fuzz.token_set_ratio(a.text, b.text, score_cutoff=SIMILARITY) >= SIMILARITY


def same_origin(a: EvidenceItem, b: EvidenceItem) -> bool:
    """True when the two items repeat one origin (design 2.7; see the module docstring for the tests)."""
    if a.vendor_id != b.vendor_id:
        return False
    return a.item_key == b.item_key or _same(_origin(a), _origin(b))


def assign_clusters(items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    """Set ``cluster_id`` on every item: union-find over :func:`same_origin` within each vendor.

    The cluster id is the smallest item key among the members, first 12 characters, so it does not depend on the
    input order. Items are returned in input order and are otherwise unchanged.
    """
    items = list(items)
    origins = [_origin(item) for item in items]
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_vendor: dict[str, list[int]] = defaultdict(list)
    for i in sorted(range(len(items)), key=lambda n: (items[n].item_key, n)):
        by_vendor[items[i].vendor_id].append(i)
    for members in by_vendor.values():
        for pos, i in enumerate(members):
            for j in members[pos + 1 :]:
                ri, rj = find(i), find(j)
                if ri != rj and (items[i].item_key == items[j].item_key or _same(origins[i], origins[j])):
                    parent[max(ri, rj)] = min(ri, rj)
    smallest: dict[int, str] = {}
    for i, item in enumerate(items):
        root = find(i)
        if root not in smallest or item.item_key < smallest[root]:
            smallest[root] = item.item_key
    return [item.model_copy(update={"cluster_id": smallest[find(i)][:12]}) for i, item in enumerate(items)]


def _publisher(name: str) -> str:
    return " ".join(normalise(name)[0].casefold().split())


_VENDOR_ORIGIN_TYPES: frozenset[str] = frozenset({"Press release mirror", "Partner press release"})
"""The vendor-origin source types when footprint.rules is missing (config/sources.toml ``origin = "vendor"``)."""


def _vendor_origin_types() -> frozenset[str]:
    rules = _rules()
    found = getattr(rules, "vendor_origin_types", None) if rules is not None else None
    return found() if found is not None else _VENDOR_ORIGIN_TYPES


def vendor_copy(item: EvidenceItem) -> bool:
    """True when a third party hosts the vendor's own words (a press-release mirror, a partner's press-room copy
    of a joint release; ``rules.vendor_origin_types``), so the item's origin is the vendor."""
    return item.source_type in _vendor_origin_types()


def independent(a: EvidenceItem, b: EvidenceItem) -> bool:
    """Design 2.7: a different origin cluster AND a different publisher or source family.

    Uses ``cluster_id`` when both items have one, else :func:`same_origin`. An item is never independent of itself
    or of another vendor's item. *(Interpretation)* A vendor-origin copy (:func:`vendor_copy`) is independent only
    of a third-party item (family IND, not itself a vendor-origin copy) of another publisher: its words are the
    vendor's, so neither another publisher's name on the page nor its IND family makes it independent of the vendor.
    """
    if a.vendor_id != b.vendor_id or a.item_key == b.item_key:
        return False
    if a.cluster_id and b.cluster_id:
        if a.cluster_id == b.cluster_id:
            return False
    elif same_origin(a, b):
        return False
    copy_a, copy_b = vendor_copy(a), vendor_copy(b)
    if copy_a or copy_b:
        if copy_a and copy_b:
            return False  # two vendor releases: one origin, the vendor
        other = b if copy_a else a
        return other.family == SourceFamily.IND and _publisher(a.publisher) != _publisher(b.publisher)
    return _publisher(a.publisher) != _publisher(b.publisher) or a.family != b.family


def distinct_sources(items: Sequence[EvidenceItem], *, prefer: Sequence[EvidenceItem] = ()) -> list[EvidenceItem]:
    """One item per independent source, for counting corroboration (syndicated copies count once).

    Walks the ``prefer`` items that are among ``items`` first, then ``items`` in order, and keeps an item only when
    it is :func:`independent` of every item kept so far: two articles of one outlet, two copies of one release, or a
    vendor release beside the vendor's own filing count once. Returns the kept items in input order. Pass the items
    best first so that the best of each source is kept.
    """
    items = list(items)
    wanted = {i.item_key for i in items}
    kept: list[EvidenceItem] = []
    for item in [*(p for p in prefer if p.item_key in wanted), *items]:
        if all(item.item_key != k.item_key and independent(item, k) for k in kept):
            kept.append(item)
    keys = {k.item_key for k in kept}
    out: list[EvidenceItem] = []
    for item in items:
        if item.item_key in keys:
            out.append(item)
            keys.discard(item.item_key)
    return out


# --------------------------------------------------------------------------- corroboration


def _is_q(item: EvidenceItem) -> bool:
    """Q (design 2.7) when footprint.rules is missing: citable, SR A/B, SP >= S2, RL >= R2, RC >= T1, U1-U4, and a
    qualifying locus."""
    t = item.tags
    return (item.citable and t.sr in ("A", "B") and t.sp_level >= 2 and t.rl_level >= 2 and t.rc_level >= 1
            and t.u_number <= 4 and t.locus in QUALIFYING_LOCI)


def _is_k(item: EvidenceItem) -> bool:
    """An item's own part of K (design 2.7) when footprint.rules is missing: citable, SR A-C, SP >= S2, RL >= R2,
    RC >= T1, U1-U4."""
    t = item.tags
    return (item.citable and t.sr in ("A", "B", "C") and t.sp_level >= 2 and t.rl_level >= 2 and t.rc_level >= 1
            and t.u_number <= 4)


def _predicates(is_q: Predicate | None, is_k: Predicate | None) -> tuple[Predicate, Predicate]:
    rules = _rules() if is_q is None or is_k is None else None
    q = is_q or getattr(rules, "is_qualifying", None) or _is_q
    k = is_k or getattr(rules, "is_corroborating", None) or _is_k
    return q, k


def _initial_ic(item: EvidenceItem) -> int:
    """The IC the rules give an item before corroboration: ``rules.retag``, else the contract's IC table.

    ``rules.retag`` keeps an IC of 1 or 5 (those come from here), so it is given the item with a neutral IC.
    """
    rules = _rules()
    retag = getattr(rules, "retag", None) if rules is not None else None
    if retag is not None:
        return retag(item.model_copy(update={"tags": item.tags.model_copy(update={"ic": 2})})).tags.ic
    t = item.tags
    if item.family == SourceFamily.DNS and t.u_class == "U4" and t.locus == "relationship":
        return 2  # a DNS verification token is a direct record (contract section 3, tag_document)
    if t.rc == "T0" or t.sr in ("E", "F"):
        return 6
    if t.sp in ("S0", "S1"):
        return 3
    if t.sr in ("C", "D"):
        return 4
    return 2


def _date(item: EvidenceItem) -> str:
    return item.published or item.retrieved_at[:10]


def _limiting(item: EvidenceItem) -> bool:
    """A citable SR A/B limiting statement (U8) about the exact service (R3): it can contradict a usage item."""
    t = item.tags
    return item.citable and t.u_class == "U8" and t.sr in ("A", "B") and t.rl == "R3"


def link_corroboration(items: Sequence[EvidenceItem], *, is_q: Predicate | None = None,
                       is_k: Predicate | None = None) -> list[EvidenceItem]:
    """Set ``corroborates`` and the corroboration IC (1 or 5) on every item; see the module docstring.

    ``is_q`` and ``is_k`` default to ``rules.is_qualifying`` and ``rules.is_corroborating``. Run it after
    :func:`assign_clusters` (without cluster ids, independence falls back to :func:`same_origin`). Items are returned
    in input order; only ``corroborates`` and ``tags.ic`` change.
    """
    items = list(items)
    q_of, k_of = _predicates(is_q, is_k)
    candidate = [bool(k_of(item)) for item in items]
    target = [candidate[n] or bool(q_of(item)) for n, item in enumerate(items)]
    links: list[list[str]] = []
    corroborated: set[tuple[str, str]] = set()
    for n, item in enumerate(items):
        keys: list[str] = []
        if candidate[n]:
            keys = sorted({other.item_key for m, other in enumerate(items)
                           if m != n and target[m] and independent(item, other)})
            corroborated.update((item.vendor_id, key) for key in keys)
        links.append(keys)
    limiting = [item for item in items if _limiting(item)]
    out: list[EvidenceItem] = []
    for n, item in enumerate(items):
        ic = item.tags.ic
        if item.tags.u_number <= 4 and any(
                neg.vendor_id == item.vendor_id and _date(neg) >= _date(item) for neg in limiting):
            ic = 5
        elif (item.vendor_id, item.item_key) in corroborated:
            ic = 1
        elif ic in (1, 5):
            ic = _initial_ic(item)
        update: dict[str, object] = {"corroborates": links[n]}
        if ic != item.tags.ic:
            update["tags"] = item.tags.model_copy(update={"ic": ic})
        out.append(item.model_copy(update=update))
    return out


__all__ = [
    "METHOD_RANK",
    "SIMILARITY",
    "TITLE_MIN_WORDS",
    "assign_clusters",
    "dedupe",
    "distinct_sources",
    "independent",
    "link_corroboration",
    "same_origin",
    "title_keys",
    "vendor_copy",
]
