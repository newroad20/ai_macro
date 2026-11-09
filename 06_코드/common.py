"""트랙(A/B) 공용 경로·시각·설정 도우미.

A: 개장 전(08:30, 컷오프 08:45) 실행, D-1 종가 -> D 종가 예측 (주 트랙)
B: 10:00 실행, T 값 -> D 종가 예측 (2026-11-09 중단·보관)
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("AI_MACRO_ROOT") or Path(__file__).resolve().parents[1])
CODE = Path(__file__).resolve().parent
SCHEMAS = CODE / "schemas"
KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없으므로 고정 오프셋
TRACKS = ("A", "B")


def fail(msg):
    print(f"[중단] {msg}", file=sys.stderr)
    sys.exit(2)


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def kst_now():
    return datetime.now(KST).replace(microsecond=0)


def kst_str(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def config():
    return load_json(CODE / "experiment_config.json")


def track_cfg(cfg, track):
    if track not in cfg["tracks"]:
        fail(f"알 수 없는 트랙: {track}")
    return cfg["tracks"][track]


def at_time(date, hhmm):
    hh, mm = map(int, hhmm.split(":"))
    return datetime.strptime(date, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=KST)


def neutral_band(tcfg, dry):
    """정식 실행은 확정값만, 시험 실행은 미확정이면 임시값."""
    if tcfg["neutral_band_ticks"] is not None:
        return tcfg["neutral_band_ticks"]
    if dry:
        return tcfg["dryrun_neutral_band_ticks"]
    fail("neutral_band_ticks 미확정(null). 정식 실행 전 experiment_config.json 에서 확정 필요.")


# ---- 경로 ----
# 정식: 02_시장데이터/{스냅샷,정답,리포트방향}/<T>/<날짜>.json, 04_예측결과/<T>/, 05_평가결과/<T>/
# 시험: B는 기존 위치(06_코드/dryrun, 99_로그/dryrun) 유지, A는 그 아래 A/ 하위 폴더

def snapshot_path(track, date, dry=False):
    if dry:
        return CODE / "dryrun" / ("" if track == "B" else track) / f"{date}.snapshot.json"
    return ROOT / "02_시장데이터" / "스냅샷" / track / f"{date}.json"


def answer_path(track, date, dry=False):
    if dry:
        return dryrun_log_dir(track) / f"{date}.answer.json"
    return ROOT / "02_시장데이터" / "정답" / track / f"{date}.json"


def answer_dir(track, dry=False):
    return dryrun_log_dir(track) if dry else ROOT / "02_시장데이터" / "정답" / track


def report_tag_path(track, date):
    return ROOT / "02_시장데이터" / "리포트방향" / track / f"{date}.json"


def prediction_dir(track):
    return ROOT / "04_예측결과" / track


def eval_dir(track):
    return ROOT / "05_평가결과" / track


def dryrun_log_dir(track):
    d = ROOT / "99_로그" / "dryrun"
    return d if track == "B" else d / track


def previous_answers(track, date, dry=False, n=2):
    """date보다 이른 정답 파일 중 최근 n개 (오래된 것 -> 최근 것). 시험 정답은 *.answer.json."""
    d = answer_dir(track, dry)
    if not d.exists():
        return []
    suffix = ".answer.json" if dry else ".json"
    files = sorted(p for p in d.glob(f"*{suffix}") if p.name[:10] < date and p.name[:10].count("-") == 2)
    return [(p.name[:10], load_json(p)) for p in files[-n:]]
