"""
Probability calibration layer.

After each settle run, learns a per-bet-type mapping from the model's predicted
win probability to a calibrated probability, by comparing predictions against
actual outcomes.  The mapping is persisted to models/cal_weights.json and loaded
by the betting engine so every subsequent pick benefits from the correction.

Primary method — Platt scaling
------------------------------
For each bet type with >= PLATT_MIN_SAMPLES settled bets, fit
    calibrated = sigmoid(a * logit(pred) + b)
via recency-weighted logistic regression (recent bets count more), regularised
toward the identity mapping (a=1, b=0) so small samples can't overcorrect.

Unlike the old single multiplicative bias, a 2-parameter map corrects
miscalibration that VARIES across the probability range (e.g. overconfident at
the extremes but fine near 50%) — which is exactly the failure mode the model's
Brier scores show.

Fallback
--------
Types below PLATT_MIN_SAMPLES keep the legacy scalar bias
(= weighted_actual / weighted_predicted, regularised toward 1.0), applied only
once they reach MIN_SAMPLES — otherwise the raw probability passes through.

Unit sizing (betting_engine._get_bias) still reads the scalar "bias" field, which
is computed for every type regardless of method — Platt governs probability
calibration; the bias scalar governs stake sizing.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from config import PLATT_MIN_SAMPLES, PLATT_WARMUP_SAMPLES

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE         = Path(__file__).parent
CAL_FILE      = _HERE / "cal_weights.json"

# ── Hyper-parameters ──────────────────────────────────────────────────────────
MIN_SAMPLES   = 40      # legacy-bias: min settled bets per type before the scalar bias applies
HALF_LIFE     = 45      # exponential decay half-life in days (older bets matter less)
MAX_BIAS      = 1.40    # cap bias factor to prevent wild swings on small samples
MIN_BIAS      = 0.60
BLEND_TOWARD_1 = 0.25   # shrink toward 1.0 by this fraction (regularisation)
PLATT_PRIOR   = 2.0     # L2 pull of the Platt fit toward identity (a=1,b=0); higher = safer on small n


# ── Public API ────────────────────────────────────────────────────────────────

def run_calibration(settled_bets: list[dict]) -> dict:
    """
    Compute calibration factors from settled_bets and persist to cal_weights.json.
    Returns the weights dict (same structure as cal_weights.json).

    settled_bets: list of dicts with keys:
        type, our_prob, result ('win'|'loss'|'push'), date (YYYY-MM-DD)
    """
    today = date.today()
    by_type: dict[str, list[tuple[float, float, float]]] = {}  # type -> [(weight, prob, outcome)]

    for b in settled_bets:
        if b["result"] not in ("win", "loss"):
            continue   # skip pushes — uncertain ground truth
        if not b.get("our_prob"):
            continue

        bet_type = _normalise_type(b["type"])
        age_days = _age_days(b.get("date", today.isoformat()), today)
        weight   = math.exp(-math.log(2) * age_days / HALF_LIFE)
        outcome  = 1.0 if b["result"] == "win" else 0.0

        by_type.setdefault(bet_type, []).append((weight, b["our_prob"], outcome))

    factors: dict[str, Any] = {}
    all_weights, all_probs, all_outcomes = [], [], []

    for bet_type, records in by_type.items():
        n = len(records)
        w_sum  = sum(w for w, _, _ in records)
        w_pred = sum(w * p for w, p, _ in records) / w_sum
        w_act  = sum(w * o for w, _, o in records) / w_sum

        # Scalar bias — always computed (drives unit sizing, and is the fallback
        # calibration for types below PLATT_MIN_SAMPLES).
        raw_bias = (w_act / w_pred) if w_pred > 0 else 1.0
        bias     = _regularise(raw_bias, n)

        brier    = sum(w * (p - o) ** 2 for w, p, o in records) / w_sum

        entry = {
            "bias":               round(bias, 4),
            "samples":            n,
            "weighted_predicted": round(w_pred, 4),
            "weighted_actual":    round(w_act, 4),
            "brier_score":        round(brier, 4),
        }

        # Platt scaling ramps in from PLATT_WARMUP_SAMPLES to full at
        # PLATT_MIN_SAMPLES (confidence applied at calibrated_prob time) — below
        # warmup, no correction (raw probabilities).
        if n >= PLATT_WARMUP_SAMPLES:
            a, b = _fit_platt(records)
            conf = min(1.0, n / PLATT_MIN_SAMPLES)
            # Report the Brier at the confidence-weighted correction actually applied.
            post_brier = sum(
                w * ((p * (1 - conf) + _apply_platt(p, a, b) * conf) - o) ** 2
                for w, p, o in records
            ) / w_sum
            entry["method"]         = "platt"
            entry["platt"]          = {"a": round(a, 4), "b": round(b, 4)}
            entry["confidence"]     = round(conf, 3)
            entry["brier_post_cal"] = round(post_brier, 4)
            entry["active"]         = True
        else:
            entry["method"] = "bias"
            entry["active"] = False

        factors[bet_type] = entry

        all_weights.extend(w for w, _, _ in records)
        all_probs.extend(p for _, p, _ in records)
        all_outcomes.extend(o for _, _, o in records)

    # Overall stats
    overall_bias   = 1.0
    overall_brier  = None
    total_settled  = sum(len(v) for v in by_type.values())
    if all_weights:
        w_sum   = sum(all_weights)
        o_pred  = sum(w * p for w, p in zip(all_weights, all_probs))  / w_sum
        o_act   = sum(w * o for w, o in zip(all_weights, all_outcomes)) / w_sum
        overall_bias  = round((o_act / o_pred) if o_pred else 1.0, 4)
        overall_brier = round(
            sum(w * (p - o) ** 2 for w, p, o in zip(all_weights, all_probs, all_outcomes)) / w_sum,
            4,
        )

    weights = {
        "updated_at":    today.isoformat(),
        "total_settled": total_settled,
        "overall_bias":  overall_bias,
        "overall_brier": overall_brier,
        "by_type":       factors,
    }

    CAL_FILE.write_text(json.dumps(weights, indent=2))
    return weights


def load_cal_weights() -> dict:
    """Load persisted calibration weights.  Returns empty/default if missing."""
    if not CAL_FILE.exists():
        return {"by_type": {}}
    try:
        return json.loads(CAL_FILE.read_text())
    except Exception:
        return {"by_type": {}}


def calibrated_prob(our_prob: float, bet_type: str, weights: dict | None = None) -> float:
    """
    Return our_prob mapped through this bet type's learned calibration.
      - method 'platt'  → sigmoid(a * logit(prob) + b)
      - method 'bias'   → prob * bias   (only once active, i.e. samples >= MIN_SAMPLES)
      - otherwise       → prob unchanged
    """
    if weights is None:
        weights = load_cal_weights()

    norm_type = _normalise_type(bet_type)
    info      = weights.get("by_type", {}).get(norm_type, {})

    if info.get("method") == "platt" and "platt" in info:
        a = info["platt"].get("a", 1.0)
        b = info["platt"].get("b", 0.0)
        # Confidence-weighted: blend raw prob with the Platt-corrected one so the
        # correction grows smoothly from PLATT_WARMUP_SAMPLES to full at
        # PLATT_MIN_SAMPLES, rather than switching on all at once.
        conf     = min(1.0, info.get("samples", 0) / PLATT_MIN_SAMPLES)
        platt_p  = _apply_platt(our_prob, a, b)
        blended  = our_prob * (1 - conf) + platt_p * conf
        return max(0.05, min(0.95, blended))

    if info.get("active", False):
        bias = info.get("bias", 1.0)
        return max(0.05, min(0.95, our_prob * bias))

    return our_prob


def print_calibration_report(weights: dict) -> None:
    """Print a formatted calibration report to stdout."""
    w = 90
    print()
    print("=" * w)
    print("  CALIBRATION REPORT  —  as of", weights.get("updated_at", "unknown"))
    print("=" * w)
    print(f"  Total settled bets : {weights.get('total_settled', 0)}")
    print(f"  Overall bias factor: {weights.get('overall_bias', 1.0):.4f}  "
          f"(1.00 = perfectly calibrated; >1 = model under-estimates; <1 = over-estimates)")
    if weights.get("overall_brier") is not None:
        print(f"  Overall Brier score: {weights['overall_brier']:.4f}  "
              f"(lower = better; 0.25 = random; 0.00 = perfect)")
    print()

    by_type = weights.get("by_type", {})
    if not by_type:
        print("  No per-type data yet.  Run more picks and settle results to build history.")
        print()
        return

    # Table header — Brier shows raw → post-calibration for Platt types
    col = "{:<20}  {:>7}  {:>9}  {:>9}  {:>16}  {:>8}  {}"
    print(col.format("Bet Type", "Samples", "Predict%", "Actual%",
                     "Brier(raw→cal)", "Method", "Status"))
    print("  " + "-" * (w - 2))
    for btype in sorted(by_type):
        d      = by_type[btype]
        n      = d["samples"]
        pred   = d["weighted_predicted"] * 100
        act    = d["weighted_actual"]    * 100
        brier  = d["brier_score"]
        method = d.get("method", "bias")
        if method == "platt":
            conf = d.get("confidence", 1.0)
            brier_str = f"{brier:.4f}→{d.get('brier_post_cal', brier):.4f}"
            method_str = "Platt"
            status = "✓ full" if conf >= 1.0 else f"◐ {conf*100:.0f}% ({PLATT_MIN_SAMPLES - n} to full)"
        else:
            brier_str = f"{brier:.4f}"
            method_str = "—"
            status = f"○ need {max(0, PLATT_WARMUP_SAMPLES - n)} to start"
        print(col.format(
            f"  {btype}", n,
            f"{pred:.1f}%", f"{act:.1f}%",
            brier_str, method_str, status,
        ))

    print()
    platt_count = sum(1 for d in by_type.values() if d.get("method") == "platt")
    full_count  = sum(1 for d in by_type.values()
                      if d.get("method") == "platt" and d.get("confidence", 1.0) >= 1.0)
    if platt_count:
        print(f"  {platt_count} type(s) applying calibration "
              f"({full_count} at full strength, the rest ramping in with more data).")
    else:
        print(f"  No types calibrating yet (Platt begins at {PLATT_WARMUP_SAMPLES} samples/type).")
    print("=" * w)
    print()


# ── Platt scaling ─────────────────────────────────────────────────────────────

def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _apply_platt(prob: float, a: float, b: float) -> float:
    """Map a raw probability through the fitted Platt sigmoid, clamped to [0.05, 0.95]."""
    z = a * _logit(prob) + b
    # numerically stable sigmoid
    if z >= 0:
        s = 1.0 / (1.0 + math.exp(-z))
    else:
        ez = math.exp(z)
        s = ez / (1.0 + ez)
    return max(0.05, min(0.95, s))


def _fit_platt(records: list[tuple[float, float, float]]) -> tuple[float, float]:
    """
    Recency-weighted logistic regression of outcome on logit(pred), fitting
    (a, b) for calibrated = sigmoid(a*logit(pred)+b).  L2-regularised toward the
    identity map (a=1, b=0) with strength PLATT_PRIOR so small samples stay close
    to the raw probability.  records: [(weight, prob, outcome)].
    """
    import numpy as np
    from scipy.optimize import minimize

    w = np.array([r[0] for r in records], dtype=float)
    p = np.clip(np.array([r[1] for r in records], dtype=float), 1e-6, 1 - 1e-6)
    y = np.array([r[2] for r in records], dtype=float)
    x = np.log(p / (1 - p))   # logit(pred)

    def nll(params):
        a, b = params
        z = a * x + b
        # weighted negative log-likelihood + L2 pull toward (1, 0)
        log_sig   = -np.logaddexp(0.0, -z)   # log sigmoid(z)
        log_1msig = -np.logaddexp(0.0,  z)   # log(1 - sigmoid(z))
        ll = np.sum(w * (y * log_sig + (1 - y) * log_1msig))
        reg = PLATT_PRIOR * ((a - 1.0) ** 2 + b ** 2)
        return -ll + reg

    def grad(params):
        a, b = params
        z = a * x + b
        s = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        d = w * (s - y)
        ga = np.sum(d * x) + 2 * PLATT_PRIOR * (a - 1.0)
        gb = np.sum(d)     + 2 * PLATT_PRIOR * b
        return np.array([ga, gb])

    try:
        res = minimize(nll, np.array([1.0, 0.0]), jac=grad, method="BFGS")
        a, b = float(res.x[0]), float(res.x[1])
        if not (math.isfinite(a) and math.isfinite(b)):
            return 1.0, 0.0
        return a, b
    except Exception:
        return 1.0, 0.0


# ── Internals ─────────────────────────────────────────────────────────────────

def _normalise_type(bet_type: str) -> str:
    """Map raw DB type strings to canonical calibration keys."""
    t = (bet_type or "").lower()
    if t == "moneyline":         return "moneyline"
    if t == "total_over":        return "total_over"
    if t == "total_under":       return "total_under"
    if t == "pitcher_k_over":    return "pitcher_k_over"
    if t == "pitcher_k_under":   return "pitcher_k_under"
    if t in ("nrfi_nrfi", "nrfi_yrfi"): return "nrfi"
    if t == "parlay":            return "parlay"
    if t == "batter_hits":       return "batter_hits"
    return "other"


def _age_days(date_str: str, today: date) -> int:
    try:
        return (today - date.fromisoformat(date_str)).days
    except Exception:
        return 0


def _regularise(raw_bias: float, n: int) -> float:
    """
    Blend raw_bias toward 1.0 based on sample size and the BLEND_TOWARD_1 factor.
    Small samples get pulled harder toward 1.0.
    """
    # Confidence weight: 0 at 0 samples, 1 at ~50 samples
    conf   = min(1.0, n / (n + 20))
    target = raw_bias * conf + 1.0 * (1 - conf)
    # Also apply fixed shrinkage
    blended = target * (1 - BLEND_TOWARD_1) + 1.0 * BLEND_TOWARD_1
    return max(MIN_BIAS, min(MAX_BIAS, blended))
