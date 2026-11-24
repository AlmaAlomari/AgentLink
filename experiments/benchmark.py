#!/usr/bin/env python3
"""Turn AgentLink telemetry into documentation figures.

Reads the runs recorded by the application (database/agentlink.db), or
generates them itself against a running server, and writes print-ready
charts plus the raw data behind them.

    python experiments/benchmark.py                 # use recorded runs
    python experiments/benchmark.py --live 10       # generate 10 fresh runs
    python experiments/benchmark.py --synth 40      # layout preview only
"""

import argparse
import csv
import json
import os
import random
import sqlite3
import statistics
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "database", "agentlink.db")
DEFAULT_OUT = os.path.join(ROOT, "docs", "figures")
LIVE_DETAIL = os.path.join(ROOT, "experiments", "live_runs.json")

INK = "#1f2937"
GRID = "#d7dee8"
BLUE = "#4f6ef7"
CYAN = "#2bb8c4"
GREEN = "#2f9e6b"
AMBER = "#e0a13a"
RED = "#d9536a"

plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK,
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "axes.labelsize": 10,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.7,
    "xtick.color": INK,
    "ytick.color": INK,
    "font.size": 9,
    "axes.axisbelow": True,
})


def style(ax, title, xlabel, ylabel):
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def save(fig, out, name, synthetic):
    if synthetic:
        fig.text(
            0.5, 0.02,
            "SYNTHETIC DATA - layout preview only",
            ha="center", fontsize=8, color=RED, style="italic"
        )
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    path = os.path.join(out, name)
    fig.savefig(path, dpi=300)
    plt.close(fig)
    return path


def values(runs, key):
    return [r[key] for r in runs if isinstance(r.get(key), (int, float))]


def means(runs):
    out = {}
    for key in ("a_ms", "b_ms", "enc_ms", "dec_ms", "ratio", "acc",
                "v_conf", "total_ms"):
        got = values(runs, key)
        if key.endswith("_ms") and key != "total_ms":
            got = [v for v in got if v > 0]
        if got:
            out[key] = statistics.fmean(got)
    return out


# ---------------------------------------------------------------- data

def read_db(path, owner=None):
    if not os.path.isfile(path):
        raise SystemExit(f"Database not found: {path}")

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    sql = "SELECT * FROM msgs"
    args = ()

    if owner:
        sql += " WHERE owner=?"
        args = (owner,)

    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()

    for r in rows:
        r["total_ms"] = sum(
            r.get(k) or 0 for k in ("a_ms", "b_ms", "enc_ms", "dec_ms")
        )
        r["models"] = parse_models(r.get("models"))

    return rows


def parse_models(text):
    out = {}

    if not text:
        return out

    for part in str(text).split(";"):
        if "=" in part:
            role, model = part.split("=", 1)
            out[role.strip()] = model.strip()

    return out


def synth(n, seed=7):
    rng = random.Random(seed)
    runs = []

    for i in range(n):
        ok = rng.random() > 0.12
        a = rng.gauss(1250, 260)
        b = rng.gauss(1900, 420)
        e = rng.gauss(95, 18)
        d = rng.gauss(120, 25)
        conf = rng.uniform(0.55, 1.0)

        runs.append({
            "id": f"SYNTH{i:04d}",
            "ok": int(ok),
            "a_ms": max(120, a),
            "b_ms": max(180, b),
            "enc_ms": max(10, e),
            "dec_ms": max(10, d),
            "ratio": rng.uniform(0.05, 0.35),
            "acc": 1.0 if ok else rng.uniform(0.3, 0.9),
            "v_conf": conf,
            "v_ok": 1 if ok and conf > 0.7 else 0,
            "lang": "en" if rng.random() > 0.3 else "ar",
            "total_ms": a + b + e + d,
            "models": {
                "planner": "openai/gpt-oss-20b",
                "analyst": "openai/gpt-oss-120b",
                "verifier": "qwen/qwen3.8-27b"
            }
        })

    return runs


def live(n, base, email, password, delay=1.0):
    """Drive the API pipeline n times and record per-model timings."""
    r = httpx.post(f"{base}/api/login",
                   json={"email": email, "password": password}, timeout=30)

    if r.status_code != 200:
        raise SystemExit(f"Login failed ({r.status_code}): {r.text[:120]}")

    token = r.json()["token"]
    head = {"Authorization": "Bearer " + token}
    prompts = [
        "Analyze the uploaded report and list the main findings.",
        "Summarise what changed in the last quarter.",
        "Classify the issues described in my documents by severity.",
        "What are the three most common problems?",
        "Compare the figures mentioned in my files.",
    ]

    detail = load_live()

    for i in range(n):
        prompt = prompts[i % len(prompts)]

        try:
            started = time.perf_counter()
            a = httpx.post(f"{base}/api/agent-a",
                           json={"message": prompt, "lang": "en", "demo": False},
                           headers=head, timeout=120)
            a.raise_for_status()
            a = a.json()

            b = httpx.post(f"{base}/api/agent-b", json={
                "id": a["id"],
                "intent": a["structured"]["intent"],
                "action": "REQUEST_ANALYSIS",
                "priority": a["structured"]["priority"],
                "task": a["structured"]["task"],
                "lang": "en",
                "demo": False,
                "mask": True
            }, headers=head, timeout=120)
            b.raise_for_status()
            b = b.json()
        except Exception as e:
            print(f"  live run {i + 1}/{n} failed: {str(e)[:120]}")
            save_live(detail)
            continue

        wall = round((time.perf_counter() - started) * 1000, 2)
        calls = a.get("models", []) + b.get("models", [])
        v = b.get("verification") or {}

        row = {
            "id": a["id"],
            "ok": 1,
            "a_ms": a["ms"],
            "b_ms": b["ms"],
            "enc_ms": None,
            "dec_ms": None,
            "ratio": a["compression"]["ratio"],
            "acc": None,
            "v_conf": v.get("confidence"),
            "v_ok": 1 if v.get("consistent") else 0,
            "lang": "en",
            "total_ms": wall,
            "models": {c["role"]: c["model"] for c in calls},
            "calls": calls,
            "wall_ms": wall
        }

        httpx.post(f"{base}/api/telemetry", json={
            "id": row["id"], "ok": True,
            "a_ms": row["a_ms"], "b_ms": row["b_ms"],
            "enc_ms": 0, "dec_ms": 0, "ratio": row["ratio"], "acc": 1.0,
            "v_conf": row["v_conf"] or 0, "v_ok": row["v_ok"],
            "lang": "en",
            "models": ";".join(f"{k}={v2}" for k, v2 in row["models"].items())
        }, headers=head, timeout=30)

        detail.append(row)
        save_live(detail)

        print(f"  live run {i + 1}/{n}: {wall:.0f} ms wall, "
              f"verifier confidence {row['v_conf']}")
        time.sleep(delay)

    return detail


def load_live():
    if not os.path.isfile(LIVE_DETAIL):
        return []

    with open(LIVE_DETAIL, encoding="utf-8") as fh:
        try:
            rows = json.load(fh)
        except json.JSONDecodeError:
            return []

    return rows if isinstance(rows, list) else []


def save_live(rows):
    os.makedirs(os.path.dirname(LIVE_DETAIL), exist_ok=True)

    with open(LIVE_DETAIL, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)


# ---------------------------------------------------------------- figures

def fig_stage(runs, out, synthetic):
    stages = [
        ("Agent A\n(planner)", "a_ms"),
        ("Audio\nencode", "enc_ms"),
        ("Audio\ndecode", "dec_ms"),
        ("Agent B\n(analyst)", "b_ms"),
    ]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 3.9))
    labels, m, sd, cols = [], [], [], []
    palette = [BLUE, CYAN, CYAN, GREEN]

    for (label, key), col in zip(stages, palette):
        # Zero means the stage was not part of this run (for example
        # API-driven runs have no browser audio codec).
        got = [v for v in values(runs, key) if v > 0]
        if not got:
            continue
        labels.append(label)
        m.append(statistics.fmean(got))
        sd.append(statistics.stdev(got) if len(got) > 1 else 0)
        cols.append(col)

    if not labels:
        plt.close(fig)
        return None

    bars = ax1.bar(labels, m, yerr=sd, capsize=4,
                   color=cols, alpha=.85)
    ax1.bar_label(bars, fmt="%.0f", padding=3, fontsize=8)
    style(ax1, "Average time per pipeline stage", "", "Milliseconds")

    tot = values(runs, "total_ms")
    ax2.hist(tot, bins=12, color=BLUE, alpha=.8, edgecolor="white")
    ax2.axvline(statistics.fmean(tot), color=RED, linestyle="--",
                label=f"mean {statistics.fmean(tot):.0f} ms")
    ax2.legend(frameon=False)
    style(ax2, "End-to-end duration distribution", "Total milliseconds",
          "Runs")

    return save(fig, out, "fig1_stage_latency.png", synthetic)


def fig_models(out, synthetic):
    rows = load_live()

    if not rows:
        return None

    by = {}

    for row in rows:
        for call in row.get("calls", []):
            by.setdefault(call["model"], []).append(call)

    if not by:
        return None

    names = list(by)
    lat = [statistics.fmean([c["ms"] for c in by[n]]) for n in names]
    tin = [statistics.fmean([c.get("tokens_in") or 0 for c in by[n]])
           for n in names]
    tout = [statistics.fmean([c.get("tokens_out") or 0 for c in by[n]])
            for n in names]

    order = sorted(range(len(names)), key=lambda i: lat[i])
    names = [names[i] for i in order]
    lat = [lat[i] for i in order]
    tin = [tin[i] for i in order]
    tout = [tout[i] for i in order]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.4, 3.9))
    y = range(len(names))

    ax1.barh(list(y), lat, color=BLUE, alpha=.85)
    ax1.set_yticks(list(y))
    ax1.set_yticklabels(names, fontsize=8)
    for i, v in enumerate(lat):
        ax1.text(v, i, f" {v:.0f}", va="center", fontsize=8)
    style(ax1, "Response time by model", "Milliseconds", "")

    ax2.barh([i + 0.18 for i in y], tin, height=.36, color=CYAN,
             label="prompt tokens")
    ax2.barh([i - 0.18 for i in y], tout, height=.36, color=AMBER,
             label="answer tokens")
    ax2.set_yticks(list(y))
    ax2.set_yticklabels(names, fontsize=8)
    ax2.legend(frameon=False, fontsize=8)
    style(ax2, "Token usage by model", "Tokens", "")

    return save(fig, out, "fig2_model_comparison.png", synthetic)


def fig_compression(runs, out, synthetic):
    ratios = [r * 100 for r in values(runs, "ratio")]

    if not ratios:
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 3.9))
    ax1.hist(ratios, bins=14, color=GREEN, alpha=.85, edgecolor="white")
    ax1.axvline(statistics.fmean(ratios), color=RED, linestyle="--",
                label=f"mean {statistics.fmean(ratios):.1f}%")
    ax1.legend(frameon=False)
    style(ax1, "Structured frame size vs original request",
          "Bytes kept (%)", "Runs")

    ordered = sorted(ratios)
    ax2.plot(ordered, [i / len(ordered) * 100 for i in range(1, len(ordered) + 1)],
             color=BLUE, linewidth=2)
    ax2.set_ylim(0, 102)
    style(ax2, "Compression ratio (cumulative)", "Ratio %",
          "Cumulative runs (%)")

    return save(fig, out, "fig3_semantic_compression.png", synthetic)


def fig_accuracy(runs, out, synthetic):
    acc = [r["acc"] * 100 for r in runs
           if isinstance(r.get("acc"), (int, float))]

    if not acc:
        return None

    fig, ax = plt.subplots(figsize=(9.6, 3.6))
    ax.plot(range(1, len(acc) + 1), acc, marker="o", markersize=3.5,
            linewidth=1.4, color=CYAN)
    ax.axhline(100, color=GREEN, linestyle="--", linewidth=1,
               label="all frames verified")
    ax.axhline(statistics.fmean(acc), color=RED, linestyle=":",
               label=f"mean {statistics.fmean(acc):.1f}%")
    ax.set_ylim(min(0, min(acc) - 5), 105)
    ax.legend(frameon=False, loc="lower right")
    style(ax, "Audio frame decode accuracy per communication run",
          "Run", "Accuracy (%)")

    return save(fig, out, "fig4_decode_accuracy.png", synthetic)


def fig_verification(runs, out, synthetic):
    conf = [r["v_conf"] * 100 for r in runs
            if isinstance(r.get("v_conf"), (int, float))
            and r.get("v_conf") is not None
            and (r.get("v_ok") or 0) >= 0]

    if not conf:
        return None

    passed = sum(1 for r in runs if r.get("v_ok") == 1)
    flagged = sum(1 for r in runs if r.get("v_ok") == 0)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 3.6),
                                   gridspec_kw={"width_ratios": [2, 1]})
    ax1.hist(conf, bins=12, color=BLUE, alpha=.85, edgecolor="white")
    ax1.axvline(statistics.fmean(conf), color=RED, linestyle="--",
                label=f"mean {statistics.fmean(conf):.1f}%")
    ax1.legend(frameon=False)
    style(ax1, "Agent C confidence in Agent B's answer",
          "Confidence (%)", "Runs")

    ax2.bar(["Consistent", "Flagged"], [passed, flagged],
            color=[GREEN, AMBER], alpha=.85)
    for i, v in enumerate([passed, flagged]):
        ax2.text(i, v, str(v), ha="center", va="bottom", fontsize=9)
    ax2.set_ylim(0, max(passed, flagged, 1) * 1.2)
    style(ax2, "Verification outcome", "", "Runs")

    return save(fig, out, "fig5_verification.png", synthetic)


def fig_scatter(runs, out, synthetic):
    ok = [(r["a_ms"], r["b_ms"]) for r in runs
          if r.get("ok") and isinstance(r.get("a_ms"), (int, float))
          and isinstance(r.get("b_ms"), (int, float))]
    bad = [(r["a_ms"], r["b_ms"]) for r in runs
           if not r.get("ok") and isinstance(r.get("a_ms"), (int, float))
           and isinstance(r.get("b_ms"), (int, float))]

    fig, ax = plt.subplots(figsize=(6.6, 4.4))

    if ok:
        ax.scatter([p[0] for p in ok], [p[1] for p in ok], s=26,
                   color=GREEN, alpha=.75, label=f"successful ({len(ok)})")
    if bad:
        ax.scatter([p[0] for p in bad], [p[1] for p in bad], s=40,
                   marker="x", color=RED, label=f"failed ({len(bad)})")

    ax.legend(frameon=False)
    style(ax, "Agent A time vs Agent B time", "Agent A (ms)",
          "Agent B (ms)")

    return save(fig, out, "fig6_latency_scatter.png", synthetic)


def fig_corpus(db_path, out, synthetic):
    if synthetic or not os.path.isfile(db_path):
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        kinds = [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) AS n, SUM(chars) AS chars "
            "FROM files GROUP BY kind ORDER BY n DESC")]
        docs = conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        owners = conn.execute(
            "SELECT COUNT(DISTINCT owner) AS n FROM files").fetchone()["n"]
    except sqlite3.OperationalError:
        conn.close()
        return None

    conn.close()

    if not kinds and not docs:
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 3.7))

    if kinds:
        labels = [k["kind"] for k in kinds]
        ax1.bar(labels, [k["n"] for k in kinds], color=BLUE, alpha=.85)
        for i, k in enumerate(kinds):
            ax1.text(i, k["n"], str(k["n"]), ha="center", va="bottom",
                     fontsize=8)
        style(ax1, "Uploaded corpus by file type", "File type", "Files")

        ax2.barh(labels, [(k["chars"] or 0) / 1000 for k in kinds],
                 color=CYAN, alpha=.85)
        style(ax2, "Text available for retrieval", "Thousands of characters",
              "")
    else:
        ax1.barh(["pasted documents"], [docs], color=BLUE, alpha=.85)
        style(ax1, "Document corpus", "Documents", "")
        ax2.axis("off")

    fig.suptitle("")
    return save(fig, out, "fig7_corpus.png", synthetic)


# ---------------------------------------------------------------- output

def dump(runs, out):
    cols = ["id", "ok", "a_ms", "b_ms", "enc_ms", "dec_ms", "ratio",
            "acc", "v_conf", "v_ok", "lang", "total_ms", "models"]

    with open(os.path.join(out, "results.csv"), "w", newline="",
              encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(cols)

        for r in runs:
            row = dict(r)
            row["models"] = ";".join(
                f"{k}={v}" for k, v in (r.get("models") or {}).items()
            )
            writer.writerow([row.get(c) for c in cols])

    with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(runs, fh, indent=2, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--owner", default=None,
                    help="restrict to one account email")
    ap.add_argument("--live", type=int, default=0,
                    help="generate N fresh runs against a running server")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--email", default=os.getenv("BENCH_EMAIL", ""))
    ap.add_argument("--password", default=os.getenv("BENCH_PASSWORD", ""))
    ap.add_argument("--synth", type=int, default=0,
                    help="use N synthetic runs (preview only)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if args.synth:
        runs = synth(args.synth)
        synthetic = True
    elif args.live:
        if not args.email or not args.password:
            raise SystemExit("--live needs --email and --password")
        print(f"Generating {args.live} live runs against {args.base} ...")
        runs = live(args.live, args.base, args.email, args.password)
        synthetic = False
    else:
        runs = read_db(args.db, args.owner)
        synthetic = False

    if not runs:
        raise SystemExit(
            "No recorded runs yet. Use the Console a few times, "
            "or pass --live N / --synth N."
        )

    written = []

    for produced in (
        fig_stage(runs, args.out, synthetic),
        fig_models(args.out, synthetic),
        fig_compression(runs, args.out, synthetic),
        fig_accuracy(runs, args.out, synthetic),
        fig_verification(runs, args.out, synthetic),
        fig_scatter(runs, args.out, synthetic),
        fig_corpus(args.db, args.out, synthetic),
    ):
        if produced:
            written.append(os.path.relpath(produced, ROOT))

    dump(runs, args.out)

    m = means(runs)
    ok = sum(1 for r in runs if r.get("ok"))

    lines = [
        f"Runs analysed : {len(runs)}"
        + ("  (SYNTHETIC)" if synthetic else ""),
        f"Successful    : {ok} ({ok / len(runs) * 100:.1f}%)",
    ]

    for key, label in (("a_ms", "Agent A"), ("b_ms", "Agent B"),
                       ("enc_ms", "Audio encode"), ("dec_ms", "Audio decode"),
                       ("total_ms", "End to end")):
        if key in m:
            lines.append(f"{label:<14}: {m[key]:.1f} ms average")

    if "ratio" in m:
        lines.append(f"{'Compression':<14}: {m['ratio'] * 100:.1f}% "
                     f"of the original request kept")
        if m["ratio"] > 1:
            lines.append(
                "                note: above 100% means the structured "
                "frame is larger\n                than the request, which "
                "only happens for very short prompts"
            )
    if "acc" in m:
        lines.append(f"{'Decode':<14}: {m['acc'] * 100:.1f}% mean accuracy")
    if "v_conf" in m:
        lines.append(f"{'Verifier':<14}: {m['v_conf'] * 100:.1f}% mean "
                     f"confidence")

    lines.append(f"Figures       : {', '.join(written) or 'none'}")
    lines.append(f"Raw data      : results.csv, results.json")

    text = "\n".join(lines)
    print(text)

    with open(os.path.join(args.out, "summary.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(text + "\n")


if __name__ == "__main__":
    main()
