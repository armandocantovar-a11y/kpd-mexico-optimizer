"""Simulación longitudinal de match runs KPD, completamente en memoria.

Las parejas y los donantes se generan de forma sintética. Un catálogo aprobado
de hospitales puede pasarse para ubicar los nodos, pero este módulo nunca escribe
en Supabase ni modifica las tablas clínicas.
"""
from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence

import pandas as pd

import benchmark_nacional as bench
import kpd_motor_emparejamiento as kpd
import logistica_geografica as geo


LIMITE_SOLVER_RONDA_S = 30.0


def _cohortes(n: int, seed: int, siguiente_id: int) -> list:
    parejas, _ = bench.generar_poblacion_nacional(n, seed)
    for pareja in parejas:
        pareja.id = f"SIM-P-{siguiente_id:07d}"
        siguiente_id += 1
    return parejas


def _altruistas(n: int, seed: int, siguiente_id: int) -> list:
    altruistas = bench.generar_altruistas_nacionales(n, seed)
    for altruista in altruistas:
        altruista.id = f"SIM-A-{siguiente_id:05d}"
        siguiente_id += 1
    return altruistas


def _preparar_catalogo(hospitales: Sequence[Mapping] | None) -> dict[str, dict]:
    catalogo = geo.CENTROS_POR_ID if hospitales is None else geo.catalogo_desde_hospitales(hospitales)
    if not catalogo:
        raise ValueError("Se requiere al menos un hospital aprobado para asignar ubicaciones.")
    return catalogo


def _asignar_hospitales(nodos, ids_hospitales: list[str], rng: random.Random, destino: dict[str, str]) -> None:
    for nodo in nodos:
        destino[nodo.id] = rng.choice(ids_hospitales)


def simular_rondas(
    *,
    meses: int = 12,
    parejas_iniciales: int = 40,
    altruistas_iniciales: int = 2,
    nuevas_parejas_mes: int = 12,
    prob_altruista_mes: float = 0.3,
    max_ciclo: int = 3,
    max_cadena: int = 4,
    max_cit: float = 8.0,
    seed: int = 2026,
    hospitales: Sequence[Mapping] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    limite_solver_s: float = LIMITE_SOLVER_RONDA_S,
) -> pd.DataFrame:
    """Ejecuta meses sucesivos y devuelve un historial mensual.

    ``hospitales`` acepta filas de ``public.hospitales`` (solo aprobadas) o
    ``None`` para usar el catálogo local de la simulación sintética.
    ``prioridad_clinica`` añade 2 puntos por cada mes de espera del receptor.
    """
    if not 3 <= int(meses) <= 24:
        raise ValueError("El horizonte debe estar entre 3 y 24 meses.")
    if parejas_iniciales < 0 or altruistas_iniciales < 0 or nuevas_parejas_mes < 0:
        raise ValueError("Los tamaños del pool no pueden ser negativos.")
    if not 0.0 <= float(prob_altruista_mes) <= 1.0:
        raise ValueError("prob_altruista_mes debe estar entre 0 y 1.")
    if max_ciclo not in (2, 3) or max_cadena < 1:
        raise ValueError("max_ciclo debe ser 2 o 3 y max_cadena debe ser al menos 1.")
    if float(max_cit) <= 0 or float(limite_solver_s) <= 0:
        raise ValueError("Los límites de CIT y solver deben ser positivos.")

    catalogo = _preparar_catalogo(hospitales)
    ids_hospitales = list(catalogo)
    rng = random.Random(int(seed))
    pool_parejas: dict[str, kpd.Pareja] = {}
    pool_altruistas: dict[str, kpd.DonanteAltruista] = {}
    ubicaciones: dict[str, str] = {}
    espera: dict[str, int] = {}
    siguiente_pareja, siguiente_altruista = 1, 1

    iniciales = _cohortes(parejas_iniciales, int(seed) + 11, siguiente_pareja)
    siguiente_pareja += len(iniciales)
    altruistas_0 = _altruistas(altruistas_iniciales, int(seed) + 19, siguiente_altruista)
    siguiente_altruista += len(altruistas_0)
    pool_parejas.update((p.id, p) for p in iniciales)
    pool_altruistas.update((a.id, a) for a in altruistas_0)
    espera.update((p.id, 0) for p in iniciales)
    _asignar_hospitales(iniciales, ids_hospitales, rng, ubicaciones)
    _asignar_hospitales(altruistas_0, ids_hospitales, rng, ubicaciones)

    filas: list[dict] = []
    trasplantes_acumulados = 0
    parejas_resueltas_acumuladas = 0
    parejas_llegadas_acumuladas = parejas_iniciales
    sensitizados_acumulados = 0

    for mes in range(1, int(meses) + 1):
        semilla_mes = int(seed) + 1009 * mes
        llegadas = _cohortes(nuevas_parejas_mes, semilla_mes, siguiente_pareja)
        siguiente_pareja += len(llegadas)
        pool_parejas.update((p.id, p) for p in llegadas)
        espera.update((p.id, 0) for p in llegadas)
        _asignar_hospitales(llegadas, ids_hospitales, rng, ubicaciones)
        parejas_llegadas_acumuladas += len(llegadas)

        n_altruistas = int(rng.random() < float(prob_altruista_mes))
        nuevos_altruistas = _altruistas(n_altruistas, semilla_mes + 37, siguiente_altruista)
        siguiente_altruista += len(nuevos_altruistas)
        pool_altruistas.update((a.id, a) for a in nuevos_altruistas)
        _asignar_hospitales(nuevos_altruistas, ids_hospitales, rng, ubicaciones)

        parejas = list(pool_parejas.values())
        altruistas = list(pool_altruistas.values())
        grafo = bench.construir_grafo_rapido(parejas, altruistas, meses_espera=espera)
        geo.filtrar_y_ponderar_grafo(
            grafo, ubicaciones, round(float(max_cit) * 60), 0.0, catalogo
        )
        ciclos, trunc_c = bench.enumerar_ciclos_acotado(grafo, max_ciclo)
        cadenas, trunc_h = (bench.enumerar_cadenas_acotado(grafo, max_cadena)
                            if altruistas else ([], False))
        solucion = kpd.resolver_ilp(
            grafo, ciclos, cadenas,
            limite_s=float(limite_solver_s), gap_rel=bench.GAP_RELATIVO,
        )

        ids_parejas_resueltas = solucion.ids_parejas_trasplantadas()
        ids_altruistas_usados = {cadena.altruista for cadena in solucion.cadenas}
        via_ciclos = sum(len(ciclo) for ciclo in solucion.ciclos)
        via_cadenas = sum(len(cadena.parejas) + 1 for cadena in solucion.cadenas)
        trasplantados_mes = via_ciclos + via_cadenas
        altas_sensibilidad = sum(
            pool_parejas[pid].receptor.hipersensibilizado for pid in ids_parejas_resueltas
        )
        espera_trasplantes = [espera[pid] for pid in ids_parejas_resueltas]
        cit_mes = [
            grafo[u][v]["cit_min"]
            for ciclo in solucion.ciclos
            for i, u in enumerate(ciclo)
            for v in (ciclo[(i + 1) % len(ciclo)],)
        ]
        cit_mes.extend(
            grafo[u][v]["cit_min"]
            for cadena in solucion.cadenas
            for u, v in zip(cadena.nodos, cadena.nodos[1:])
        )

        for pid in ids_parejas_resueltas:
            pool_parejas.pop(pid, None)
            espera.pop(pid, None)
            ubicaciones.pop(pid, None)
        for aid in ids_altruistas_usados:
            pool_altruistas.pop(aid, None)
            ubicaciones.pop(aid, None)
        for pid in pool_parejas:
            espera[pid] += 1

        trasplantes_acumulados += trasplantados_mes
        parejas_resueltas_acumuladas += len(ids_parejas_resueltas)
        sensitizados_acumulados += altas_sensibilidad
        espera_pool = [espera[pid] for pid in pool_parejas]
        filas.append({
            "mes": mes,
            "pool_activo": len(pool_parejas) + len(pool_altruistas),
            "parejas_activas": len(pool_parejas),
            "altruistas_activos": len(pool_altruistas),
            "trasplantados_mes": trasplantados_mes,
            "trasplantados_acumulados": trasplantes_acumulados,
            "via_ciclos": via_ciclos,
            "via_cadenas": via_cadenas,
            "trasplantados_alta_sensibilizacion": altas_sensibilidad,
            "sensibilizados_rescatados_acumulados": sensitizados_acumulados,
            "espera_promedio_pool": (sum(espera_pool) / len(espera_pool)) if espera_pool else 0.0,
            "espera_promedio_trasplante_mes": (sum(espera_trasplantes) / len(espera_trasplantes)) if espera_trasplantes else 0.0,
            "cit_promedio_mes": (sum(cit_mes) / len(cit_mes)) if cit_mes else 0.0,
            "parejas_llegadas_mes": len(llegadas),
            "parejas_llegadas_acumuladas": parejas_llegadas_acumuladas,
            "parejas_resueltas_mes": len(ids_parejas_resueltas),
            "parejas_resueltas_acumuladas": parejas_resueltas_acumuladas,
            "candidatos_truncados": bool(trunc_c or trunc_h),
            "solver_optimo": bool(solucion.optimo),
        })
        if on_progress:
            on_progress(mes, int(meses))

    historial = pd.DataFrame(filas)
    historial.attrs["seed"] = int(seed)
    historial.attrs["parejas_totales_ingresadas"] = parejas_llegadas_acumuladas
    historial.attrs["parejas_resueltas"] = parejas_resueltas_acumuladas
    historial.attrs["trasplantes_totales"] = trasplantes_acumulados
    return historial
