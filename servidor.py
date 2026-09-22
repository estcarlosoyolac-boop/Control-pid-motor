# Servidor: HTTP + base de datos SQL + web
# pip install flask
# python servidor.py   ->  http://localhost:5000  (o http://IP_DEL_PC:5000)
#
#   ESP32  --POST /api/datos-->  guarda en SQL, responde {"sp": ...}
#   Web    --GET  /api/estado-->     último valor
#   Web    --GET  /api/historico-->  últimos 5 min para la gráfica
#   Web    --POST /api/setpoint-->   cambia el setpoint (lo recoge el ESP32 en su próximo POST)
#   Informe--GET  /api/exportar.csv  descarga toda la tabla
import csv
import io
import os
import sqlite3
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

SP_MIN, SP_MAX = 0.0, 200.0          # rango de operación (RPM) de tu fase 1
SP_INICIAL = 100.0
VENTANA_S = 300                      # 5 minutos

CARPETA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(CARPETA, "datos_motor.db")
SCHEMA = """
-- ================= FASE 4: estructura de la base de datos =================
-- Se ejecuta automáticamente al iniciar el servidor (CREATE ... IF NOT EXISTS)

-- Una fila por muestra que manda el ESP32
CREATE TABLE IF NOT EXISTS mediciones (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,  -- número de fila, lo pone SQLite
    ts        REAL    NOT NULL,   -- marca de tiempo UNIX en segundos (para filtrar/ordenar)
    ts_iso    TEXT    NOT NULL,   -- la misma marca legible: 2026-09-21 10:15:03.250
    t_esp_ms  INTEGER NOT NULL,   -- millis() del ESP32 (para revisar el muestreo)
    rpm       REAL    NOT NULL,   -- variable controlada
    setpoint  REAL    NOT NULL,   -- referencia en ese instante
    pwm       REAL,               -- acción de control
    error     REAL                -- setpoint - rpm
);
-- Índice: hace rápido el "WHERE ts > ..." de la gráfica de 5 min
CREATE INDEX IF NOT EXISTS idx_mediciones_ts ON mediciones(ts);

-- Historial de cambios de setpoint desde la web (trazabilidad)
CREATE TABLE IF NOT EXISTS cambios_setpoint (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    ts_iso  TEXT NOT NULL,
    valor   REAL NOT NULL
);

-- Valor actual del setpoint remoto (una sola fila)
CREATE TABLE IF NOT EXISTS config (
    clave  TEXT PRIMARY KEY,
    valor  REAL NOT NULL
);
"""

app = Flask(__name__)
ultimo_post = {"t": 0.0}             # para saber si el ESP32 está conectado


def iso(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def conectar():
    con = sqlite3.connect(DB, timeout=5)
    con.row_factory = sqlite3.Row    # permite leer columnas por nombre: fila["rpm"]
    return con


def crear_bd():
    con = conectar()
    con.execute("PRAGMA journal_mode=WAL")   # permite leer (web) mientras se escribe (ESP32)
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO config VALUES ('setpoint', ?)", (SP_INICIAL,))
    con.commit()
    con.close()


def leer_setpoint(con):
    return con.execute("SELECT valor FROM config WHERE clave='setpoint'").fetchone()["valor"]


# ------------------- ESP32 -> BD -------------------
@app.post("/api/datos")
def recibir():
    ahora = time.time()
    lote = request.get_json(silent=True, force=True) or {}
    lote = lote.get("m", [])
    con = conectar()
    if lote:
        t_ultima = lote[-1][0]
        filas = []
        for t_ms, rpm, sp, pwm in lote:
            ts = ahora - (t_ultima - t_ms) / 1000.0
            filas.append((ts, iso(ts), t_ms, rpm, sp, pwm, sp - rpm))
        con.executemany(
            "INSERT INTO mediciones (ts, ts_iso, t_esp_ms, rpm, setpoint, pwm, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", filas)
        con.commit()
    sp = leer_setpoint(con)
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
    f = con.execute("SELECT * FROM mediciones ORDER BY id DESC LIMIT 1").fetchone()
    total = con.execute("SELECT COUNT(*) FROM mediciones").fetchone()[0]
    sp = leer_setpoint(con)
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
    filas = con.execute(
        "SELECT ts, rpm, setpoint, pwm FROM mediciones WHERE ts > ? ORDER BY ts",
        (ahora - VENTANA_S,)).fetchall()
    con.close()
    # x = segundos relativos a "ahora" (-300 ... 0): así el eje no depende del reloj del navegador
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
    con.execute("UPDATE config SET valor=? WHERE clave='setpoint'", (v,))
    con.execute("INSERT INTO cambios_setpoint (ts, ts_iso, valor) VALUES (?, ?, ?)",
                (ahora, iso(ahora), v))
    con.commit()
    con.close()
    return jsonify(sp=v)


@app.get("/api/exportar.csv")
def exportar():
    con = conectar()
    filas = con.execute("SELECT ts_iso, ts, t_esp_ms, rpm, setpoint, pwm, error "
                        "FROM mediciones ORDER BY ts").fetchall()
    con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts_iso", "ts", "t_esp_ms", "rpm", "setpoint", "pwm", "error"])
    w.writerows([tuple(f) for f in filas])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=mediciones.csv"})


if __name__ == "__main__":
    crear_bd()
    app.run(host="0.0.0.0", port=5000, threaded=True)
