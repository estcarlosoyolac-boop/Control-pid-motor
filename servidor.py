# Servidor: HTTP + base de datos SQL + web
#
#  - En tu PC          -> usa SQLite (archivo datos_motor.db).      python servidor.py
#  - En Render         -> usa PostgreSQL si existe la variable DATABASE_URL.
#    El mismo código funciona en los dos lados; no hay que cambiar nada.
import csv
import io
import os
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

SP_MIN, SP_MAX = 0.0, 200.0          # rango de operación (RPM)
SP_INICIAL = 100.0
VENTANA_S = 300                      # 5 minutos de historial en la gráfica

# ---------- ¿PostgreSQL (Render) o SQLite (PC)? ----------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PG = DATABASE_URL.startswith("postgres")

if PG:
    import psycopg
    from psycopg.rows import dict_row
    P = "%s"                          # marcador de parámetros en PostgreSQL
    SERIAL = "SERIAL PRIMARY KEY"
    REAL = "DOUBLE PRECISION"
    ENTERO = "BIGINT"
else:
    import sqlite3
    P = "?"                           # marcador de parámetros en SQLite
    SERIAL = "INTEGER PRIMARY KEY AUTOINCREMENT"
    REAL = "REAL"
    ENTERO = "INTEGER"
    DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos_motor.db")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS mediciones (
    id        {SERIAL},
    ts        {REAL}   NOT NULL,   -- marca de tiempo UNIX en segundos
    ts_iso    TEXT     NOT NULL,   -- la misma marca legible
    t_esp_ms  {ENTERO} NOT NULL,   -- millis() del ESP32
    rpm       {REAL}   NOT NULL,   -- variable controlada
    setpoint  {REAL}   NOT NULL,   -- referencia
    pwm       {REAL},              -- acción de control
    error     {REAL}               -- setpoint - rpm
);
CREATE INDEX IF NOT EXISTS idx_mediciones_ts ON mediciones(ts);

CREATE TABLE IF NOT EXISTS cambios_setpoint (
    id      {SERIAL},
    ts      {REAL} NOT NULL,
    ts_iso  TEXT   NOT NULL,
    valor   {REAL} NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    clave  TEXT PRIMARY KEY,
    valor  {REAL} NOT NULL
);
"""

app = Flask(__name__)
ultimo_post = {"t": 0.0}             # para saber si el ESP32 está conectado


def iso(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def conectar():
    if PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    con = sqlite3.connect(DB, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def crear_bd():
    con = conectar()
    cur = con.cursor()
    if PG:
        cur.execute(SCHEMA)
        cur.execute(f"INSERT INTO config VALUES ('setpoint', {P}) ON CONFLICT DO NOTHING",
                    (SP_INICIAL,))
    else:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        con.execute(f"INSERT OR IGNORE INTO config VALUES ('setpoint', {P})", (SP_INICIAL,))
    con.commit()
    con.close()


def leer_setpoint(cur):
    cur.execute("SELECT valor FROM config WHERE clave = 'setpoint'")
    return float(cur.fetchone()["valor"])


# ------------------- ESP32 -> BD -------------------
@app.post("/api/datos")
def recibir():
    ahora = time.time()
    lote = (request.get_json(silent=True, force=True) or {}).get("m", [])
    con = conectar()
    cur = con.cursor()
    if lote:
        t_ultima = lote[-1][0]
        filas = []
        for t_ms, rpm, sp, pwm in lote:
            ts = ahora - (t_ultima - t_ms) / 1000.0
            filas.append((ts, iso(ts), int(t_ms), float(rpm), float(sp), float(pwm),
                          float(sp) - float(rpm)))
        cur.executemany(
            "INSERT INTO mediciones (ts, ts_iso, t_esp_ms, rpm, setpoint, pwm, error) "
            f"VALUES ({P}, {P}, {P}, {P}, {P}, {P}, {P})", filas)
        con.commit()
    sp = leer_setpoint(cur)
    con.close()
    ultimo_post["t"] = ahora
    return jsonify(sp=sp)            # el ESP32 lee este setpoint


# ------------------- BD -> Web -------------------
@app.get("/")
def pagina():
    return render_template("index.html", sp_min=SP_MIN, sp_max=SP_MAX, ventana=VENTANA_S)


@app.get("/api/estado")
def estado():
    con = conectar()
    cur = con.cursor()
    cur.execute("SELECT rpm, pwm, error, ts_iso FROM mediciones ORDER BY id DESC LIMIT 1")
    f = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS n FROM mediciones")
    total = cur.fetchone()["n"]
    sp = leer_setpoint(cur)
    con.close()
    return jsonify(
        rpm=f["rpm"] if f else None,
        pwm=f["pwm"] if f else None,
        error=f["error"] if f else None,
        hora=f["ts_iso"] if f else None,
        setpoint=sp,
        total=total,
        conectado=(time.time() - ultimo_post["t"]) < 3,
    )


@app.get("/api/historico")
def historico():
    ahora = time.time()
    con = conectar()
    cur = con.cursor()
    cur.execute(f"SELECT ts, rpm, setpoint, pwm FROM mediciones WHERE ts > {P} ORDER BY ts",
                (ahora - VENTANA_S,))
    filas = cur.fetchall()
    con.close()
    # x = segundos relativos a "ahora" (-300 ... 0)
    return jsonify(
        t=[round(f["ts"] - ahora, 2) for f in filas],
        rpm=[f["rpm"] for f in filas],
        sp=[f["setpoint"] for f in filas],
        pwm=[f["pwm"] for f in filas],
    )


@app.post("/api/setpoint")
def cambiar_setpoint():
    try:
        v = float(request.get_json(force=True)["sp"])
    except (TypeError, ValueError, KeyError):
        return jsonify(error="Escribe un número"), 400
    if not SP_MIN <= v <= SP_MAX:
        return jsonify(error=f"Fuera del rango {SP_MIN:g} a {SP_MAX:g} RPM"), 400
    ahora = time.time()
    con = conectar()
    cur = con.cursor()
    cur.execute(f"UPDATE config SET valor = {P} WHERE clave = 'setpoint'", (v,))
    cur.execute(f"INSERT INTO cambios_setpoint (ts, ts_iso, valor) VALUES ({P}, {P}, {P})",
                (ahora, iso(ahora), v))
    con.commit()
    con.close()
    return jsonify(sp=v)


@app.get("/api/exportar.csv")
def exportar():
    con = conectar()
    cur = con.cursor()
    cur.execute("SELECT ts_iso, ts, t_esp_ms, rpm, setpoint, pwm, error "
                "FROM mediciones ORDER BY ts")
    filas = cur.fetchall()
    con.close()
    cols = ["ts_iso", "ts", "t_esp_ms", "rpm", "setpoint", "pwm", "error"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows([[f[c] for c in cols] for f in filas])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=mediciones.csv"})


crear_bd()          # se ejecuta también en Render, que arranca la app con gunicorn

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
