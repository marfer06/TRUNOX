"""
Reserva de turnos TurnosX hablando directo con la API (para Termux / celular).
Solo usa la librería estándar de Python: no hay que instalar nada.

La sesión vive en sesion.json (misma carpeta): {"accessToken": "...", "refreshToken": "..."}
El script renueva solo el accessToken (dura 60 min) con el refreshToken y guarda lo nuevo.
NUNCA pegues el contenido de sesion.json en un chat.

1) Probar la sesión (no lista ni reserva nada):
       python reservar_celular.py --solo-sesion
2) Probar el listado (no reserva):
       python reservar_celular.py --prueba --ahora --actividad Musculación --hora 19:00
3) Uso normal (arranca solo a las 12:00 hora Argentina; CrossFit 19:00, lun a vie):
       python reservar_celular.py
"""
import argparse
import base64
from http.client import HTTPException, HTTPSConnection
import json
import os
import socket
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

API = "https://api.turnosx.com"
ORG = "019f869c-19ed-7319-8da3-5107ae59805c"
SEDES = {"BOMBAL": "019f869c-1a7b-783a-8484-169e94285c00"}
ART = timezone(timedelta(hours=-3))  # hora de Argentina, sin depender de la config del celular
PENDIENTES = {"SIN_CUPO", "NO_ENCONTRADA", "ERROR", "SIN_CONFIRMAR"}
RUTA_SESION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sesion.json")


def log(msg):
    print(f"{datetime.now(ART):%H:%M:%S.%f}"[:-3] + " " + msg, flush=True)


# ---------------------------------------------------------------- HTTP básico
# Solo IPv4: en algunas redes (Windows/celular) Python pierde 10 s probando IPv6 antes de caer a IPv4.
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, port, family=0, *a, **k: _getaddrinfo(host, port, socket.AF_INET, *a, **k)

API_HOST = "api.turnosx.com"
_conn = None


def _nueva_conexion(timeout):
    return HTTPSConnection(API_HOST, timeout=timeout)


CONEXION = _nueva_conexion  # se puede reemplazar en pruebas


def http(metodo, url, headers=None, cuerpo=None, timeout=8):
    """Una sola conexión reutilizada (keep-alive): evita repetir el saludo TLS en cada llamada."""
    global _conn
    p = urllib.parse.urlsplit(url)
    path = p.path + ("?" + p.query if p.query else "")
    h = {
        "Accept": "application/json",
        "Origin": "https://pwa.turnosx.com",
        "Referer": "https://pwa.turnosx.com/",
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
    }
    h.update(headers or {})
    data = None
    if cuerpo is not None:
        data = json.dumps(cuerpo).encode()
        h["Content-Type"] = "application/json"
    err = ""
    for intento in range(2):
        try:
            if _conn is None:
                _conn = CONEXION(timeout)
            _conn.request(metodo, path, body=data, headers=h)
            r = _conn.getresponse()
            txt = r.read().decode("utf-8", "replace")
            try:
                return r.status, (json.loads(txt) if txt else None)
            except ValueError:
                return r.status, txt
        except (HTTPException, OSError) as e:
            err = str(e)
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
    return 0, err


# ---------------------------------------------------------------- avisos al celular (ntfy.sh)
def _conexion_aviso(timeout):
    return HTTPSConnection("ntfy.sh", timeout=timeout)


AVISO_CONEXION = _conexion_aviso  # se puede reemplazar en pruebas


def avisar(topic, texto, prioridad="default"):
    """Manda una notificación push a la app ntfy (iPhone/Android). No hace nada si no hay topic."""
    if not topic:
        return
    try:
        c = AVISO_CONEXION(8)
        c.request("POST", "/" + urllib.parse.quote(topic, safe=""), body=texto.encode("utf-8"),
                  headers={"Title": "TurnosX", "Priority": prioridad,
                           "Content-Type": "text/plain; charset=utf-8"})
        c.getresponse().read()
        c.close()
    except Exception as e:
        log(f"(no pude enviar el aviso: {e})")


# ---------------------------------------------------------------- sesión
def jwt_exp(token):
    """Fecha de vencimiento (epoch) leída del propio token. No valida la firma."""
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp")
    except Exception:
        return None


def leer_json_tolerante(ruta):
    """Busca en el archivo el bloque con la sesión (accessToken/refreshToken).
    Tolera BOM, UTF-16, texto de más antes o después, o valores pegados dos veces."""
    with open(ruta, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        txt = raw.decode("utf-16")
    else:
        txt = raw.decode("utf-8-sig", "replace")

    dec = json.JSONDecoder()
    pos = txt.find("{")
    while pos != -1:
        try:
            obj, _ = dec.raw_decode(txt[pos:])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            d = obj.get("state") if isinstance(obj.get("state"), dict) else obj
            if d.get("refreshToken"):
                return obj
        pos = txt.find("{", pos + 1)
    raise ValueError("no encontré un bloque con accessToken y refreshToken")


class Sesion:
    def __init__(self, ruta=RUTA_SESION):
        self.ruta = ruta
        try:
            d = leer_json_tolerante(ruta)
        except FileNotFoundError:
            log(f"[!] Falta {ruta} (ver instrucciones).")
            sys.exit(1)
        except Exception as e:
            log(f"[!] sesion.json no tiene un formato válido ({e}). Tiene que empezar con {{ y contener accessToken y refreshToken.")
            sys.exit(1)
        d = d.get("state", d)  # acepta tanto el valor de turnosx-session pegado tal cual como {"accessToken","refreshToken"}
        self.access = d.get("accessToken") or ""
        self.refresh = d.get("refreshToken") or ""
        self.rotado = False
        if not self.refresh:
            log("[!] sesion.json no tiene refreshToken.")
            sys.exit(1)

    def guardar(self):
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"accessToken": self.access, "refreshToken": self.refresh}, f)
        os.replace(tmp, self.ruta)  # escritura atómica: nunca queda a medias

    def segundos_restantes(self):
        exp = jwt_exp(self.access) if self.access else None
        return (exp - time.time()) if exp else -1

    def renovar(self):
        """Pide un accessToken nuevo (POST /v1/auth/refresh), igual que la app."""
        for intento in range(3):
            st, resp = http("POST", f"{API}/v1/auth/refresh", cuerpo={"refreshToken": self.refresh})
            if st == 429:
                time.sleep(2 + intento * 3)
                continue
            if st == 200 and isinstance(resp, dict) and resp.get("accessToken") and resp.get("refreshToken"):
                if resp["refreshToken"] != self.refresh:
                    self.rotado = True
                self.access, self.refresh = resp["accessToken"], resp["refreshToken"]
                self.guardar()
                return True
            log(f"[!] No pude renovar la sesión (HTTP {st}).")
            return False
        log("[!] Renovar sesión: demasiados pedidos (429).")
        return False

    def asegurar(self, margen=600):
        """Renueva si el token vence en menos de `margen` segundos."""
        if self.segundos_restantes() < margen:
            return self.renovar()
        return True

    def api(self, metodo, url, cuerpo=None):
        """Llamada autenticada. Si da 401 renueva y reintenta una vez (como la app)."""
        for vuelta in range(2):
            st, resp = http(metodo, url, {"Authorization": f"Bearer {self.access}"}, cuerpo)
            if st == 401 and vuelta == 0:
                if self.renovar():
                    continue
            return st, resp
        return st, resp


# ---------------------------------------------------------------- lógica
def rango_dia(d):
    """Día calendario argentino -> rango UTC que usa la app (03:00Z a 02:59:59.999Z)."""
    ini = datetime(d.year, d.month, d.day, tzinfo=ART).astimezone(timezone.utc)
    fin = ini + timedelta(days=1) - timedelta(milliseconds=1)
    f = lambda x: x.strftime("%Y-%m-%dT%H:%M:%S.") + f"{x.microsecond // 1000:03d}Z"
    return f(ini), f(fin)


def hora_art(start_at):
    t = datetime.strptime(start_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return t.astimezone(ART).strftime("%H:%M")


def listar_slots(sesion, sede_id, d):
    desde, hasta = rango_dia(d)
    q = urllib.parse.urlencode({"from": desde, "to": hasta})
    return sesion.api("GET", f"{API}/v1/pwa/organizations/{ORG}/businesses/{sede_id}/slots?{q}")


def buscar_slot(slots, actividad, hora):
    for s in slots:
        if actividad.lower() in (s.get("title") or "").lower() and hora_art(s["startAt"]) == hora:
            return s
    return None


def reservar_dia(sesion, sede_id, d, args):
    etiqueta = f"{d:%a %d/%m} {args.hora}"
    status, slots = listar_slots(sesion, sede_id, d)
    if status in (401, 403):
        log(f"[!] {etiqueta}: la sesión no sirve (HTTP {status}). Hay que volver a copiar sesion.json.")
        return "SESION_VENCIDA"
    if status != 200 or not isinstance(slots, list):
        log(f"[!] {etiqueta}: error al listar (HTTP {status}): {str(slots)[:200]}")
        return "ERROR"

    s = buscar_slot(slots, args.actividad, args.hora)
    if not s:
        if slots:
            log(f"[-] {etiqueta}: ese día no hay {args.actividad} a esa hora. Paso al siguiente.")
            return "SIN_CLASE"
        return "NO_ENCONTRADA"   # el día no tiene turnos publicados (todavía)

    if s.get("status") == "booked":
        log(f"[=] {etiqueta}: ya estaba reservado")
        return "YA_RESERVADO"
    if s.get("capacity") is not None and s.get("bookedCount", 0) >= s["capacity"]:
        log(f"[~] {etiqueta}: sin cupo ({s['bookedCount']}/{s['capacity']}), queda en espera")
        return "SIN_CUPO"

    if args.prueba:
        log(f"[PRUEBA] {etiqueta}: reservaría '{s['title']}' ({s['bookedCount']}/{s['capacity']})")
        return "PRUEBA_OK"

    t0 = time.time()
    st, resp = sesion.api("POST", f"{API}/v1/pwa/organizations/{ORG}/businesses/{sede_id}/bookings",
                          {"slotId": s["id"]})
    ms = int((time.time() - t0) * 1000)
    if st in (200, 201) and isinstance(resp, dict) and resp.get("status") == "confirmed":
        log(f"[+] {etiqueta}: RESERVADO ({ms} ms)")
        return "RESERVADO"
    if st in (401, 403):
        log(f"[!] {etiqueta}: la sesión no sirve (HTTP {st}).")
        return "SESION_VENCIDA"
    log(f"[?] {etiqueta}: HTTP {st}: {str(resp)[:300]}")
    return "SIN_CONFIRMAR"


DIAS_SEM = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
NOMBRES_DIA = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def objetivo_arranque(texto, ahora=None):
    """'17:00' = hoy a esa hora. 'sab 17:00' = el próximo sábado a esa hora (hoy mismo si es sábado). Hora Argentina."""
    partes = texto.lower().replace("é", "e").replace("á", "a").split()
    h, m = map(int, partes[-1].split(":"))
    ahora = ahora or datetime.now(ART)
    obj = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
    if len(partes) > 1:
        obj = obj + timedelta(days=(DIAS_SEM[partes[0][:3]] - ahora.weekday()) % 7)
    return obj


def esperar_hasta(texto, sesion):
    objetivo = objetivo_arranque(texto)
    if datetime.now(ART) >= objetivo:
        log(f"Ya pasó {texto}: arranco ahora mismo.")
        return
    log(f"Temporizador: arranco solo el {NOMBRES_DIA[objetivo.weekday()]} {objetivo:%d/%m} "
        f"a las {objetivo:%H:%M}:00 (hora Argentina).")
    ultimo, renovado = None, False
    while True:
        falta = (objetivo - datetime.now(ART)).total_seconds()
        if falta <= 0:
            break
        paso = 60 if falta < 7200 else 3600
        marca = int(falta // paso)
        if falta > 60 and marca != ultimo and falta % paso < 1:
            ultimo = marca
            if falta >= 7200:
                log(f"    faltan {int(falta // 3600)} h {int(falta % 3600 // 60)} min...")
            else:
                log(f"    faltan {int(falta // 60) + 1} min...")
        if falta < 45 and not renovado:      # dejar el token fresco ANTES de la hora
            sesion.asegurar(margen=900)
            renovado = True
        time.sleep(0.5 if falta > 2 else 0.005)
    log(f"Son las {objetivo:%H:%M}: ARRANCO.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actividad", default="Crossfit")
    ap.add_argument("--hora", default="19:00", help="hora de Argentina, ej 19:00")
    ap.add_argument("--sede", default="BOMBAL")
    ap.add_argument("--dias-semana", default="lun,mar,mie,jue,vie")
    ap.add_argument("--fecha", help="un solo día AAAA-MM-DD")
    ap.add_argument("--esperar", default="sab 17:00", help="'17:00' (hoy) o 'sab 17:00' (próximo sábado), hora Argentina")
    ap.add_argument("--ahora", action="store_true", help="no esperar la hora")
    ap.add_argument("--prueba", action="store_true", help="solo mirar, NO reservar")
    ap.add_argument("--solo-sesion", action="store_true", help="solo comprobar/renovar la sesión")
    ap.add_argument("--reintentos", type=int, default=6, help="segundos probando cada día antes de pasar al siguiente")
    ap.add_argument("--espera", type=int, default=3, help="minutos extra para lo pendiente")
    ap.add_argument("--pausa", type=int, default=10)
    ap.add_argument("--aviso", help="tema de ntfy.sh para recibir el resultado en el celular")
    args = ap.parse_args()

    sesion = Sesion()

    if args.solo_sesion:
        ok = sesion.renovar()
        if ok:
            log(f"[OK] Sesión válida. El token vence en {int(sesion.segundos_restantes() // 60)} min "
                f"(se renueva solo). No se reservó nada.")
            extra = ""
            if sesion.rotado:
                extra = "\nOJO: el servidor cambió el refreshToken; hay que actualizar la sesión guardada."
                log("[!]" + extra.strip())
            avisar(args.aviso, "Sesión OK (no se reservó nada)." + extra)
        else:
            log("[X] La sesión no se pudo renovar: hay que volver a copiar sesion.json.")
            avisar(args.aviso, "La sesión NO sirve: hay que volver a copiar sesion.json.", "high")
        return

    sede_id = SEDES[args.sede.upper()]
    if args.fecha:
        fechas = [datetime.strptime(args.fecha, "%Y-%m-%d").date()]
    else:
        hoy = datetime.now(ART).date()
        if not args.ahora and args.esperar:
            hoy = objetivo_arranque(args.esperar).date()   # los días se cuentan desde el día de arranque
        mapa = {"lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6}
        limpio = args.dias_semana.lower().replace("é", "e").replace("á", "a")
        ok = {mapa[x.strip()[:3]] for x in limpio.split(",") if x.strip()}
        fechas = [f for f in (hoy + timedelta(days=i) for i in range(7)) if f.weekday() in ok]

    log(f"=== {args.actividad} {args.hora} | sede {args.sede} | "
        f"{'MODO PRUEBA (no reserva)' if args.prueba else 'RESERVA REAL'} ===")
    log("Días: " + ", ".join(f"{f:%d/%m}" for f in fechas))

    if not sesion.asegurar(margen=900):
        log("[X] No se pudo preparar la sesión. Corto acá.")
        avisar(args.aviso, "No se pudo preparar la sesión: no se reservó nada. Hay que renovar sesion.json.", "high")
        return

    if not args.ahora and args.esperar:
        esperar_hasta(args.esperar, sesion)

    resultados = {}
    for d in fechas:
        limite = time.time() + args.reintentos
        while True:
            r = reservar_dia(sesion, sede_id, d, args)
            if r == "SESION_VENCIDA":
                log("Corto acá: volvé a copiar sesion.json y corré de nuevo.")
                avisar(args.aviso, "La sesión dejó de servir en plena reserva. Hay que renovar sesion.json.", "high")
                return
            if r not in PENDIENTES or r == "SIN_CUPO" or time.time() >= limite:
                break          # sin cupo: sigo con el próximo día y vuelvo después
            time.sleep(0.3)
        resultados[d] = r

    pend = [d for d, r in resultados.items() if r in PENDIENTES]
    if pend and args.espera > 0:
        limite = time.time() + args.espera * 60
        log("EN ESPERA: " + ", ".join(f"{d:%d/%m} ({resultados[d]})" for d in pend))
        while pend and time.time() < limite:
            time.sleep(args.pausa)
            for d in list(pend):
                resultados[d] = reservar_dia(sesion, sede_id, d, args)
                if resultados[d] not in PENDIENTES:
                    pend.remove(d)

    log("--- Resumen ---")
    dias = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    lineas = []
    for d, r in resultados.items():
        log(f"{d:%a %d/%m} {args.hora}: {r}")
        lineas.append(f"{dias[d.weekday()]} {d:%d/%m} {args.hora}: {r}")
    bien = {"RESERVADO", "YA_RESERVADO", "PRUEBA_OK"}
    todo_ok = all(r in bien for r in resultados.values())
    texto = f"{args.actividad} {args.sede} {'(PRUEBA)' if args.prueba else ''}\n" + "\n".join(lineas)
    if sesion.rotado:
        texto += "\nOJO: el servidor cambió el refreshToken; hay que actualizar la sesión guardada."
        log("[!] El servidor cambió el refreshToken: hay que actualizar la sesión guardada.")
    avisar(args.aviso, texto, "default" if todo_ok else "high")


if __name__ == "__main__":
    main()
