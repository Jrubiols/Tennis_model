"""
TENNIS BETTING MODEL v5.0
Mejoras vs v4:
  1. Forma CORREGIDA — cuenta victorias reales del jugador (bug v4 arreglado)
  2. Surface ELO — ELO separado por superficie (Hard/Clay/Grass) usando historial real
  3. ELO consenso — solo puntua si ELO general Y ELO superficie apuntan igual
  4. Racha reciente — forma ultimas 3 semanas ademas de ultimos 5 partidos
"""

import numpy as np
import requests
import json
import os
import time
from datetime import datetime, timedelta

try:
    from colorama import Fore, Style, init
    init(autoreset=True)
    GREEN  = Fore.GREEN
    RED    = Fore.RED
    YELLOW = Fore.YELLOW
    CYAN   = Fore.CYAN
    BOLD   = Style.BRIGHT
    RESET  = Style.RESET_ALL
except ImportError:
    GREEN = RED = YELLOW = CYAN = BOLD = RESET = ""

# ============================================================
# CONFIG
# ============================================================
API_KEY  = "794558d47064c313aaf7af272503014d578ac2629612cb6e49f6057cab5dcce4"
API_BASE = "https://api.api-tennis.com/tennis/"

CONFIG = {
    "min_value_pct":     5.0,
    "nev_gap":           15,
    "fatigue_threshold": 8,
    "cold_threshold":    14,
    "score_enter":       3,
    "score_marginal":    2,
    "cache_dir":         "cache_v5",
    "cache_hours":       12,
    "elo_k":             32,
    "elo_base":          1500,
    "min_surface_matches": 5,   # minimo partidos en superficie para usar surface ELO
}

SURFACE = "Hard"

# ============================================================
# PARTIDOS DE HOY
# Formato: (jugador_a, jugador_b, cuota_a, cuota_b)
# ============================================================
TODAYS_MATCHES = [
    ("Joao Fonseca",              "Raphael Collignon",          1.75, 2.05),
    ("Stefanos Tsitsipas",        "Denis Shapovalov",           1.75, 2.05),
    ("Terence Atmane",            "Grigor Dimitrov",            2.25, 1.68),
    ("Jacob Fearnley",            "Damir Dzumhur",              1.78, 2.10),
    ("Hubert Hurkacz",            "Aleksandar Kovacevic",       1.41, 3.05),
    ("Zachary Svajda",            "Marin Cilic",                2.10, 1.74),
    ("Kamil Majchrzak",           "Giovanni Mpetshi Perricard", 1.76, 2.15),
    ("Alexander Shevchenko",      "Shimabukuro Sho",            2.20, 1.70),
    ("Chun Hsin Tseng",           "Sebastian Baez",             3.40, 1.35),
    ("Fabian Marozsan",           "Roberto Bautista Agut",      1.31, 3.65),
]

# ============================================================
# CACHE
# ============================================================
def cache_get(key):
    os.makedirs(CONFIG['cache_dir'], exist_ok=True)
    path = f"{CONFIG['cache_dir']}/{key}.json"
    if os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 3600
        if age < CONFIG['cache_hours']:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    return None

def cache_set(key, data):
    os.makedirs(CONFIG['cache_dir'], exist_ok=True)
    path = f"{CONFIG['cache_dir']}/{key}.json"
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ============================================================
# API
# ============================================================
def api_call(method, params={}):
    all_params = {"method": method, "APIkey": API_KEY}
    all_params.update(params)
    try:
        r = requests.get(API_BASE, params=all_params, timeout=15)
        return r.json()
    except Exception as e:
        print(f"  {RED}API error: {e}{RESET}")
        return {}

# ============================================================
# RANKING ATP
# ============================================================
_ranking_cache = {}

def cargar_ranking():
    global _ranking_cache
    cached = cache_get("atp_standings")
    if cached:
        _ranking_cache = cached
        return

    print(f"  {YELLOW}Descargando ranking ATP...{RESET}", flush=True)
    data = api_call("get_standings", {"event_type": "ATP"})
    results = data.get('result', [])

    if not isinstance(results, list):
        print(f"  {RED}Error cargando ranking{RESET}")
        return

    for p in results:
        nombre = p.get('player', '').strip()
        key    = p.get('player_key')
        place  = p.get('place')
        if nombre and key:
            _ranking_cache[nombre] = {
                'player_key': key,
                'ranking':    int(place) if place and str(place).isdigit() else 999,
                'points':     p.get('points', 0),
            }

    cache_set("atp_standings", _ranking_cache)
    print(f"  {GREEN}{len(_ranking_cache)} jugadores en ranking{RESET}")

def buscar_jugador(nombre):
    nombre_lower = nombre.lower()
    apellido = nombre_lower.split()[-1]

    if nombre in _ranking_cache:
        return _ranking_cache[nombre]

    candidatos = []
    for key_name, data in _ranking_cache.items():
        key_lower = key_name.lower()
        if apellido in key_lower:
            score = sum(1 for word in nombre_lower.split() if word in key_lower)
            candidatos.append((score, key_name, data))

    if candidatos:
        candidatos.sort(reverse=True)
        return candidatos[0][2]
    return None

# ============================================================
# HISTORIAL via H2H
# ============================================================
def obtener_historial(player_key):
    ckey = f"historial_{player_key}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached

    data = api_call("get_H2H", {
        "first_player_key":  player_key,
        "second_player_key": 1
    })
    result  = data.get('result', {})
    partidos = result.get('firstPlayerResults', [])

    if not partidos:
        data2   = api_call("get_H2H", {"first_player_key": 1, "second_player_key": player_key})
        result2 = data2.get('result', {})
        partidos = result2.get('secondPlayerResults', [])

    cache_set(ckey, partidos)
    return partidos

# ============================================================
# ELO CALCULADO DESDE HISTORIAL REAL
# ============================================================
def calcular_elo_superficie(partidos, player_key, superficie):
    """
    Calcula ELO del jugador solo con partidos en la superficie dada.
    Usa sistema ELO estandar K=32 sobre todos los partidos disponibles ordenados por fecha.
    Devuelve (elo_actual, num_partidos_superficie)
    """
    # Palabras clave por superficie
    surface_keywords = {
        "Hard":  ["hard", "hard (indoor)"],
        "Clay":  ["clay"],
        "Grass": ["grass"],
    }
    keywords = surface_keywords.get(superficie, ["hard"])

    # Filtrar partidos de la superficie y ordenar por fecha ascendente
    p_surface = []
    for p in partidos:
        tipo = str(p.get('tournament_surface', '') or p.get('event_surface', '')).lower()
        if any(k in tipo for k in keywords):
            p_surface.append(p)

    # Si no hay campo surface en el historial, usar todos (fallback)
    if not p_surface:
        p_surface = partidos

    p_surface = sorted(p_surface, key=lambda x: x.get('event_date', ''))

    elo = CONFIG['elo_base']
    k   = CONFIG['elo_k']
    n   = 0

    for p in p_surface:
        winner  = str(p.get('event_winner', ''))
        f_key   = str(p.get('first_player_key', ''))
        s_key   = str(p.get('second_player_key', ''))
        pid     = str(player_key)

        # Determinar si ganó o perdió
        if pid == f_key:
            ganó = 'First' in winner
            elo_opp = CONFIG['elo_base']  # oponente asumido base (no tenemos su ELO aqui)
        elif pid == s_key:
            ganó = 'Second' in winner
            elo_opp = CONFIG['elo_base']
        else:
            continue

        # Skip retiradas
        result_str = str(p.get('event_final_result', '')).upper()
        if 'RET' in result_str or 'W/O' in result_str:
            continue

        expected = 1 / (1 + 10 ** ((elo_opp - elo) / 400))
        actual   = 1.0 if ganó else 0.0
        elo      = elo + k * (actual - expected)
        n       += 1

    return round(elo), n

# ============================================================
# ANALISIS COMPLETO DE JUGADOR
# ============================================================
def analizar_jugador(nombre):
    info = buscar_jugador(nombre)
    if not info:
        return {
            'nombre': nombre, 'ranking': 999, 'player_key': None,
            'found': False, 'matches_year': 0, 'days_since': 999,
            'last_3sets': False, 'recent_21d': 0, 'ret_ratio': 0.0,
            'form_last5': 0.5, 'form_3w': 0.5,
            'elo_surface': CONFIG['elo_base'], 'n_surface': 0,
            'total_matches': 0,
        }

    player_key = info['player_key']
    ranking    = info['ranking']
    partidos   = obtener_historial(player_key)
    time.sleep(0.2)

    # Filtrar ATP Singles (no doubles)
    atp = [p for p in partidos
           if 'ouble' not in str(p.get('event_type_type', ''))]

    atp_sorted = sorted(atp, key=lambda x: x.get('event_date', ''), reverse=True)

    year_str = str(datetime.now().year)
    p_year   = [p for p in atp_sorted if str(p.get('event_date', '')).startswith(year_str)]

    # --- Dias desde ultimo partido ---
    days_since  = 999
    last_3sets  = False
    if atp_sorted:
        try:
            days_since = (datetime.now() - datetime.strptime(atp_sorted[0]['event_date'], '%Y-%m-%d')).days
        except:
            pass
        score_str = str(atp_sorted[0].get('event_final_result', ''))
        try:
            nums = [int(x) for x in score_str.replace('-', ' ').split() if x.isdigit()]
            if sum(nums) >= 5:
                last_3sets = True
        except:
            pass

    # --- Partidos ultimos 21 dias ---
    cutoff_21 = (datetime.now() - timedelta(days=21)).strftime('%Y-%m-%d')
    recent_21d = sum(1 for p in atp_sorted if p.get('event_date', '') >= cutoff_21)

    # --- Ratio retiradas ---
    ret       = sum(1 for p in atp_sorted if 'RET' in str(p.get('event_final_result', '')).upper())
    ret_ratio = ret / max(len(atp_sorted), 1)

    # --- FORMA CORREGIDA: contar victorias reales del jugador ---
    pid = str(player_key)

    def es_victoria(p):
        winner = str(p.get('event_winner', ''))
        f_key  = str(p.get('first_player_key', ''))
        s_key  = str(p.get('second_player_key', ''))
        if pid == f_key and 'First' in winner:
            return True
        if pid == s_key and 'Second' in winner:
            return True
        return False

    # Forma ultimos 5 partidos
    ultimos5   = [p for p in atp_sorted[:10] if 'RET' not in str(p.get('event_final_result','')).upper()][:5]
    wins5      = sum(1 for p in ultimos5 if es_victoria(p))
    form_last5 = wins5 / max(len(ultimos5), 1) if ultimos5 else 0.5

    # Forma ultimas 3 semanas
    cutoff_3w  = (datetime.now() - timedelta(days=21)).strftime('%Y-%m-%d')
    p_3w       = [p for p in atp_sorted
                  if p.get('event_date', '') >= cutoff_3w
                  and 'RET' not in str(p.get('event_final_result','')).upper()]
    wins_3w    = sum(1 for p in p_3w if es_victoria(p))
    form_3w    = wins_3w / max(len(p_3w), 1) if p_3w else form_last5  # fallback a form5

    # --- Surface ELO ---
    elo_surface, n_surface = calcular_elo_superficie(atp_sorted, player_key, SURFACE)

    return {
        'nombre':       nombre,
        'ranking':      ranking,
        'player_key':   player_key,
        'found':        True,
        'matches_year': len(p_year),
        'days_since':   days_since,
        'last_3sets':   last_3sets,
        'recent_21d':   recent_21d,
        'ret_ratio':    round(ret_ratio, 3),
        'form_last5':   round(form_last5, 2),
        'form_3w':      round(form_3w, 2),
        'elo_surface':  elo_surface,
        'n_surface':    n_surface,
        'total_matches': len(atp_sorted),
        'wins_3w':      wins_3w,
        'total_3w':     len(p_3w),
    }

# ============================================================
# ELO POR RANKING (general)
# ============================================================
def ranking_to_elo(ranking):
    if not ranking or ranking <= 0:
        return 1500
    return max(round(2400 - 400 * np.log10(max(ranking, 1))), 1200)

def prob_elo(elo_a, elo_b):
    return 1 / (1 + 10 ** ((elo_b - elo_a) / 400))

# ============================================================
# SCORING v5
# ============================================================
def calcular_score(value_pct, elo_general_ok, elo_surface_ok, n_surface, d):
    score      = 0
    criterios  = []

    # 1. Value matematico
    if value_pct >= CONFIG['min_value_pct']:
        score += 1
        criterios.append((True,  f"VALUE      +{value_pct:.1f}% sobre la casa"))
    else:
        criterios.append((False, f"VALUE      +{value_pct:.1f}% insuficiente"))

    # 2. ELO consenso (general + superficie apuntan igual = mas fiable)
    if elo_general_ok and elo_surface_ok:
        score += 1
        criterios.append((True,  f"ELO        General + {SURFACE} confirman el pick"))
    elif elo_general_ok and n_surface < CONFIG['min_surface_matches']:
        score += 0.5
        criterios.append((None,  f"ELO        General confirma, pocos datos {SURFACE} ({n_surface})"))
    elif elo_general_ok:
        score += 0.5
        criterios.append((None,  f"ELO        General confirma pero {SURFACE} contradice"))
    else:
        criterios.append((False, "ELO        Contradice el pick"))

    # 3. Datos fiables 2026
    if d['matches_year'] >= 5:
        score += 1
        criterios.append((True,  f"DATOS      {d['matches_year']} partidos en {datetime.now().year}"))
    elif d['matches_year'] >= 2:
        score += 0.5
        criterios.append((None,  f"DATOS      {d['matches_year']} partidos en {datetime.now().year} — limitado"))
    else:
        criterios.append((False, f"DATOS      {d['matches_year']} partidos en {datetime.now().year}"))

    # 4. Desgaste fisico
    if d['recent_21d'] <= CONFIG['fatigue_threshold'] and not d['last_3sets']:
        score += 1
        criterios.append((True,  f"DESGASTE   {d['recent_21d']} partidos/21d, ultimo en 2 sets"))
    elif d['recent_21d'] <= CONFIG['fatigue_threshold']:
        score += 0.5
        criterios.append((None,  f"DESGASTE   Ultimo partido en 3 sets"))
    else:
        criterios.append((False, f"DESGASTE   {d['recent_21d']} partidos/21d — cargado"))

    # 5. Ritmo
    if d['days_since'] <= CONFIG['cold_threshold']:
        score += 1
        criterios.append((True,  f"RITMO      Jugo hace {d['days_since']} dias"))
    elif d['days_since'] <= 21:
        score += 0.5
        criterios.append((None,  f"RITMO      Jugo hace {d['days_since']} dias"))
    else:
        criterios.append((False, f"RITMO      {d['days_since']} dias sin jugar — frio"))

    # --- Bonus / Alertas (no suman al score base pero informan) ---

    # Forma ultimas 3 semanas (bonus si hay partidos suficientes)
    if d['total_3w'] >= 3:
        if d['form_3w'] >= 0.67:
            criterios.append((True,  f"RACHA 3W   {d['wins_3w']}/{d['total_3w']} victorias ultimas 3 semanas"))
        elif d['form_3w'] <= 0.33:
            criterios.append((False, f"RACHA 3W   {d['wins_3w']}/{d['total_3w']} victorias ultimas 3 semanas — mala racha"))
        else:
            criterios.append((None,  f"RACHA 3W   {d['wins_3w']}/{d['total_3w']} victorias ultimas 3 semanas"))
    elif d['form_last5'] >= 0.6:
        criterios.append((True,  f"FORMA 5    {int(d['form_last5']*5)}/5 victorias ultimos 5"))
    elif d['form_last5'] <= 0.2 and d['total_matches'] >= 5:
        criterios.append((False, f"FORMA 5    {int(d['form_last5']*5)}/5 victorias ultimos 5 — mala racha"))

    # Surface ELO informativo
    if n_surface >= CONFIG['min_surface_matches']:
        criterios.append((None, f"ELO {SURFACE:5}  {d['elo_surface']} pts ({n_surface} partidos)"))

    # Alerta retiradas
    if d['ret_ratio'] > 0.10:
        criterios.append((False, f"LESION     ALERTA {d['ret_ratio']*100:.0f}% retiradas historicas"))

    return round(score, 1), criterios

# ============================================================
# ANALIZAR PARTIDO
# ============================================================
def analizar_partido(pa_nombre, pb_nombre, oa, ob):
    print(f"\n  {CYAN}{pa_nombre} vs {pb_nombre}{RESET}")
    da = analizar_jugador(pa_nombre)
    db = analizar_jugador(pb_nombre)

    ra = da['ranking']
    rb = db['ranking']

    print(f"    {pa_nombre}: #{ra}  ELO-{SURFACE}={da['elo_surface']}  forma3w={da['wins_3w']}/{da['total_3w']}  {da['days_since']}d")
    print(f"    {pb_nombre}: #{rb}  ELO-{SURFACE}={db['elo_surface']}  forma3w={db['wins_3w']}/{db['total_3w']}  {db['days_since']}d")

    gap = abs(ra - rb)
    if gap < CONFIG['nev_gap'] and ra != 999 and rb != 999:
        return {
            'pa': pa_nombre, 'pb': pb_nombre, 'ra': ra, 'rb': rb,
            'oa': oa, 'ob': ob,
            'decision': 'PASS', 'motivo': f'NEV: gap {gap} < {CONFIG["nev_gap"]} spots',
        }

    # ELO general por ranking
    elo_gen_a = ranking_to_elo(ra)
    elo_gen_b = ranking_to_elo(rb)
    p_gen_a   = prob_elo(elo_gen_a, elo_gen_b)
    p_gen_b   = 1 - p_gen_a

    # ELO superficie
    elo_sur_a = da['elo_surface']
    elo_sur_b = db['elo_surface']
    p_sur_a   = prob_elo(elo_sur_a, elo_sur_b)
    p_sur_b   = 1 - p_sur_a

    # Probabilidad combinada (60% superficie si hay datos, 100% general si no)
    n_a = da['n_surface']
    n_b = db['n_surface']
    use_surface = (n_a >= CONFIG['min_surface_matches'] and n_b >= CONFIG['min_surface_matches'])

    if use_surface:
        p_a = 0.4 * p_gen_a + 0.6 * p_sur_a
        p_b = 1 - p_a
    else:
        p_a = p_gen_a
        p_b = p_gen_b

    # Cuotas implícitas
    raw_a, raw_b = 1/oa, 1/ob
    ov    = raw_a + raw_b
    imp_a = raw_a / ov
    imp_b = raw_b / ov

    val_a = (p_a - imp_a) * 100
    val_b = (p_b - imp_b) * 100

    # Pick con mas value
    if val_a >= val_b:
        pick, pick_odds, val = pa_nombre, oa, val_a
        elo_general_ok  = p_gen_a > 0.5
        elo_surface_ok  = p_sur_a > 0.5
        dpick, n_surf   = da, n_a
    else:
        pick, pick_odds, val = pb_nombre, ob, val_b
        elo_general_ok  = p_gen_b > 0.5
        elo_surface_ok  = p_sur_b > 0.5
        dpick, n_surf   = db, n_b

    score, criterios = calcular_score(val, elo_general_ok, elo_surface_ok, n_surf, dpick)

    if val < CONFIG['min_value_pct']:
        decision, motivo = 'PASS',     'Sin value suficiente'
    elif score >= CONFIG['score_enter']:
        decision, motivo = 'ENTRA',    f'Score {score}/5'
    elif score >= CONFIG['score_marginal']:
        decision, motivo = 'MARGINAL', f'Score {score}/5'
    else:
        decision, motivo = 'PASS',     f'Score {score}/5'

    return {
        'pa': pa_nombre, 'pb': pb_nombre, 'ra': ra, 'rb': rb,
        'oa': oa, 'ob': ob, 'gap': gap,
        'elo_gen_a': elo_gen_a, 'elo_gen_b': elo_gen_b,
        'elo_sur_a': elo_sur_a, 'elo_sur_b': elo_sur_b,
        'p_a': round(p_a*100,1), 'p_b': round(p_b*100,1),
        'imp_a': round(imp_a*100,1), 'imp_b': round(imp_b*100,1),
        'val_a': round(val_a,1), 'val_b': round(val_b,1),
        'overround': round((ov-1)*100,1),
        'pick': pick, 'pick_odds': pick_odds, 'val': round(val,1),
        'score': score, 'criterios': criterios,
        'decision': decision, 'motivo': motivo,
        'use_surface': use_surface,
    }

# ============================================================
# DISPLAY
# ============================================================
def vc(v):
    return GREEN if v >= CONFIG['min_value_pct'] else (YELLOW if v > 0 else RED)

def ic(estado):
    if estado is True:  return f"{GREEN}SI{RESET}"
    if estado is False: return f"{RED}NO{RESET}"
    return f"{YELLOW}--{RESET}"

def mostrar(r):
    print(f"\n{'─'*64}")
    print(f"  {BOLD}{CYAN}{r['pa']} vs {r['pb']}{RESET}")
    print(f"  #{r['ra']} vs #{r['rb']}  |  Cuotas: {r['oa']} / {r['ob']}")
    print(f"{'─'*64}")

    if 'NEV' in r.get('motivo',''):
        print(f"  {YELLOW}PASS — {r['motivo']}{RESET}")
        return
    if r['decision'] == 'PASS' and 'value' in r.get('motivo','').lower():
        print(f"  Value: {vc(r['val_a'])}{r['val_a']:+.1f}%{RESET} / {vc(r['val_b'])}{r['val_b']:+.1f}%")
        print(f"  {RED}PASS — {r['motivo']}{RESET}")
        return

    surf_tag = f" (60% {SURFACE} ELO)" if r.get('use_surface') else " (solo ranking)"
    print(f"\n  Probabilidad combinada{surf_tag}:")
    print(f"  {r['pa'][:32]:32}  {vc(r['val_a'])}{r['val_a']:+5.1f}%{RESET}  ELO {r['p_a']:5.1f}%  Casa {r['imp_a']:5.1f}%")
    print(f"  {r['pb'][:32]:32}  {vc(r['val_b'])}{r['val_b']:+5.1f}%{RESET}  ELO {r['p_b']:5.1f}%  Casa {r['imp_b']:5.1f}%")
    print(f"  Overround: {r['overround']}%  |  ELO Hard: {r['elo_sur_a']} vs {r['elo_sur_b']}")

    print(f"\n  Pick: {BOLD}{r['pick']}{RESET} @ {r['pick_odds']}  ({r['val']:+.1f}% value)")
    print(f"\n  Criterios ({r['score']}/5):")
    for estado, desc in r['criterios']:
        print(f"    [{ic(estado)}] {desc}")

    if r['decision'] == 'ENTRA':
        print(f"\n  {GREEN}{BOLD}>>> ENTRA — {r['motivo']}{RESET}")
    elif r['decision'] == 'MARGINAL':
        print(f"\n  {YELLOW}{BOLD}>>> MARGINAL — {r['motivo']}{RESET}")
    else:
        print(f"\n  {RED}>>> PASS — {r['motivo']}{RESET}")

def resumen(results):
    enters    = [r for r in results if r.get('decision') == 'ENTRA']
    marginals = [r for r in results if r.get('decision') == 'MARGINAL']

    print(f"\n{'═'*64}")
    print(f"  {BOLD}{CYAN}RESUMEN — {datetime.now().strftime('%d/%m/%Y')}  |  {SURFACE}{RESET}")
    print(f"{'═'*64}")

    if not enters and not marginals:
        print(f"  {YELLOW}Sin picks validos hoy.{RESET}")
        return

    if enters:
        print(f"\n  {GREEN}{BOLD}ENTRA:{RESET}")
        for r in enters:
            s = "***" if r['val']>10 else ("**" if r['val']>7 else "*")
            print(f"    {BOLD}{r['pick']:34}{RESET} @ {r['pick_odds']}  {r['val']:+.1f}%  {r['score']}/5  {s}")

    if marginals:
        print(f"\n  {YELLOW}{BOLD}MARGINAL:{RESET}")
        for r in marginals:
            print(f"    {r['pick']:34} @ {r['pick_odds']}  {r['val']:+.1f}%  {r['score']}/5")

    # Combinadas sugeridas (solo picks ENTRA con score >= 4)
    solidos = [r for r in enters if r['score'] >= 4]
    if len(solidos) >= 2:
        print(f"\n  {'─'*50}")
        print(f"  {BOLD}COMBINADAS SUGERIDAS (score >= 4/5):{RESET}")
        # Todas las combinaciones de 2
        from itertools import combinations
        for r1, r2 in combinations(solidos, 2):
            comb = r1['pick_odds'] * r2['pick_odds']
            print(f"  {r1['pick'].split()[-1]} + {r2['pick'].split()[-1]}: {round(comb,2)}x  "
                  f"(10e={comb*10:.0f}  20e={comb*20:.0f})")

    if len(enters) >= 3:
        comb_all = 1.0
        for r in enters: comb_all *= r['pick_odds']
        print(f"\n  Full ({len(enters)} picks): {round(comb_all,2)}x  — solo funbet")

# ============================================================
# MAIN
# ============================================================
def main():
    print(f"\n{'═'*64}")
    print(f"  {BOLD}{CYAN}TENNIS BETTING MODEL v5.0{RESET}")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}  |  {SURFACE}  |  Surface ELO + Forma real")
    print(f"{'═'*64}\n")

    os.makedirs(CONFIG['cache_dir'], exist_ok=True)

    print("Cargando ranking ATP...")
    cargar_ranking()
    print("Analizando jugadores...\n")

    results = []
    for pa, pb, oa, ob in TODAYS_MATCHES:
        r = analizar_partido(pa, pb, oa, ob)
        mostrar(r)
        results.append(r)
        time.sleep(0.3)

    resumen(results)

    output = [{k: v for k, v in r.items() if k != 'criterios'} for r in results]
    with open('results_today.json', 'w', encoding='utf-8') as f:
        json.dump({'date': datetime.now().strftime('%Y-%m-%d'),
                   'surface': SURFACE, 'matches': output},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Guardado en results_today.json\n")

if __name__ == "__main__":
    main()
