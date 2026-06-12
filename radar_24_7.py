# =============================================================
# RADAR 24/7 — Escáner de fondo para GARP Scout
# Corre en su propia terminal, escanea cada 20 min, 24/7.
# Escribe en los MISMOS archivos que lee tu app de Streamlit:
#   - radar_resultados.json  (todos los hallazgos, rankeados)
#   - elite_resultados.json  (solo las que pasan el filtro ÉLITE)
#
# Requisitos:  pip install yfinance pandas   (ya los tienes)
# Uso:         python radar_24_7.py
#              (guárdalo en la MISMA carpeta que garp_app.py)
# Detener:     Ctrl + C
# =============================================================

import yfinance as yf
import pandas as pd
import json
import os
import time
from datetime import datetime

# ----------------- CONFIGURACIÓN -----------------
INTERVALO_SEG = 1200            # 20 minutos entre escaneos
MAX_NUEVOS_POR_CICLO = 15       # tope de tickers nuevos evaluados por ciclo (anti rate-limit)
RETENCION_HORAS = 72            # hallazgos sin reaparecer se purgan a las 72h
ELITE_CONFIRMACION_HORAS = 48   # élite que deja de cumplir criterios se purga a las 48h
CACHE_DIR = "cache_radar"
CACHE_HORAS = 12

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

# ----------------- CRITERIOS ÉLITE (los 6 deben cumplirse) -----------------
ELITE_MIN_CREC_INGRESOS = 0.25   # ingresos YoY > 25%
ELITE_MIN_CREC_EPS = 0.20        # crecimiento EPS fwd o utilidades > 20%
ELITE_MIN_MARGEN_OP = 0.10       # margen operativo > 10% Y en expansión
ELITE_MAX_PEG = 1.5              # PEG < 1.5
ELITE_MIN_UPSIDE = 0.20          # upside al target medio > 20%
ELITE_MIN_ANALISTAS = 5          # con al menos 5 analistas cubriendo
# + consenso Buy / Strong Buy

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
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=1)
    except Exception as e:
        print(f"  [!] No pude guardar {path}: {e}")

def cache_get(clave):
    p = os.path.join(CACHE_DIR, clave + ".json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < CACHE_HORAS * 3600:
        try:
            with open(p, "r") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def cache_set(clave, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(os.path.join(CACHE_DIR, clave + ".json"), "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def pct(x):
    return f"{x*100:.1f}%" if x is not None else "n/d"

def usd(x):
    if x is None: return "n/d"
    if x >= 1e12: return f"${x/1e12:.2f}T"
    if x >= 1e9:  return f"${x/1e9:.2f}B"
    if x >= 1e6:  return f"${x/1e6:.1f}M"
    return f"${x:,.2f}"

def upside(precio, target):
    if not precio or not target: return None
    return (target - precio) / precio

# ----------------- EXTRACCIÓN (con caché en disco 12h) -----------------

def info_ticker(t):
    cached = cache_get(t)
    if cached:
        return cached
    try:
        info = yf.Ticker(t).info or {}
    except Exception:
        info = {}
    d = {
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
    cache_set(t, d)
    return d

def margenes_fiscales(t):
    """Margen operativo de los últimos 2 años fiscales (solo para candidatas élite)."""
    cached = cache_get(t + "_marg")
    if cached is not None:
        return cached.get("act"), cached.get("ant")
    m_act, m_ant = None, None
    try:
        fin = yf.Ticker(t).financials
        if fin is not None and fin.shape[1] >= 2 and "Total Revenue" in fin.index and "Operating Income" in fin.index:
            rev, op = fin.loc["Total Revenue"], fin.loc["Operating Income"]
            if rev.iloc[0]: m_act = float(op.iloc[0]) / float(rev.iloc[0])
            if rev.iloc[1]: m_ant = float(op.iloc[1]) / float(rev.iloc[1])
    except Exception:
        pass
    cache_set(t + "_marg", {"act": m_act, "ant": m_ant})
    return m_act, m_ant

def crec_eps_consenso(t):
    cached = cache_get(t + "_eps")
    if cached is not None:
        return cached.get("g")
    g = None
    try:
        est = yf.Ticker(t).earnings_estimate
        if est is not None and "+1y" in est.index:
            val = est.loc["+1y"].get("growth")
            if val is not None and not pd.isna(val):
                g = float(val)
    except Exception:
        pass
    cache_set(t + "_eps", {"g": g})
    return g

# ----------------- SCORE Y FILTRO ÉLITE -----------------

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
    """Checks baratos (sin llamadas extra). Si no pasa esto, ni gastamos en lo caro."""
    g = d.get("crec_ingresos")
    up = upside(d.get("precio"), d.get("target_mean"))
    return (g is not None and g > ELITE_MIN_CREC_INGRESOS
            and up is not None and up > ELITE_MIN_UPSIDE
            and (d.get("num_analistas") or 0) >= ELITE_MIN_ANALISTAS
            and d.get("rec_clave") in ("strong_buy", "buy")
            and (d.get("margen_op") or 0) > ELITE_MIN_MARGEN_OP)

def evaluar_elite(d):
    """Evalúa los 6 criterios estrictos. TODOS deben cumplirse."""
    t = d["ticker"]
    criterios = []
    ok_total = True

    def check(nombre, cond, detalle):
        nonlocal ok_total
        criterios.append(f"{'✅' if cond else '❌'} {nombre} — {detalle}")
        if not cond:
            ok_total = False

    # 1. Hipercrecimiento de ingresos
    g = d.get("crec_ingresos")
    check(f"Ingresos YoY > {pct(ELITE_MIN_CREC_INGRESOS)}",
          g is not None and g > ELITE_MIN_CREC_INGRESOS, pct(g))

    # 2. Crecimiento de EPS forward (consenso) o utilidades
    g_eps = crec_eps_consenso(t)
    g_uti = d.get("crec_utilidades")
    mejor = max([x for x in (g_eps, g_uti) if x is not None], default=None)
    check(f"Crecimiento EPS/utilidades > {pct(ELITE_MIN_CREC_EPS)}",
          mejor is not None and mejor > ELITE_MIN_CREC_EPS, pct(mejor))

    # 3. Margen operativo >10% Y en expansión (apalancamiento operativo real)
    m_act, m_ant = margenes_fiscales(t)
    cond_margen = (d.get("margen_op") or 0) > ELITE_MIN_MARGEN_OP and \
                  m_act is not None and m_ant is not None and m_act > m_ant
    detalle_m = f"TTM {pct(d.get('margen_op'))}, fiscal {pct(m_ant)} → {pct(m_act)}"
    check(f"Margen op. > {pct(ELITE_MIN_MARGEN_OP)} y en expansión", cond_margen, detalle_m)

    # 4. PEG < 1.5
    peg = None
    if d.get("pe_fwd") and g_eps and g_eps > 0:
        peg = d["pe_fwd"] / (g_eps * 100)
    elif d.get("peg_yahoo"):
        peg = d["peg_yahoo"]
    check(f"PEG < {ELITE_MAX_PEG}", peg is not None and peg < ELITE_MAX_PEG,
          f"{peg:.2f}" if peg is not None else "n/d")

    # 5. Upside > 20% con cobertura suficiente
    up = upside(d.get("precio"), d.get("target_mean"))
    check(f"Upside 12m > {pct(ELITE_MIN_UPSIDE)} con ≥{ELITE_MIN_ANALISTAS} analistas",
          up is not None and up > ELITE_MIN_UPSIDE and (d.get("num_analistas") or 0) >= ELITE_MIN_ANALISTAS,
          f"{pct(up)} ({d.get('num_analistas') or 0} analistas)")

    # 6. Consenso de compra
    check("Consenso Buy/Strong Buy",
          d.get("rec_clave") in ("strong_buy", "buy"), str(d.get("rec_clave")))

    return ok_total, criterios

# ----------------- SCREENERS -----------------

def consultar_screeners():
    pool = {}
    if not hasattr(yf, "screen"):
        print("  [!] Tu versión de yfinance no tiene yf.screen — actualiza: pip install -U yfinance")
        return pool
    for pantalla in PANTALLAS_YAHOO:
        try:
            res = yf.screen(pantalla, size=25)
            for q in (res or {}).get("quotes", []):
                sym = (q.get("symbol") or "").upper()
                if not sym or "." in sym or "-" in sym or "=" in sym or len(sym) > 5:
                    continue
                pool.setdefault(sym, True)
        except Exception as e:
            print(f"  [!] Screener {pantalla} falló: {e}")
    return list(pool.keys())

# ----------------- CICLO DE ESCANEO -----------------

def ciclo():
    ahora = time.time()
    hora = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*64}\n[{hora}] ESCANEO — consultando screeners de Yahoo...")

    candidatos = consultar_screeners()
    print(f"[{hora}] {len(candidatos)} candidatos en el pool del momento")

    registro = leer_json(RADAR_FILE, {})
    elite = leer_json(ELITE_FILE, {})
    nuevos = 0
    nuevas_elite = []

    for sym in candidatos:
        es_nuevo = sym not in registro
        if es_nuevo and nuevos >= MAX_NUEVOS_POR_CICLO:
            continue
        d = info_ticker(sym)
        if not d.get("precio"):
            continue
        if es_nuevo:
            nuevos += 1
            time.sleep(1.5)  # pausa generosa: estamos 24/7, no hay prisa

        previo = registro.get(sym, {})
        registro[sym] = {
            "score": score_crecimiento(d),
            "nombre": d["nombre"],
            "sector": d["sector"],
            "industria": d["industria"],
            "precio": d["precio"],
            "cambio_pct": d["cambio_pct"],
            "crec_ingresos": d["crec_ingresos"],
            "upside": upside(d["precio"], d["target_mean"]),
            "rec_clave": d["rec_clave"],
            "market_cap": d["market_cap"],
            "primera": previo.get("primera", ahora),
            "ultima": ahora,
        }

        # ----- FILTRO ÉLITE (solo si pasa el pre-filtro barato) -----
        if pre_filtro_elite(d):
            ok, criterios = evaluar_elite(d)
            time.sleep(1.0)
            if ok:
                ya_era = sym in elite
                elite[sym] = {
                    "nombre": d["nombre"],
                    "sector": d["sector"],
                    "industria": d["industria"],
                    "precio": d["precio"],
                    "market_cap": d["market_cap"],
                    "score": registro[sym]["score"],
                    "criterios": criterios,
                    "primera": elite.get(sym, {}).get("primera", ahora),
                    "ultima_confirmacion": ahora,
                }
                if not ya_era:
                    nuevas_elite.append(sym)
            elif sym in elite:
                # sigue en élite hasta que expire su confirmación (48h)
                pass

    # Purgas
    registro = {k: v for k, v in registro.items()
                if ahora - v.get("ultima", ahora) < RETENCION_HORAS * 3600}
    elite = {k: v for k, v in elite.items()
             if ahora - v.get("ultima_confirmacion", ahora) < ELITE_CONFIRMACION_HORAS * 3600}

    guardar_json(RADAR_FILE, registro)
    guardar_json(ELITE_FILE, elite)

    # ----- REPORTE EN CONSOLA -----
    ranking = sorted(registro.items(), key=lambda kv: kv[1]["score"], reverse=True)
    print(f"\n[{hora}] TOP 10 DEL REGISTRO ({len(registro)} empresas, {nuevos} nuevas este ciclo):")
    print(f"  {'SCORE':>5}  {'TICKER':<6} {'EMPRESA':<28} {'SECTOR':<22} {'CREC':>7} {'UPSIDE':>7}")
    for sym, v in ranking[:10]:
        marca = " 🏆" if sym in elite else ""
        print(f"  {v['score']:>5}  {sym:<6} {str(v['nombre'])[:27]:<28} {str(v['sector'])[:21]:<22} "
              f"{pct(v.get('crec_ingresos')):>7} {pct(v.get('upside')):>7}{marca}")

    if nuevas_elite:
        print("\a")  # beep
        print("  " + "🏆" * 20)
        for sym in nuevas_elite:
            e = elite[sym]
            print(f"\n  🏆 NUEVA ÉLITE DETECTADA: {sym} — {e['nombre']} ({usd(e['market_cap'])})")
            for c in e["criterios"]:
                print(f"     {c}")
        print("\n  " + "🏆" * 20)

    if elite:
        print(f"\n[{hora}] ÉLITE VIGENTE ({len(elite)}): {', '.join(elite.keys())}")
    else:
        print(f"\n[{hora}] Élite vigente: ninguna (el filtro es estricto a propósito — los 6 criterios a la vez)")

    print(f"[{hora}] Próximo escaneo en {INTERVALO_SEG // 60} min. Ctrl+C para detener.")

# ----------------- MAIN 24/7 -----------------

if __name__ == "__main__":
    print("=" * 64)
    print("  RADAR 24/7 — GARP Scout (escaneo cada 20 min)")
    print("  Resultados → radar_resultados.json (tu app los muestra)")
    print("  Élite      → elite_resultados.json (los 6 criterios estrictos)")
    print("  Detener    → Ctrl + C")
    print("=" * 64)
    print("\nNOTA: los datos son de Yahoo Finance (no oficiales, con demora).")
    print("Las élite son candidatas a deep-dive, no recomendaciones de compra.\n")

    while True:
        try:
            ciclo()
        except KeyboardInterrupt:
            print("\n\nRadar detenido. Tus registros quedaron guardados.")
            break
        except Exception as e:
            print(f"\n[!] Error en el ciclo: {e}. Reintento en el próximo ciclo.")
        try:
            time.sleep(INTERVALO_SEG)
        except KeyboardInterrupt:
            print("\n\nRadar detenido. Tus registros quedaron guardados.")
            break