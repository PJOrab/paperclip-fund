#!/usr/bin/env python3
"""validate-output: structural check of a stage's JSON against its schema.

Usage: <stage produces JSON> | python fund_skills/validate_output.py --schema triage
Exit 0 + {"valid": true} when valid; exit 1 + {"valid": false, "errors": [...]} otherwise.
Schemas: triage | analyst | thesis | devil
"""
import argparse
import json
import sys

CATEGORIES = {"earnings", "product", "chips", "capex", "regulation",
              "research", "funding", "sentiment", "macro", "ipo", "m&a", "launch",
              "insider_trade"}


def fail(errors: list[str]) -> None:
    print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False))
    sys.exit(1)


def need(cond: bool, msg: str, errs: list[str]) -> None:
    if not cond:
        errs.append(msg)


def validate(schema: str, data: dict) -> list[str]:
    """Importable validator. Returns list of error strings (empty = valid)."""
    errs: list[str] = []
    if schema == "triage":
        need(isinstance(data.get("clusters"), list), "missing 'clusters' list", errs)
        for i, c in enumerate(data.get("clusters", []) or []):
            for k in ("title", "tickers", "category", "why", "importance"):
                need(k in c, f"clusters[{i}] missing '{k}'", errs)
            need(c.get("category") in CATEGORIES, f"clusters[{i}] bad category", errs)
            need(isinstance(c.get("importance"), int) and 1 <= c.get("importance", 0) <= 5,
                 f"clusters[{i}] importance must be int 1-5", errs)
            need(isinstance(c.get("title"), str) and c.get("title", "").strip(),
                 f"clusters[{i}] title must be a non-empty string", errs)
            need(isinstance(c.get("why"), str) and c.get("why", "").strip(),
                 f"clusters[{i}] why must be a non-empty string", errs)
            need(isinstance(c.get("tickers"), list),
                 f"clusters[{i}] tickers must be a list", errs)
    elif schema == "analyst":
        need(isinstance(data.get("analyses"), list), "missing 'analyses' list", errs)
        for i, x in enumerate(data.get("analyses", []) or []):
            for k in ("title", "tickers", "read", "magnitude", "horizon",
                      "key_facts", "key_uncertainty", "consensus_view", "differentiation"):
                need(k in x, f"analyses[{i}] missing '{k}'", errs)
            need(x.get("read") in {"bullish", "bearish", "mixed"}, f"analyses[{i}] bad read", errs)
            need(x.get("magnitude") in {"low", "medium", "high"}, f"analyses[{i}] bad magnitude", errs)
            need(x.get("horizon") in {"days", "weeks", "quarters"}, f"analyses[{i}] bad horizon", errs)
            need(x.get("consensus_view") in {"aligned", "differentiated", "unclear"},
                 f"analyses[{i}] bad consensus_view", errs)
            need(isinstance(x.get("key_facts"), list) and len(x.get("key_facts", [])) > 0,
                 f"analyses[{i}] key_facts must be a non-empty list", errs)
            need(isinstance(x.get("key_uncertainty"), str) and x.get("key_uncertainty", "").strip(),
                 f"analyses[{i}] key_uncertainty must be a non-empty string", errs)
            need(x.get("consensus_view") != "differentiated" or
                 (isinstance(x.get("differentiation"), str) and x.get("differentiation", "").strip()),
                 f"analyses[{i}] differentiation must be non-empty when consensus_view='differentiated'", errs)
            # consensus_anchor: optional but must be non-empty string when present
            ca = x.get("consensus_anchor")
            if ca is not None:
                need(isinstance(ca, str) and ca.strip(),
                     f"analyses[{i}] consensus_anchor must be non-empty string when present", errs)
    elif schema == "thesis":
        need(isinstance(data.get("theses"), list), "missing 'theses' list", errs)
        for i, x in enumerate(data.get("theses", []) or []):
            for k in ("id", "tickers", "direction", "thesis", "bull_case",
                      "bear_case", "catalysts", "horizon", "conviction",
                      "is_differentiated"):
                need(k in x, f"theses[{i}] missing '{k}'", errs)
            need(x.get("direction") in {"long", "short", "pair"}, f"theses[{i}] bad direction", errs)
            need(isinstance(x.get("is_differentiated"), bool),
                 f"theses[{i}] is_differentiated must be bool", errs)
            need(x.get("horizon") in {"days", "weeks", "quarters"}, f"theses[{i}] bad horizon", errs)
            need(isinstance(x.get("catalysts"), list) and len(x.get("catalysts", [])) > 0,
                 f"theses[{i}] catalysts must be a non-empty list", errs)
            need(isinstance(x.get("bull_case"), list) and len(x.get("bull_case", [])) > 0,
                 f"theses[{i}] bull_case must be a non-empty list", errs)
            need(isinstance(x.get("bear_case"), list) and len(x.get("bear_case", [])) > 0,
                 f"theses[{i}] bear_case must be a non-empty list", errs)
            # scenarios optional but validated when present (investment-grade output)
            sc = x.get("scenarios")
            if sc is not None:
                need(isinstance(sc, dict), f"theses[{i}] scenarios must be a dict", errs)
                for case in ("bull", "base", "bear"):
                    c = sc.get(case)
                    if c is not None:
                        need(isinstance(c, dict), f"theses[{i}] scenarios.{case} must be dict", errs)
                        need("prob" in c and isinstance(c.get("prob"), (int, float)) and 0 <= c["prob"] <= 1,
                             f"theses[{i}] scenarios.{case}.prob must be 0-1 float", errs)
                        need("trigger" in c and isinstance(c.get("trigger"), str) and c["trigger"].strip(),
                             f"theses[{i}] scenarios.{case}.trigger must be non-empty string", errs)
            conv = x.get("conviction")
            need(isinstance(conv, (int, float)) and 0 <= conv <= 1,
                 f"theses[{i}] conviction must be 0.0-1.0", errs)
            need(not isinstance(conv, (int, float)) or conv >= 0.40,
                 f"theses[{i}] conviction {conv} below minimum tradeable floor 0.40", errs)
    elif schema == "devil":
        need(isinstance(data.get("critiques"), list), "missing 'critiques' list", errs)
        for i, x in enumerate(data.get("critiques", []) or []):
            for k in ("id", "strongest_counter", "already_priced_in",
                      "falsification", "blind_spot", "verdict"):
                need(k in x, f"critiques[{i}] missing '{k}'", errs)
            need(x.get("verdict") in {"agree", "caution", "reject"}, f"critiques[{i}] bad verdict", errs)
            need(isinstance(x.get("falsification"), list) and len(x.get("falsification", [])) > 0,
                 f"critiques[{i}] falsification must be a non-empty list", errs)
    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", required=True, choices=["triage", "analyst", "thesis", "devil"])
    ap.add_argument("--file", default="-")
    a = ap.parse_args()
    raw = sys.stdin.read() if a.file == "-" else open(a.file).read()
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        fail([f"not valid JSON: {e}"])
    errs = validate(a.schema, data)
    if errs:
        fail(errs)
    print(json.dumps({"valid": True}))


if __name__ == "__main__":
    main()
