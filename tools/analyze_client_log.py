#!/usr/bin/env python3
"""Summarise a LinuxPlay client log (or probe log) for before/after comparisons.

Reads both pacing formats: the old one ("N corrupt, N ts-errors, host …") and
the current one ("| loss: N damaged, N held, N refused, N corrupt, frozen N ms
… | host … , recv …"). For old logs, frames the decoder refused are counted
from its "Error constructing the frame RPS" lines (one per refused frame).

    tools/analyze_client_log.py linuxplay_client.log [more.log …] [--json]
"""
import json
import re
import statistics
import sys
from collections import Counter

PACING = re.compile(r"^(\d\d):(\d\d):(\d\d) \[INFO\] Frame pacing: (\d+) frames in ([\d.]+)s "
                    r"\(([\d.]+) fps\), max gap (\d+) ms")
OLD = re.compile(r"(\d+) corrupt, (\d+) ts-errors")
NEW = re.compile(r"loss: (\d+) damaged, (\d+) held, (\d+) refused, (\d+) corrupt, "
                 r"frozen (\d+) ms(?: \(longest (\d+)\))?")
MSGS = re.compile(r", (\d+) decoder msgs")
RECV = re.compile(r"recv ([\d.]+) Mb/s")
DECODER_LINES = ("First slice in a frame missing", "Skipping invalid undecodable NALU",
                 "Could not find ref with POC", "Error constructing the frame RPS",
                 "PPS id out of range", "Ignoring POC change", "Non-matching NAL types",
                 "CABAC_MAX_BIN", "cu_qp_delta")
EVENTS = {
    "hw_fallback": "switching to CPU",
    "hw_reset": "Hardware decoder rejected the stream",
    "hw_retry": "hardware decode.",                  # "Retrying videotoolbox hardware decode."
    "reconnect": "Stream ended — reconnecting",
    "audio_restart": "Audio player exited",
    "audio_stall": "Audio stalled",
    "audio_silent": "Audio: stream silent",
    "audio_sound": "Audio: sound again",
}


def analyze(path):
    pacing, levels, decoder = [], Counter(), Counter()
    events = Counter()
    old = Counter()
    new = Counter()
    longest = 0
    first = last = None
    day = 0
    prev = None
    for line in open(path, errors="ignore"):
        m = re.match(r"^(\d\d):(\d\d):(\d\d) \[(\w+)\]", line)
        if m:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if prev is not None and prev - t > 7200:
                day += 1                      # midnight
            prev = t
            t += day * 86400
            first = t if first is None else first
            last = t
            levels[m.group(4)] += 1
        p = PACING.match(line)
        if p:
            fps = float(p.group(6))
            rec = {"fps": fps, "gap": int(p.group(7))}
            o = OLD.search(line)
            if o:
                old["corrupt"] += int(o.group(1))
                old["ts_errors"] += int(o.group(2))
            n = NEW.search(line)
            if n:
                for k, g in zip(("damaged", "held", "refused", "corrupt", "frozen_ms"), n.groups()):
                    new[k] += int(g)
                longest = max(longest, int(n.group(6) or n.group(5)))
            mm = MSGS.search(line)
            if mm:
                new["decoder_msgs"] += int(mm.group(1))
            r = RECV.search(line)
            if r:
                rec["recv"] = float(r.group(1))
            pacing.append(rec)
            continue
        for d in DECODER_LINES:
            if d in line:
                decoder[d] += 1
                break
        for key, needle in EVENTS.items():
            if needle in line:
                events[key] += 1
    hours = max(1e-9, ((last or 0) - (first or 0)) / 3600.0)
    fps = sorted(r["fps"] for r in pacing) or [0.0]
    out = {
        "file": path,
        "hours": round(hours, 2),
        "pacing_reports": len(pacing),
        "fps_p5_p50_p95": [fps[int(len(fps) * 0.05)], fps[len(fps) // 2], fps[int(len(fps) * 0.95)]],
        "max_gap_ms_p95": sorted(r["gap"] for r in pacing)[int(len(pacing) * 0.95)] if pacing else 0,
        "recv_mbps_avg": round(statistics.mean(r["recv"] for r in pacing if "recv" in r), 2)
        if any("recv" in r for r in pacing) else None,
        "warning_lines": levels["WARNING"], "error_lines": levels["ERROR"],
        "decoder_lines": sum(decoder.values()),
        "decoder_lines_per_hour": round(sum(decoder.values()) / hours, 1),
        "events": dict(events),
    }
    if old:
        out["old_format"] = {
            "corrupt": old["corrupt"], "ts_errors (damaged frames)": old["ts_errors"],
            "refused frames (RPS errors)": decoder["Error constructing the frame RPS"],
            "loss events per hour": round(old["ts_errors"] / hours, 1),
        }
    if new:
        out["new_format"] = dict(new)
        out["new_format"]["loss events per hour"] = round(new["damaged"] / hours, 1)
        out["new_format"]["longest freeze ms"] = longest
        if new["damaged"]:
            out["new_format"]["held per damaged frame"] = round(new["held"] / new["damaged"], 1)
            out["new_format"]["frozen ms per loss event"] = round(new["frozen_ms"] / new["damaged"])
    return out


def main(argv):
    as_json = "--json" in argv
    paths = [a for a in argv if not a.startswith("--")]
    results = [analyze(p) for p in paths]
    if as_json:
        print(json.dumps(results, indent=1))
        return 0
    for r in results:
        print(f"== {r['file']}")
        for k, v in r.items():
            if k != "file":
                print(f"  {k:24} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
