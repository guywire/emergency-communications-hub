"""
ech/core/hamlog.py
------------------
Ham radio logging: export formats, import parsers, and live upload helpers.

Supported export formats:
  ADIF    — general exchange format (LoTW, QRZ, Club Log, most loggers)
  Cabrillo 3.0 — ARRL contest submission format (Field Day, etc.)
  POTA CSV — Parks on the Air activation log
  SOTA CSV — Summits on the Air activation log

Supported import formats:
  ADIF (.adi / .adif) — from any ADIF-compatible logger
  Cabrillo (.log / .cbr) — contest logs
  CSV (.csv) — generic; header row maps columns automatically

Supported live uploads:
  QRZ.com logbook API
  Club Log realtime API
  POTA upload API
  SOTA upload API (api2.sota.org.uk)
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Band → nominal frequency (kHz) for Cabrillo ──────────────────────────────

BAND_FREQ_KHZ: dict[str, int] = {
    "160M": 1825,  "80M": 3550,  "60M": 5357,  "40M": 7050,
    "30M": 10125,  "20M": 14225, "17M": 18100, "15M": 21225,
    "12M": 24950,  "10M": 28500, "6M": 50125,  "2M": 144200,
    "1.25M": 222100, "70CM": 432100, "33CM": 902100, "23CM": 1296200,
}

# ── Mode normalisation ────────────────────────────────────────────────────────

def _adif_mode(mode: str) -> tuple[str, str]:
    """Return (ADIF MODE, ADIF SUBMODE) pair."""
    m = mode.upper()
    mapping = {
        "PH":      ("SSB",    ""),
        "SSB":     ("SSB",    ""),
        "USB":     ("SSB",    "USB"),
        "LSB":     ("SSB",    "LSB"),
        "CW":      ("CW",     ""),
        "DI":      ("DIGI",   ""),
        "FM":      ("FM",     ""),
        "AM":      ("AM",     ""),
        "FT8":     ("FT8",    ""),
        "FT4":     ("FT4",    ""),
        "JS8":     ("JS8CALL",""),
        "JS8CALL": ("JS8CALL",""),
        "WSPR":    ("WSPR",   ""),
        "PACKET":  ("PKT",    ""),
        "APRS":    ("PKT",    ""),
        "WINLINK": ("PKT",    ""),
    }
    return mapping.get(m, (m, ""))


def _cabrillo_mode(mode: str) -> str:
    """Map mode to Cabrillo mode token."""
    m = mode.upper()
    if m in ("PH", "SSB", "USB", "LSB", "FM", "AM"):
        return "PH"
    if m == "CW":
        return "CW"
    return "DI"   # all digital modes → DI


def _freq_khz_to_band(freq_khz: float) -> str:
    if freq_khz < 2000:   return "160M"
    if freq_khz < 4000:   return "80M"
    if freq_khz < 5500:   return "60M"
    if freq_khz < 7500:   return "40M"
    if freq_khz < 11000:  return "30M"
    if freq_khz < 15000:  return "20M"
    if freq_khz < 18500:  return "17M"
    if freq_khz < 21500:  return "15M"
    if freq_khz < 25000:  return "12M"
    if freq_khz < 30000:  return "10M"
    if freq_khz < 60000:  return "6M"
    if freq_khz < 150000: return "2M"
    if freq_khz < 230000: return "1.25M"
    return "70CM"


def _adif_mode_to_ech(adif_mode: str, submode: str = "") -> str:
    m = adif_mode.upper()
    s = submode.upper()
    if m == "SSB":
        if s == "USB": return "USB"
        if s == "LSB": return "LSB"
        return "PH"
    if m == "CW":   return "CW"
    if m == "FM":   return "FM"
    if m == "AM":   return "AM"
    if m == "FT8":  return "FT8"
    if m == "FT4":  return "FT4"
    if m in ("JS8CALL", "JS8"): return "JS8"
    if m in ("DIGI", "RTTY", "PKT", "PSK", "MSK144", "JT65", "JT9",
             "WSPR", "Q65", "OLIVIA"): return "DI"
    return "PH"


# ── ADIF import ───────────────────────────────────────────────────────────────

def _parse_adif_record(text: str) -> dict:
    """Parse one ADIF record into a {FIELD: value} dict using length-aware decoding."""
    fields: dict[str, str] = {}
    i = 0
    while i < len(text):
        lt = text.find("<", i)
        if lt == -1:
            break
        gt = text.find(">", lt)
        if gt == -1:
            break
        tag_part = text[lt + 1:gt]
        parts = tag_part.split(":")
        name = parts[0].upper().strip()
        if name in ("EOR", "EOH"):
            break
        try:
            length = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            i = gt + 1
            continue
        value = text[gt + 1: gt + 1 + length]
        fields[name] = value
        i = gt + 1 + length
    return fields


def parse_adif_file(text: str) -> list[dict]:
    """Parse an ADIF file and return a list of raw field-dicts (one per QSO)."""
    # Skip header before <EOH>
    eoh = text.upper().find("<EOH>")
    body = text[eoh + 5:] if eoh != -1 else text
    records = []
    for chunk in re.split(r"<EOR>", body, flags=re.IGNORECASE):
        chunk = chunk.strip()
        if not chunk:
            continue
        r = _parse_adif_record(chunk)
        if r.get("CALL"):
            records.append(r)
    return records


def adif_to_ech_qso(adif: dict, contest: str = "GENERAL", station_id: str = "1") -> dict | None:
    """Convert a parsed ADIF field-dict to an ECH qso_log row dict."""
    callsign = adif.get("CALL", "").upper().strip()
    if not callsign:
        return None

    # Timestamp
    qso_date = adif.get("QSO_DATE", "")
    time_on  = adif.get("TIME_ON", "000000").ljust(6, "0")[:6]
    try:
        ts = datetime(
            int(qso_date[:4]), int(qso_date[4:6]), int(qso_date[6:8]),
            int(time_on[:2]), int(time_on[2:4]), int(time_on[4:6]),
            tzinfo=timezone.utc,
        )
        timestamp = ts.isoformat()
    except Exception:
        timestamp = datetime.now(timezone.utc).isoformat()

    # Frequency / band
    freq_mhz: float | None = None
    freq_str = adif.get("FREQ", "")
    if freq_str:
        try:
            freq_mhz = float(freq_str)
        except ValueError:
            pass
    band = adif.get("BAND", "").upper()
    if not band and freq_mhz:
        band = _freq_khz_to_band(freq_mhz * 1000)

    mode = _adif_mode_to_ech(adif.get("MODE", "SSB"), adif.get("SUBMODE", ""))

    pota_ref = ""
    if adif.get("MY_SIG", "").upper() == "POTA":
        pota_ref = adif.get("MY_SIG_INFO", "")

    return {
        "id":             str(uuid.uuid4()),
        "station_id":     station_id,
        "callsign":       callsign,
        "band":           band or "20M",
        "mode":           mode,
        "freq_mhz":       freq_mhz,
        "sent_rst":       adif.get("RST_SENT", "59") or "59",
        "rcvd_rst":       adif.get("RST_RCVD", "59") or "59",
        "sent_exch":      adif.get("STX_STRING", adif.get("STX", "")),
        "rcvd_exch":      adif.get("SRX_STRING", adif.get("SRX", "")),
        "notes":          adif.get("COMMENT", adif.get("NOTES", "")),
        "timestamp":      timestamp,
        "source":         "import_adif",
        "source_adapter": None,
        "contest":        contest,
        "pota_ref":       pota_ref,
        "sota_ref":       adif.get("MY_SOTA_REF", ""),
        "name":           adif.get("NAME", ""),
        "power":          adif.get("TX_PWR", ""),
        "state":          adif.get("STATE", ""),
        "country":        adif.get("COUNTRY", ""),
        "county":         adif.get("CNTY", ""),
        "time_off":       None,
        "grid":           adif.get("GRIDSQUARE", ""),
    }


# ── Cabrillo import ───────────────────────────────────────────────────────────

def parse_cabrillo_file(text: str) -> list[dict]:
    """Parse a Cabrillo 3.0 log file and return raw QSO dicts."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line.upper().startswith("QSO:"):
            continue
        # QSO: freq mode date time mycall sent_rst sent_exch call rcvd_rst rcvd_exch [xmtr]
        parts = line[4:].split()
        if len(parts) < 9:
            continue
        records.append({
            "freq_khz":   parts[0],
            "cab_mode":   parts[1],
            "date":       parts[2],   # YYYY-MM-DD
            "time":       parts[3],   # HHMM
            "mycall":     parts[4],
            "sent_rst":   parts[5],
            "sent_exch":  parts[6],
            "callsign":   parts[7],
            "rcvd_rst":   parts[8],
            "rcvd_exch":  parts[9] if len(parts) > 9 else "",
        })
    return records


def cabrillo_to_ech_qso(cab: dict, contest: str = "GENERAL", station_id: str = "1") -> dict | None:
    callsign = cab.get("callsign", "").upper().strip()
    if not callsign:
        return None

    try:
        freq_khz = float(cab["freq_khz"])
    except (ValueError, KeyError):
        freq_khz = 14225.0

    band = _freq_khz_to_band(freq_khz)
    freq_mhz = round(freq_khz / 1000, 4)

    cab_mode = cab.get("cab_mode", "PH").upper()
    mode = {"PH": "PH", "CW": "CW", "DI": "DI", "RY": "DI", "FM": "FM"}.get(cab_mode, "PH")

    date_str = cab.get("date", "")
    time_str = cab.get("time", "0000").ljust(4, "0")
    try:
        ts = datetime(
            int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]),
            int(time_str[:2]), int(time_str[2:4]),
            tzinfo=timezone.utc,
        )
        timestamp = ts.isoformat()
    except Exception:
        timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "id":             str(uuid.uuid4()),
        "station_id":     station_id,
        "callsign":       callsign,
        "band":           band,
        "mode":           mode,
        "freq_mhz":       freq_mhz,
        "sent_rst":       cab.get("sent_rst", "59"),
        "rcvd_rst":       cab.get("rcvd_rst", "59"),
        "sent_exch":      cab.get("sent_exch", ""),
        "rcvd_exch":      cab.get("rcvd_exch", ""),
        "notes":          "",
        "timestamp":      timestamp,
        "source":         "import_cabrillo",
        "source_adapter": None,
        "contest":        contest,
        "pota_ref":       "",
        "sota_ref":       "",
        "name":           "",
        "power":          "",
        "state":          "",
        "country":        "",
        "county":         "",
        "time_off":       None,
        "grid":           "",
    }


# ── CSV import ────────────────────────────────────────────────────────────────

# Maps common column header names (lowercase) to ECH QSO field names
_CSV_COL_MAP: dict[str, str] = {
    # callsign variants
    "call": "callsign", "callsign": "callsign", "dx call": "callsign",
    "dx_call": "callsign", "contact": "callsign",
    # band
    "band": "band", "bandrx": "band",
    # mode
    "mode": "mode", "moderx": "mode",
    # frequency
    "freq": "freq_mhz", "frequency": "freq_mhz", "tx freq": "freq_mhz",
    "tx_freq": "freq_mhz", "rx freq": "freq_mhz", "rx_freq": "freq_mhz",
    # date/time
    "date": "date", "qso_date": "date", "qsodate": "date",
    "time": "time", "time(utc)": "time", "timeon": "time", "time_on": "time",
    # RST
    "rst sent": "sent_rst", "rst_sent": "sent_rst", "rstst": "sent_rst",
    "rst rcvd": "rcvd_rst", "rst_rcvd": "rcvd_rst", "rstrcv": "rcvd_rst",
    # exchange
    "stx_string": "sent_exch", "srx_string": "rcvd_exch",
    "sent exch": "sent_exch", "rcvd exch": "rcvd_exch",
    "sent_exch": "sent_exch", "rcvd_exch": "rcvd_exch",
    # extras
    "name": "name", "notes": "notes", "comment": "notes",
    "grid": "grid", "gridsquare": "grid", "dx grid": "grid",
    "power": "power", "tx pwr": "power", "tx_pwr": "power",
    "country": "country", "state": "state",
}


def parse_csv_file(text: str) -> list[dict]:
    """Parse a CSV log file with auto-detected column mapping. Returns raw row dicts."""
    import csv, io
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    # Build column map from headers
    col_map: dict[str, str] = {}
    for header in reader.fieldnames:
        key = (header or "").strip().lower()
        if key in _CSV_COL_MAP:
            col_map[header] = _CSV_COL_MAP[key]
    records = []
    for row in reader:
        mapped: dict[str, str] = {}
        for header, ech_field in col_map.items():
            val = (row.get(header) or "").strip()
            if val:
                mapped[ech_field] = val
        if mapped.get("callsign"):
            records.append(mapped)
    return records


def csv_to_ech_qso(row: dict, contest: str = "GENERAL", station_id: str = "1") -> dict | None:
    callsign = row.get("callsign", "").upper().strip()
    if not callsign:
        return None

    # Frequency / band
    freq_mhz: float | None = None
    freq_str = row.get("freq_mhz", "")
    if freq_str:
        try:
            val = float(freq_str)
            # Detect if value is in MHz or Hz
            freq_mhz = val if val < 1000 else val / 1_000_000
        except ValueError:
            pass
    band = row.get("band", "").upper()
    if not band and freq_mhz:
        band = _freq_khz_to_band(freq_mhz * 1000)

    mode = _adif_mode_to_ech(row.get("mode", "SSB").upper())

    # Timestamp — try to combine date + time fields
    date_str = row.get("date", "")
    time_str = row.get("time", "000000").replace(":", "").ljust(6, "0")[:6]
    # Support YYYYMMDD or YYYY-MM-DD
    date_clean = date_str.replace("-", "").replace("/", "")
    try:
        ts = datetime(
            int(date_clean[:4]), int(date_clean[4:6]), int(date_clean[6:8]),
            int(time_str[:2]), int(time_str[2:4]), int(time_str[4:6]),
            tzinfo=timezone.utc,
        )
        timestamp = ts.isoformat()
    except Exception:
        timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "id":             str(uuid.uuid4()),
        "station_id":     station_id,
        "callsign":       callsign,
        "band":           band or "20M",
        "mode":           mode,
        "freq_mhz":       freq_mhz,
        "sent_rst":       row.get("sent_rst", "59") or "59",
        "rcvd_rst":       row.get("rcvd_rst", "59") or "59",
        "sent_exch":      row.get("sent_exch", ""),
        "rcvd_exch":      row.get("rcvd_exch", ""),
        "notes":          row.get("notes", ""),
        "timestamp":      timestamp,
        "source":         "import_csv",
        "source_adapter": None,
        "contest":        contest,
        "pota_ref":       "",
        "sota_ref":       "",
        "name":           row.get("name", ""),
        "power":          row.get("power", ""),
        "state":          row.get("state", ""),
        "country":        row.get("country", ""),
        "county":         "",
        "time_off":       None,
        "grid":           row.get("grid", ""),
    }


# ── ADIF export ───────────────────────────────────────────────────────────────

def _adif_field(tag: str, value: str) -> str:
    v = str(value)
    return f"<{tag.upper()}:{len(v)}>{v}"


def format_adif(qsos: list[dict], config: dict) -> str:
    from ech import __version__ as V
    lines = [
        _adif_field("ADIF_VER", "3.1.4"),
        _adif_field("PROGRAMID", "ECH"),
        _adif_field("PROGRAMVERSION", V),
        "<EOH>",
        "",
    ]
    for q in qsos:
        ts = q.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            qso_date = dt.strftime("%Y%m%d")
            qso_time = dt.strftime("%H%M%S")
        except Exception:
            qso_date = ""
            qso_time = ""

        adif_mode, submode = _adif_mode(q.get("mode", ""))
        freq_mhz = q.get("freq_mhz")
        if freq_mhz is None:
            band = q.get("band", "")
            freq_khz = BAND_FREQ_KHZ.get(band.upper(), 0)
            freq_mhz = freq_khz / 1000.0 if freq_khz else None

        record = []
        record.append(_adif_field("CALL",      q.get("callsign", "")))
        record.append(_adif_field("QSO_DATE",  qso_date))
        record.append(_adif_field("TIME_ON",   qso_time))
        record.append(_adif_field("BAND",      q.get("band", "")))
        record.append(_adif_field("MODE",      adif_mode))
        if submode:
            record.append(_adif_field("SUBMODE", submode))
        if freq_mhz:
            record.append(_adif_field("FREQ", f"{freq_mhz:.4f}"))
        rst_s = q.get("sent_rst") or "59"
        rst_r = q.get("rcvd_rst") or "59"
        record.append(_adif_field("RST_SENT", rst_s))
        record.append(_adif_field("RST_RCVD", rst_r))
        if q.get("sent_exch"):
            record.append(_adif_field("STX_STRING", q["sent_exch"]))
        if q.get("rcvd_exch"):
            record.append(_adif_field("SRX_STRING", q["rcvd_exch"]))
        if q.get("notes"):
            record.append(_adif_field("COMMENT", q["notes"]))
        mycall = config.get("callsign", "")
        if mycall:
            record.append(_adif_field("STATION_CALLSIGN", mycall))
        if q.get("pota_ref"):
            record.append(_adif_field("MY_SIG", "POTA"))
            record.append(_adif_field("MY_SIG_INFO", q["pota_ref"]))
        if q.get("sota_ref"):
            record.append(_adif_field("MY_SOTA_REF", q["sota_ref"]))
        if q.get("name"):
            record.append(_adif_field("NAME", q["name"]))
        if q.get("power"):
            record.append(_adif_field("TX_PWR", q["power"]))
        if q.get("state"):
            record.append(_adif_field("STATE", q["state"]))
        if q.get("country"):
            record.append(_adif_field("COUNTRY", q["country"]))
        if q.get("county"):
            record.append(_adif_field("CNTY", q["county"]))
        if q.get("grid"):
            record.append(_adif_field("GRIDSQUARE", q["grid"]))
        if q.get("time_off"):
            try:
                to = q["time_off"]
                if len(to) == 4 and to.isdigit():
                    dt2 = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    time_off_str = f"{dt2.strftime('%Y%m%d')}{to}00"
                    record.append(_adif_field("TIME_OFF", time_off_str[8:14]))
            except Exception:
                pass
        record.append("<EOR>")
        lines.append("".join(record))

    return "\n".join(lines) + "\n"


# ── Cabrillo export ───────────────────────────────────────────────────────────

def format_cabrillo(qsos: list[dict], config: dict, bonuses: dict | None = None) -> str:
    callsign = config.get("callsign", "N0CALL").upper()
    contest  = config.get("contest", "ARRL-FIELD-DAY").upper()
    fd_class = config.get("field_day_class", "1A").upper()
    section  = config.get("field_day_section", "").upper()
    power    = config.get("power", "LOW").upper()
    grid     = config.get("grid", "")
    ops      = config.get("operators", callsign)

    # Determine category-operator from number of unique station IDs
    station_ids = {q.get("station_id", "1") for q in qsos}
    cat_op = "MULTI-OP" if len(station_ids) > 1 else "SINGLE-OP"

    # QSO points only — ARRL FD bonus activities are submitted separately on the ARRL website
    qso_pts = sum(1 if q.get("mode", "PH").upper() == "PH" else 2 for q in qsos)

    lines = [
        "START-OF-LOG: 3.0",
        f"CALLSIGN: {callsign}",
        f"CONTEST: {contest}",
        f"CATEGORY-OPERATOR: {cat_op}",
        "CATEGORY-BAND: ALL",
        "CATEGORY-MODE: MIXED",
        f"CATEGORY-POWER: {power}",
        "CATEGORY-STATION: FIXED",
        f"OPERATORS: {ops}",
        f"CLAIMED-SCORE: {qso_pts}",
        f"SOAPBOX: Logged with ECH Emergency Communications Hub",
    ]
    if grid:
        lines.append(f"GRID-LOCATOR: {grid}")

    for q in qsos:
        ts = q.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            cab_date = dt.strftime("%Y-%m-%d")
            cab_time = dt.strftime("%H%M")
        except Exception:
            cab_date = "0000-00-00"
            cab_time = "0000"

        band = q.get("band", "20M").upper()
        freq = BAND_FREQ_KHZ.get(band, 14225)
        cab_mode = _cabrillo_mode(q.get("mode", "PH"))
        their_call = q.get("callsign", "").upper()
        sent_exch  = q.get("sent_exch", f"{fd_class} {section}").upper()
        rcvd_exch  = q.get("rcvd_exch", "").upper()
        xmtr = q.get("station_id", "0")
        try:
            xmtr = str(int(xmtr) - 1)   # Cabrillo transmitter is 0-based
        except (ValueError, TypeError):
            xmtr = "0"

        # QSO: freq mode date time mycall sent_exch their_call rcvd_exch xmtr
        lines.append(
            f"QSO: {freq:>5} {cab_mode:<2} {cab_date} {cab_time} "
            f"{callsign:<13} {sent_exch:<10} {their_call:<13} {rcvd_exch:<10} {xmtr}"
        )

    lines.append("END-OF-LOG:")
    return "\n".join(lines) + "\n"


# ── POTA CSV export ───────────────────────────────────────────────────────────

def format_pota_csv(qsos: list[dict], config: dict) -> str:
    """POTA ADIF is preferred; we generate the hunter-friendly CSV version."""
    callsign = config.get("callsign", "N0CALL").upper()
    pota_ref  = config.get("pota_ref", "")
    rows = ["Version 2"]
    for q in qsos:
        ref = q.get("pota_ref") or pota_ref
        ts = q.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y%m%d")
            time_str = dt.strftime("%H%M")
        except Exception:
            date_str = ""
            time_str = ""
        band = q.get("band", "").upper()
        freq_mhz = q.get("freq_mhz")
        if freq_mhz is None:
            freq_khz = BAND_FREQ_KHZ.get(band, 0)
            freq_mhz = freq_khz / 1000.0 if freq_khz else 0
        mode_str = q.get("mode", "SSB").upper()
        if mode_str in ("PH", "SSB"):
            mode_str = "SSB"
        elif mode_str == "DI":
            mode_str = "DATA"
        their_call = q.get("callsign", "").upper()
        rows.append(f"{callsign},{ref},{date_str},{time_str},{freq_mhz:.3f},{mode_str},{their_call}")
    return "\n".join(rows) + "\n"


# ── SOTA CSV export ───────────────────────────────────────────────────────────

def format_sota_csv(qsos: list[dict], config: dict) -> str:
    """SOTA V2 upload format."""
    callsign  = config.get("callsign", "N0CALL").upper()
    sota_ref  = config.get("sota_ref", "")
    rows = ["V2"]
    for q in qsos:
        ref = q.get("sota_ref") or sota_ref
        ts = q.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            date_str = dt.strftime("%d/%m/%Y")
            time_str = dt.strftime("%H%M")
        except Exception:
            date_str = ""
            time_str = ""
        freq_mhz = q.get("freq_mhz")
        if freq_mhz is None:
            band = q.get("band", "").upper()
            freq_khz = BAND_FREQ_KHZ.get(band, 0)
            freq_mhz = freq_khz / 1000.0 if freq_khz else 0
        mode_str = q.get("mode", "SSB").upper()
        if mode_str in ("PH", "SSB"):
            mode_str = "SSB"
        elif mode_str == "DI":
            mode_str = "DATA"
        their_call = q.get("callsign", "").upper()
        rst_s = q.get("sent_rst", "59")
        rst_r = q.get("rcvd_rst", "59")
        # V2 format: MyCall,MyRef,Date,Time,Freq,Mode,TheirCall,SentRST,RcvdRST
        rows.append(f"{callsign}/{ref},{ref},{date_str},{time_str},{freq_mhz:.3f},{mode_str},{their_call},{rst_s},{rst_r}")
    return "\n".join(rows) + "\n"


# ── Live upload helpers ───────────────────────────────────────────────────────

async def upload_qrz(qsos: list[dict], config: dict) -> dict:
    """Upload QSOs to QRZ.com logbook via their API."""
    api_key = config.get("qrz_api_key", "")
    if not api_key:
        return {"status": "error", "detail": "qrz_api_key not configured"}
    adif = format_adif(qsos, config)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://logbook.qrz.com/api",
                data={"KEY": api_key, "ACTION": "INSERT", "ADIF": adif},
            )
        body = r.text
        if "RESULT=OK" in body or "RESULT=REPLACE" in body:
            return {"status": "ok", "detail": body.split("&")[0]}
        return {"status": "error", "detail": body[:200]}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def upload_clublog(qsos: list[dict], config: dict) -> dict:
    """Upload QSOs to Club Log via their realtime API."""
    api_key  = config.get("clublog_api_key", "")
    email    = config.get("clublog_email", "")
    callsign = config.get("callsign", "")
    if not api_key or not email or not callsign:
        return {"status": "error", "detail": "clublog_api_key / clublog_email / callsign not configured"}
    adif = format_adif(qsos, config)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://clublog.org/realtime.php",
                data={
                    "api":      api_key,
                    "email":    email,
                    "callsign": callsign.upper(),
                    "adif":     adif,
                },
            )
        if r.status_code == 200:
            return {"status": "ok", "detail": r.text[:100]}
        return {"status": "error", "detail": f"HTTP {r.status_code}: {r.text[:100]}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def upload_pota(qsos: list[dict], config: dict) -> dict:
    """Upload QSOs to POTA as ADIF."""
    username = config.get("pota_username", "")
    password = config.get("pota_password", "")
    if not username or not password:
        return {"status": "error", "detail": "pota_username / pota_password not configured"}
    adif = format_adif(qsos, config)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            # Obtain session token
            auth = await client.post(
                "https://api.pota.app/auth/login",
                json={"username": username, "password": password},
            )
            auth.raise_for_status()
            token = auth.json().get("accessToken", "")
            if not token:
                return {"status": "error", "detail": "POTA login failed"}
            # Upload ADIF
            r = await client.post(
                "https://api.pota.app/activator/uploadlog",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain",
                },
                content=adif.encode(),
            )
        if r.status_code in (200, 201):
            return {"status": "ok", "detail": "POTA upload accepted"}
        return {"status": "error", "detail": f"HTTP {r.status_code}: {r.text[:100]}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def upload_sota(qsos: list[dict], config: dict) -> dict:
    """Upload QSOs to SOTA via their V2 CSV API."""
    username  = config.get("sota_username", "")
    password  = config.get("sota_password", "")
    sota_ref  = config.get("sota_ref", "")
    callsign  = config.get("callsign", "")
    if not username or not password:
        return {"status": "error", "detail": "sota_username / sota_password not configured"}
    if not sota_ref:
        return {"status": "error", "detail": "sota_ref not configured"}
    csv_data = format_sota_csv(qsos, config)
    import base64
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api2.sota.org.uk/api/logs/upload",
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "text/csv",
                },
                content=csv_data.encode(),
            )
        if r.status_code in (200, 201, 204):
            return {"status": "ok", "detail": "SOTA upload accepted"}
        return {"status": "error", "detail": f"HTTP {r.status_code}: {r.text[:100]}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


# ── Auto-import from ECH messages ─────────────────────────────────────────────

def import_from_messages(messages: list[dict], config: dict) -> list[dict]:
    """
    Scan ECH message records and extract QSOs.
    Returns a list of QSO dicts (not yet saved — caller saves them).
    """
    import uuid
    callsign = config.get("callsign", "").upper()
    contest  = config.get("contest", "GENERAL")
    qsos = []

    for m in messages:
        adapter = m.get("source_adapter", "")
        from_id = m.get("from_id", "")
        channel = m.get("source_channel", "")

        # Skip outbound / our own messages
        if from_id in ("local", callsign, "ECH Operator"):
            continue
        if channel == "outbound":
            continue

        # Determine source category
        source = None
        band   = None
        mode   = None

        if "winlink" in adapter.lower() or "pat" in adapter.lower():
            source = "winlink"
            band   = "HF"
            mode   = "WINLINK"
        elif "aprs" in adapter.lower():
            source = "aprs"
            band   = "2M"
            mode   = "APRS"
            # Try to extract frequency from channel name (e.g. "144.390")
            try:
                freq = float(channel)
                if freq > 100:
                    band = "2M"
                elif freq > 50:
                    band = "6M"
            except (ValueError, TypeError):
                pass
        else:
            continue  # only auto-import from known digital adapters

        # Best-effort callsign extraction: from_id is usually the callsign
        their_call = from_id.upper().strip()
        if not their_call or len(their_call) < 3:
            continue

        qsos.append({
            "id":             str(uuid.uuid4()),
            "station_id":     "1",
            "callsign":       their_call,
            "band":           band,
            "mode":           mode,
            "freq_mhz":       None,
            "sent_rst":       "59",
            "rcvd_rst":       "59",
            "sent_exch":      config.get("field_day_class", "") + " " + config.get("field_day_section", ""),
            "rcvd_exch":      "",
            "notes":          (m.get("body") or "")[:80],
            "timestamp":      m.get("timestamp", ""),
            "source":         source,
            "source_adapter": adapter,
            "contest":        contest,
            "pota_ref":       config.get("pota_ref", ""),
            "sota_ref":       config.get("sota_ref", ""),
        })

    return qsos
