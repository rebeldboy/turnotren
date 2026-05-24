import csv
import io
import json
import re
import zipfile
import urllib.request
from urllib.parse import urljoin
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public"

GTFS_ZIP = DATA_DIR / "fomento_transit.zip"
JSON_SALIDA = PUBLIC_DIR / "turnotren.json"
REPORTE_SALIDA = PUBLIC_DIR / "turnotren_reporte.txt"
INDEX_HTML = PUBLIC_DIR / "index.html"

GTFS_URL = "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip"
URL_ALERTS = "https://gtfsrt.renfe.com/alerts.json"
URL_TRIP_UPDATES = "https://gtfsrt.renfe.com/trip_updates.json"
URL_VEHICLE_POSITIONS = "https://gtfsrt.renfe.com/vehicle_positions.json"

TZ = ZoneInfo("Europe/Madrid")
MARGEN_ENTRADA_TRABAJO_MINUTOS = 10

STOP_TOLOSA = "11500"
STOP_BILLABONA = "11503"
NOMBRE_TOLOSA = "Tolosa"
NOMBRE_BILLABONA = "Billabona-Zizurkil"

TURNOS_ENTRADA = {
    "manana": {"nombre": "MAÑANA", "hora": "06:15", "tipo": "entrada"},
    "tarde": {"nombre": "TARDE", "hora": "14:15", "tipo": "entrada"},
    "noche": {"nombre": "NOCHE", "hora": "22:15", "tipo": "entrada"},
}

# VUELTA TRABAJO -> CASA:
# Estos horarios son la SALIDA DEL TRABAJO tras cumplir 8 horas.
# Nunca se puede proponer un tren anterior en la parada de Billabona-Zizurkil.
TURNOS_VUELTA = {
    "manana": {"nombre": "MAÑANA", "hora": "14:15", "tipo": "salida"},
    "tarde": {"nombre": "TARDE", "hora": "22:15", "tipo": "salida"},
    "noche": {"nombre": "NOCHE", "hora": "06:15", "tipo": "salida"},
}


def ahora_madrid():
    return datetime.now(TZ)


def log(linea="", salida=None):
    print(linea)
    if salida is not None:
        salida.append(str(linea))


def limpiar_row(row):
    limpio = {}
    for k, v in row.items():
        kk = "" if k is None else str(k).strip()
        vv = "" if v is None else str(v).strip()
        limpio[kk] = vv
    return limpio


def leer_csv_zip(zf, nombre):
    raw = zf.read(nombre)
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [limpiar_row(r) for r in reader]


def iter_csv_zip(zf, nombre):
    raw = zf.read(nombre)
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        yield limpiar_row(r)


def descargar_json(url, timeout=20, intentos=3, salida=None, nombre="json"):
    ultimo_error = None

    for intento in range(1, intentos + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "TurnoTren/1.0",
                    "Accept": "application/json,text/plain,*/*",
                    "Cache-Control": "no-cache",
                }
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()

            if len(raw) < 2:
                raise RuntimeError(f"respuesta vacía ({len(raw)} bytes)")

            texto = raw.decode("utf-8", errors="replace").strip()

            # A veces Renfe devuelve un JSON truncado. Reintentamos antes de rendirnos.
            if not (texto.startswith("{") or texto.startswith("[")):
                raise RuntimeError(f"respuesta no parece JSON: {texto[:40]}")

            return json.loads(texto)

        except Exception as e:
            ultimo_error = e
            if salida is not None:
                log(f"  {nombre}: intento {intento}/{intentos} fallido: {type(e).__name__}: {e}", salida)

    raise ultimo_error


def descargar_json_seguro(url, nombre, salida, timeout=20, intentos=3):
    try:
        data = descargar_json(url, timeout=timeout, intentos=intentos, salida=salida, nombre=nombre)
        log(f"  {nombre}: OK · {len(data.get('entity', []))} entidades", salida)
        return data, True
    except Exception as e:
        log(f"  {nombre}: ERROR {type(e).__name__}: {e}", salida)
        return {"entity": [], "error": f"{type(e).__name__}: {e}"}, False


def descargar_gtfs(salida):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    try:
        log("Descargando GTFS estático de Renfe...", salida)
        req = urllib.request.Request(GTFS_URL, headers={"User-Agent": "TurnoTren/1.0"})
        with urllib.request.urlopen(req, timeout=90) as response:
            data = response.read()

        if len(data) < 100_000:
            raise RuntimeError("La descarga GTFS parece demasiado pequeña.")

        GTFS_ZIP.write_bytes(data)
        log(f"GTFS guardado: {GTFS_ZIP} · {len(data)} bytes", salida)
        return True

    except Exception as e:
        log(f"ERROR descargando GTFS: {type(e).__name__}: {e}", salida)
        return False


def parse_hora(hora):
    try:
        p = hora.split(":")
        h = int(p[0])
        m = int(p[1])
        s = int(p[2]) if len(p) > 2 else 0
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def hhmm(seg):
    if seg is None:
        return "—"
    seg = seg % (24 * 3600)
    return f"{seg // 3600:02d}:{(seg % 3600) // 60:02d}"


def segundos_a_datetime(fecha, segundos):
    dias_extra = segundos // (24 * 3600)
    seg_dia = segundos % (24 * 3600)
    return datetime.combine(fecha + timedelta(days=dias_extra), dtime(0, 0), TZ) + timedelta(seconds=seg_dia)


def iso_desde_timestamp(valor):
    if not valor:
        return None
    try:
        return datetime.fromtimestamp(int(valor), TZ).isoformat(timespec="seconds")
    except Exception:
        return None


def servicios_activos(calendar_rows, calendar_dates_rows, fecha):
    activos = set()
    fecha_yyyymmdd = fecha.strftime("%Y%m%d")
    dia = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][fecha.weekday()]

    for row in calendar_rows:
        service_id = row.get("service_id", "")
        if not service_id:
            continue

        if row.get(dia, "0") == "1" and row.get("start_date", "") <= fecha_yyyymmdd <= row.get("end_date", ""):
            activos.add(service_id)

    # Por si Renfe añade calendar_dates en el futuro.
    for row in calendar_dates_rows:
        if row.get("date") != fecha_yyyymmdd:
            continue

        service_id = row.get("service_id", "")
        exception = row.get("exception_type", "")
        if exception == "1":
            activos.add(service_id)
        elif exception == "2":
            activos.discard(service_id)

    return activos


def leer_trips_candidatos(trips_rows, servicios):
    trip_to_route = {}
    trip_to_service = {}

    for row in trips_rows:
        trip_id = row.get("trip_id", "")
        service_id = row.get("service_id", "")
        route_id = row.get("route_id", "")

        if not trip_id:
            continue

        if service_id in servicios:
            trip_to_route[trip_id] = route_id
            trip_to_service[trip_id] = service_id

    return trip_to_route, trip_to_service


def encontrar_directos_para_fecha(zf, origen_id, destino_id, trip_to_route, fecha_servicio):
    datos = {}

    for row in iter_csv_zip(zf, "stop_times.txt"):
        trip_id = row.get("trip_id", "")
        if trip_id not in trip_to_route:
            continue

        stop_id = row.get("stop_id", "")
        if stop_id != origen_id and stop_id != destino_id:
            continue

        try:
            seq = int(row.get("stop_sequence", ""))
        except Exception:
            continue

        arrival = row.get("arrival_time", "")
        departure = row.get("departure_time", "") or arrival

        d = datos.setdefault(trip_id, {})

        if stop_id == origen_id:
            d["origen_seq"] = seq
            d["origen_hora"] = departure

        if stop_id == destino_id:
            d["destino_seq"] = seq
            d["destino_hora"] = arrival

    directos = []

    for trip_id, d in datos.items():
        if "origen_seq" not in d or "destino_seq" not in d:
            continue
        if d["destino_seq"] <= d["origen_seq"]:
            continue

        salida_seg = parse_hora(d["origen_hora"])
        llegada_seg = parse_hora(d["destino_hora"])

        if salida_seg is None or llegada_seg is None:
            continue

        salida_dt = segundos_a_datetime(fecha_servicio, salida_seg)
        llegada_dt = segundos_a_datetime(fecha_servicio, llegada_seg)

        directos.append({
            "trip_id": trip_id,
            "route_id": trip_to_route.get(trip_id, ""),
            "fecha_servicio": fecha_servicio.isoformat(),
            "salida_programada": hhmm(salida_seg),
            "llegada_programada": hhmm(llegada_seg),
            "salida_programada_iso": salida_dt.isoformat(timespec="seconds"),
            "llegada_programada_iso": llegada_dt.isoformat(timespec="seconds"),
            "salida_seg": salida_seg,
            "llegada_seg": llegada_seg,
            "salida_dt": salida_dt,
            "llegada_dt": llegada_dt,
            "origen_stop_id": origen_id,
            "destino_stop_id": destino_id,
        })

    return sorted(directos, key=lambda x: x["salida_dt"])


def cargar_directos_dia(zf, trips_rows, calendar_rows, calendar_dates_rows, origen_id, destino_id, fecha_servicio):
    servicios = servicios_activos(calendar_rows, calendar_dates_rows, fecha_servicio)
    trip_to_route, _ = leer_trips_candidatos(trips_rows, servicios)

    directos = encontrar_directos_para_fecha(
        zf=zf,
        origen_id=origen_id,
        destino_id=destino_id,
        trip_to_route=trip_to_route,
        fecha_servicio=fecha_servicio,
    )

    return directos, len(servicios), len(trip_to_route)


def seleccionar_proximo(directos_hoy, directos_manana, ahora):
    candidatos = [t for t in directos_hoy if t["salida_dt"] >= ahora]
    if candidatos:
        return min(candidatos, key=lambda x: x["salida_dt"])

    if directos_manana:
        return min(directos_manana, key=lambda x: x["salida_dt"])

    return None


def seleccionar_tren_antes_de_hora(directos_hoy, directos_manana, hora_hhmm, ahora):
    """
    IDA CASA -> TRABAJO.

    PREMISA DEFINITIVA:
    - MAÑANA: entrada 06:15 -> llegada máxima a Billabona-Zizurkil 06:05.
    - TARDE:  entrada 14:15 -> llegada máxima a Billabona-Zizurkil 14:05.
    - NOCHE:  entrada 22:15 -> llegada máxima a Billabona-Zizurkil 22:05.

    Se elige SIEMPRE el último tren real Tolosa -> Billabona-Zizurkil
    cuya LLEGADA sea <= hora de entrada - 10 minutos.

    Si ese tren llega 30, 45 o 60 minutos antes, vale.
    Lo que NO vale es llegar demasiado justo o después de la hora máxima.
    """
    h, m = map(int, hora_hhmm.split(":"))
    entrada_hoy = datetime.combine(ahora.date(), dtime(h, m), TZ)
    entrada = entrada_hoy if entrada_hoy > ahora else entrada_hoy + timedelta(days=1)
    llegada_maxima = entrada - timedelta(minutes=MARGEN_ENTRADA_TRABAJO_MINUTOS)

    candidatos = []
    for t in directos_hoy + directos_manana:
        if t["llegada_dt"] <= llegada_maxima:
            candidatos.append(t)

    if not candidatos:
        return None

    elegido = max(candidatos, key=lambda x: x["llegada_dt"])

    # Doble seguro: jamás llega después del margen máximo.
    if elegido["llegada_dt"] > llegada_maxima:
        return None

    elegido = dict(elegido)
    elegido["objetivo_turno"] = entrada.isoformat(timespec="seconds")
    elegido["hora_turno"] = hora_hhmm
    elegido["margen_llegada_minutos"] = MARGEN_ENTRADA_TRABAJO_MINUTOS
    elegido["llegada_maxima_turno"] = llegada_maxima.isoformat(timespec="seconds")
    elegido["criterio_turno"] = f"último tren que llega >= {MARGEN_ENTRADA_TRABAJO_MINUTOS} min antes de la entrada"
    elegido["regla_seguridad_entrada"] = "llegada_dt <= entrada - 10 min"
    return elegido


def seleccionar_tren_despues_de_hora(directos_hoy, directos_manana, hora_hhmm, ahora):
    """
    VUELTA TRABAJO -> CASA.

    PREMISA DEFINITIVA:
    - MAÑANA: salida del trabajo 14:15 -> primer tren desde Billabona-Zizurkil >= 14:15.
    - TARDE:  salida del trabajo 22:15 -> primer tren desde Billabona-Zizurkil >= 22:15.
    - NOCHE:  salida del trabajo 06:15 -> primer tren desde Billabona-Zizurkil >= 06:15 del día correcto.

    Nunca se propone un tren anterior a la salida del trabajo.
    Valen Cercanías, Regional, Media Distancia o Largo Recorrido si el GTFS de Renfe
    indica que paran en Billabona-Zizurkil y después en Tolosa.
    """
    h, m = map(int, hora_hhmm.split(":"))
    objetivo_hoy = datetime.combine(ahora.date(), dtime(h, m), TZ)
    objetivo = objetivo_hoy if objetivo_hoy > ahora else objetivo_hoy + timedelta(days=1)

    candidatos = []
    for t in directos_hoy + directos_manana:
        if t["salida_dt"] >= objetivo:
            candidatos.append(t)

    if not candidatos:
        return None

    elegido = min(candidatos, key=lambda x: x["salida_dt"])

    # Doble seguro: jamás sale antes de la hora de salida del trabajo.
    if elegido["salida_dt"] < objetivo:
        return None

    elegido = dict(elegido)
    elegido["objetivo_turno"] = objetivo.isoformat(timespec="seconds")
    elegido["hora_turno"] = hora_hhmm
    elegido["criterio_turno"] = f"primer tren con salida >= {hora_hhmm}"
    elegido["regla_seguridad_salida"] = "salida_dt >= hora_salida_trabajo"
    return elegido



def numero_tren_desde_texto(texto):
    """
    Extrae el número comercial del tren para poder emparejar:
    - trip_id GTFS: 6141D32775C1
    - vehicle label: C1-32775-PLATF.(1)
    - entity id: VP_C1-32775
    """
    if texto is None:
        return ""

    s = str(texto).upper()

    # C1-32775, C1_32775, etc.
    m = re.search(r"C\d+\D+(\d{4,6})", s)
    if m:
        return m.group(1)

    # 6141D32775C1 / 6141S32775C1
    m = re.search(r"[A-Z](\d{4,6})C\d", s)
    if m:
        return m.group(1)

    # Último recurso: números de 4-6 cifras.
    nums = re.findall(r"\d{4,6}", s)
    if nums:
        # Preferimos el último porque en trip_id suele ser el número útil.
        return nums[-1]

    return ""


def entidad_numero_tren(ent):
    veh = ent.get("vehicle", {})
    trip = veh.get("trip", {})
    vehicle_info = veh.get("vehicle", {})

    candidatos = [
        ent.get("id", ""),
        trip.get("tripId", ""),
        vehicle_info.get("id", ""),
        vehicle_info.get("label", ""),
    ]

    for c in candidatos:
        n = numero_tren_desde_texto(c)
        if n:
            return n

    return ""


def leer_recorrido_trip(zf, stops_map, trip_id):
    """
    Devuelve todas las paradas reales del viaje según stop_times.txt.
    No inventa una línea fija: sale del GTFS de Renfe.
    """
    filas = []

    for row in iter_csv_zip(zf, "stop_times.txt"):
        if row.get("trip_id", "") != trip_id:
            continue

        try:
            seq = int(row.get("stop_sequence", ""))
        except Exception:
            continue

        stop_id = row.get("stop_id", "")
        arr = row.get("arrival_time", "")
        dep = row.get("departure_time", "") or arr

        filas.append({
            "stop_sequence": seq,
            "stop_id": stop_id,
            "stop_name": stops_map.get(stop_id, stop_id),
            "arrival_time": hhmm(parse_hora(arr)),
            "departure_time": hhmm(parse_hora(dep)),
        })

    return sorted(filas, key=lambda x: x["stop_sequence"])



def leer_recorridos_trips(zf, stops_map, trip_ids):
    """
    Lee stop_times.txt una sola vez para todos los trip_id que necesita la app.
    Evita mezclar recorridos de un tren con otro.
    """
    trip_ids = set(str(x) for x in trip_ids if x)
    recorridos = {tid: [] for tid in trip_ids}

    if not trip_ids:
        return recorridos

    for row in iter_csv_zip(zf, "stop_times.txt"):
        tid = row.get("trip_id", "")
        if tid not in trip_ids:
            continue

        try:
            seq = int(row.get("stop_sequence", ""))
        except Exception:
            continue

        stop_id = row.get("stop_id", "")
        arr = row.get("arrival_time", "")
        dep = row.get("departure_time", "") or arr

        recorridos[tid].append({
            "stop_sequence": seq,
            "stop_id": stop_id,
            "stop_name": stops_map.get(stop_id, stop_id),
            "arrival_time": hhmm(parse_hora(arr)),
            "departure_time": hhmm(parse_hora(dep)),
        })

    for tid in list(recorridos.keys()):
        recorridos[tid] = sorted(recorridos[tid], key=lambda x: x["stop_sequence"])

    return recorridos



URL_RENFE_FLOTA_BASES = [
    "https://grt-nginx-visor-publico.desa.sir.renfe.es/renfe-visor/flota.json?v=",
    "https://grt-nginx-visor-publico.desa.sir.renfe.es/renfe-visor/flota.json?",
    "https://grt-nginx-visor-publico.desa.sir.renfe.es/renfe-visor/flota.json",
    "https://tiempo-real.renfe.com/renfe-visor/flota.json?v=",
    "https://tiempo-real.renfe.com/renfe-visor/flota.json?",
    "https://tiempo-real.renfe.com/renfe-visor/flota.json",
]


def descargar_flota_renfe(salida):
    """
    Fuente que usa el visor web de Renfe Cercanías.
    El JS del visor llama a urlTrenes + flota.json + timestamp.
    """
    unix = str(int(time.time() * 1000))
    errores = []

    for base in URL_RENFE_FLOTA_BASES:
        url = base + unix if base.endswith(("?v=", "?")) else base
        try:
            data = descargar_json(url, timeout=20, intentos=3, salida=salida, nombre="renfe_flota")
            trenes = extraer_lista_trenes_flota(data)
            log(f"  renfe_flota: OK · {len(trenes)} trenes · {url}", salida)
            return data, True, url
        except Exception as e:
            errores.append(f"{url} -> {type(e).__name__}: {e}")

    log("  renfe_flota: ERROR · no se ha podido leer flota.json", salida)
    for e in errores[:6]:
        log(f"    {e}", salida)

    return {"trenes": [], "errores": errores}, False, ""


def extraer_lista_trenes_flota(data):
    if isinstance(data, dict):
        for key in ("trenes", "features", "data", "items"):
            val = data.get(key)
            if isinstance(val, list):
                return val

    if isinstance(data, list):
        return data

    return []


def get_any(d, *names, default=""):
    if not isinstance(d, dict):
        return default

    # Directo
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]

    # Case-insensitive
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(str(n).lower())
        if v not in (None, ""):
            return v

    # GeoJSON properties
    props = d.get("properties")
    if isinstance(props, dict):
        return get_any(props, *names, default=default)

    return default


def flota_numero_tren(item):
    candidatos = [
        get_any(item, "CODTREN", "codTren", "codtren", "numeroTren", "numero_tren", "trainNumber"),
        get_any(item, "tripId", "TRIPID", "trip_id"),
        get_any(item, "id", "ID"),
    ]
    for c in candidatos:
        n = numero_tren_desde_texto(c)
        if n:
            return n
    return ""


def coordenadas_flota(item):
    # GeoJSON
    geom = item.get("geometry") if isinstance(item, dict) else None
    if isinstance(geom, dict):
        coords = geom.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            try:
                return float(coords[1]), float(coords[0])
            except Exception:
                pass

    lat = get_any(item, "lat", "LAT", "latitud", "LATITUD", "latitude", "Latitude")
    lon = get_any(item, "lon", "LON", "lng", "longitud", "LONGITUD", "longitude", "Longitude")
    try:
        if lat not in ("", None) and lon not in ("", None):
            return float(lat), float(lon)
    except Exception:
        pass

    return None, None


def normalizar_item_flota(item):
    lat, lon = coordenadas_flota(item)
    numero = flota_numero_tren(item)

    return {
        "encontrado": True,
        "fuente": "renfe_flota",
        "numero_tren": numero,
        "trip_id": str(get_any(item, "tripId", "TRIPID", "trip_id", default="")),
        "cod_tren": str(get_any(item, "CODTREN", "codTren", "codtren", default=numero)),
        "linea": str(get_any(item, "LINEA", "linea", "line", default="")),
        "origen": str(get_any(item, "ORIGEN", "origen", default="")),
        "destino": str(get_any(item, "DESTINO", "destino", default="")),
        "parada_actual": str(get_any(item, "PARADAACTUAL", "paradaActual", "parada_actual", "actual", default="")),
        "parada_anterior": str(get_any(item, "PARADAANTERIOR", "paradaAnterior", "parada_anterior", "anterior", default="")),
        "parada_siguiente": str(get_any(item, "PARADASIGUIENTE", "paradaSiguiente", "parada_siguiente", "siguiente", default="")),
        "via_actual": str(get_any(item, "VIAACTUAL", "viaActual", "via_actual", default="")),
        "via_anterior": str(get_any(item, "VIAACTUALANT", "viaActualAnt", "via_anterior", default="")),
        "via_siguiente": str(get_any(item, "VIASIGUIENTE", "viaSiguiente", "via_siguiente", default="")),
        "retraso": str(get_any(item, "RETRASO", "retraso", "delay", "variacion", default="")),
        "estado": str(get_any(item, "ESTADO", "estado", "status", default="")),
        "lat": lat,
        "lon": lon,
        "raw_keys": list(item.keys()) if isinstance(item, dict) else [],
    }


def buscar_seguimiento_flota(flota_json, trip_id):
    numero_objetivo = numero_tren_desde_texto(trip_id)
    candidatos = []

    for item in extraer_lista_trenes_flota(flota_json):
        numero = flota_numero_tren(item)
        if numero:
            candidatos.append({
                "numero_tren": numero,
                "trip_id": str(get_any(item, "tripId", "TRIPID", "trip_id", default="")),
                "origen": str(get_any(item, "ORIGEN", "origen", default="")),
                "destino": str(get_any(item, "DESTINO", "destino", default="")),
                "parada_actual": str(get_any(item, "PARADAACTUAL", "paradaActual", default="")),
                "parada_siguiente": str(get_any(item, "PARADASIGUIENTE", "paradaSiguiente", default="")),
            })

        if numero and numero == numero_objetivo:
            seg = normalizar_item_flota(item)
            seg["match"] = "numero_tren"
            seg["trip_id_objetivo"] = trip_id
            return seg

        trip_flota = str(get_any(item, "tripId", "TRIPID", "trip_id", default=""))
        if trip_flota and trip_flota == str(trip_id):
            seg = normalizar_item_flota(item)
            seg["match"] = "trip_id"
            seg["trip_id_objetivo"] = trip_id
            return seg

    return {
        "encontrado": False,
        "fuente": "renfe_flota",
        "trip_id_objetivo": trip_id,
        "numero_objetivo": numero_objetivo,
        "motivo": "No aparece en flota.json del visor Renfe",
        "debug_candidatos": candidatos[:40],
    }


def aplicar_extras_tren(tren_json, recorrido, vehicle_positions_json, flota_json=None):
    """
    Añade datos específicos del tren seleccionado:
    - origen real del tren completo
    - destino final real del tren completo
    - recorrido completo de ESE trip_id
    - posición real solo de ESE trip_id
    """
    if tren_json is None:
        return None

    rec = recorrido or []
    tren_json = dict(tren_json)

    tren_json["origen_tren"] = rec[0]["stop_name"] if rec else ""
    tren_json["destino_final_tren"] = rec[-1]["stop_name"] if rec else ""
    tren_json["recorrido_completo"] = rec
    tren_json["numero_tren"] = numero_tren_desde_texto(tren_json.get("trip_id", ""))
    tren_json["vehicle_position"] = buscar_vehicle_position(
        vehicle_positions_json,
        tren_json.get("trip_id", ""),
        [p.get("stop_id", "") for p in rec]
    )
    tren_json["seguimiento_real"] = buscar_seguimiento_flota(flota_json or {}, tren_json.get("trip_id", ""))

    return tren_json


def buscar_trip_update(trip_updates_json, trip_id, origen_stop_id, destino_stop_id):
    for ent in trip_updates_json.get("entity", []):
        tu = ent.get("tripUpdate", {})
        trip = tu.get("trip", {})
        if trip.get("tripId") != trip_id:
            continue

        resultado = {
            "encontrado": True,
            "delay_general_segundos": tu.get("delay"),
            "schedule_relationship": trip.get("scheduleRelationship"),
            "origen": None,
            "destino": None,
            "raw_id": ent.get("id", ""),
        }

        for stu in tu.get("stopTimeUpdate", []):
            sid = str(stu.get("stopId", ""))

            if sid == str(origen_stop_id):
                resultado["origen"] = {
                    "arrival_delay": stu.get("arrival", {}).get("delay"),
                    "arrival_time": iso_desde_timestamp(stu.get("arrival", {}).get("time")),
                    "departure_delay": stu.get("departure", {}).get("delay"),
                    "departure_time": iso_desde_timestamp(stu.get("departure", {}).get("time")),
                    "schedule_relationship": stu.get("scheduleRelationship"),
                }

            if sid == str(destino_stop_id):
                resultado["destino"] = {
                    "arrival_delay": stu.get("arrival", {}).get("delay"),
                    "arrival_time": iso_desde_timestamp(stu.get("arrival", {}).get("time")),
                    "departure_delay": stu.get("departure", {}).get("delay"),
                    "departure_time": iso_desde_timestamp(stu.get("departure", {}).get("time")),
                    "schedule_relationship": stu.get("scheduleRelationship"),
                }

        return resultado

    return {"encontrado": False}


def buscar_vehicle_position(vehicle_positions_json, trip_id, recorrido_stop_ids=None):
    """
    Busca posición real del tren.

    Cambios v1.7:
    - Coincidencia exacta por trip_id si Renfe la publica.
    - Si no hay exacta, coincidencia por número comercial del tren.
      Ejemplo: 6141D32715C1 -> 32715 y C1-32715-PLATF.(1) -> 32715.
    - Ya NO bloquea la posición si el stop_id no cruza con stop_times, porque
      Renfe/Adif puede publicar una variante de stop_id o el vehículo puede venir
      con stop_id de plataforma.
    - Incluye debug para ver por qué se ha aceptado o no la posición.
    """
    recorrido_stop_ids = set(str(x) for x in (recorrido_stop_ids or []) if x)
    trip_id = str(trip_id or "")
    numero_objetivo = numero_tren_desde_texto(trip_id)

    candidatos_debug = []

    def construir(ent, modo_match):
        veh = ent.get("vehicle", {})
        trip = veh.get("trip", {})
        pos = veh.get("position", {})
        vehicle_info = veh.get("vehicle", {})
        stop_id = str(veh.get("stopId", "") or "")
        numero_entidad = entidad_numero_tren(ent)

        return {
            "encontrado": True,
            "match": modo_match,
            "raw_id": ent.get("id", ""),
            "trip_id": trip.get("tripId", ""),
            "trip_id_objetivo": trip_id,
            "numero_tren": numero_entidad or numero_objetivo,
            "numero_objetivo": numero_objetivo,
            "stop_in_recorrido": (stop_id in recorrido_stop_ids) if stop_id else False,
            "lat": pos.get("latitude"),
            "lon": pos.get("longitude"),
            "bearing": pos.get("bearing"),
            "speed": pos.get("speed"),
            "current_status": veh.get("currentStatus"),
            "timestamp": iso_desde_timestamp(veh.get("timestamp")),
            "stop_id": veh.get("stopId"),
            "vehicle_id": vehicle_info.get("id"),
            "label": vehicle_info.get("label"),
        }

    # 1) Coincidencia exacta por trip_id.
    for ent in vehicle_positions_json.get("entity", []):
        veh = ent.get("vehicle", {})
        trip = veh.get("trip", {})
        if str(trip.get("tripId", "")) == trip_id:
            return construir(ent, "trip_id_exacto")

    # 2) Coincidencia por número de tren.
    if numero_objetivo:
        for ent in vehicle_positions_json.get("entity", []):
            veh = ent.get("vehicle", {})
            vehicle_info = veh.get("vehicle", {})
            stop_id = str(veh.get("stopId", "") or "")
            numero_entidad = entidad_numero_tren(ent)

            if numero_entidad:
                candidatos_debug.append({
                    "id": ent.get("id", ""),
                    "trip_id": veh.get("trip", {}).get("tripId", ""),
                    "numero": numero_entidad,
                    "label": vehicle_info.get("label", ""),
                    "stop_id": stop_id,
                    "stop_in_recorrido": (stop_id in recorrido_stop_ids) if stop_id else False,
                })

            if numero_entidad == numero_objetivo:
                return construir(ent, "numero_tren")

    return {
        "encontrado": False,
        "trip_id_objetivo": trip_id,
        "numero_objetivo": numero_objetivo,
        "motivo": "Renfe no publica vehicle_position coincidente para este tren",
        "debug_muestras": candidatos_debug[:12],
    }

def extraer_texto_alerta(alert):
    desc = alert.get("descriptionText", {})
    translations = desc.get("translation", [])
    textos = [t.get("text", "").strip() for t in translations if t.get("text")]
    return "\n".join(textos).strip()


def alertas_para_route(alerts_json, route_id):
    encontradas = []

    for ent in alerts_json.get("entity", []):
        alert = ent.get("alert", {})
        informed = alert.get("informedEntity", [])

        afecta = any(item.get("routeId") == route_id for item in informed)

        if afecta:
            txt = extraer_texto_alerta(alert)
            if txt:
                encontradas.append({
                    "id": ent.get("id", ""),
                    "texto": txt
                })

    return encontradas


def aplicar_tiempo_real(tren, trip_update):
    if tren is None:
        return None

    salida_real = tren["salida_programada"]
    llegada_real = tren["llegada_programada"]
    variacion_minutos = 0
    estado = "PROGRAMADO"
    tiempo_real_validado = False

    if trip_update.get("encontrado"):
        tiempo_real_validado = True

        delay = trip_update.get("delay_general_segundos")
        origen = trip_update.get("origen") or {}
        destino = trip_update.get("destino") or {}

        usado = origen.get("departure_delay")
        if usado is None:
            usado = delay
        if usado is None:
            usado = destino.get("arrival_delay")

        if usado is not None:
            try:
                usado = int(usado)
                variacion_minutos = round(usado / 60)
                salida_real = hhmm(tren["salida_seg"] + usado)
                llegada_real = hhmm(tren["llegada_seg"] + usado)
                estado = "RETRASADO" if variacion_minutos > 0 else ("ADELANTADO" if variacion_minutos < 0 else "PUNTUAL")
            except Exception:
                pass

    espera = max(0, round((tren["salida_dt"] - ahora_madrid()).total_seconds() / 60))

    return {
        "trip_id": tren["trip_id"],
        "route_id": tren["route_id"],
        "fecha_servicio": tren["fecha_servicio"],
        "salida_programada": tren["salida_programada"],
        "llegada_programada": tren["llegada_programada"],
        "salida_programada_iso": tren["salida_programada_iso"],
        "llegada_programada_iso": tren["llegada_programada_iso"],
        "salida_real": salida_real,
        "llegada_real": llegada_real,
        "variacion_minutos": variacion_minutos,
        "estado": estado,
        "tiempo_real_validado": tiempo_real_validado,
        "espera_minutos": espera,
        "origen_stop_id": tren["origen_stop_id"],
        "destino_stop_id": tren["destino_stop_id"],
        "entrada_turno": tren.get("entrada_turno"),
        "objetivo_turno": tren.get("objetivo_turno"),
        "hora_turno": tren.get("hora_turno"),
        "margen_llegada_minutos": tren.get("margen_llegada_minutos"),
        "llegada_maxima_turno": tren.get("llegada_maxima_turno"),
        "regla_seguridad_salida": tren.get("regla_seguridad_salida"),
        "regla_seguridad_entrada": tren.get("regla_seguridad_entrada"),
    }


def limpiar_tren_lista(t):
    return {
        "trip_id": t["trip_id"],
        "route_id": t["route_id"],
        "fecha_servicio": t["fecha_servicio"],
        "salida_programada": t["salida_programada"],
        "llegada_programada": t["llegada_programada"],
        "salida_programada_iso": t["salida_programada_iso"],
        "llegada_programada_iso": t["llegada_programada_iso"],
        "origen_stop_id": t["origen_stop_id"],
        "destino_stop_id": t["destino_stop_id"],
    }



def validar_tren_turno_final(clave_ruta, info_turno, tren):
    """
    Verificación final antes de escribir el JSON.

    CASA -> TRABAJO:
      llegada_programada_iso <= entrada - 10 min

    TRABAJO -> CASA:
      salida_programada_iso >= salida trabajo
    """
    if tren is None:
        return False, "SIN_TREN"

    objetivo_txt = tren.get("objetivo_turno")
    if not objetivo_txt:
        return False, "SIN_OBJETIVO_TURNO"

    objetivo = datetime.fromisoformat(objetivo_txt)

    if clave_ruta == "casa_trabajo":
        llegada = tren.get("llegada_dt")
        llegada_maxima_txt = tren.get("llegada_maxima_turno")
        llegada_maxima = datetime.fromisoformat(llegada_maxima_txt) if llegada_maxima_txt else objetivo - timedelta(minutes=MARGEN_ENTRADA_TRABAJO_MINUTOS)

        if llegada is None:
            return False, "SIN_LLEGADA"

        if llegada <= llegada_maxima:
            return True, f"OK llegada {llegada.strftime('%H:%M')} <= max {llegada_maxima.strftime('%H:%M')}"

        return False, f"ERROR llegada {llegada.strftime('%H:%M')} > max {llegada_maxima.strftime('%H:%M')}"

    if clave_ruta == "trabajo_casa":
        salida = tren.get("salida_dt")

        if salida is None:
            return False, "SIN_SALIDA"

        if salida >= objetivo:
            return True, f"OK salida {salida.strftime('%H:%M')} >= min {objetivo.strftime('%H:%M')}"

        return False, f"ERROR salida {salida.strftime('%H:%M')} < min {objetivo.strftime('%H:%M')}"

    return False, "RUTA_DESCONOCIDA"


def quitar_campos_datetime(d):
    limpio = dict(d)
    limpio.pop("salida_dt", None)
    limpio.pop("llegada_dt", None)
    limpio.pop("salida_seg", None)
    limpio.pop("llegada_seg", None)
    return limpio


def preparar_ruta(zf, stops_map, trips_rows, calendar_rows, calendar_dates_rows, alerts, trip_updates, vehicle_positions, flota_renfe, clave, nombre, origen_id, destino_id, origen_nombre, destino_nombre, ahora, salida):
    hoy = ahora.date()
    manana = hoy + timedelta(days=1)

    directos_hoy, servicios_hoy, trips_hoy = cargar_directos_dia(
        zf, trips_rows, calendar_rows, calendar_dates_rows, origen_id, destino_id, hoy
    )
    directos_manana, servicios_manana, trips_manana = cargar_directos_dia(
        zf, trips_rows, calendar_rows, calendar_dates_rows, origen_id, destino_id, manana
    )

    proximo = seleccionar_proximo(directos_hoy, directos_manana, ahora)

    log(f"Ruta {nombre}", salida)
    log(f"  Directos hoy: {len(directos_hoy)} · mañana: {len(directos_manana)}", salida)

    turnos = {}
    turnos_cfg = TURNOS_ENTRADA if clave == "casa_trabajo" else TURNOS_VUELTA

    for clave_turno, info in turnos_cfg.items():
        if clave == "casa_trabajo":
            tren_turno = seleccionar_tren_antes_de_hora(directos_hoy, directos_manana, info["hora"], ahora)
        else:
            tren_turno = seleccionar_tren_despues_de_hora(directos_hoy, directos_manana, info["hora"], ahora)

        valido, detalle_validacion = validar_tren_turno_final(clave, info, tren_turno)

        if tren_turno and valido:
            tu_turno = buscar_trip_update(trip_updates, tren_turno["trip_id"], origen_id, destino_id)
            tren_aplicado = aplicar_tiempo_real(tren_turno, tu_turno)

            log(
                f"  VERIFICADO {clave} / {info['nombre']} ({info['tipo']} {info['hora']}): "
                f"{tren_aplicado['salida_programada']} -> {tren_aplicado['llegada_programada']} · "
                f"{tren_aplicado['trip_id']} · {detalle_validacion}",
                salida
            )

            turnos[clave_turno] = {
                "nombre": info["nombre"],
                "tipo": info["tipo"],
                "hora": info["hora"],
                "criterio": tren_turno.get("criterio_turno", ""),
                "margen_llegada_minutos": tren_turno.get("margen_llegada_minutos"),
                "llegada_maxima_turno": tren_turno.get("llegada_maxima_turno"),
                "verificacion": {
                    "ok": True,
                    "detalle": detalle_validacion,
                    "ruta": clave,
                    "turno": clave_turno,
                    "hora_referencia": info["hora"],
                },
                "tren": tren_aplicado,
            }
        else:
            log(
                f"  BLOQUEADO {clave} / {info['nombre']} ({info['tipo']} {info['hora']}): "
                f"{detalle_validacion}",
                salida
            )

            turnos[clave_turno] = {
                "nombre": info["nombre"],
                "tipo": info["tipo"],
                "hora": info["hora"],
                "criterio": "sin tren real que cumpla las premisas",
                "margen_llegada_minutos": MARGEN_ENTRADA_TRABAJO_MINUTOS if clave == "casa_trabajo" else None,
                "llegada_maxima_turno": tren_turno.get("llegada_maxima_turno") if tren_turno else None,
                "verificacion": {
                    "ok": False,
                    "detalle": detalle_validacion,
                    "ruta": clave,
                    "turno": clave_turno,
                    "hora_referencia": info["hora"],
                },
                "tren": None,
            }

    if proximo:
        log(f"  Próximo AHORA: {proximo['salida_programada']} → {proximo['llegada_programada']} · {proximo['trip_id']}", salida)
        tu = buscar_trip_update(trip_updates, proximo["trip_id"], origen_id, destino_id)
        vp = buscar_vehicle_position(vehicle_positions, proximo["trip_id"])
        al = alertas_para_route(alerts, proximo["route_id"])
        tren_final = aplicar_tiempo_real(proximo, tu)

        estado = "OK"
        mensaje = "Horario programado real obtenido del GTFS."
        if tu.get("encontrado"):
            mensaje += " Tiempo real aplicado mediante trip_updates."
        else:
            mensaje += " Sin actualización trip_updates para este trip_id ahora mismo."
    else:
        log("  No se ha encontrado próximo tren.", salida)
        tu = {"encontrado": False}
        vp = {"encontrado": False}
        al = []
        tren_final = None
        estado = "SIN_DATO_REAL"
        mensaje = "No se ha encontrado tren directo real en GTFS."

    primeros = [t for t in directos_hoy if t["salida_dt"] >= ahora][:40]
    if len(primeros) < 40:
        primeros += directos_manana[:40 - len(primeros)]

    trip_ids_necesarios = set()
    if proximo:
        trip_ids_necesarios.add(proximo["trip_id"])

    for t in primeros:
        trip_ids_necesarios.add(t["trip_id"])

    for info_turno in turnos.values():
        tr = info_turno.get("tren")
        if tr:
            trip_ids_necesarios.add(tr.get("trip_id", ""))

    recorridos_map = leer_recorridos_trips(zf, stops_map, trip_ids_necesarios)

    if tren_final:
        tren_final = aplicar_extras_tren(
            tren_final,
            recorridos_map.get(tren_final.get("trip_id", ""), []),
            vehicle_positions,
            flota_renfe
        )

    for clave_turno, info_turno in turnos.items():
        tr = info_turno.get("tren")
        if tr:
            info_turno["tren"] = aplicar_extras_tren(
                tr,
                recorridos_map.get(tr.get("trip_id", ""), []),
                vehicle_positions,
                flota_renfe
            )

    primeros_json = []
    for t in primeros:
        tu_p = buscar_trip_update(trip_updates, t["trip_id"], origen_id, destino_id)
        tj = aplicar_tiempo_real(t, tu_p)
        tj = aplicar_extras_tren(tj, recorridos_map.get(t["trip_id"], []), vehicle_positions, flota_renfe)
        primeros_json.append(tj)

    recorrido_completo = tren_final.get("recorrido_completo", []) if tren_final else []

    return {
        "clave": clave,
        "nombre": nombre,
        "estado": estado,
        "mensaje": mensaje,
        "origen": origen_nombre,
        "destino": destino_nombre,
        "origen_stop_id": origen_id,
        "destino_stop_id": destino_id,
        "directos_hoy": len(directos_hoy),
        "directos_manana": len(directos_manana),
        "servicios_hoy": servicios_hoy,
        "servicios_manana": servicios_manana,
        "trips_hoy": trips_hoy,
        "trips_manana": trips_manana,
        "proximo_tren": tren_final,
        "turnos": turnos,
        "verificacion_reglas": {
            "casa_trabajo": "último tren con llegada <= entrada - 10 min",
            "trabajo_casa": "primer tren con salida >= fin de turno",
            "turnos_entrada": TURNOS_ENTRADA if clave == "casa_trabajo" else None,
            "turnos_salida": TURNOS_VUELTA if clave == "trabajo_casa" else None,
        },
        "trip_update": tu,
        "vehicle_position": vp,
        "alertas": al,
        "recorrido_completo": recorrido_completo,
        "primeros_directos": primeros_json,
    }


def escribir_index():
    INDEX_HTML.write_text("""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>TurnoTren</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; background:#07111A; color:white; padding:24px; }
    a { color:#8ED1F5; }
    code { background:#101A26; padding:3px 6px; border-radius:6px; }
  </style>
</head>
<body>
  <h1>TurnoTren</h1>
  <p>Archivo JSON público para la app personal TurnoTren.</p>
  <p><a href="turnotren.json">Abrir turnotren.json</a></p>
  <p><a href="turnotren_reporte.txt">Abrir reporte</a></p>
</body>
</html>
""", encoding="utf-8")



def simplificar_vehicle_positions(vehicle_positions_json):
    simplificados = []

    for ent in vehicle_positions_json.get("entity", []):
        veh = ent.get("vehicle", {})
        trip = veh.get("trip", {})
        pos = veh.get("position", {})
        vehicle_info = veh.get("vehicle", {})

        simplificados.append({
            "id": ent.get("id", ""),
            "trip_id": trip.get("tripId", ""),
            "route_id": trip.get("routeId", ""),
            "numero_desde_id": numero_tren_desde_texto(ent.get("id", "")),
            "numero_desde_trip": numero_tren_desde_texto(trip.get("tripId", "")),
            "numero_desde_vehicle_id": numero_tren_desde_texto(vehicle_info.get("id", "")),
            "numero_desde_label": numero_tren_desde_texto(vehicle_info.get("label", "")),
            "numero_detectado": entidad_numero_tren(ent),
            "stop_id": veh.get("stopId", ""),
            "current_status": veh.get("currentStatus", ""),
            "timestamp": iso_desde_timestamp(veh.get("timestamp")),
            "vehicle_id": vehicle_info.get("id", ""),
            "label": vehicle_info.get("label", ""),
            "lat": pos.get("latitude"),
            "lon": pos.get("longitude"),
            "raw_trip": trip,
        })

    return simplificados


def diagnosticar_trenes_objetivo(trenes, vehicle_positions_json):
    vehicles = simplificar_vehicle_positions(vehicle_positions_json)
    salida = []

    for tren in trenes:
        if not tren:
            continue

        trip_id = tren.get("trip_id", "")
        numero = tren.get("numero_tren") or numero_tren_desde_texto(trip_id)

        exactos = [v for v in vehicles if v.get("trip_id") == trip_id]
        por_numero = [v for v in vehicles if v.get("numero_detectado") == numero and numero]

        cercanos = []
        try:
            n = int(numero)
            for v in vehicles:
                vn = v.get("numero_detectado", "")
                if vn and vn.isdigit() and abs(int(vn) - n) <= 8:
                    cercanos.append(v)
        except Exception:
            pass

        salida.append({
            "trip_id": trip_id,
            "numero_tren": numero,
            "salida": tren.get("salida_programada") or tren.get("salida_real", ""),
            "llegada": tren.get("llegada_programada") or tren.get("llegada_real", ""),
            "origen_tren": tren.get("origen_tren", ""),
            "destino_final_tren": tren.get("destino_final_tren", ""),
            "vehicle_position_actual": tren.get("vehicle_position", {}),
            "matches_trip_id": exactos[:10],
            "matches_numero": por_numero[:10],
            "matches_cercanos_numero": cercanos[:20],
        })

    return salida


def descubrir_endpoints_tiempo_real(salida):
    base_url = "https://tiempo-real.renfe.com/"
    resultado = {
        "base_url": base_url,
        "ok": False,
        "scripts": [],
        "hints": [],
        "snippets": [],
        "ajax_calls": [],
        "error": "",
    }

    claves = [
        "CODTREN", "PARADAACTUAL", "PARADAANTERIOR", "PARADASIGUIENTE",
        "VIAACTUAL", "VIASIGUIENTE", "RETRASO", "CODESTACION",
        "NOMBREESTACION", "CIRCUL", "circul", "tren", "Tren",
        "salidas", "llegadas", "Get", "get", "ajax", "$.ajax", ".post", ".get",
        "url:", "service", "Service", "estacion", "Estacion"
    ]

    def extraer_snippets(nombre, texto):
        encontrados = []
        for clave in claves:
            for m in re.finditer(re.escape(clave), texto):
                ini = max(0, m.start() - 260)
                fin = min(len(texto), m.end() + 260)
                snip = texto[ini:fin]
                snip = re.sub(r"\s+", " ", snip).strip()
                encontrados.append({
                    "source": nombre,
                    "clave": clave,
                    "snippet": snip
                })
                if len(encontrados) > 500:
                    return encontrados
        return encontrados

    try:
        req = urllib.request.Request(base_url, headers={"User-Agent": "TurnoTren/diagnostico"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")

        resultado["ok"] = True
        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
        scripts = [urljoin(base_url, s) for s in scripts]
        resultado["scripts"] = scripts[:100]

        textos = [{"source": "index.html", "url": base_url, "text": html}]

        for js_url in scripts[:40]:
            try:
                req_js = urllib.request.Request(js_url, headers={"User-Agent": "TurnoTren/diagnostico"})
                with urllib.request.urlopen(req_js, timeout=20) as rj:
                    js = rj.read().decode("utf-8", errors="replace")
                textos.append({"source": js_url.split("/")[-1], "url": js_url, "text": js})
            except Exception as e:
                resultado["hints"].append({"script_error": js_url, "error": f"{type(e).__name__}: {e}"})

        # Hints generales: URLs/rutas sospechosas.
        patrones = [
            r'https?://[^"\'\\\s]+',
            r'["\']([^"\']*(?:api|Api|servicio|Servicio|tren|Tren|cercan|Cercan|posicion|Posicion|circul|Circul|estacion|Estacion|parada|Parada|salida|Salida|llegada|Llegada)[^"\']*)["\']',
            r'["\']([^"\']*\.(?:json|geojson|php|ashx|aspx|svc)[^"\']*)["\']',
        ]

        hints = []
        ajax_calls = []
        snippets = []

        for idx, item in enumerate(textos):
            nombre = item["source"]
            texto = item["text"]

            snippets.extend(extraer_snippets(nombre, texto))

            # Capturas de $.ajax / $.get / $.post con algo de contexto.
            for pat in [r'\$\.ajax\s*\((.{0,1200}?)\)\s*;', r'\$\.get\s*\((.{0,800}?)\)\s*;', r'\$\.post\s*\((.{0,800}?)\)\s*;']:
                for m in re.finditer(pat, texto, flags=re.S):
                    snip = re.sub(r"\s+", " ", m.group(0)).strip()
                    ajax_calls.append({"source": nombre, "snippet": snip[:1600]})

            for pat in patrones:
                for m in re.findall(pat, texto):
                    if isinstance(m, tuple):
                        m = m[0]
                    s = str(m)
                    if len(s) < 3 or len(s) > 350:
                        continue
                    if any(x in s.lower() for x in ["api", "tren", "cercan", "posicion", "circul", "estacion", "json", "geojson", "parada", "salida", "llegada"]):
                        hints.append({"source_index": idx, "source": nombre, "text": s})

        # dedupe preservando orden
        vistos = set()
        limpios = []
        for h in hints:
            k = (h.get("source"), h["text"])
            if k not in vistos:
                vistos.add(k)
                limpios.append(h)

        vistos_s = set()
        snippets_limpios = []
        for s in snippets:
            k = (s["source"], s["clave"], s["snippet"])
            if k not in vistos_s:
                vistos_s.add(k)
                snippets_limpios.append(s)

        resultado["hints"] = limpios[:800]
        resultado["snippets"] = snippets_limpios[:800]
        resultado["ajax_calls"] = ajax_calls[:200]

    except Exception as e:
        resultado["error"] = f"{type(e).__name__}: {e}"

    log(f"Diagnóstico visor Renfe tiempo-real: {'OK' if resultado['ok'] else 'ERROR'} · scripts {len(resultado.get('scripts', []))} · hints {len(resultado.get('hints', []))} · snippets {len(resultado.get('snippets', []))} · ajax {len(resultado.get('ajax_calls', []))}", salida)
    return resultado


def escribir_debug_tiempo_real_txt(path, debug):
    lineas = []
    lineas.append("DIAGNÓSTICO RENFE TIEMPO REAL")
    lineas.append("=" * 80)
    lineas.append(f"Base URL: {debug.get('base_url')}")
    lineas.append(f"OK: {debug.get('ok')}")
    lineas.append(f"Error: {debug.get('error')}")
    lineas.append("")

    lineas.append("SCRIPTS")
    lineas.append("-" * 80)
    for s in debug.get("scripts", []):
        lineas.append(str(s))
    lineas.append("")

    lineas.append("AJAX / GET / POST DETECTADOS")
    lineas.append("-" * 80)
    for a in debug.get("ajax_calls", []):
        lineas.append(f"[{a.get('source')}] {a.get('snippet')}")
        lineas.append("")

    lineas.append("HINTS / RUTAS SOSPECHOSAS")
    lineas.append("-" * 80)
    for h in debug.get("hints", []):
        lineas.append(f"[{h.get('source')}] {h.get('text')}")
    lineas.append("")

    lineas.append("SNIPPETS POR PALABRAS CLAVE")
    lineas.append("-" * 80)
    for s in debug.get("snippets", []):
        lineas.append(f"[{s.get('source')}] clave={s.get('clave')}")
        lineas.append(str(s.get("snippet")))
        lineas.append("")

    path.write_text("\n".join(lineas), encoding="utf-8")




def main():
    salida = []
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    ahora = ahora_madrid()

    log("GENERADOR TURNOTREN GITHUB", salida)
    log("Hora Madrid: " + ahora.isoformat(timespec="seconds"), salida)

    if not descargar_gtfs(salida):
        resultado = {
            "app": "TurnoTren",
            "estado": "SIN_DATO_REAL",
            "generado": ahora.isoformat(timespec="seconds"),
            "mensaje": "No se ha podido descargar o abrir el GTFS estático.",
            "rutas": [],
            "regla_seguridad": "CASA_TRABAJO: último tren cuya llegada <= entrada - 10 min. TRABAJO_CASA: primer tren cuya salida >= fin de turno. Si no hay dato real válido, mostrar SIN DATO y no inventar horarios."
        }
        JSON_SALIDA.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORTE_SALIDA.write_text("\n".join(salida), encoding="utf-8")
        escribir_index()
        return

    log("Tiempo real Renfe:", salida)
    alerts, alerts_ok = descargar_json_seguro(URL_ALERTS, "alerts", salida, timeout=20, intentos=3)
    trip_updates, trip_updates_ok = descargar_json_seguro(URL_TRIP_UPDATES, "trip_updates", salida, timeout=20, intentos=3)
    vehicle_positions, vehicle_positions_ok = descargar_json_seguro(URL_VEHICLE_POSITIONS, "vehicle_positions", salida, timeout=20, intentos=4)

    rt_ok = alerts_ok or trip_updates_ok or vehicle_positions_ok

    if vehicle_positions_ok:
        log("Tiempo real Renfe: vehicle_positions disponible para posición real.", salida)
    else:
        log("Tiempo real Renfe: vehicle_positions NO disponible; no se podrá mostrar posición GPS.", salida)

    log("Seguimiento visor Renfe:", salida)
    flota_renfe, flota_renfe_ok, flota_renfe_url = descargar_flota_renfe(salida)

    with zipfile.ZipFile(GTFS_ZIP, "r") as zf:
        stops = leer_csv_zip(zf, "stops.txt")
        stops_map = {row.get("stop_id", ""): row.get("stop_name", "") for row in stops}
        trips = leer_csv_zip(zf, "trips.txt")
        calendar = leer_csv_zip(zf, "calendar.txt")
        calendar_dates = leer_csv_zip(zf, "calendar_dates.txt") if "calendar_dates.txt" in zf.namelist() else []

        rutas = [
            preparar_ruta(
                zf, stops_map, trips, calendar, calendar_dates, alerts, trip_updates, vehicle_positions, flota_renfe,
                "casa_trabajo", "Tolosa → Billabona-Zizurkil",
                STOP_TOLOSA, STOP_BILLABONA, NOMBRE_TOLOSA, NOMBRE_BILLABONA, ahora, salida
            ),
            preparar_ruta(
                zf, stops_map, trips, calendar, calendar_dates, alerts, trip_updates, vehicle_positions, flota_renfe,
                "trabajo_casa", "Billabona-Zizurkil → Tolosa",
                STOP_BILLABONA, STOP_TOLOSA, NOMBRE_BILLABONA, NOMBRE_TOLOSA, ahora, salida
            ),
        ]

    trenes_objetivo = []
    for ruta in rutas:
        if ruta.get("proximo_tren"):
            trenes_objetivo.append(ruta["proximo_tren"])
        for t in ruta.get("primeros_directos", [])[:12]:
            trenes_objetivo.append(t)
        for info in ruta.get("turnos", {}).values():
            if info.get("tren"):
                trenes_objetivo.append(info["tren"])

    debug_vehicle_positions = {
        "total": len(vehicle_positions.get("entity", [])),
        "numeros_publicados": sorted({v.get("numero_detectado", "") for v in simplificar_vehicle_positions(vehicle_positions) if v.get("numero_detectado")}),
        "muestras": simplificar_vehicle_positions(vehicle_positions)[:80],
        "diagnostico_trenes_objetivo": diagnosticar_trenes_objetivo(trenes_objetivo, vehicle_positions),
    }

    debug_tiempo_real_renfe = descubrir_endpoints_tiempo_real(salida)

    resultado = {
        "app": "TurnoTren",
        "generado": ahora.isoformat(timespec="seconds"),
        "timezone": "Europe/Madrid",
        "estado": "OK" if any(r["estado"] == "OK" for r in rutas) else "SIN_DATO_REAL",
        "fuentes": {
            "gtfs_estatico": "OK",
            "tiempo_real": "OK" if rt_ok else "ERROR",
            "alerts_ok": alerts_ok,
            "trip_updates_ok": trip_updates_ok,
            "vehicle_positions_ok": vehicle_positions_ok,
            "renfe_flota_ok": flota_renfe_ok,
            "renfe_flota_url": flota_renfe_url,
            "renfe_flota_trenes": len(extraer_lista_trenes_flota(flota_renfe)),
            "alerts_entidades": len(alerts.get("entity", [])),
            "trip_updates_entidades": len(trip_updates.get("entity", [])),
            "vehicle_positions_entidades": len(vehicle_positions.get("entity", [])),
        },
        "rutas": rutas,
        "debug_vehicle_positions": debug_vehicle_positions,
        "debug_tiempo_real_renfe": debug_tiempo_real_renfe,
            "debug_renfe_flota": {
                "ok": flota_renfe_ok,
                "url": flota_renfe_url,
                "total": len(extraer_lista_trenes_flota(flota_renfe)),
                "muestras": [normalizar_item_flota(x) for x in extraer_lista_trenes_flota(flota_renfe)[:80]],
            },
        "regla_seguridad": "CASA_TRABAJO: último tren cuya llegada <= entrada - 10 min. TRABAJO_CASA: primer tren cuya salida >= fin de turno. Si no hay dato real válido, mostrar SIN DATO y no inventar horarios."
    }

    JSON_SALIDA.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    (PUBLIC_DIR / "turnotren_debug_realtime.json").write_text(
        json.dumps({
            "generado": ahora.isoformat(timespec="seconds"),
            "fuentes": resultado["fuentes"],
            "debug_vehicle_positions": debug_vehicle_positions,
            "debug_tiempo_real_renfe": debug_tiempo_real_renfe,
            "debug_renfe_flota": {
                "ok": flota_renfe_ok,
                "url": flota_renfe_url,
                "total": len(extraer_lista_trenes_flota(flota_renfe)),
                "muestras": [normalizar_item_flota(x) for x in extraer_lista_trenes_flota(flota_renfe)[:80]],
            },
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    escribir_debug_tiempo_real_txt(PUBLIC_DIR / "turnotren_debug_realtime.txt", debug_tiempo_real_renfe)
    REPORTE_SALIDA.write_text("\n".join(salida), encoding="utf-8")
    escribir_index()

    log("Archivos generados:", salida)
    log(str(JSON_SALIDA), salida)
    log(str(REPORTE_SALIDA), salida)


if __name__ == "__main__":
    main()
