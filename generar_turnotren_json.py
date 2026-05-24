import csv
import io
import json
import re
import zipfile
import urllib.request
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


def descargar_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "TurnoTren/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


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


def aplicar_extras_tren(tren_json, recorrido, vehicle_positions_json):
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
    tren_json["vehicle_position"] = buscar_vehicle_position(vehicle_positions_json, tren_json.get("trip_id", ""))

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


def buscar_vehicle_position(vehicle_positions_json, trip_id):
    for ent in vehicle_positions_json.get("entity", []):
        veh = ent.get("vehicle", {})
        trip = veh.get("trip", {})

        if trip.get("tripId") != trip_id:
            continue

        pos = veh.get("position", {})
        vehicle_info = veh.get("vehicle", {})

        return {
            "encontrado": True,
            "raw_id": ent.get("id", ""),
            "trip_id": trip_id,
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

    return {"encontrado": False}


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


def preparar_ruta(zf, stops_map, trips_rows, calendar_rows, calendar_dates_rows, alerts, trip_updates, vehicle_positions, clave, nombre, origen_id, destino_id, origen_nombre, destino_nombre, ahora, salida):
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
            vehicle_positions
        )

    for clave_turno, info_turno in turnos.items():
        tr = info_turno.get("tren")
        if tr:
            info_turno["tren"] = aplicar_extras_tren(
                tr,
                recorridos_map.get(tr.get("trip_id", ""), []),
                vehicle_positions
            )

    primeros_json = []
    for t in primeros:
        tu_p = buscar_trip_update(trip_updates, t["trip_id"], origen_id, destino_id)
        tj = aplicar_tiempo_real(t, tu_p)
        tj = aplicar_extras_tren(tj, recorridos_map.get(t["trip_id"], []), vehicle_positions)
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

    try:
        alerts = descargar_json(URL_ALERTS)
        trip_updates = descargar_json(URL_TRIP_UPDATES)
        vehicle_positions = descargar_json(URL_VEHICLE_POSITIONS)
        rt_ok = True
        log("Tiempo real Renfe: OK", salida)
        log(f"  alerts: {len(alerts.get('entity', []))} entidades", salida)
        log(f"  trip_updates: {len(trip_updates.get('entity', []))} entidades", salida)
        log(f"  vehicle_positions: {len(vehicle_positions.get('entity', []))} entidades", salida)
    except Exception as e:
        alerts = {"entity": []}
        trip_updates = {"entity": []}
        vehicle_positions = {"entity": []}
        rt_ok = False
        log(f"Tiempo real Renfe: ERROR {type(e).__name__}: {e}", salida)

    with zipfile.ZipFile(GTFS_ZIP, "r") as zf:
        stops = leer_csv_zip(zf, "stops.txt")
        stops_map = {row.get("stop_id", ""): row.get("stop_name", "") for row in stops}
        trips = leer_csv_zip(zf, "trips.txt")
        calendar = leer_csv_zip(zf, "calendar.txt")
        calendar_dates = leer_csv_zip(zf, "calendar_dates.txt") if "calendar_dates.txt" in zf.namelist() else []

        rutas = [
            preparar_ruta(
                zf, stops_map, trips, calendar, calendar_dates, alerts, trip_updates, vehicle_positions,
                "casa_trabajo", "Tolosa → Billabona-Zizurkil",
                STOP_TOLOSA, STOP_BILLABONA, NOMBRE_TOLOSA, NOMBRE_BILLABONA, ahora, salida
            ),
            preparar_ruta(
                zf, stops_map, trips, calendar, calendar_dates, alerts, trip_updates, vehicle_positions,
                "trabajo_casa", "Billabona-Zizurkil → Tolosa",
                STOP_BILLABONA, STOP_TOLOSA, NOMBRE_BILLABONA, NOMBRE_TOLOSA, ahora, salida
            ),
        ]

    resultado = {
        "app": "TurnoTren",
        "generado": ahora.isoformat(timespec="seconds"),
        "timezone": "Europe/Madrid",
        "estado": "OK" if any(r["estado"] == "OK" for r in rutas) else "SIN_DATO_REAL",
        "fuentes": {
            "gtfs_estatico": "OK",
            "tiempo_real": "OK" if rt_ok else "ERROR",
            "alerts_entidades": len(alerts.get("entity", [])),
            "trip_updates_entidades": len(trip_updates.get("entity", [])),
            "vehicle_positions_entidades": len(vehicle_positions.get("entity", [])),
        },
        "rutas": rutas,
        "regla_seguridad": "CASA_TRABAJO: último tren cuya llegada <= entrada - 10 min. TRABAJO_CASA: primer tren cuya salida >= fin de turno. Si no hay dato real válido, mostrar SIN DATO y no inventar horarios."
    }

    JSON_SALIDA.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORTE_SALIDA.write_text("\n".join(salida), encoding="utf-8")
    escribir_index()

    log("Archivos generados:", salida)
    log(str(JSON_SALIDA), salida)
    log(str(REPORTE_SALIDA), salida)


if __name__ == "__main__":
    main()
