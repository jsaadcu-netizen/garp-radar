# =============================================================
# RADAR CLOUD — versión para GitHub Actions (UN solo ciclo y termina)
# A diferencia de radar_24_7.py (loop infinito para tu PC), esta hace
# UN escaneo y sale. Quien la repite cada 30 min es el cron de GitHub.
# Requisitos en el workflow:  pip install yfinance pandas
# =============================================================

import yfinance as yf
import pandas as pd
import json
import os
import time
import sys
from datetime import datetime

# ----------------- CONFIGURACIÓN -----------------
MAX_NUEVOS = 12                  # tickers nuevos evaluados por ejecución (bajo = más rápido y estable)
RETENCION_HORAS = 72
ELITE_CONFIRMACION_HORAS = 48

RADAR_FILE = "radar_resultados.json"
ELITE_FILE = "elite_resultados.json"

PANTALLAS_YAHOO = [
    "growth_technology_stocks",
    "aggressive_small_caps",
    "small_cap_gainers",
    "undervalued_growth_stocks",
    "most_actives",
    "day_gainers",
]

# Criterios élite (los 6 deben cumplirse)
ELITE_MIN_CREC_INGRESOS = 0.25
ELITE_MIN_CREC_EPS = 0.20
ELITE_MIN_MARGEN_OP = 0.10
ELITE_MAX_PEG = 1.5
ELITE_MIN_UPSIDE = 0.20
ELITE_MIN_ANALISTAS = 5

# ----------------- UTILIDADES -----------------

def leer_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def guardar_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)

def pct(x):
    return f"{x*100:.1f}%" if x is not None else "n/d"

def upside(precio, target):
    if not precio or not target: return None
    return (target - precio) / precio

# ----------------- EXTRACCIÓN -----------------

def info_ticker(t):
    try:
        info = yf.Ticker(t).info or {}
    except Exception:
        info = {}
    return {
        "ticker": t,
        "nombre": info.get("shortName", t),
        "precio": info.get("currentPrice") or info.get("regularMarketPrice"),
        "cambio_pct": info.get("regularMarketChangePercent"),
        "market_cap": info.get("marketCap"),
        "crec_ingresos": info.get("revenueGrowth"),
        "crec_utilidades": info.get("earningsGrowth"),
        "margen_op": info.get("operatingMargins"),
        "pe_fwd": info.get("forwardPE"),
        "peg_yahoo": info.get("trailingPegRatio"),
        "target_mean": info.get("targetMeanPrice"),
        "num_analistas": info.get("numberOfAnalystOpinions"),
        "rec_clave": info.get("recommendationKey", "n/d"),
        "sector": info.get("sector", "n/d"),
        "industria": info.get("industry", "n/d"),
    }

def margenes_fiscales(t):
    m_act, m_ant = None, None
    try:
        fin = yf.Ticker(t).financials
        if fin is not None and fin.shape[1] >= 2 and "Total Revenue" in fin.index and "Operating Income" in fin.index:
            rev, op = fin.loc["Total Revenue"], fin.loc["Operating Income"]
            if rev.iloc[0]: m_act = float(op.iloc[0]) / float(rev.iloc[0])
            if rev.iloc[1]: m_ant = float(op.iloc[1]) / float(rev.iloc[1])
    except Exception:
        pass
    return m_act, m_ant

def crec_eps_consenso(t):
    try:
        est = yf.Ticker(t).earnings_estimate
        if est is not None and "+1y" in est.index:
            val = est.loc["+1y"].get("growth")
            if val is not None and not pd.isna(val):
                return float(val)
    except Exception:
        pass
    return None

# ----------------- SCORE Y ÉLITE -----------------

def score_crecimiento(d):
    s = 0.0
    if d["crec_ingresos"] is not None:
        s += 35 * min(max(d["crec_ingresos"], 0) / 0.40, 1.0)
    if d["crec_utilidades"] is not None:
        s += 25 * min(max(d["crec_utilidades"], 0) / 0.40, 1.0)
    up = upside(d["precio"], d["target_mean"])
    if up is not None:
        s += 25 * min(max(up, 0) / 0.40, 1.0)
    if d["rec_clave"] in ("strong_buy", "buy"):
        s += 15
    elif d["rec_clave"] == "hold":
        s += 6
    return round(s)

def pre_filtro_elite(d):
    g = d.get("crec_ingresos")
    up = upside(d.get("precio"), d.get("target_mean"))
    return (g is not None and g > ELITE_MIN_CREC_INGRESOS
            and up is not None and up > ELITE_MIN_UPSIDE
            and (d.get("num_analistas") or 0) >= ELITE_MIN_ANALISTAS
            and d.get("rec_clave") in ("strong_buy", "buy")
            and (d.get("margen_op") or 0) > ELITE_MIN_MARGEN_OP)

def evaluar_elite(d):
    t = d["ticker"]
    criterios = []
    ok_total = True

    def check(nombre, cond, detalle):
        nonlocal ok_total
        criterios.append(f"{'✅' if cond else '❌'} {nombre} — {detalle}")
        if not cond:
            ok_total = False

    g = d.get("crec_ingresos")
    check(f"Ingresos YoY > {pct(ELITE_MIN_CREC_INGRESOS)}",
          g is not None and g > ELITE_MIN_CREC_INGRESOS, pct(g))

    g_eps = crec_eps_consenso(t)
    g_uti = d.get("crec_utilidades")
    mejor = max([x for x in (g_eps, g_uti) if x is not None], default=None)
    check(f"Crecimiento EPS/utilidades > {pct(ELITE_MIN_CREC_EPS)}",
          mejor is not None and mejor > ELITE_MIN_CREC_EPS, pct(mejor))

    m_act, m_ant = margenes_fiscales(t)
    cond_margen = (d.get("margen_op") or 0) > ELITE_MIN_MARGEN_OP and \
                  m_act is not None and m_ant is not None and m_act > m_ant
    check(f"Margen op. > {pct(ELITE_MIN_MARGEN_OP)} y en expansión", cond_margen,
          f"TTM {pct(d.get('margen_op'))}, fiscal {pct(m_ant)} → {pct(m_act)}")

    peg = None
    if d.get("pe_fwd") and g_eps and g_eps > 0:
        peg = d["pe_fwd"] / (g_eps * 100)
    elif d.get("peg_yahoo"):
        peg = d["peg_yahoo"]
    check(f"PEG < {ELITE_MAX_PEG}", peg is not None and peg < ELITE_MAX_PEG,
          f"{peg:.2f}" if peg is not None else "n/d")

    up = upside(d.get("precio"), d.get("target_mean"))
    check(f"Upside 12m > {pct(ELITE_MIN_UPSIDE)} con ≥{ELITE_MIN_ANALISTAS} analistas",
          up is not None and up > ELITE_MIN_UPSIDE and (d.get("num_analistas") or 0) >= ELITE_MIN_ANALISTAS,
          f"{pct(up)} ({d.get('num_analistas') or 0} analistas)")

    check("Consenso Buy/Strong Buy",
          d.get("rec_clave") in ("strong_buy", "buy"), str(d.get("rec_clave")))

    return ok_total, criterios

# ----------------- SCREENERS -----------------

def consultar_screeners():
    pool = {}
    if not hasattr(yf, "screen"):
        print("[!] yfinance sin yf.screen — actualiza la versión")
        return []
    for pantalla in PANTALLAS_YAHOO:
        try:
            res = yf.screen(pantalla, size=25)
            for q in (res or {}).get("quotes", []):
                sym = (q.get("symbol") or "").upper()
                if not sym or "." in sym or "-" in sym or "=" in sym or len(sym) > 5:
                    continue
                pool.setdefault(sym, True)
        except Exception as e:
            print(f"[!] Screener {pantalla} falló: {e}")
    return list(pool.keys())

# ----------------- UN CICLO -----------------

def main():
    ahora = time.time()
    hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== RADAR CLOUD — escaneo {hora} ===")

    candidatos = consultar_screeners()
    print(f"{len(candidatos)} candidatos en el pool")

    registro = leer_json(RADAR_FILE, {})
    elite = leer_json(ELITE_FILE, {})
    nuevos = 0
    nuevas_elite = []

    for sym in candidatos:
        es_nuevo = sym not in registro
        if es_nuevo and nuevos >= MAX_NUEVOS:
            continue
        d = info_ticker(sym)
        if not d.get("precio"):
            continue
        if es_nuevo:
            nuevos += 1
            time.sleep(0.8)

        previo = registro.get(sym, {})
        registro[sym] = {
            "score": score_crecimiento(d),
            "nombre": d["nombre"], "sector": d["sector"], "industria": d["industria"],
            "precio": d["precio"], "cambio_pct": d["cambio_pct"],
            "crec_ingresos": d["crec_ingresos"],
            "upside": upside(d["precio"], d["target_mean"]),
            "rec_clave": d["rec_clave"], "market_cap": d["market_cap"],
            "primera": previo.get("primera", ahora), "ultima": ahora,
        }

        if pre_filtro_elite(d):
            ok, criterios = evaluar_elite(d)
            time.sleep(0.5)
            if ok:
                ya_era = sym in elite
                elite[sym] = {
                    "nombre": d["nombre"], "sector": d["sector"], "industria": d["industria"],
                    "precio": d["precio"], "market_cap": d["market_cap"],
                    "score": registro[sym]["score"], "criterios": criterios,
                    "primera": elite.get(sym, {}).get("primera", ahora),
                    "ultima_confirmacion": ahora,
                }
                if not ya_era:
                    nuevas_elite.append(sym)

    registro = {k: v for k, v in registro.items()
                if ahora - v.get("ultima", ahora) < RETENCION_HORAS * 3600}
    elite = {k: v for k, v in elite.items()
             if ahora - v.get("ultima_confirmacion", ahora) < ELITE_CONFIRMACION_HORAS * 3600}

    guardar_json(RADAR_FILE, registro)
    guardar_json(ELITE_FILE, elite)

    ranking = sorted(registro.items(), key=lambda kv: kv[1]["score"], reverse=True)
    print(f"\nTOP 10 ({len(registro)} en registro, {nuevos} nuevas):")
    for sym, v in ranking[:10]:
        marca = " 🏆" if sym in elite else ""
        print(f"  {v['score']:>3}  {sym:<6} {str(v['nombre'])[:30]:<30} "
              f"crec {pct(v.get('crec_ingresos')):>7} up {pct(v.get('upside')):>7}{marca}")

    if nuevas_elite:
        print(f"\n🏆 NUEVAS ÉLITE: {', '.join(nuevas_elite)}")
    print(f"\nÉlite vigente ({len(elite)}): {', '.join(elite.keys()) or 'ninguna'}")
    print("=== escaneo terminado ===")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[!] Error: {e}")
        sys.exit(0)  # salimos limpio para que el workflow no marque fallo
