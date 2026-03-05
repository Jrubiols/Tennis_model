"""
TENNIS BOT TELEGRAM v1.0
Envía partidos y cuotas, recibe análisis completo.

INSTALACION:
pip install python-telegram-bot==20.7

USO:
python tennis_bot.py

COMANDOS:
  /start          — bienvenida
  /setkey KEY     — cambia la API key de api-tennis.com
  /surface Hard   — cambia superficie (Hard/Clay/Grass)
  /nev 15         — cambia el gap minimo NEV

FORMATO PARTIDOS (un partido por linea):
  Fonseca vs Collignon 1.75 2.05
  Tseng vs Baez 3.40 1.35
  Dzumhur vs Fearnley 2.10 1.78
"""

import os
import json
import time
import re
import numpy as np
import requests
from datetime import datetime, timedelta
from itertools import combinations

# ============================================================
# TELEGRAM
# ============================================================
try:
    from telegram import Update, constants
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
except ImportError:
    print("Instala python-telegram-bot: pip install python-telegram-bot==20.7")
    exit(1)

# ============================================================
# CONFIGURACION PERSISTENTE
# ============================================================
BOT_TOKEN    = "8767038110:AAHmpQjZIgIzy8sw1aXeK3rinb5Iglv8VVM"
CONFIG_FILE  = "bot_config.json"
CACHE_DIR    = "cache_bot"

DEFAULT_CONFIG = {
    "api_key":           "794558d47064c313aaf7af272503014d578ac2629612cb6e49f6057cab5dcce4",
    "surface":           "Hard",
    "min_value_pct":     5.0,
    "nev_gap":           15,
    "fatigue_threshold": 8,
    "cold_threshold":    14,
    "score_enter":       3,
    "score_marginal":    2,
    "elo_k":             32,
    "elo_base":          1500,
    "min_surface_matches": 5,
    "cache_hours":       12,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            # Añadir claves nuevas si faltan
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

# ============================================================
# CACHE
# ============================================================
def cache_get(key, cache_hours):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = f"{CACHE_DIR}/{key}.json"
    if os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 3600
        if age < cache_hours:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    return None

def cache_set(key, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = f"{CACHE_DIR}/{key}.json"
    with open(path, 'w', encoding='utf-8') as f:
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
    except Exception as e:
        return {}

def cargar_ranking(cfg):
    cached = cache_get("atp_standings", cfg['cache_hours'])
    if cached:
        return cached

    data    = api_call("get_standings", {"event_type": "ATP"}, cfg['api_key'])
    results = data.get('result', [])
    if not isinstance(results, list):
        return {}

    ranking = {}
    for p in results:
        nombre = p.get('player', '').strip()
        key    = p.get('player_key')
        place  = p.get('place')
        if nombre and key:
            ranking[nombre] = {
                'player_key': key,
                'ranking':    int(place) if place and str(place).isdigit() else 999,
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
    ckey   = f"historial_{player_key}"
    cached = cache_get(ckey, cache_hours)
    if cached is not None:
        return cached

    data    = api_call("get_H2H", {"first_player_key": player_key, "second_player_key": 1}, api_key)
    result  = data.get('result', {})
    partidos = result.get('firstPlayerResults', [])

    if not partidos:
        data2   = api_call("get_H2H", {"first_player_key": 1, "second_player_key": player_key}, api_key)
        result2 = data2.get('result', {})
        partidos = result2.get('secondPlayerResults', [])

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
    keywords = {
        "Hard":  ["hard", "hard (indoor)"],
        "Clay":  ["clay"],
        "Grass": ["grass"],
    }.get(superficie, ["hard"])

    p_surf = [p for p in partidos
              if any(k in str(p.get('tournament_surface', '') or '').lower() for k in keywords)]
    if not p_surf:
        p_surf = partidos

    p_surf = sorted(p_surf, key=lambda x: x.get('event_date', ''))
    elo    = cfg['elo_base']
    k      = cfg['elo_k']
    n      = 0
    pid    = str(player_key)

    for p in p_surf:
        winner = str(p.get('event_winner', ''))
        f_key  = str(p.get('first_player_key', ''))
        s_key  = str(p.get('second_player_key', ''))
        result = str(p.get('event_final_result', '')).upper()

        if 'RET' in result or 'W/O' in result:
            continue

        if pid == f_key:
            gano = 'First' in winner
        elif pid == s_key:
            gano = 'Second' in winner
        else:
            continue

        exp  = 1 / (1 + 10 ** ((cfg['elo_base'] - elo) / 400))
        elo  = elo + k * ((1.0 if gano else 0.0) - exp)
        n   += 1

    return round(elo), n

# ============================================================
# ANALISIS DE JUGADOR
# ============================================================
def analizar_jugador(nombre, ranking_data, cfg):
    info = buscar_jugador(nombre, ranking_data)
    if not info:
        return {
            'nombre': nombre, 'ranking': 999, 'player_key': None, 'found': False,
            'matches_year': 0, 'days_since': 999, 'last_3sets': False,
            'recent_21d': 0, 'ret_ratio': 0.0, 'form_last5': 0.5, 'form_3w': 0.5,
            'elo_surface': cfg['elo_base'], 'n_surface': 0,
            'total_matches': 0, 'wins_3w': 0, 'total_3w': 0,
        }

    player_key = info['player_key']
    ranking_pos = info['ranking']
    partidos   = obtener_historial(player_key, cfg['api_key'], cfg['cache_hours'])
    time.sleep(0.15)

    atp = [p for p in partidos if 'ouble' not in str(p.get('event_type_type', ''))]
    atp_sorted = sorted(atp, key=lambda x: x.get('event_date', ''), reverse=True)

    year_str = str(datetime.now().year)
    p_year   = [p for p in atp_sorted if str(p.get('event_date', '')).startswith(year_str)]

    days_since = 999
    last_3sets = False
    if atp_sorted:
        try:
            days_since = (datetime.now() - datetime.strptime(atp_sorted[0]['event_date'], '%Y-%m-%d')).days
        except:
            pass
        try:
            nums = [int(x) for x in str(atp_sorted[0].get('event_final_result','')).replace('-',' ').split() if x.isdigit()]
            if sum(nums) >= 5:
                last_3sets = True
        except:
            pass

    cutoff_21  = (datetime.now() - timedelta(days=21)).strftime('%Y-%m-%d')
    recent_21d = sum(1 for p in atp_sorted if p.get('event_date','') >= cutoff_21)

    ret       = sum(1 for p in atp_sorted if 'RET' in str(p.get('event_final_result','')).upper())
    ret_ratio = ret / max(len(atp_sorted), 1)

    pid = str(player_key)
    def es_victoria(p):
        w = str(p.get('event_winner',''))
        return (pid == str(p.get('first_player_key','')) and 'First' in w) or \
               (pid == str(p.get('second_player_key','')) and 'Second' in w)

    ultimos5   = [p for p in atp_sorted[:10] if 'RET' not in str(p.get('event_final_result','')).upper()][:5]
    wins5      = sum(1 for p in ultimos5 if es_victoria(p))
    form_last5 = wins5 / max(len(ultimos5), 1) if ultimos5 else 0.5

    p_3w    = [p for p in atp_sorted if p.get('event_date','') >= cutoff_21
               and 'RET' not in str(p.get('event_final_result','')).upper()]
    wins_3w = sum(1 for p in p_3w if es_victoria(p))
    form_3w = wins_3w / max(len(p_3w), 1) if p_3w else form_last5

    elo_surface, n_surface = calcular_elo_superficie(atp_sorted, player_key, cfg['surface'], cfg)

    return {
        'nombre': nombre, 'ranking': ranking_pos, 'player_key': player_key, 'found': True,
        'matches_year': len(p_year), 'days_since': days_since, 'last_3sets': last_3sets,
        'recent_21d': recent_21d, 'ret_ratio': round(ret_ratio, 3),
        'form_last5': round(form_last5, 2), 'form_3w': round(form_3w, 2),
        'elo_surface': elo_surface, 'n_surface': n_surface,
        'total_matches': len(atp_sorted), 'wins_3w': wins_3w, 'total_3w': len(p_3w),
    }

# ============================================================
# ANALISIS DE PARTIDO
# ============================================================
def analizar_partido(pa_nombre, pb_nombre, oa, ob, ranking_data, cfg):
    da = analizar_jugador(pa_nombre, ranking_data, cfg)
    db = analizar_jugador(pb_nombre, ranking_data, cfg)

    ra, rb = da['ranking'], db['ranking']
    gap    = abs(ra - rb)

    if ra == 999 or rb == 999:
        no_enc = pa_nombre if ra == 999 else pb_nombre
        return {'pa': pa_nombre, 'pb': pb_nombre, 'ra': ra, 'rb': rb,
                'oa': oa, 'ob': ob, 'decision': 'ERROR',
                'motivo': f'⚠️ "{no_enc}" no encontrado en ranking ATP — revisa el nombre'}

    if gap < cfg['nev_gap']:
        return {'pa': pa_nombre, 'pb': pb_nombre, 'ra': ra, 'rb': rb,
                'oa': oa, 'ob': ob, 'decision': 'PASS',
                'motivo': f'NEV: gap {gap} spots — demasiado igualado'}

    elo_gen_a = ranking_to_elo(ra, cfg['elo_base'])
    elo_gen_b = ranking_to_elo(rb, cfg['elo_base'])
    p_gen_a   = prob_elo(elo_gen_a, elo_gen_b)

    elo_sur_a, n_a = da['elo_surface'], da['n_surface']
    elo_sur_b, n_b = db['elo_surface'], db['n_surface']
    p_sur_a        = prob_elo(elo_sur_a, elo_sur_b)

    use_surface = (n_a >= cfg['min_surface_matches'] and n_b >= cfg['min_surface_matches'])
    p_a = (0.4 * p_gen_a + 0.6 * p_sur_a) if use_surface else p_gen_a
    p_b = 1 - p_a

    raw_a, raw_b = 1/oa, 1/ob
    ov    = raw_a + raw_b
    imp_a = raw_a / ov
    imp_b = raw_b / ov
    val_a = (p_a - imp_a) * 100
    val_b = (p_b - imp_b) * 100

    if val_a >= val_b:
        pick, pick_odds, val = pa_nombre, oa, val_a
        elo_gen_ok, elo_sur_ok, dpick, n_surf = p_gen_a > 0.5, p_sur_a > 0.5, da, n_a
    else:
        pick, pick_odds, val = pb_nombre, ob, val_b
        elo_gen_ok, elo_sur_ok, dpick, n_surf = (1-p_gen_a) > 0.5, (1-p_sur_a) > 0.5, db, n_b

    # Score
    score = 0
    lines = []

    if val >= cfg['min_value_pct']:
        score += 1; lines.append(f"✅ VALUE     +{val:.1f}% sobre la casa")
    else:
        lines.append(f"❌ VALUE     +{val:.1f}% insuficiente")

    if elo_gen_ok and elo_sur_ok:
        score += 1; lines.append(f"✅ ELO       General + {cfg['surface']} confirman")
    elif elo_gen_ok and n_surf < cfg['min_surface_matches']:
        score += 0.5; lines.append(f"〰️ ELO       General confirma, pocos datos {cfg['surface']}")
    elif elo_gen_ok:
        score += 0.5; lines.append(f"〰️ ELO       General sí, {cfg['surface']} contradice")
    else:
        lines.append(f"❌ ELO       Contradice el pick")

    if dpick['matches_year'] >= 5:
        score += 1; lines.append(f"✅ DATOS     {dpick['matches_year']} partidos en {datetime.now().year}")
    elif dpick['matches_year'] >= 2:
        score += 0.5; lines.append(f"〰️ DATOS     {dpick['matches_year']} partidos en {datetime.now().year} — limitado")
    else:
        lines.append(f"❌ DATOS     {dpick['matches_year']} partidos en {datetime.now().year}")

    if dpick['recent_21d'] <= cfg['fatigue_threshold'] and not dpick['last_3sets']:
        score += 1; lines.append(f"✅ DESGASTE  {dpick['recent_21d']} partidos/21d, último en 2 sets")
    elif dpick['recent_21d'] <= cfg['fatigue_threshold']:
        score += 0.5; lines.append(f"〰️ DESGASTE  Último partido en 3 sets")
    else:
        lines.append(f"❌ DESGASTE  {dpick['recent_21d']} partidos/21d — cargado")

    if dpick['days_since'] <= cfg['cold_threshold']:
        score += 1; lines.append(f"✅ RITMO     Jugó hace {dpick['days_since']} días")
    elif dpick['days_since'] <= 21:
        score += 0.5; lines.append(f"〰️ RITMO     Jugó hace {dpick['days_since']} días")
    else:
        lines.append(f"❌ RITMO     {dpick['days_since']} días sin jugar — frío")

    if dpick['total_3w'] >= 3:
        if dpick['form_3w'] >= 0.67:
            lines.append(f"🔥 RACHA 3W  {dpick['wins_3w']}/{dpick['total_3w']} victorias últimas 3 semanas")
        elif dpick['form_3w'] <= 0.33:
            lines.append(f"📉 RACHA 3W  {dpick['wins_3w']}/{dpick['total_3w']} victorias últimas 3 semanas")
        else:
            lines.append(f"➡️ RACHA 3W  {dpick['wins_3w']}/{dpick['total_3w']} victorias últimas 3 semanas")
    elif dpick['form_last5'] >= 0.6:
        lines.append(f"🔥 FORMA 5   {int(dpick['form_last5']*5)}/5 victorias últimos 5")
    elif dpick['form_last5'] <= 0.2 and dpick['total_matches'] >= 5:
        lines.append(f"📉 FORMA 5   {int(dpick['form_last5']*5)}/5 victorias últimos 5")

    if n_surf >= cfg['min_surface_matches']:
        lines.append(f"📊 ELO {cfg['surface']:5}  {dpick['elo_surface']} pts ({n_surf} partidos)")

    if dpick['ret_ratio'] > 0.10:
        lines.append(f"🚨 LESIÓN    {dpick['ret_ratio']*100:.0f}% partidos con retirada histórica")

    score = round(score, 1)

    if val < cfg['min_value_pct']:
        decision, motivo = 'PASS', 'Sin value suficiente'
    elif score >= cfg['score_enter']:
        decision, motivo = 'ENTRA', f'Score {score}/5'
    elif score >= cfg['score_marginal']:
        decision, motivo = 'MARGINAL', f'Score {score}/5'
    else:
        decision, motivo = 'PASS', f'Score {score}/5'

    return {
        'pa': pa_nombre, 'pb': pb_nombre, 'ra': ra, 'rb': rb,
        'oa': oa, 'ob': ob, 'gap': gap,
        'p_a': round(p_a*100,1), 'p_b': round(p_b*100,1),
        'imp_a': round(imp_a*100,1), 'imp_b': round(imp_b*100,1),
        'val_a': round(val_a,1), 'val_b': round(val_b,1),
        'overround': round((ov-1)*100,1),
        'elo_sur_a': elo_sur_a, 'elo_sur_b': elo_sur_b,
        'pick': pick, 'pick_odds': pick_odds, 'val': round(val,1),
        'score': score, 'lines': lines,
        'decision': decision, 'motivo': motivo,
        'use_surface': use_surface,
    }

# ============================================================
# FORMATEAR RESPUESTA TELEGRAM
# ============================================================
def formatear_partido(r, cfg):
    if r['decision'] == 'ERROR':
        return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"
                f"❌ *{r['pa']} vs {r['pb']}*
"
                f"{r['motivo']}
")

    if r['decision'] == 'PASS' and 'NEV' in r['motivo']:
        return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚪ *{r['pa']} vs {r['pb']}*\n"
                f"#{r['ra']} vs #{r['rb']} | {r['oa']} / {r['ob']}\n"
                f"⏭ PASS — {r['motivo']}\n")

    if r['decision'] == 'PASS' and 'value' in r['motivo'].lower():
        mejor_val = max(r['val_a'], r['val_b'])
        return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚪ *{r['pa']} vs {r['pb']}*\n"
                f"#{r['ra']} vs #{r['rb']} | {r['oa']} / {r['ob']}\n"
                f"Value: {r['val_a']:+.1f}% / {r['val_b']:+.1f}%\n"
                f"⏭ PASS — {r['motivo']}\n")

    surf_tag = f"60% {cfg['surface']} ELO" if r.get('use_surface') else "solo ranking"
    icon = "🟢" if r['decision'] == 'ENTRA' else ("🟡" if r['decision'] == 'MARGINAL' else "🔴")

    msg  = f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{icon} *{r['pa']} vs {r['pb']}*\n"
    msg += f"#{r['ra']} vs #{r['rb']} | {r['oa']} / {r['ob']}\n"
    msg += f"ELO Hard: {r['elo_sur_a']} vs {r['elo_sur_b']} | {surf_tag}\n\n"
    msg += f"*Pick: {r['pick']} @ {r['pick_odds']}* ({r['val']:+.1f}% value)\n\n"
    msg += f"Criterios ({r['score']}/5):\n"
    for line in r['lines']:
        msg += f"  {line}\n"
    msg += f"\n*{'>>> ' + r['decision'] + ' — ' + r['motivo']}*\n"
    return msg

def formatear_resumen(results, cfg):
    enters    = [r for r in results if r.get('decision') == 'ENTRA']
    marginals = [r for r in results if r.get('decision') == 'MARGINAL']

    msg = f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📋 *RESUMEN — {datetime.now().strftime('%d/%m/%Y')} | {cfg['surface']}*\n\n"

    if not enters and not marginals:
        msg += "Sin picks válidos hoy.\n"
        return msg

    if enters:
        msg += "🟢 *ENTRA:*\n"
        for r in enters:
            s = "⭐⭐⭐" if r['val']>10 else ("⭐⭐" if r['val']>7 else "⭐")
            msg += f"  • *{r['pick']}* @ {r['pick_odds']}  {r['val']:+.1f}%  {r['score']}/5  {s}\n"

    if marginals:
        msg += "\n🟡 *MARGINAL:*\n"
        for r in marginals:
            msg += f"  • {r['pick']} @ {r['pick_odds']}  {r['val']:+.1f}%  {r['score']}/5\n"

    # Combinadas de 2 con score >= 4
    solidos = [r for r in enters if r['score'] >= 4]
    if len(solidos) >= 2:
        msg += "\n💡 *Combinadas sugeridas (score ≥4):*\n"
        for r1, r2 in combinations(solidos, 2):
            comb = r1['pick_odds'] * r2['pick_odds']
            msg += (f"  {r1['pick'].split()[-1]} + {r2['pick'].split()[-1]}: "
                    f"*{round(comb,2)}x*  (10€→{comb*10:.0f}€)\n")

    if len(enters) >= 3:
        comb_all = 1.0
        for r in enters: comb_all *= r['pick_odds']
        msg += f"\n🎰 Full funbet ({len(enters)} picks): *{round(comb_all,2)}x* — solo monedas\n"

    return msg

# ============================================================
# PARSEAR MENSAJE CON PARTIDOS
# ============================================================
def parsear_partidos(texto):
    """
    Acepta formatos:
      Fonseca vs Collignon 1.75 2.05
      Fonseca - Collignon 1.75 2.05
      Fonseca / Collignon 1.75 2.05
    """
    partidos = []
    lineas   = [l.strip() for l in texto.strip().split('\n') if l.strip()]

    for linea in lineas:
        # Buscar dos números al final (cuotas)
        nums = re.findall(r'\d+[.,]\d+', linea)
        if len(nums) < 2:
            continue
        oa = float(nums[-2].replace(',', '.'))
        ob = float(nums[-1].replace(',', '.'))

        # Eliminar las cuotas del texto para quedarnos con los nombres
        texto_nombres = re.sub(r'\d+[.,]\d+', '', linea).strip()

        # Separar por vs, -, /
        sep = re.split(r'\s+vs\.?\s+|\s+-\s+|\s+/\s+', texto_nombres, flags=re.IGNORECASE)
        if len(sep) < 2:
            continue

        pa = sep[0].strip().title()
        pb = sep[1].strip().title()

        if pa and pb and oa > 1 and ob > 1:
            partidos.append((pa, pb, oa, ob))

    return partidos

# ============================================================
# HANDLERS TELEGRAM
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = (
        "🎾 *Tennis Model Bot v5.0*\n\n"
        "Envíame los partidos en este formato:\n"
        "```\n"
        "Fonseca vs Collignon 1.75 2.05\n"
        "Tseng vs Baez 3.40 1.35\n"
        "Dzumhur vs Fearnley 2.10 1.78\n"
        "```\n\n"
        "*Comandos:*\n"
        "`/setkey KEY` — actualizar API key\n"
        "`/surface Hard` — cambiar superficie\n"
        "`/nev 15` — cambiar gap mínimo NEV\n"
        "`/config` — ver configuración actual\n\n"
        f"Superficie actual: *{cfg['surface']}*\n"
        f"NEV gap: *{cfg['nev_gap']}*"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def cmd_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/setkey TU_API_KEY`", parse_mode='Markdown')
        return
    cfg = load_config()
    cfg['api_key'] = context.args[0]
    save_config(cfg)
    # Limpiar caché de ranking para forzar re-descarga con nueva key
    rpath = f"{CACHE_DIR}/atp_standings.json"
    if os.path.exists(rpath):
        os.remove(rpath)
    await update.message.reply_text("✅ API key actualizada. Caché de ranking limpiada.")

async def cmd_surface(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/surface Hard` (Hard/Clay/Grass)", parse_mode='Markdown')
        return
    sup = context.args[0].capitalize()
    if sup not in ['Hard', 'Clay', 'Grass']:
        await update.message.reply_text("Superficie válida: Hard, Clay, Grass")
        return
    cfg = load_config()
    cfg['surface'] = sup
    save_config(cfg)
    await update.message.reply_text(f"✅ Superficie cambiada a *{sup}*", parse_mode='Markdown')

async def cmd_nev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: `/nev 15`", parse_mode='Markdown')
        return
    try:
        gap = int(context.args[0])
        cfg = load_config()
        cfg['nev_gap'] = gap
        save_config(cfg)
        await update.message.reply_text(f"✅ NEV gap cambiado a *{gap}* spots", parse_mode='Markdown')
    except:
        await update.message.reply_text("Introduce un número entero. Ej: `/nev 15`", parse_mode='Markdown')

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = (
        f"⚙️ *Configuración actual:*\n\n"
        f"Superficie: *{cfg['surface']}*\n"
        f"NEV gap: *{cfg['nev_gap']}* spots\n"
        f"Value mínimo: *{cfg['min_value_pct']}%*\n"
        f"Score ENTRA: *{cfg['score_enter']}/5*\n"
        f"Score MARGINAL: *{cfg['score_marginal']}/5*\n"
        f"Caché: *{cfg['cache_hours']}h*\n"
        f"API key: `...{cfg['api_key'][-8:]}`"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto    = update.message.text
    partidos = parsear_partidos(texto)

    if not partidos:
        await update.message.reply_text(
            "No entendí el formato. Envíame los partidos así:\n\n"
            "`Fonseca vs Collignon 1.75 2.05`\n"
            "`Tseng vs Baez 3.40 1.35`",
            parse_mode='Markdown'
        )
        return

    cfg = load_config()
    await update.message.reply_text(
        f"⏳ Analizando {len(partidos)} partido(s)... (puede tardar 30-60 segundos)"
    )

    # Cargar ranking
    ranking_data = cargar_ranking(cfg)
    if not ranking_data:
        await update.message.reply_text("❌ Error conectando con API-Tennis. Comprueba la API key con /config")
        return

    results = []
    for pa, pb, oa, ob in partidos:
        r = analizar_partido(pa, pb, oa, ob, ranking_data, cfg)
        results.append(r)

    # Enviar análisis partido a partido
    for r in results:
        msg = formatear_partido(r, cfg)
        await update.message.reply_text(msg, parse_mode='Markdown')
        time.sleep(0.3)

    # Resumen final
    if len(results) > 1:
        resumen_msg = formatear_resumen(results, cfg)
        await update.message.reply_text(resumen_msg, parse_mode='Markdown')

# ============================================================
# MAIN
# ============================================================
def main():
    print("🎾 Tennis Model Bot arrancando...")
    print(f"   Token: ...{BOT_TOKEN[-10:]}")
    print("   Escribe /start en Telegram para comenzar\n")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("setkey",  cmd_setkey))
    app.add_handler(CommandHandler("surface", cmd_surface))
    app.add_handler(CommandHandler("nev",     cmd_nev))
    app.add_handler(CommandHandler("config",  cmd_config))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot activo. Ctrl+C para parar.\n")
    app.run_polling()

if __name__ == "__main__":
    main()
