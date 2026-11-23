
"""
Reserva de turnos TurnosX - API directa (Termux / celular).
Solo utiliza la biblioteca estándar de Python.

SESIÓN:
  sesion.json en la misma carpeta.
  Renueva accessToken mediante refreshToken automáticamente.

PROGRAMACIÓN:
  Lunes:       CrossFit 19:00
  Martes:      Cross Open 1h 18:00 -> CrossFit 19:00
  Miércoles:   CrossFit 19:00
  Jueves:      Cross Open 1h 18:00 -> CrossFit 19:00
  Viernes:     VLK 19:00

En martes y jueves:
  - Una consulta inicial de horarios por día.
  - Primero se intenta Cross Open 1h.
  - Si no existe, no tiene cupo o falla la reserva,
    se pasa inmediatamente a CrossFit.
  - Nunca se espera por la primera actividad para intentar la segunda.

PRUEBAS:
  python reservar_celular.py --solo-sesion

  python reservar_celular.py --prueba --ahora --dias-semana mar,jue

  python reservar_celular.py --prueba --ahora --fecha 2026-11-24

USO NORMAL:
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

SEDES = {
    "BOMBAL": "019f869c-1a7b-783a-8484-169e94285c00"
}

ART = timezone(timedelta(hours=-3))

PENDIENTES = {
    "SIN_CUPO",
    "NO_ENCONTRADA",
    "ERROR",
    "SIN_CONFIRMAR"
}

RUTA_SESION = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "sesion.json"
)


def log(msg):
    print(
        f"{datetime.now(ART):%H:%M:%S.%f}"[:-3]
        + " "
        + msg,
        flush=True
    )


# ----------------------------------------------------------------
# HTTP básico
# ----------------------------------------------------------------

_getaddrinfo = socket.getaddrinfo

socket.getaddrinfo = lambda host, port, family=0, *a, **k: (
    _getaddrinfo(host, port, socket.AF_INET, *a, **k)
)

API_HOST = "api.turnosx.com"
_conn = None


def _nueva_conexion(timeout):
    return HTTPSConnection(API_HOST, timeout=timeout)


CONEXION = _nueva_conexion


def http(metodo, url, headers=None, cuerpo=None, timeout=8):
    """Una sola conexión reutilizada (keep-alive)."""

    global _conn

    p = urllib.parse.urlsplit(url)

    path = p.path + (
        "?" + p.query if p.query else ""
    )

    h = {
        "Accept": "application/json",
        "Origin": "https://pwa.turnosx.com",
        "Referer": "https://pwa.turnosx.com/",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) "
            "AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
        ),
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

            _conn.request(
                metodo,
                path,
                body=data,
                headers=h
            )

            r = _conn.getresponse()
            txt = r.read().decode("utf-8", "replace")

            try:
                return r.status, (
                    json.loads(txt) if txt else None
                )
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


# ----------------------------------------------------------------
# Avisos al celular
# ----------------------------------------------------------------

def _conexion_aviso(timeout):
    return HTTPSConnection("ntfy.sh", timeout=timeout)


AVISO_CONEXION = _conexion_aviso


def avisar(topic, texto, prioridad="default"):
    """Envía una notificación push a ntfy."""

    if not topic:
        return

    try:
        c = AVISO_CONEXION(8)

        c.request(
            "POST",
            "/" + urllib.parse.quote(topic, safe=""),
            body=texto.encode("utf-8"),
            headers={
                "Title": "TurnosX",
                "Priority": prioridad,
                "Content-Type": "text/plain; charset=utf-8",
            },
        )

        c.getresponse().read()
        c.close()

    except Exception as e:
        log(f"(no pude enviar el aviso: {e})")


# ----------------------------------------------------------------
# Sesión
# ----------------------------------------------------------------

def jwt_exp(token):
    """Lee la fecha de vencimiento del JWT. No valida su firma."""

    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)

        return json.loads(
            base64.urlsafe_b64decode(p)
        ).get("exp")

    except Exception:
        return None


def leer_json_tolerante(ruta):
    """Busca el bloque de sesión y tolera BOM, UTF-16 y texto adicional."""

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
            d = (
                obj.get("state")
                if isinstance(obj.get("state"), dict)
                else obj
            )

            if d.get("refreshToken"):
                return obj

        pos = txt.find("{", pos + 1)

    raise ValueError(
        "no encontré un bloque con accessToken y refreshToken"
    )


class Sesion:

    def __init__(self, ruta=RUTA_SESION):

        self.ruta = ruta

        try:
            d = leer_json_tolerante(ruta)

        except FileNotFoundError:
            log(f"[!] Falta {ruta} (ver instrucciones).")
            sys.exit(1)

        except Exception as e:
            log(
                f"[!] sesion.json no tiene un formato válido ({e})."
            )
            sys.exit(1)

        d = d.get("state", d)

        self.access = d.get("accessToken") or ""
        self.refresh = d.get("refreshToken") or ""
        self.rotado = False

        if not self.refresh:
            log("[!] sesion.json no tiene refreshToken.")
            sys.exit(1)

    def guardar(self):

        tmp = self.ruta + ".tmp"

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "accessToken": self.access,
                    "refreshToken": self.refresh
                },
                f
            )

        os.replace(tmp, self.ruta)

    def segundos_restantes(self):

        exp = jwt_exp(self.access) if self.access else None

        return (exp - time.time()) if exp else -1

    def renovar(self):
        """Renueva la sesión mediante POST /v1/auth/refresh."""

        for intento in range(3):

            st, resp = http(
                "POST",
                f"{API}/v1/auth/refresh",
                cuerpo={
                    "refreshToken": self.refresh
                },
            )

            if st == 429:
                time.sleep(2 + intento * 3)
                continue

            if (
                st == 200
                and isinstance(resp, dict)
                and resp.get("accessToken")
                and resp.get("refreshToken")
            ):

                if resp["refreshToken"] != self.refresh:
                    self.rotado = True

                self.access = resp["accessToken"]
                self.refresh = resp["refreshToken"]

                self.guardar()

                return True

            log(
                f"[!] No pude renovar la sesión (HTTP {st})."
            )

            return False

        log(
            "[!] Renovar sesión: demasiados pedidos (429)."
        )

        return False

    def asegurar(self, margen=600):

        if self.segundos_restantes() < margen:
            return self.renovar()

        return True

    def api(self, metodo, url, cuerpo=None):
        """Llamada autenticada. Si da 401 renueva y reintenta una vez."""

        for vuelta in range(2):

            st, resp = http(
                metodo,
                url,
                {
                    "Authorization": f"Bearer {self.access}"
                },
                cuerpo,
            )

            if st == 401 and vuelta == 0:

                if self.renovar():
                    continue

            return st, resp

        return st, resp


# ----------------------------------------------------------------
# Lógica de horarios
# ----------------------------------------------------------------

def rango_dia(d):
    """Día argentino convertido al rango UTC utilizado por la aplicación."""

    ini = datetime(
        d.year,
        d.month,
        d.day,
        tzinfo=ART
    ).astimezone(timezone.utc)

    fin = (
        ini
        + timedelta(days=1)
        - timedelta(milliseconds=1)
    )

    f = lambda x: (
        x.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{x.microsecond // 1000:03d}Z"
    )

    return f(ini), f(fin)


def hora_art(start_at):

    t = datetime.strptime(
        start_at,
        "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)

    return t.astimezone(ART).strftime("%H:%M")


def listar_slots(sesion, sede_id, d):
    """Consulta todos los horarios de una fecha."""

    desde, hasta = rango_dia(d)

    q = urllib.parse.urlencode({
        "from": desde,
        "to": hasta
    })

    return sesion.api(
        "GET",
        (
            f"{API}/v1/pwa/organizations/{ORG}"
            f"/businesses/{sede_id}/slots?{q}"
        ),
    )


def buscar_slot(slots, actividad, hora):

    for s in slots:

        if (
            actividad.lower()
            in (s.get("title") or "").lower()
            and hora_art(s["startAt"]) == hora
        ):
            return s

    return None


# ----------------------------------------------------------------
# Programación semanal
# ----------------------------------------------------------------

def actividades_del_dia(d, args):
    """
    Devuelve las actividades en orden de prioridad.

    Martes y jueves:
      1. Cross Open 1h - 18:00
      2. CrossFit - 19:00

    El resto conserva la programación original.
    """

    if d.weekday() in (1, 3):

        return [
            (
                args.actividad_open,
                args.hora_open
            ),
            (
                args.actividad,
                args.hora
            ),
        ]

    if d.weekday() == 4:

        return [
            (
                args.actividad_viernes,
                args.hora
            ),
        ]

    if d.weekday() in (0, 2):

        return [
            (
                args.actividad,
                args.hora
            ),
        ]

    return []


# ----------------------------------------------------------------
# Reserva individual
# ----------------------------------------------------------------

def reservar_actividad(
    sesion,
    sede_id,
    d,
    actividad,
    hora,
    slots,
    args
):
    """
    Procesa una actividad utilizando los horarios ya consultados.

    No vuelve a consultar la API por cada actividad.
    """

    etiqueta = (
        f"{d:%a %d/%m} {actividad} {hora}"
    )

    if not slots:
        log(
            f"[-] {etiqueta}: no se encontraron horarios."
        )

        return "NO_ENCONTRADA"

    s = buscar_slot(
        slots,
        actividad,
        hora
    )

    if not s:

        log(
            f"[-] {etiqueta}: no hay turno disponible. "
            "Paso inmediatamente a la siguiente actividad."
        )

        return "SIN_CLASE"

    if s.get("status") == "booked":

        log(
            f"[=] {etiqueta}: ya estaba reservado."
        )

        return "YA_RESERVADO"

    capacity = s.get("capacity")
    booked = s.get("bookedCount", 0)

    if (
        capacity is not None
        and booked >= capacity
    ):

        log(
            f"[~] {etiqueta}: sin cupo "
            f"({booked}/{capacity}). "
            "Paso inmediatamente a la siguiente actividad."
        )

        return "SIN_CUPO"

    if args.prueba:

        log(
            f"[PRUEBA] {etiqueta}: reservaría "
            f"'{s['title']}' ({booked}/{capacity}). "
            "No se envió ninguna reserva."
        )

        return "PRUEBA_OK"

    # Reserva real
    t0 = time.time()

    st, resp = sesion.api(
        "POST",
        (
            f"{API}/v1/pwa/organizations/{ORG}"
            f"/businesses/{sede_id}/bookings"
        ),
        {
            "slotId": s["id"]
        },
    )

    ms = int(
        (time.time() - t0) * 1000
    )

    if (
        st in (200, 201)
        and isinstance(resp, dict)
        and resp.get("status") == "confirmed"
    ):

        log(
            f"[+] {etiqueta}: RESERVADO ({ms} ms)"
        )

        return "RESERVADO"

    if st in (401, 403):

        log(
            f"[!] {etiqueta}: la sesión no sirve (HTTP {st})."
        )

        return "SESION_VENCIDA"

    log(
        f"[?] {etiqueta}: HTTP {st}: "
        f"{str(resp)[:300]}"
    )

    return "SIN_CONFIRMAR"


# ----------------------------------------------------------------
# Procesamiento de un día completo
# ----------------------------------------------------------------

def reservar_dia(sesion, sede_id, d, args):
    """
    Consulta los horarios una sola vez y procesa todas las
    actividades del día en el orden establecido.
    """

    tareas = actividades_del_dia(d, args)

    resultados = {}

    if not tareas:
        return resultados

    status, slots = listar_slots(
        sesion,
        sede_id,
        d
    )

    if status in (401, 403):

        log(
            f"[!] {d:%a %d/%m}: sesión inválida "
            f"(HTTP {status})."
        )

        for actividad, hora in tareas:
            resultados[(actividad, hora)] = "SESION_VENCIDA"

        return resultados

    if status != 200 or not isinstance(slots, list):

        log(
            f"[!] {d:%a %d/%m}: error al listar "
            f"(HTTP {status}): {str(slots)[:200]}"
        )

        for actividad, hora in tareas:
            resultados[(actividad, hora)] = "ERROR"

        return resultados

    # IMPORTANTE:
    # Se procesan las actividades en orden.
    # Si la primera no tiene cupo o no existe,
    # se pasa inmediatamente a la segunda.

    for actividad, hora in tareas:

        r = reservar_actividad(
            sesion,
            sede_id,
            d,
            actividad,
            hora,
            slots,
            args
        )

        resultados[(actividad, hora)] = r

        if r == "SESION_VENCIDA":
            break

    return resultados


DIAS_SEM = {
    "lun": 0,
    "mar": 1,
    "mie": 2,
    "jue": 3,
    "vie": 4,
    "sab": 5,
    "dom": 6,
}

NOMBRES_DIA = [
    "lun",
    "mar",
    "mié",
    "jue",
    "vie",
    "sáb",
    "dom"
]


# ----------------------------------------------------------------
# Temporizador corregido
# ----------------------------------------------------------------

def objetivo_arranque(
    texto,
    ahora=None,
    tolerancia_h=36
):
    """
    '17:00' = hoy a esa hora.

    'sab 17:00' = sábado de esta semana si el horario
    pasó hace menos de 36 horas; si no, próximo sábado.

    Hora Argentina.
    """

    partes = (
        texto.lower()
        .replace("é", "e")
        .replace("á", "a")
        .split()
    )

    h, m = map(
        int,
        partes[-1].split(":")
    )

    ahora = ahora or datetime.now(ART)

    obj = ahora.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    if len(partes) > 1:

        obj += timedelta(
            days=(
                DIAS_SEM[partes[0][:3]]
                - ahora.weekday()
            ) % 7
        )

        anterior = obj - timedelta(days=7)

        if (
            timedelta(0)
            <= ahora - anterior
            <= timedelta(hours=tolerancia_h)
        ):
            obj = anterior

    return obj


# ----------------------------------------------------------------
# Espera corregida
# ----------------------------------------------------------------

def esperar_hasta(texto, sesion):

    objetivo = objetivo_arranque(texto)

    if datetime.now(ART) >= objetivo:

        log(
            f"Ya pasó {texto}: arranco ahora mismo."
        )

        return True

    log(
        f"Temporizador: arranco solo el "
        f"{NOMBRES_DIA[objetivo.weekday()]} "
        f"{objetivo:%d/%m} a las "
        f"{objetivo:%H:%M}:00 (hora Argentina)."
    )

    ultimo, renovado = None, False

    while True:

        falta = (
            objetivo - datetime.now(ART)
        ).total_seconds()

        if falta <= 0:
            break

        paso = 60 if falta < 7200 else 3600
        marca = int(falta // paso)

        if (
            falta > 60
            and marca != ultimo
            and falta % paso < 1
        ):

            ultimo = marca

            if falta >= 7200:

                log(
                    f"    faltan {int(falta // 3600)} h "
                    f"{int(falta % 3600 // 60)} min..."
                )

            else:

                log(
                    f"    faltan {int(falta // 60) + 1} min..."
                )

        # Renovar el token antes del horario de inicio.

        if falta < 45 and not renovado:

            if not sesion.asegurar(margen=900):

                log(
                    "[X] No se pudo renovar la sesión "
                    "antes de comenzar."
                )

                return False

            renovado = True

        time.sleep(
            0.5 if falta > 2 else 0.005
        )

    log(
        f"Son las {objetivo:%H:%M}: ARRANCO."
    )

    return True


# ----------------------------------------------------------------
# Programa principal
# ----------------------------------------------------------------

def main():

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--actividad",
        default="Crossfit",
        help="actividad de lunes a jueves"
    )

    ap.add_argument(
        "--actividad-viernes",
        default="VLK",
        help="actividad de los viernes"
    )

    ap.add_argument(
        "--actividad-open",
        default="Cross Open 1h",
        help="actividad adicional martes y jueves"
    )

    ap.add_argument(
        "--hora",
        default="19:00",
        help="hora de CrossFit y VLK"
    )

    ap.add_argument(
        "--hora-open",
        default="18:00",
        help="hora de Cross Open 1h"
    )

    ap.add_argument(
        "--sede",
        default="BOMBAL"
    )

    ap.add_argument(
        "--dias-semana",
        default="lun,mar,mie,jue,vie"
    )

    ap.add_argument(
        "--fecha",
        help="un solo día AAAA-MM-DD"
    )

    ap.add_argument(
        "--esperar",
        default="sab 17:00",
        help="'17:00' (hoy) o 'sab 17:00'"
    )

    ap.add_argument(
        "--ahora",
        action="store_true",
        help="no esperar la hora"
    )

    ap.add_argument(
        "--prueba",
        action="store_true",
        help="solo mirar, NO reservar"
    )

    ap.add_argument(
        "--solo-sesion",
        action="store_true",
        help="solo comprobar/renovar la sesión"
    )

    ap.add_argument(
        "--reintentos",
        type=int,
        default=6,
        help="segundos probando cada día"
    )

    ap.add_argument(
        "--espera",
        type=int,
        default=3,
        help="minutos extra para lo pendiente"
    )

    ap.add_argument(
        "--pausa",
        type=int,
        default=10
    )

    ap.add_argument(
        "--aviso",
        help="tema de ntfy.sh"
    )

    args = ap.parse_args()

    sesion = Sesion()

    # ------------------------------------------------------------
    # Solo sesión
    # ------------------------------------------------------------

    if args.solo_sesion:

        ok = sesion.renovar()

        if ok:

            log(
                f"[OK] Sesión válida. El token vence en "
                f"{int(sesion.segundos_restantes() // 60)} min "
                f"(se renueva solo). No se reservó nada."
            )

            extra = ""

            if sesion.rotado:

                extra = (
                    "\nOJO: el servidor cambió el refreshToken; "
                    "hay que actualizar la sesión guardada."
                )

                log(
                    "[!]" + extra.strip()
                )

            avisar(
                args.aviso,
                "Sesión OK (no se reservó nada)." + extra
            )

        else:

            log(
                "[X] La sesión no se pudo renovar: "
                "hay que volver a copiar sesion.json."
            )

            avisar(
                args.aviso,
                "La sesión NO sirve: hay que volver a copiar sesion.json.",
                "high"
            )

        return

    # ------------------------------------------------------------
    # Sede
    # ------------------------------------------------------------

    if args.sede.upper() not in SEDES:

        log(
            f"[X] Sede desconocida: {args.sede}"
        )

        return

    sede_id = SEDES[args.sede.upper()]

    # ------------------------------------------------------------
    # Fechas
    # ------------------------------------------------------------

    if args.fecha:

        fechas = [
            datetime.strptime(
                args.fecha,
                "%Y-%m-%d"
            ).date()
        ]

    else:

        hoy = datetime.now(ART).date()

        if not args.ahora and args.esperar:

            hoy = objetivo_arranque(
                args.esperar
            ).date()

        mapa = {
            "lun": 0,
            "mar": 1,
            "mie": 2,
            "jue": 3,
            "vie": 4,
            "sab": 5,
            "dom": 6,
        }

        limpio = (
            args.dias_semana.lower()
            .replace("é", "e")
            .replace("á", "a")
        )

        ok = {
            mapa[x.strip()[:3]]
            for x in limpio.split(",")
            if x.strip()
        }

        fechas = [
            f
            for f in (
                hoy + timedelta(days=i)
                for i in range(7)
            )
            if f.weekday() in ok
        ]

    # ------------------------------------------------------------
    # Información inicial
    # ------------------------------------------------------------

    log(
        f"=== Lun-jue: {args.actividad} {args.hora} | "
        f"Mar-jue adicional: {args.actividad_open} "
        f"{args.hora_open} | "
        f"Vie: {args.actividad_viernes} {args.hora} | "
        f"Sede {args.sede} | "
        f"{'MODO PRUEBA (no reserva)' if args.prueba else 'RESERVA REAL'} ==="
    )

    log(
        "Días: "
        + ", ".join(
            f"{f:%d/%m}" for f in fechas
        )
    )

    # ------------------------------------------------------------
    # Preparar sesión
    # ------------------------------------------------------------

    if not sesion.asegurar(margen=900):

        log(
            "[X] No se pudo preparar la sesión. Corto acá."
        )

        avisar(
            args.aviso,
            "No se pudo preparar la sesión: no se reservó nada. "
            "Hay que renovar sesion.json.",
            "high"
        )

        return

    # ------------------------------------------------------------
    # Espera
    # ------------------------------------------------------------

    if not args.ahora and args.esperar:

        if not esperar_hasta(
            args.esperar,
            sesion
        ):

            avisar(
                args.aviso,
                "No se pudo renovar la sesión antes de reservar.",
                "high"
            )

            return

    # ------------------------------------------------------------
    # Primera pasada: procesar todas las fechas
    # ------------------------------------------------------------

    resultados = {}

    for d in fechas:

        limite = time.time() + args.reintentos

        while True:

            salida = reservar_dia(
                sesion,
                sede_id,
                d,
                args
            )

            if any(
                r == "SESION_VENCIDA"
                for r in salida.values()
            ):

                log(
                    "Corto acá: volvé a copiar sesion.json "
                    "y corré de nuevo."
                )

                avisar(
                    args.aviso,
                    "La sesión dejó de servir en plena reserva. "
                    "Hay que renovar sesion.json.",
                    "high"
                )

                return

            for (actividad, hora), r in salida.items():

                resultados[
                    (d, actividad, hora)
                ] = r

            # Si no hay pendientes recuperables,
            # pasar inmediatamente al siguiente día.

            recuperables = [
                r
                for r in salida.values()
                if r in PENDIENTES
                and r != "SIN_CUPO"
            ]

            if (
                not recuperables
                or time.time() >= limite
            ):
                break

            # Mantiene los reintentos originales.
            # La segunda actividad ya fue intentada
            # antes de llegar a esta espera.

            time.sleep(0.3)

    # ------------------------------------------------------------
    # Segunda pasada: pendientes
    # ------------------------------------------------------------

    def pendientes_actuales():

        return [
            clave
            for clave, r in resultados.items()
            if r in PENDIENTES
        ]

    pend = pendientes_actuales()

    if pend and args.espera > 0:

        limite = (
            time.time()
            + args.espera * 60
        )

        log(
            "EN ESPERA: "
            + ", ".join(
                f"{d:%d/%m} {actividad} {hora} ({resultados[(d, actividad, hora)]})"
                for d, actividad, hora in pend
            )
        )

        while pend and time.time() < limite:

            time.sleep(args.pausa)

            # Agrupar pendientes por fecha:
            # una sola consulta de horarios por día.

            fechas_pendientes = sorted({
                clave[0]
                for clave in pend
            })

            for d in fechas_pendientes:

                tareas = [
                    (actividad, hora)
                    for fecha, actividad, hora in pend
                    if fecha == d
                ]

                status, slots = listar_slots(
                    sesion,
                    sede_id,
                    d
                )

                if status in (401, 403):

                    log(
                        f"[!] Sesión inválida durante "
                        f"los reintentos del {d:%d/%m}."
                    )

                    avisar(
                        args.aviso,
                        "La sesión dejó de servir durante los reintentos.",
                        "high"
                    )

                    return

                if (
                    status != 200
                    or not isinstance(slots, list)
                ):

                    log(
                        f"[!] Error al consultar pendientes "
                        f"del {d:%d/%m}: HTTP {status}"
                    )

                    continue

                for actividad, hora in tareas:

                    clave = (
                        d,
                        actividad,
                        hora
                    )

                    # Solo intentar las actividades
                    # que continúan pendientes.

                    if resultados.get(clave) not in PENDIENTES:
                        continue

                    r = reservar_actividad(
                        sesion,
                        sede_id,
                        d,
                        actividad,
                        hora,
                        slots,
                        args
                    )

                    if r == "SESION_VENCIDA":

                        avisar(
                            args.aviso,
                            "La sesión dejó de servir durante una reserva.",
                            "high"
                        )

                        return

                    resultados[clave] = r

            pend = pendientes_actuales()

    # ------------------------------------------------------------
    # Resumen
    # ------------------------------------------------------------

    log("--- Resumen ---")

    dias = [
        "lun",
        "mar",
        "mié",
        "jue",
        "vie",
        "sáb",
        "dom"
    ]

    lineas = []

    for (d, actividad, hora), r in resultados.items():

        log(
            f"{d:%a %d/%m} "
            f"{actividad} {hora}: {r}"
        )

        lineas.append(
            f"{dias[d.weekday()]} {d:%d/%m} "
            f"{actividad} {hora}: {r}"
        )

    bien = {
        "RESERVADO",
        "YA_RESERVADO",
        "PRUEBA_OK"
    }

    todo_ok = (
        bool(resultados)
        and all(
            r in bien
            for r in resultados.values()
        )
    )

    texto = (
        f"CrossFit: {args.actividad} {args.hora}\n"
        f"Cross Open: {args.actividad_open} {args.hora_open}\n"
        f"Viernes: {args.actividad_viernes} {args.hora}\n"
        f"Sede: {args.sede}\n"
        f"{'(PRUEBA)' if args.prueba else 'RESERVA REAL'}\n"
        + "\n".join(lineas)
    )

    if sesion.rotado:

        texto += (
            "\nOJO: el servidor cambió el refreshToken; "
            "hay que actualizar la sesión guardada."
        )

        log(
            "[!] El servidor cambió el refreshToken: "
            "hay que actualizar la sesión guardada."
        )

    avisar(
        args.aviso,
        texto,
        "default" if todo_ok else "high"
    )


if __name__ == "__main__":
    main()
