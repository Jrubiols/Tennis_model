"""
TENNIS BOT TELEGRAM v2.0
Novedades vs v1:
  1. Tracking ROI
  2. Pinnacle como referencia de cuotas
  3. Split dia/noche por hora del partido

COMANDOS:
  /start               — bienvenida
  /setkey KEY          — cambia API key api-tennis.com
  /surface Hard        — cambia superficie (Hard/Clay/Grass)
  /nev 15              — cambia gap minimo NEV
  /config              — ver configuracion actual
  /gano Tseng          — registra victoria
  /perdio Tseng        — registra derrota
  /roi                 — ver historial y ROI acumulado
  /limpiar             — borrar historial ROI

FORMATO PARTIDOS:
  Fonseca vs Collignon 1.75 2.05
  Tseng vs Baez 3.40 1.35 21:00
"""

import os
import json
import time
import re
import numpy as np
import requests
from datetime import datetime, timedelta
from itertools import combinations

try:
    from telegram import Update, constants
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
except ImportError:
    print("Instala: pip install python-telegram-bot==21.3")
    exit(1)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN   = "8767038110:AAHmpQjZIgIzy8sw1aXeK3rinb5Iglv8VVM"
CONFIG_FILE = "bot_config.json"
ROI_FILE    = "roi_tracking.json"
CACHE_DIR   = "cache_bot"
SEP         = "-" * 28

DEFAULT_CONFIG = {
    "api_key":             "794558d47064c313aaf7af272503014d578ac2629612cb6e49f6057cab5dcce4",
    "surface":             "Hard",
    "min_value_pct":       5.0,
    "nev_gap":             15,
    "fatigue_threshold":   8,
    "cold_threshold":      14,
    "score_enter":         3,
    "score_marginal":      2,
    "elo_k":               32,
    "elo_base":            1500,
    "min_surface_matches": 5,
    "cache_hours":         12,
    "default_stake":       10,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ============================================================
# ROI TRACKING
# ============================================================
def load_roi():
    if os.path.exists(ROI_FILE):
        with open(ROI_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"picks": [], "pending": {}}

def save_roi(data):
    with open(ROI_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def registrar_pick(pick_name, odds, value_pct, score, stake=10):
    data    = load_roi()
    pick_id = pick_name.lower().replace(" ", "_") + "_" + datetime.now().strftime("%Y%m%d")
    data["pending"][pick_id] = {
        "nombre":    pick_name,
        "odds":      odds,
        "value_pct": value_pct,
        "score":     score,
        "stake":     stake,
        "fecha":     datetime.now().strftime("%Y-%m-%d"),
    }
    save_roi(data)

def resolver_pick(nombre_parcial, gano):
    data         = load_roi()
    nombre_lower = nombre_parcial.lower()
    match_key    = None
    for k, v in data["pending"].items():
        if nombre_lower in v["nombre"].lower():
            match_key = k
            break
    if not match_key:
        return None, "No encontre ese pick en pendientes."
    pick              = data["pending"].pop(match_key)
    pick["resultado"] = "WIN" if gano else "LOSS"
    pick["profit"]    = round(pick["stake"] * (pick["odds"] - 1), 2) if gano else -pick["stake"]
    data["picks"].append(pick)
    save_roi(data)
    return pick, None

def calcular_roi_stats():
    data  = load_roi()
    picks = data["picks"]
    if not picks:
        return None
    total_stake  = sum(p["stake"] for p in picks)
    total_profit = sum(p["profit"] for p in picks)
    wins         = sum(1 for p in picks if p["resultado"] == "WIN")
    losses       = len(picks) - wins
    roi_pct      = (total_profit / total_stake * 100) if total_stake > 0 else 0
    return {
        "total":        len(picks),
        "wins":         wins,
        "losses":       losses,
        "winrate":      round(wins / len(picks) * 100, 1),
        "total_stake":  total_stake,
        "total_profit": round(total_profit, 2),
        "roi_pct":      round(roi_pct, 1),
        "pending":      len(data["pending"]),
        "picks":        picks[-5:],
    }

# ============================================================
# PINNACLE ODDS (API publica via RapidAPI demo)
# ============================================================
def get_pinnacle_odds(player_a, player_b):
    try:
        url     = "https://pinnacle-odds.p.rapidapi.com/kit/v1/markets"
        headers = {
            "X-RapidAPI-Key":  "DEMO",
            "X-RapidAPI-Host": "pinnacle-odds.p.rapidapi.com",
        }
        params = {"sport_id": 33, "is_have_odds": "true"}
        r      = requests.get(url, headers=headers, params=params, timeout=8)
        if r.status_code != 200:
            return None
        events  = r.json().get("events", [])
        a_lower = player_a.lower().split()[-1]
        b_lower = player_b.lower().split()[-1]
        for ev in events:
            home = str(ev.get("home", "")).lower()
            away = str(ev.get("away", "")).lower()
            if (a_lower in home or a_lower in away) and (b_lower in home or b_lower in away):
                ml = ev.get("periods", {}).get("num_0", {}).get("money_line", {})
                if ml:
                    def to_dec(o):
                        if o is None:
                            return None
                        if isinstance(o, float) and o > 1:
                            return round(o, 3)
                        if o > 0:
                            return round(o / 100 + 1, 3)
                        return round(100 / abs(o) + 1, 3)
                    oa = to_dec(ml.get("home"))
                    ob = to_dec(ml.get("away"))
                    if oa and ob:
                        return (oa, ob) if a_lower in home else (ob, oa)
    except Exception:
        pass
    return None

# ============================================================
# SPLIT DIA/NOCHE
# ============================================================
def analizar_hora(hora_str):
    if not hora_str:
        return None
    try:
        h = int(hora_str.split(":")[0])
    except Exception:
        return None
    if 10 <= h <= 17:
        return {"sesion": "DIA", "desc": "Sesion de dia — favorece servidores y agresivos"}
    if h >= 19 or h < 4:
        return {"sesion": "NOCHE", "desc": "Sesion de noche — favorece grinders y arcilleros"}
    return {"sesion": "TARDE", "desc": "Sesion de tarde — condiciones intermedias"}

# ============================================================
# CACHE
# ============================================================
def cache_get(key, cache_hours):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 3600
        if age < cache_hours:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return None

def cache_set(key, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, key + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ============================================================
# API-TENNIS.COM
# ============================================================
API_BASE = "https://api.api-tennis.com/tennis/"

def api_call(method, params, api_key):
    all_params = {"method": method, "APIkey": api_key}
    all_params.update(params)
    try:
        r = requests.get(API_BASE, params=all_params, timeout=15)
        return r.json()
    except Exception:
        return {}

def cargar_ranking(cfg):
    cached = cache_get("atp_standings", cfg["cache_hours"])
    if cached:
        return cached
    data    = api_call("get_standings", {"event_type": "ATP"}, cfg["api_key"])
    results = data.get("result", [])
    if not isinstance(results, list):
        return {}
    ranking = {}
    for p in results:
        nombre = p.get("player", "").strip()
        key    = p.get("player_key")
        place  = p.get("place")
        if nombre and key:
            ranking[nombre] = {
                "player_key": key,
                "ranking":    int(place) if place and str(place).isdigit() else 999,
            }
    cache_set("atp_standings", ranking)
    return ranking

def buscar_jugador(nombre, ranking):
    nombre_lower = nombre.lower()
    apellido     = nombre_lower.split()[-1]
    if nombre in ranking:
        return ranking[nombre]
    candidatos = []
    for key_name, data in ranking.items():
        key_lower = key_name.lower()
        if apellido in key_lower:
            score = sum(1 for word in nombre_lower.split() if word in key_lower)
            candidatos.append((score, key_name, data))
    if candidatos:
        candidatos.sort(reverse=True)
        return candidatos[0][2]
    return None

def obtener_historial(player_key, api_key, cache_hours):
    ckey    = "historial_" + str(player_key)
    cached  = cache_get(ckey, cache_hours)
    if cached is not None:
        return cached
    data    = api_call("get_H2H", {"first_player_key": player_key, "second_player_key": 1}, api_key)
    result  = data.get("result", {})
    partidos = result.get("firstPlayerResults", [])
    if not partidos:
        data2   = api_call("get_H2H", {"first_player_key": 1, "second_player_key": player_key}, api_key)
        result2 = data2.get("result", {})
        partidos = result2.get("secondPlayerResults", [])
    cache_set(ckey, partidos)
    return partidos

# ============================================================
# ELO
# ============================================================
def ranking_to_elo(ranking_pos, base=1500):
    if not ranking_pos or ranking_pos <= 0:
        return base
    return max(round(2400 - 400 * np.log10(max(ranking_pos, 1))), 1200)

def prob_elo(elo_a, elo_b):
    return 1 / (1 + 10 ** ((elo_b - elo_a) / 400))

def calcular_elo_superficie(partidos, player_key, superficie, cfg):
    keywords = {"Hard": ["hard", "hard (indoor)"], "Clay": ["clay"], "Grass": ["grass"]}.get(superficie, ["hard"])
    p_surf   = [p for p in partidos if any(k in str(p.get("tournament_surface", "") or "").lower() for k in keywords)]
    if not p_surf:
        p_surf = partidos
    p_surf = sorted(p_surf, key=lambda x: x.get("event_date", ""))
    elo    = cfg["elo_base"]
    k      = cfg["elo_k"]
    n      = 0
    pid    = str(player_key)
    for p in p_surf:
        winner = str(p.get("event_winner", ""))
        f_key  = str(p.get("first_player_key", ""))
        s_key  = str(p.get("second_player_key", ""))
        result = str(p.get("event_final_result", "")).upper()
        if "RET" in result or "W/O" in result:
            continue
        if pid == f_key:
            gano = "First" in winner
        elif pid == s_key:
            gano = "Second" in winner
        else:
            continue
        exp = 1 / (1 + 10 ** ((cfg["elo_base"] - elo) / 400))
        elo = elo + k * ((1.0 if gano else 0.0) - exp)
        n  += 1
    return round(elo), n

# ============================================================
# ANALISIS JUGADOR
# ============================================================
def analizar_jugador(nombre, ranking_data, cfg):
    info = buscar_jugador(nombre, ranking_data)
    if not info:
        return {
            "nombre": nombre, "ranking": 999, "player_key": None, "found": False,
            "matches_year": 0, "days_since": 999, "last_3sets": False,
            "recent_21d": 0, "ret_ratio": 0.0, "form_last5": 0.5, "form_3w": 0.5,
            "elo_surface": cfg["elo_base"], "n_surface": 0,
            "total_matches": 0, "wins_3w": 0, "total_3w": 0,
        }
    player_key  = info["player_key"]
    ranking_pos = info["ranking"]
    partidos    = obtener_historial(player_key, cfg["api_key"], cfg["cache_hours"])
    time.sleep(0.15)
    atp        = [p for p in partidos if "ouble" not in str(p.get("event_type_type", ""))]
    atp_sorted = sorted(atp, key=lambda x: x.get("event_date", ""), reverse=True)
    year_str   = str(datetime.now().year)
    p_year     = [p for p in atp_sorted if str(p.get("event_date", "")).startswith(year_str)]
    days_since = 999
    last_3sets = False
    if atp_sorted:
        try:
            days_since = (datetime.now() - datetime.strptime(atp_sorted[0]["event_date"], "%Y-%m-%d")).days
        except Exception:
            pass
        try:
            nums = [int(x) for x in str(atp_sorted[0].get("event_final_result", "")).replace("-", " ").split() if x.isdigit()]
            if sum(nums) >= 5:
                last_3sets = True
        except Exception:
            pass
    cutoff_21  = (datetime.now() - timedelta(days=21)).strftime("%Y-%m-%d")
    recent_21d = sum(1 for p in atp_sorted if p.get("event_date", "") >= cutoff_21)
    ret        = sum(1 for p in atp_sorted if "RET" in str(p.get("event_final_result", "")).upper())
    ret_ratio  = ret / max(len(atp_sorted), 1)
    pid        = str(player_key)

    def es_victoria(p):
        w = str(p.get("event_winner", ""))
        return (pid == str(p.get("first_player_key", "")) and "First" in w) or \
               (pid == str(p.get("second_player_key", "")) and "Second" in w)

    ultimos5   = [p for p in atp_sorted[:10] if "RET" not in str(p.get("event_final_result", "")).upper()][:5]
    wins5      = sum(1 for p in ultimos5 if es_victoria(p))
    form_last5 = wins5 / max(len(ultimos5), 1) if ultimos5 else 0.5
    p_3w       = [p for p in atp_sorted if p.get("event_date", "") >= cutoff_21 and "RET" not in str(p.get("event_final_result", "")).upper()]
    wins_3w    = sum(1 for p in p_3w if es_victoria(p))
    form_3w    = wins_3w / max(len(p_3w), 1) if p_3w else form_last5
    elo_surface, n_surface = calcular_elo_superficie(atp_sorted, player_key, cfg["surface"], cfg)
    return {
        "nombre": nombre, "ranking": ranking_pos, "player_key": player_key, "found": True,
        "matches_year": len(p_year), "days_since": days_since, "last_3sets": last_3sets,
        "recent_21d": recent_21d, "ret_ratio": round(ret_ratio, 3),
        "form_last5": round(form_last5, 2), "form_3w": round(form_3w, 2),
        "elo_surface": elo_surface, "n_surface": n_surface,
        "total_matches": len(atp_sorted), "wins_3w": wins_3w, "total_3w": len(p_3w),
    }

# ============================================================
# ANALISIS PARTIDO
# ============================================================
def analizar_partido(pa_nombre, pb_nombre, oa, ob, ranking_data, cfg, hora=None):
    da = analizar_jugador(pa_nombre, ranking_data, cfg)
    db = analizar_jugador(pb_nombre, ranking_data, cfg)
    ra, rb = da["ranking"], db["ranking"]
    gap    = abs(ra - rb)

    if ra == 999 or rb == 999:
        no_enc = pa_nombre if ra == 999 else pb_nombre
        return {"pa": pa_nombre, "pb": pb_nombre, "ra": ra, "rb": rb,
                "oa": oa, "ob": ob, "decision": "ERROR",
                "motivo": no_enc + " no encontrado en ranking ATP"}

    if gap < cfg["nev_gap"]:
        return {"pa": pa_nombre, "pb": pb_nombre, "ra": ra, "rb": rb,
                "oa": oa, "ob": ob, "decision": "PASS",
                "motivo": "NEV: gap " + str(gap) + " spots — demasiado igualado"}

    elo_gen_a = ranking_to_elo(ra, cfg["elo_base"])
    elo_gen_b = ranking_to_elo(rb, cfg["elo_base"])
    p_gen_a   = prob_elo(elo_gen_a, elo_gen_b)
    elo_sur_a, n_a = da["elo_surface"], da["n_surface"]
    elo_sur_b, n_b = db["elo_surface"], db["n_surface"]
    p_sur_a        = prob_elo(elo_sur_a, elo_sur_b)
    use_surface    = (n_a >= cfg["min_surface_matches"] and n_b >= cfg["min_surface_matches"])
    p_a = (0.4 * p_gen_a + 0.6 * p_sur_a) if use_surface else p_gen_a
    p_b = 1 - p_a

    raw_a, raw_b = 1 / oa, 1 / ob
    ov    = raw_a + raw_b
    imp_a = raw_a / ov
    imp_b = raw_b / ov
    val_a = (p_a - imp_a) * 100
    val_b = (p_b - imp_b) * 100

    pinnacle  = get_pinnacle_odds(pa_nombre, pb_nombre)

    if val_a >= val_b:
        pick, pick_odds, val = pa_nombre, oa, val_a
        elo_gen_ok = p_gen_a > 0.5
        elo_sur_ok = p_sur_a > 0.5
        dpick, n_surf = da, n_a
        pin_odd  = pinnacle[0] if pinnacle else None
    else:
        pick, pick_odds, val = pb_nombre, ob, val_b
        elo_gen_ok = (1 - p_gen_a) > 0.5
        elo_sur_ok = (1 - p_sur_a) > 0.5
        dpick, n_surf = db, n_b
        pin_odd  = pinnacle[1] if pinnacle else None

    pin_diff = round(((pick_odds / pin_odd) - 1) * 100, 1) if pin_odd else None

    hora_info = analizar_hora(hora)

    score     = 0
    penalizacion = 0
    lines     = []
    vetos     = []  # vetos duros que fuerzan PASS independientemente del score

    # ── 1. VALUE (base) ─────────────────────────────────────────
    if val >= cfg["min_value_pct"]:
        score += 1
        lines.append("VALUE     +" + str(round(val, 1)) + "% sobre la casa")
    else:
        lines.append("VALUE     +" + str(round(val, 1)) + "% insuficiente")

    # ── 2. ELO CONSENSO ─────────────────────────────────────────
    # Solo puntua completo si general Y superficie apuntan al mismo lado
    # Si superficie contradice con datos suficientes -> veto duro
    if elo_gen_ok and elo_sur_ok:
        score += 1
        lines.append("ELO       General + " + cfg["surface"] + " confirman")
    elif elo_gen_ok and n_surf < cfg["min_surface_matches"]:
        score += 0.5
        lines.append("ELO       General confirma, pocos datos " + cfg["surface"])
    elif elo_gen_ok and not elo_sur_ok and n_surf >= cfg["min_surface_matches"]:
        # ELO superficie contradice con datos suficientes — veto
        vetos.append("ELO " + cfg["surface"] + " contradice con " + str(n_surf) + " partidos de datos")
        lines.append("ELO       VETO — " + cfg["surface"] + " contradice el pick (" + str(dpick["elo_surface"]) + " pts)")
    else:
        # ELO general tambien contradice
        vetos.append("ELO general contradice el pick")
        lines.append("ELO       VETO — ELO general contradice")

    # ── 3. DATOS FIABLES ────────────────────────────────────────
    if dpick["matches_year"] >= 5:
        score += 1
        lines.append("DATOS     " + str(dpick["matches_year"]) + " partidos en " + str(datetime.now().year))
    elif dpick["matches_year"] >= 2:
        score += 0.5
        lines.append("DATOS     " + str(dpick["matches_year"]) + " partidos — limitado")
    else:
        lines.append("DATOS     " + str(dpick["matches_year"]) + " partidos — insuficiente")

    # ── 4. DESGASTE ─────────────────────────────────────────────
    if dpick["recent_21d"] <= cfg["fatigue_threshold"] and not dpick["last_3sets"]:
        score += 1
        lines.append("DESGASTE  " + str(dpick["recent_21d"]) + " partidos/21d, ultimo en 2 sets")
    elif dpick["recent_21d"] <= cfg["fatigue_threshold"]:
        score += 0.5
        lines.append("DESGASTE  Ultimo partido en 3 sets")
    else:
        lines.append("DESGASTE  " + str(dpick["recent_21d"]) + " partidos/21d — cargado")

    # ── 5. RITMO ────────────────────────────────────────────────
    if dpick["days_since"] <= cfg["cold_threshold"]:
        score += 1
        lines.append("RITMO     Jugo hace " + str(dpick["days_since"]) + " dias")
    elif dpick["days_since"] <= 21:
        score += 0.5
        lines.append("RITMO     Jugo hace " + str(dpick["days_since"]) + " dias")
    else:
        lines.append("RITMO     " + str(dpick["days_since"]) + " dias sin jugar — frio")

    # ── RACHA 3W — penalizacion real si mala racha ──────────────
    if dpick["total_3w"] >= 3:
        if dpick["form_3w"] >= 0.67:
            score += 0.5  # bonus por buena racha
            lines.append("RACHA 3W  " + str(dpick["wins_3w"]) + "/" + str(dpick["total_3w"]) + " victorias — buena racha")
        elif dpick["form_3w"] <= 0.33:
            penalizacion += 1.0  # penalizacion dura por mala racha
            lines.append("RACHA 3W  " + str(dpick["wins_3w"]) + "/" + str(dpick["total_3w"]) + " victorias — MALA RACHA (-1 score)")
        else:
            lines.append("RACHA 3W  " + str(dpick["wins_3w"]) + "/" + str(dpick["total_3w"]) + " victorias — neutro")
    elif dpick["form_last5"] >= 0.6:
        score += 0.5
        lines.append("FORMA 5   " + str(int(dpick["form_last5"] * 5)) + "/5 victorias ultimos 5")
    elif dpick["form_last5"] <= 0.2 and dpick["total_matches"] >= 5:
        penalizacion += 0.5
        lines.append("FORMA 5   " + str(int(dpick["form_last5"] * 5)) + "/5 victorias — mala forma (-0.5)")

    # ── ELO superficie informativo ───────────────────────────────
    if n_surf >= cfg["min_surface_matches"]:
        lines.append("ELO " + cfg["surface"] + "  " + str(dpick["elo_surface"]) + " pts (" + str(n_surf) + " partidos)")

    # ── Retiradas historicas — veto si alto ──────────────────────
    if dpick["ret_ratio"] > 0.15:
        vetos.append("Historial de retiradas alto (" + str(round(dpick["ret_ratio"] * 100)) + "%)")
        lines.append("LESION    VETO — " + str(round(dpick["ret_ratio"] * 100)) + "% retiradas historicas")
    elif dpick["ret_ratio"] > 0.10:
        penalizacion += 0.5
        lines.append("LESION    ALERTA " + str(round(dpick["ret_ratio"] * 100)) + "% retiradas (-0.5)")

    # ── Pinnacle ─────────────────────────────────────────────────
    if pin_diff is not None and pin_odd is not None:
        if pin_diff > 2:
            score += 0.5  # bonus CLV confirmado
            lines.append("PINNACLE  Winamax +" + str(pin_diff) + "% vs Pinnacle (" + str(pin_odd) + ") — CLV CONFIRMADO (+0.5)")
        elif pin_diff < -5:
            vetos.append("Pinnacle da cuota mayor que Winamax — mercado en contra")
            lines.append("PINNACLE  VETO — Pinnacle (" + str(pin_odd) + ") mejor que Winamax — sin CLV")
        elif pin_diff < -2:
            penalizacion += 0.5
            lines.append("PINNACLE  Winamax " + str(pin_diff) + "% vs Pinnacle (" + str(pin_odd) + ") — mercado en contra (-0.5)")
        else:
            lines.append("PINNACLE  Cuota similar a Pinnacle (" + str(pin_odd) + ")")

    # ── Hora/sesion ──────────────────────────────────────────────
    if hora_info:
        lines.append("SESION    " + hora_info["sesion"] + " — " + hora_info["desc"])

    # ── Score final con penalizaciones ──────────────────────────
    score = round(max(score - penalizacion, 0), 1)

    # ── Decision ────────────────────────────────────────────────
    if val < cfg["min_value_pct"]:
        decision, motivo = "PASS", "Sin value suficiente"
    elif vetos:
        decision = "PASS"
        motivo   = "VETO — " + vetos[0]
    elif score >= cfg["score_enter"]:
        decision, motivo = "ENTRA", "Score " + str(score) + "/5"
    elif score >= cfg["score_marginal"]:
        decision, motivo = "MARGINAL", "Score " + str(score) + "/5"
    else:
        decision, motivo = "PASS", "Score " + str(score) + "/5"

    return {
        "pa": pa_nombre, "pb": pb_nombre, "ra": ra, "rb": rb,
        "oa": oa, "ob": ob, "gap": gap,
        "p_a": round(p_a * 100, 1), "p_b": round(p_b * 100, 1),
        "imp_a": round(imp_a * 100, 1), "imp_b": round(imp_b * 100, 1),
        "val_a": round(val_a, 1), "val_b": round(val_b, 1),
        "overround": round((ov - 1) * 100, 1),
        "elo_sur_a": elo_sur_a, "elo_sur_b": elo_sur_b,
        "pick": pick, "pick_odds": pick_odds, "val": round(val, 1),
        "score": score, "lines": lines,
        "decision": decision, "motivo": motivo,
        "use_surface": use_surface,
        "hora_info": hora_info,
        "pin_diff": pin_diff,
    }

# ============================================================
# FORMATEAR
# ============================================================
def formatear_partido(r, cfg):
    if r["decision"] == "ERROR":
        return SEP + "\nERROR: " + r["pa"] + " vs " + r["pb"] + "\n" + r["motivo"] + "\n"

    if r["decision"] == "PASS" and "NEV" in r["motivo"]:
        return (SEP + "\nPASS  " + r["pa"] + " vs " + r["pb"] + "\n"
                + "#" + str(r["ra"]) + " vs #" + str(r["rb"]) + " | " + str(r["oa"]) + " / " + str(r["ob"]) + "\n"
                + "NEV — " + r["motivo"] + "\n")

    if r["decision"] == "PASS" and "value" in r["motivo"].lower():
        return (SEP + "\nPASS  " + r["pa"] + " vs " + r["pb"] + "\n"
                + "#" + str(r["ra"]) + " vs #" + str(r["rb"]) + " | " + str(r["oa"]) + " / " + str(r["ob"]) + "\n"
                + "Value: " + str(r["val_a"]) + "% / " + str(r["val_b"]) + "%\n"
                + "PASS — " + r["motivo"] + "\n")

    surf_tag = "60pct " + cfg["surface"] + " ELO" if r.get("use_surface") else "solo ranking"
    msg  = SEP + "\n"
    msg += "*" + r["pa"] + " vs " + r["pb"] + "*\n"
    msg += "#" + str(r["ra"]) + " vs #" + str(r["rb"]) + " | " + str(r["oa"]) + " / " + str(r["ob"]) + "\n"
    msg += "ELO " + cfg["surface"] + ": " + str(r["elo_sur_a"]) + " vs " + str(r["elo_sur_b"]) + " | " + surf_tag + "\n\n"
    msg += "*Pick: " + r["pick"] + " @ " + str(r["pick_odds"]) + "* (+" + str(r["val"]) + "% value)\n\n"
    msg += "Criterios (" + str(r["score"]) + "/5):\n"
    for line in r["lines"]:
        msg += "  " + line + "\n"
    msg += "\n*" + r["decision"] + " — " + r["motivo"] + "*\n"
    return msg

def formatear_resumen(results, cfg):
    enters    = [r for r in results if r.get("decision") == "ENTRA"]
    marginals = [r for r in results if r.get("decision") == "MARGINAL"]
    msg       = SEP + "\n*RESUMEN " + datetime.now().strftime("%d/%m/%Y") + " | " + cfg["surface"] + "*\n\n"

    if not enters and not marginals:
        msg += "Sin picks validos hoy.\n"
        return msg

    if enters:
        msg += "*ENTRA:*\n"
        for r in enters:
            s   = "***" if r["val"] > 10 else ("**" if r["val"] > 7 else "*")
            pin = " [Pin +" + str(r["pin_diff"]) + "%]" if r.get("pin_diff") and r["pin_diff"] > 0 else ""
            msg += "  " + r["pick"] + " @ " + str(r["pick_odds"]) + "  +" + str(r["val"]) + "%  " + str(r["score"]) + "/5  " + s + pin + "\n"

    if marginals:
        msg += "\n*MARGINAL:*\n"
        for r in marginals:
            msg += "  " + r["pick"] + " @ " + str(r["pick_odds"]) + "  +" + str(r["val"]) + "%  " + str(r["score"]) + "/5\n"

    solidos = [r for r in enters if r["score"] >= 4]
    if len(solidos) >= 2:
        msg += "\n*Combinadas (score 4+):*\n"
        for r1, r2 in combinations(solidos, 2):
            comb = round(r1["pick_odds"] * r2["pick_odds"], 2)
            msg += "  " + r1["pick"].split()[-1] + " + " + r2["pick"].split()[-1] + ": *" + str(comb) + "x* (10e=" + str(round(comb * 10)) + "e)\n"

    if len(enters) >= 3:
        comb_all = 1.0
        for r in enters:
            comb_all *= r["pick_odds"]
        msg += "\nFunbet (" + str(len(enters)) + " picks): *" + str(round(comb_all, 2)) + "x* — solo monedas\n"

    roi_data = load_roi()
    if roi_data["pending"]:
        msg += "\n*Pendientes de resultado: " + str(len(roi_data["pending"])) + "*\n"
        for k, v in roi_data["pending"].items():
            msg += "  " + v["nombre"] + " @ " + str(v["odds"]) + " (" + v["fecha"] + ")\n"
        msg += "Usa /gano o /perdio para registrar\n"

    return msg

# ============================================================
# PARSEAR PARTIDOS
# ============================================================
def parsear_partidos(texto):
    partidos = []
    lineas   = [l.strip() for l in texto.strip().split("\n") if l.strip()]
    for linea in lineas:
        hora_match    = re.search(r"\b(\d{1,2}:\d{2})\b", linea)
        hora          = hora_match.group(1) if hora_match else None
        linea_sin_hora = re.sub(r"\b\d{1,2}:\d{2}\b", "", linea).strip()
        nums           = re.findall(r"\d+[.,]\d+", linea_sin_hora)
        if len(nums) < 2:
            continue
        oa = float(nums[-2].replace(",", "."))
        ob = float(nums[-1].replace(",", "."))
        texto_nombres = re.sub(r"\d+[.,]\d+", "", linea_sin_hora).strip()
        sep = re.split(r"\s+vs\.?\s+|\s+-\s+|\s+/\s+", texto_nombres, flags=re.IGNORECASE)
        if len(sep) < 2:
            continue
        pa = sep[0].strip().title()
        pb = sep[1].strip().title()
        if pa and pb and oa > 1 and ob > 1:
            partidos.append((pa, pb, oa, ob, hora))
    return partidos

# ============================================================
# HANDLERS
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg      = load_config()
    roi_data = load_roi()
    pending  = len(roi_data["pending"])
    txt_pend = "\nPicks pendientes: *" + str(pending) + "*" if pending else ""
    msg = ("TENNIS MODEL BOT v2.0\n\n"
           "Envia los partidos:\n"
           "`Fonseca vs Collignon 1.75 2.05`\n"
           "`Tseng vs Baez 3.40 1.35 21:00`\n\n"
           "*Comandos:*\n"
           "`/setkey KEY` — nueva API key\n"
           "`/surface Hard` — superficie\n"
           "`/nev 15` — gap NEV\n"
           "`/config` — ver config\n"
           "`/gano Tseng` — pick ganado\n"
           "`/perdio Tseng` — pick perdido\n"
           "`/roi` — historial y ROI\n"
           "`/limpiar` — borrar historial\n\n"
           "Superficie: *" + cfg["surface"] + "* | NEV: *" + str(cfg["nev_gap"]) + "*" + txt_pend)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/setkey TU_API_KEY`", parse_mode="Markdown")
        return
    cfg = load_config()
    cfg["api_key"] = context.args[0]
    save_config(cfg)
    rpath = os.path.join(CACHE_DIR, "atp_standings.json")
    if os.path.exists(rpath):
        os.remove(rpath)
    await update.message.reply_text("API key actualizada. Cache limpiada.")

async def cmd_surface(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/surface Hard` (Hard/Clay/Grass)", parse_mode="Markdown")
        return
    sup = context.args[0].capitalize()
    if sup not in ["Hard", "Clay", "Grass"]:
        await update.message.reply_text("Opciones: Hard, Clay, Grass")
        return
    cfg = load_config()
    cfg["surface"] = sup
    save_config(cfg)
    await update.message.reply_text("Superficie: *" + sup + "*", parse_mode="Markdown")

async def cmd_nev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/nev 15`", parse_mode="Markdown")
        return
    try:
        gap = int(context.args[0])
        cfg = load_config()
        cfg["nev_gap"] = gap
        save_config(cfg)
        await update.message.reply_text("NEV gap: *" + str(gap) + "* spots", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("Numero entero. Ej: `/nev 15`", parse_mode="Markdown")

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = ("*Config actual:*\n\n"
           "Superficie: *" + cfg["surface"] + "*\n"
           "NEV gap: *" + str(cfg["nev_gap"]) + "* spots\n"
           "Value minimo: *" + str(cfg["min_value_pct"]) + "%*\n"
           "Score ENTRA: *" + str(cfg["score_enter"]) + "/5*\n"
           "Cache: *" + str(cfg["cache_hours"]) + "h*\n"
           "API key: `..." + cfg["api_key"][-8:] + "`")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_gano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/gano Tseng`", parse_mode="Markdown")
        return
    nombre = " ".join(context.args)
    pick, err = resolver_pick(nombre, gano=True)
    if err:
        await update.message.reply_text("No encontre '" + nombre + "' en picks pendientes.")
        return
    await update.message.reply_text(
        "GANADO — " + pick["nombre"] + " @ " + str(pick["odds"]) + "\n"
        "Beneficio: +" + str(pick["profit"]) + "e\n\nUsa /roi para ver el historial."
    )

async def cmd_perdio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/perdio Tseng`", parse_mode="Markdown")
        return
    nombre = " ".join(context.args)
    pick, err = resolver_pick(nombre, gano=False)
    if err:
        await update.message.reply_text("No encontre '" + nombre + "' en picks pendientes.")
        return
    await update.message.reply_text(
        "PERDIDO — " + pick["nombre"] + " @ " + str(pick["odds"]) + "\n"
        "Perdida: -" + str(pick["stake"]) + "e\n\nUsa /roi para ver el historial."
    )

async def cmd_roi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats    = calcular_roi_stats()
    roi_data = load_roi()
    if not stats:
        pending = len(roi_data.get("pending", {}))
        msg     = "Sin historial todavia."
        if pending:
            msg += "\n" + str(pending) + " picks pendientes de resultado."
        await update.message.reply_text(msg)
        return
    trend = "arriba" if stats["roi_pct"] > 0 else "abajo"
    msg  = "*ROI ACUMULADO*\n\n"
    msg += "Total: " + str(stats["total"]) + " (" + str(stats["wins"]) + "W / " + str(stats["losses"]) + "L)\n"
    msg += "Winrate: " + str(stats["winrate"]) + "%\n"
    msg += "Stake total: " + str(stats["total_stake"]) + "e\n"
    msg += "Profit: " + ("+" if stats["total_profit"] > 0 else "") + str(stats["total_profit"]) + "e\n"
    msg += "ROI: *" + ("+" if stats["roi_pct"] > 0 else "") + str(stats["roi_pct"]) + "%* (" + trend + ")\n"
    if roi_data.get("pending"):
        msg += "\nPendientes: " + str(len(roi_data["pending"])) + "\n"
        for k, v in roi_data["pending"].items():
            msg += "  " + v["nombre"] + " @ " + str(v["odds"]) + " (" + v["fecha"] + ")\n"
    if stats["picks"]:
        msg += "\n*Ultimos picks:*\n"
        for p in reversed(stats["picks"]):
            r   = "W" if p["resultado"] == "WIN" else "L"
            prf = ("+" if p["profit"] > 0 else "") + str(round(p["profit"]))
            msg += "[" + r + "] " + p["nombre"] + " @ " + str(p["odds"]) + " = " + prf + "e\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_roi({"picks": [], "pending": {}})
    await update.message.reply_text("Historial ROI borrado.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto    = update.message.text
    partidos = parsear_partidos(texto)
    if not partidos:
        await update.message.reply_text(
            "Formato:\n`Fonseca vs Collignon 1.75 2.05`\n`Tseng vs Baez 3.40 1.35 21:00`",
            parse_mode="Markdown"
        )
        return
    cfg = load_config()
    await update.message.reply_text("Analizando " + str(len(partidos)) + " partido(s)... (30-60s)")
    ranking_data = cargar_ranking(cfg)
    if not ranking_data:
        await update.message.reply_text("Error con API-Tennis. Comprueba la key con /config")
        return
    results = []
    for pa, pb, oa, ob, hora in partidos:
        r = analizar_partido(pa, pb, oa, ob, ranking_data, cfg, hora)
        results.append(r)
    for r in results:
        msg = formatear_partido(r, cfg)
        await update.message.reply_text(msg, parse_mode="Markdown")
        if r.get("decision") == "ENTRA":
            registrar_pick(r["pick"], r["pick_odds"], r["val"], r["score"],
                           stake=cfg.get("default_stake", 10))
        time.sleep(0.3)
    if len(results) > 1:
        await update.message.reply_text(formatear_resumen(results, cfg), parse_mode="Markdown")

# ============================================================
# MAIN
# ============================================================
def main():
    print("Tennis Model Bot v2.0 arrancando...")
    print("Token: ..." + BOT_TOKEN[-10:])
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("setkey",  cmd_setkey))
    app.add_handler(CommandHandler("surface", cmd_surface))
    app.add_handler(CommandHandler("nev",     cmd_nev))
    app.add_handler(CommandHandler("config",  cmd_config))
    app.add_handler(CommandHandler("gano",    cmd_gano))
    app.add_handler(CommandHandler("perdio",  cmd_perdio))
    app.add_handler(CommandHandler("roi",     cmd_roi))
    app.add_handler(CommandHandler("limpiar", cmd_limpiar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot activo.")
    app.run_polling()

if __name__ == "__main__":
    main()
