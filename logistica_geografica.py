"""Logística geográfica sintética para el prototipo KPD de México.

Los tiempos son estimaciones operativas de demostración, no rutas clínicas.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Mapping


CENTROS = [
    {"id":"cmn_siglo_xxi", "nombre":"CMN Siglo XXI", "ciudad":"Ciudad de México", "lat":19.4067, "lon":-99.1558, "hub_aereo":True},
    {"id":"cmn_la_raza", "nombre":"CMN La Raza", "ciudad":"Ciudad de México", "lat":19.4668, "lon":-99.1365, "hub_aereo":True},
    {"id":"incmnsz", "nombre":"INCMNSZ Nutrición", "ciudad":"Ciudad de México", "lat":19.2850, "lon":-99.1630, "hub_aereo":True},
    {"id":"cmno", "nombre":"CMNO (UMAE)", "ciudad":"Guadalajara", "lat":20.6736, "lon":-103.3440, "hub_aereo":True},
    {"id":"um_a_nl", "nombre":"UMAE Hospital de Especialidades 25", "ciudad":"Monterrey", "lat":25.6866, "lon":-100.3161, "hub_aereo":True},
    {"id":"umae_puebla", "nombre":"UMAE Hospital de Especialidades Manuel Ávila Camacho", "ciudad":"Puebla", "lat":19.0414, "lon":-98.2063, "hub_aereo":False},
    {"id":"cmn_bajio", "nombre":"Centro Médico Nacional del Bajío", "ciudad":"León", "lat":21.1220, "lon":-101.6820, "hub_aereo":False},
    {"id":"h_juarez_merida", "nombre":"Hospital Regional de Alta Especialidad de la Península de Yucatán", "ciudad":"Mérida", "lat":20.9674, "lon":-89.5926, "hub_aereo":True},
    {"id":"h_general_tijuana", "nombre":"Hospital General de Tijuana", "ciudad":"Tijuana", "lat":32.5149, "lon":-117.0382, "hub_aereo":True},
    {"id":"h_privado_sonora", "nombre":"Hospital Privado San José de Hermosillo", "ciudad":"Hermosillo", "lat":29.0729, "lon":-110.9559, "hub_aereo":True},
    {"id":"cmn_veracruz", "nombre":"CMN Adolfo Ruiz Cortines", "ciudad":"Veracruz", "lat":19.1738, "lon":-96.1342, "hub_aereo":False},
    {"id":"he_slp", "nombre":"Hospital de Especialidades No. 50", "ciudad":"San Luis Potosí", "lat":22.1511, "lon":-100.9760, "hub_aereo":False},
]
CENTROS_POR_ID = {c["id"]: c for c in CENTROS}

# Tiempo vial de ambulancia entre centros de la misma zona metropolitana.
_URBANO = {
    frozenset(("cmn_siglo_xxi", "cmn_la_raza")): 55,
    frozenset(("cmn_siglo_xxi", "incmnsz")): 48,
    frozenset(("cmn_la_raza", "incmnsz")): 82,
}


def _centro(valor: str | Mapping) -> Mapping:
    if isinstance(valor, Mapping):
        return valor
    try:
        return CENTROS_POR_ID[valor]
    except KeyError as exc:
        raise ValueError(f"Centro desconocido: {valor!r}") from exc


def distancia_haversine_km(origen: str | Mapping, destino: str | Mapping) -> float:
    a, b = _centro(origen), _centro(destino)
    lat1, lat2 = radians(a["lat"]), radians(b["lat"])
    dlat, dlon = lat2-lat1, radians(b["lon"]-a["lon"])
    h = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 6371.0088 * 2 * asin(sqrt(h))


def calcular_tiempo_isquemia_min(centro_origen: str | Mapping, centro_destino: str | Mapping) -> int:
    """Estima minutos puerta a puerta: urbano, terrestre o aéreo con conexión."""
    a, b = _centro(centro_origen), _centro(centro_destino)
    if a["id"] == b["id"]:
        return 0
    if a["ciudad"] == b["ciudad"]:
        key = frozenset((a["id"], b["id"]))
        return _URBANO.get(key, max(35, round(distancia_haversine_km(a, b) / 18 * 60 + 25)))
    km = distancia_haversine_km(a, b)
    # 720 km/h aire, 45 min por aeropuerto/operación y traslados urbanos.
    # Sin hub se agrega una penalización de conexión de 90 min.
    vuelo = km / 720 * 60
    carretera_terminales = 70
    conexion = 90 if not (a["hub_aereo"] and b["hub_aereo"]) else 0
    return round(vuelo + carretera_terminales + 45 + conexion)


def asignar_centros(parejas, altruistas=(), semilla: int = 2026) -> dict[str, str]:
    """Distribución reproducible, estratificada y equilibrada de nodos."""
    import random
    ids = [x.id for x in list(parejas) + list(altruistas)]
    rng = random.Random(semilla)
    centros = [c["id"] for c in CENTROS]
    rng.shuffle(centros)
    return {n: centros[i % len(centros)] for i, n in enumerate(ids)}


def filtrar_y_ponderar_grafo(grafo, ubicaciones: Mapping[str, str], max_cit_min: int, lambda_logistica: float):
    """Elimina aristas fuera de CIT y guarda penalización temporal por arista."""
    for u, v, datos in list(grafo.edges(data=True)):
        minutos = calcular_tiempo_isquemia_min(ubicaciones[u], ubicaciones[v])
        if minutos > max_cit_min:
            grafo.remove_edge(u, v)
        else:
            datos["cit_min"] = minutos
            datos["penalizacion_logistica"] = float(lambda_logistica) * minutos
            datos["weight"] = max(1, round(datos["weight"] - datos["penalizacion_logistica"]))
    return grafo

