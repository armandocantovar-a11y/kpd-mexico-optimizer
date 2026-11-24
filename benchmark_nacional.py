#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
 BENCHMARK NACIONAL v2.0 DE DONACIÓN RENAL PAREADA (KPD)
 Hospitales aislados  vs.  red nacional  vs.  red nacional + donantes altruistas
===============================================================================
AVISO: simulación con datos 100% sintéticos y un modelo HLA simplificado.
       Los porcentajes de calibración provienen de los parámetros indicados
       por el usuario; NO es una estimación epidemiológica validada ni una
       herramienta de decisión clínica.

Requiere en la misma carpeta:  kpd_motor_emparejamiento.py (v2.0)
Dependencias:                  networkx  scipy   (HiGHS vía scipy.optimize.milp)

Escenarios:
    A  20 hospitales aislados de 50 parejas (sin altruistas)
    B  red nacional unificada de 1000 parejas (sin altruistas)
    C  red nacional + N donantes altruistas (ciclos <= k_max  +  cadenas <= L_max)

Uso:
    python benchmark_nacional.py                          # k_max=2, 5 altruistas, L_max=5
    python benchmark_nacional.py --altruistas 10 --lmax 4
    python benchmark_nacional.py --altruistas 0           # solo A y B (como la v1)
    python benchmark_nacional.py --kmax 3                 # ciclos de 3 con poda (más lento)
    python benchmark_nacional.py --barrido 0,1,2,5,10     # curva de impacto marginal
    python benchmark_nacional.py --sin-memoria            # omite la pasada de memoria

Control de explosión combinatoria (1000 nodos):
  * Ciclos: poda "por escasez" (se conservan, por nodo, las opciones hacia los
    pacientes con menos alternativas) + tope duro MAX_CICLOS.
  * Cadenas: DFS desde cada altruista con ramificación acotada (top_k_inicio en
    el primer eslabón, top_k_cadena en los siguientes, también por escasez) +
    tope duro MAX_CADENAS. Sin poda, 400 aristas por nodo y L_max=5 serían ~10^13
    caminos.
===============================================================================
"""

import argparse
import random
import time
import tracemalloc
from collections import defaultdict
from functools import lru_cache

import networkx as nx

import kpd_motor_emparejamiento as kpd

# ----------------------------------------------------------------------------
# PARÁMETROS DEL EXPERIMENTO
# ----------------------------------------------------------------------------
SEMILLA = 2026
N_PAREJAS = 1000
N_HOSPITALES = 20            # Escenario A: 20 hospitales x 50 parejas
N_ALTRUISTAS = 5             # Escenario C
LMAX_CADENA = 5              # nº máximo de parejas por cadena abierta

# Calibración (parámetros indicados para México)
DIST_ABO = {"O": 58, "A": 28, "B": 11, "AB": 3}          # % aprox.
P_CPRA_ALTA = 0.27           # 80-99 %
P_CPRA_MEDIA = 0.09          # 25-79 %   (el resto, 64 %, es 0-24 %)
P_PEDIATRICO = 0.13          # < 18 años
EDAD_ADULTA_MEDIA = 38
EDAD_ADULTA_DESV = 10

PANEL_REF = 400              # donantes de referencia para calibrar el cPRA
INTENTOS_DONANTE = 10        # reintentos de HLA antes de forzar un DSA

# Límites de seguridad para la enumeración
MAX_CICLOS = 250_000
TOP_K_PARES = 40             # poda: ciclos de 2 que se conservan por nodo
TOP_K_TRIADAS = 10           # poda: aristas salientes por nodo para ciclos de 3
MAX_CADENAS = 150_000        # tope duro de cadenas candidatas
TOP_K_INICIO = 10            # poda: receptores candidatos del PRIMER eslabón
TOP_K_CADENA = 4             # poda: sucesores por eslabón posterior
LIMITE_SOLVER_S = 120        # tiempo máximo del solver por problema
GAP_RELATIVO = 1e-3          # tolerancia del ILP (~1 trasplante de ~1000)

IDX_HLA = {a: i for i, a in enumerate(kpd.TODOS_HLA)}


# ----------------------------------------------------------------------------
# 1. GENERADOR SINTÉTICO CALIBRADO
# ----------------------------------------------------------------------------
def _mascara(antigenos) -> int:
    m = 0
    for a in antigenos:
        m |= 1 << IDX_HLA[a]
    return m


def _desde_mascara(m: int) -> frozenset:
    return frozenset(a for a, i in IDX_HLA.items() if (m >> i) & 1)


def _muestrear_abo(rng):
    return rng.choices(list(DIST_ABO), list(DIST_ABO.values()))[0]


def _muestrear_cpra(rng) -> int:
    u = rng.random()
    if u < P_CPRA_ALTA:
        return rng.randint(80, 99)
    if u < P_CPRA_ALTA + P_CPRA_MEDIA:
        return rng.randint(25, 79)
    return rng.randint(0, 24)


def _muestrear_edad(rng) -> int:
    if rng.random() < P_PEDIATRICO:
        return rng.randint(2, 17)
    return min(72, max(18, round(rng.gauss(EDAD_ADULTA_MEDIA, EDAD_ADULTA_DESV))))


def _inaceptables_calibrados(rng, mascara_propia, objetivo, panel):
    """Elige antígenos inaceptables hasta que la fracción del panel de donantes
    de referencia que resulta incompatible se acerque al cPRA objetivo.
    (Es la definición operativa del cPRA: % de donantes incompatibles.)"""
    if objetivo <= 0:
        return 0
    candidatos = [i for i in range(len(kpd.TODOS_HLA)) if not (mascara_propia >> i) & 1]
    rng.shuffle(candidatos)
    mascara, restantes, frac = 0, panel, 0.0
    for i in candidatos:
        bit = 1 << i
        nuevos = [d for d in restantes if not d & bit]
        f = 1 - len(nuevos) / len(panel)
        if abs(f - objetivo) <= abs(frac - objetivo):
            mascara |= bit
            restantes, frac = nuevos, f
            if frac >= objetivo:
                break
    return mascara


def generar_poblacion_nacional(n: int, semilla: int):
    """Devuelve (parejas, stats). Todas las parejas son incompatibles entre sí.
    Los marginales (ABO de donantes y receptores, cPRA, edad) se muestrean
    directamente de la distribución objetivo. Si una pareja resulta compatible
    por azar, primero se re-sortea el HLA del donante y, como último recurso,
    se añade un anticuerpo donante-específico (DSA), que es la causa clínica
    habitual de incompatibilidad en parejas ABO-compatibles de bajo cPRA."""
    rng = random.Random(semilla)
    panel = [_mascara(kpd._hla_aleatorio(rng)) for _ in range(PANEL_REF)]
    parejas, dsa_forzados = [], 0

    for i in range(n):
        cpra = _muestrear_cpra(rng)
        hla_r = kpd._hla_aleatorio(rng)
        inac = _desde_mascara(_inaceptables_calibrados(rng, _mascara(hla_r), cpra / 100, panel))
        rec = kpd.Receptor(abo=_muestrear_abo(rng), hla=hla_r, inaceptables=inac,
                           cpra=cpra, edad=_muestrear_edad(rng))
        abo_d = _muestrear_abo(rng)

        for _ in range(INTENTOS_DONANTE):
            don = kpd.Donante(abo_d, kpd._hla_aleatorio(rng))
            if not kpd.es_compatible(don, rec):
                break
        else:  # sigue compatible: forzar un DSA contra un antígeno del donante
            while not (don.hla - rec.hla):
                don = kpd.Donante(abo_d, kpd._hla_aleatorio(rng))
            rec.inaceptables = rec.inaceptables | {rng.choice(sorted(don.hla - rec.hla))}
            dsa_forzados += 1

        assert not kpd.es_compatible(don, rec)
        parejas.append(kpd.Pareja(f"NAC-{i + 1:04d}", don, rec))

    return parejas, {"dsa_forzados": dsa_forzados}


def generar_altruistas_nacionales(n: int, semilla: int, modo_abo: str = "O") -> list:
    """Altruistas ALT-01... con generador propio (semilla+2) para NO alterar la
    población de parejas: los escenarios A y B reproducen exactamente la v1.
    modo_abo='O': todos grupo O (donante universal);  'mx': ABO ~ distribución
    mexicana (58/28/11/3)."""
    rng = random.Random(semilla + 2)
    altruistas = []
    for i in range(n):
        abo = "O" if modo_abo == "O" else _muestrear_abo(rng)
        altruistas.append(kpd.DonanteAltruista(f"ALT-{i + 1:02d}",
                                               kpd.Donante(abo, kpd._hla_aleatorio(rng))))
    return altruistas


def resumen_calibracion(parejas, stats):
    n = len(parejas)
    pct = lambda x: f"{100 * x / n:5.1f}%"
    print("Verificación de calibración (observado vs. objetivo):")
    for rol, get in (("Receptores", lambda p: p.receptor.abo), ("Donantes   ", lambda p: p.donante.abo)):
        obs = {g: sum(get(p) == g for p in parejas) for g in DIST_ABO}
        print(f"  ABO {rol}: " + "  ".join(f"{g} {pct(obs[g])}" for g in DIST_ABO)
              + "   (objetivo 58/28/11/3)")
    alta = sum(p.receptor.cpra >= 80 for p in parejas)
    media = sum(25 <= p.receptor.cpra < 80 for p in parejas)
    baja = n - alta - media
    print(f"  cPRA: 80-99% {pct(alta)} | 25-79% {pct(media)} | 0-24% {pct(baja)}"
          f"   (objetivo 27/9/64)")
    ped = sum(p.receptor.pediatrico for p in parejas)
    adultos = [p.receptor.edad for p in parejas if not p.receptor.pediatrico]
    print(f"  Pediátricos: {pct(ped)} (objetivo 13%) | edad media adultos: "
          f"{sum(adultos) / len(adultos):.1f} años (objetivo ~38)")
    print(f"  Pares incompatibles por construcción: 100% | "
          f"con DSA añadido por azar de HLA:  {pct(stats['dsa_forzados'])}")


# ----------------------------------------------------------------------------
# 2. GRAFO RÁPIDO (máscaras de bits) — equivalente a kpd.construir_grafo
# ----------------------------------------------------------------------------
def construir_grafo_rapido(parejas, altruistas=(), meses_espera=None) -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_nodes_from((p.id, {"tipo": "pareja", "pareja": p}) for p in parejas)
    G.add_nodes_from((a.id, {"tipo": "altruista", "altruista": a}) for a in altruistas)

    m_inac = [_mascara(p.receptor.inaceptables) for p in parejas]
    abo_rec = [p.receptor.abo for p in parejas]
    donantes = [(p.id, p.donante) for p in parejas] + [(a.id, a.donante) for a in altruistas]

    aristas = []
    for id_d, don in donantes:
        permitidos = kpd.ABO_PUEDE_DONAR_A[don.abo]
        md = _mascara(don.hla)
        for j, pj in enumerate(parejas):
            if id_d != pj.id and abo_rec[j] in permitidos and not (md & m_inac[j]):
                aristas.append((id_d, pj.id, {
                    "weight": kpd.PESO_BASE + kpd.prioridad_clinica(
                        pj.receptor, don, (meses_espera or {}).get(pj.id, 0)
                    ),
                    "mismatches": kpd.desajustes_hla(don, pj.receptor),
                }))
    G.add_edges_from(aristas)
    return G


def _nodos_pareja(G):
    return [u for u in G.nodes if G.nodes[u]["tipo"] == "pareja"]


def _grado_entrada_pares(G) -> dict:
    """In-degree de cada pareja contando SOLO donantes de otras parejas (no
    altruistas), para que la poda de A, B y C sea idéntica entre escenarios."""
    return {v: sum(1 for u in G.predecessors(v) if G.nodes[u]["tipo"] == "pareja")
            for v in _nodos_pareja(G)}


# ----------------------------------------------------------------------------
# 3. ENUMERACIÓN CON LÍMITES DE SEGURIDAD: CICLOS Y CADENAS
# ----------------------------------------------------------------------------
class _TopeAlcanzado(Exception):
    pass


def enumerar_ciclos_acotado(G, k_max, max_ciclos=MAX_CICLOS,
                            top_k_pares=TOP_K_PARES, top_k_triadas=TOP_K_TRIADAS):
    """Devuelve (ciclos, truncado). Con top_k = None no se poda nada.

    Poda "por escasez": en un pool grande la mayoría de ciclos son redundantes
    (un paciente fácil de emparejar tiene cientos de opciones). Para cada nodo
    se conservan las opciones que lo conectan con los pacientes MÁS DIFÍCILES
    de emparejar (los de menos alternativas), usando el peso solo como
    desempate. Podar por peso a secas sería peor: todos los nodos competirían
    por los mismos pacientes prioritarios y se perdería cobertura.

    * 2-ciclos : por nodo, los `top_k_pares` con socios de menor grado.
    * 3-ciclos : sobre un subgrafo que conserva, por nodo, las `top_k_triadas`
      aristas salientes hacia receptores con menos donantes compatibles.
    * max_ciclos: tope duro de seguridad; si se alcanza, `truncado=True`."""
    ciclos = []
    nodos = _nodos_pareja(G)

    def anadir(c):
        ciclos.append(c)
        if len(ciclos) >= max_ciclos:
            raise _TopeAlcanzado

    try:
        pares = [(u, v) for u, v in G.edges
                 if u < v and G.nodes[u]["tipo"] == "pareja" and G.has_edge(v, u)]
        if top_k_pares is not None:
            grado = defaultdict(int)
            for u, v in pares:
                grado[u] += 1
                grado[v] += 1
            por_nodo = defaultdict(list)
            for u, v in pares:
                p = G[u][v]["weight"] + G[v][u]["weight"]
                por_nodo[u].append((grado[v], -p, (u, v)))
                por_nodo[v].append((grado[u], -p, (u, v)))
            conservar = set()
            for lst in por_nodo.values():
                lst.sort()
                conservar.update(c for _, _, c in lst[:top_k_pares])
            pares = sorted(conservar)
        for c in pares:
            anadir(c)

        if k_max >= 3:
            gin = _grado_entrada_pares(G)
            ady = {}
            for u in nodos:
                suc = sorted((v for v in G.successors(u)),
                             key=lambda v: (gin[v], -G[u][v]["weight"]))
                ady[u] = suc if top_k_triadas is None else suc[:top_k_triadas]
            ady_set = {u: set(s) for u, s in ady.items()}
            for a in nodos:
                for b in ady[a]:
                    if b < a:
                        continue
                    for c in ady[b]:
                        if c <= a or c == b:
                            continue
                        if a in ady_set[c]:          # c -> a dentro del subgrafo podado
                            anadir((a, b, c))        # a es el menor: sin duplicados
    except _TopeAlcanzado:
        return ciclos, True
    return ciclos, False


def enumerar_cadenas_acotado(G, l_max, max_cadenas=MAX_CADENAS,
                             top_k_inicio=TOP_K_INICIO, top_k=TOP_K_CADENA):
    """Devuelve (cadenas, truncado). DFS desde cada altruista; cada prefijo de
    1 a `l_max` parejas es una cadena candidata. Con top_k = None no se poda.

    Ramificación acotada por escasez: en cada eslabón solo se exploran los
    `top_k` receptores compatibles con MENOS donantes en el pool (primer
    eslabón: `top_k_inicio`). Así las cadenas se dirigen precisamente a los
    pacientes difíciles (hipersensibilizados). Coste: O(A * top_k_inicio *
    top_k^(L-1)) en vez de O(A * grado^L)."""
    cadenas = []
    gin = _grado_entrada_pares(G)
    cache = {}

    def sucesores(u, k):
        clave = (u, k)
        if clave not in cache:
            suc = sorted(G.successors(u), key=lambda v: (gin[v], -G[u][v]["weight"]))
            cache[clave] = suc if k is None else suc[:k]
        return cache[clave]

    def extender(altruista, camino):
        cadenas.append(kpd.Cadena(altruista, tuple(camino)))
        if len(cadenas) >= max_cadenas:
            raise _TopeAlcanzado
        if len(camino) < l_max:
            for v in sucesores(camino[-1], top_k):
                if v not in camino:
                    camino.append(v)
                    extender(altruista, camino)
                    camino.pop()

    try:
        for alt in (u for u in G.nodes if G.nodes[u]["tipo"] == "altruista"):
            for v in sucesores(alt, top_k_inicio):
                extender(alt, [v])
    except _TopeAlcanzado:
        return cadenas, True
    return cadenas, False


# ----------------------------------------------------------------------------
# 4. MOTOR COMPLETO PARA UN POOL + ESCENARIOS
# ----------------------------------------------------------------------------
def correr_motor(parejas, altruistas, cfg) -> dict:
    t0 = time.perf_counter()
    G = construir_grafo_rapido(parejas, altruistas)
    t1 = time.perf_counter()
    ciclos, trunc_c = enumerar_ciclos_acotado(G, cfg.kmax, cfg.max_ciclos, cfg.top_k, cfg.top_k3)
    cadenas, trunc_h = ([], False)
    if altruistas:
        cadenas, trunc_h = enumerar_cadenas_acotado(G, cfg.lmax, cfg.max_cadenas,
                                                    cfg.top_k_inicio, cfg.top_k_cadena)
    t2 = time.perf_counter()
    sol = kpd.resolver_ilp(G, ciclos, cadenas, cfg.limite_solver, GAP_RELATIVO)
    t3 = time.perf_counter()
    return {
        "n": len(parejas), "aristas": G.number_of_edges(),
        "ciclos": len(ciclos), "cadenas": len(cadenas),
        "truncado": trunc_c or trunc_h, "optimo": sol.optimo, "objetivo": sol.objetivo,
        "ids_trasplantados": sol.ids_parejas_trasplantadas(),
        "n_ciclos_sel": len(sol.ciclos), "n_cadenas_sel": len(sol.cadenas),
        "n_lista_espera": sol.n_lista_espera,
        "largos_cadenas": [len(h.parejas) for h in sol.cadenas],
        "n_altruistas": len(altruistas),
        "t_grafo": t1 - t0, "t_ciclos": t2 - t1, "t_ilp": t3 - t2, "t_total": t3 - t0,
    }


def escenario_a(parejas, cfg) -> dict:
    """Hospitales aislados: cada uno optimiza solo con sus propias parejas."""
    tam = len(parejas) // cfg.hospitales
    resultados = [correr_motor(parejas[h * tam:(h + 1) * tam], [], cfg)
                  for h in range(cfg.hospitales)]
    return {
        "n": len(parejas),
        "aristas": sum(r["aristas"] for r in resultados),
        "ciclos": sum(r["ciclos"] for r in resultados), "cadenas": 0,
        "truncado": any(r["truncado"] for r in resultados),
        "optimo": all(r["optimo"] for r in resultados),
        "objetivo": sum(r["objetivo"] for r in resultados),
        "ids_trasplantados": set().union(*(r["ids_trasplantados"] for r in resultados)),
        "n_ciclos_sel": sum(r["n_ciclos_sel"] for r in resultados), "n_cadenas_sel": 0,
        "n_lista_espera": 0, "largos_cadenas": [], "n_altruistas": 0,
        "t_total": sum(r["t_total"] for r in resultados),
        "por_hospital": [len(r["ids_trasplantados"]) for r in resultados],
    }


def escenario_b(parejas, cfg) -> dict:
    """Red nacional: un único pool compartido, sin altruistas."""
    return correr_motor(parejas, [], cfg)


def escenario_c(parejas, altruistas, cfg) -> dict:
    """Red nacional + donantes altruistas (ciclos + cadenas abiertas)."""
    return correr_motor(parejas, altruistas, cfg)


def medir_memoria_mb(funcion, *args) -> float:
    """Pico de memoria Python (tracemalloc) de una ejecución completa."""
    tracemalloc.start()
    funcion(*args)
    pico = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return pico / 1e6


# ----------------------------------------------------------------------------
# 5. AUTOVERIFICACIÓN MATEMÁTICA (antes de correr el benchmark)
# ----------------------------------------------------------------------------
def optimo_exhaustivo(G, ciclos, cadenas) -> int:
    """Verificador INDEPENDIENTE del solver: búsqueda exhaustiva con memoización
    del empaquetamiento de conjuntos de peso máximo (solo para instancias
    pequeñas). Cada estructura se considera en su nodo de menor posición."""
    estructuras = [(frozenset(c), kpd.peso_ciclo(G, c)) for c in ciclos]
    estructuras += [(frozenset(h.nodos), kpd.peso_cadena(G, h)) for h in cadenas]
    if not estructuras:
        return 0
    nodos = sorted(set().union(*(e[0] for e in estructuras)))
    pos = {v: i for i, v in enumerate(nodos)}
    por_minimo = defaultdict(list)
    for miembros, w in estructuras:
        mascara = sum(1 << pos[v] for v in miembros)
        por_minimo[min(pos[v] for v in miembros)].append((mascara, w))

    @lru_cache(maxsize=None)
    def mejor(usado, i):
        if i == len(nodos):
            return 0
        resultado = mejor(usado, i + 1)
        for mascara, w in por_minimo[i]:
            if not usado & mascara:
                resultado = max(resultado, w + mejor(usado | mascara, i + 1))
        return resultado

    return mejor(0, 0)


def autoverificacion(parejas, altruistas, cfg):
    """Tres comprobaciones contra el motor base y contra un verificador exacto."""
    # 1) Grafo: construcción rápida == kpd.construir_grafo; altruistas sin entradas
    muestra, alts = parejas[:40], altruistas[:2] or \
        generar_altruistas_nacionales(2, SEMILLA, "O")
    G_ref, G_fast = kpd.construir_grafo(muestra, alts), construir_grafo_rapido(muestra, alts)
    arista = lambda G: {(u, v, d["weight"]) for u, v, d in G.edges(data=True)}
    ok_grafo = arista(G_ref) == arista(G_fast)
    ok_sin_entradas = all(G_fast.in_degree(a.id) == 0 for a in alts)

    # 2) Enumeración: acotada sin poda == exhaustiva del motor base
    #    (cadenas de hasta 3 parejas en 40 nodos para mantenerla rápida)
    ciclos_ref, cadenas_ref = set(kpd.enumerar_ciclos(G_ref, 3)), set(kpd.enumerar_cadenas(G_ref, 3))
    ciclos_mio, _ = enumerar_ciclos_acotado(G_fast, 3, 10**9, None, None)
    cadenas_mio, _ = enumerar_cadenas_acotado(G_fast, 3, 10**9, None, None)
    ok_enum = (ciclos_ref == set(ciclos_mio)) and (cadenas_ref == set(cadenas_mio))

    # 3) Optimalidad: ILP (gap 0) == búsqueda exhaustiva (12 parejas + 2 altruistas)
    chica = parejas[:12]
    Gc = kpd.construir_grafo(chica, alts)
    cc, ch = kpd.enumerar_ciclos(Gc, 3), kpd.enumerar_cadenas(Gc, min(cfg.lmax, 4))
    sol = kpd.resolver_ilp(Gc, cc, ch, gap_rel=0.0)
    ok_obj = sol.objetivo == optimo_exhaustivo(Gc, cc, ch)
    # factibilidad física de la solución del ILP
    usados = [v for c in sol.ciclos for v in c] + [v for h in sol.cadenas for v in h.nodos]
    ok_disj = len(usados) == len(set(usados))

    ok = all((ok_grafo, ok_sin_entradas, ok_enum, ok_obj, ok_disj))
    print(f"Autoverificación (40 parejas + 2 altruistas / 12 parejas para el óptimo exacto):\n"
          f"  grafo={ok_grafo}  altruistas_sin_entradas={ok_sin_entradas}  "
          f"enumeración={ok_enum}  óptimo_vs_exhaustivo={ok_obj}  disjunción={ok_disj} "
          f"-> {'OK' if ok else 'FALLÓ'}")
    if not ok:
        raise SystemExit("La autoverificación falló: revisar el benchmark.")


# ----------------------------------------------------------------------------
# 6. MÉTRICAS Y TABLA COMPARATIVA
# ----------------------------------------------------------------------------
def metricas(parejas, res):
    ids = res["ids_trasplantados"]
    hip = [p for p in parejas if p.receptor.hipersensibilizado]
    ped = [p for p in parejas if p.receptor.pediatrico]
    tx = len(ids)
    return {
        "tx": tx, "lista": res["n_lista_espera"], "total": tx + res["n_lista_espera"],
        "hip_tx": sum(p.id in ids for p in hip), "hip_n": len(hip),
        "ped_tx": sum(p.id in ids for p in ped), "ped_n": len(ped),
        "clinico": res["objetivo"] - kpd.PESO_BASE * (tx + res["n_lista_espera"]),
    }


def _signo(x):
    return f"{x:+d}"


def imprimir_tabla(parejas, columnas, memorias):
    """columnas: lista de (etiqueta, resultado). memorias: lista paralela (o None)."""
    n = len(parejas)
    ms = [metricas(parejas, r) for _, r in columnas]
    w0, w = 42, 24
    ancho = w0 + w * len(columnas)

    def fila(nombre, *vals):
        print(f"{nombre:<{w0}}" + "".join(f"{v:>{w}}" for v in vals))

    print("\n" + "=" * ancho)
    print(" TABLA COMPARATIVA")
    print("=" * ancho)
    fila("Métrica", *[e for e, _ in columnas])
    print("-" * ancho)
    fila("Receptores de parejas trasplantados", *[f"{m['tx']} ({m['tx'] / n:.1%})" for m in ms])
    fila("Trasplantes a lista de espera (cadenas)", *[str(m["lista"]) for m in ms])
    fila("TRASPLANTES TOTALES", *[str(m["total"]) for m in ms])
    fila("Hipersensibilizados (cPRA>=80%)",
         *[f"{m['hip_tx']}/{m['hip_n']} ({m['hip_tx'] / m['hip_n']:.1%})" for m in ms])
    fila("Pediátricos (<18 años)",
         *[f"{m['ped_tx']}/{m['ped_n']} ({m['ped_tx'] / m['ped_n']:.1%})" for m in ms])
    fila("Puntaje clínico acumulado", *[str(m["clinico"]) for m in ms])
    print("-" * ancho)
    fila("Altruistas usados / disponibles",
         *[f"{r['n_cadenas_sel']}/{r['n_altruistas']}" if r["n_altruistas"] else "—" for _, r in columnas])
    fila("Longitud media de cadena (parejas)",
         *[f"{sum(r['largos_cadenas']) / len(r['largos_cadenas']):.1f}" if r["largos_cadenas"] else "—"
           for _, r in columnas])
    fila("Aristas compatibles", *[f"{r['aristas']:,}" for _, r in columnas])
    fila("Ciclos candidatos evaluados", *[f"{r['ciclos']:,}" for _, r in columnas])
    fila("Cadenas candidatas evaluadas", *[f"{r['cadenas']:,}" if r["cadenas"] else "—" for _, r in columnas])
    fila("Estructuras elegidas (ciclos+cadenas)",
         *[f"{r['n_ciclos_sel']}+{r['n_cadenas_sel']}" for _, r in columnas])
    fila("Tiempo total de cómputo (s)", *[f"{r['t_total']:.2f}" for _, r in columnas])
    if memorias and memorias[0] is not None:
        fila("Memoria pico Python (MB, tracemalloc)", *[f"{x:.1f}" for x in memorias])
    fila("Óptimo (gap<=0.1%) / tope alcanzado",
         *[f"{'sí' if r['optimo'] else 'NO'} / {'sí' if r['truncado'] else 'no'}" for _, r in columnas])
    print("-" * ancho)

    A, B = ms[0], ms[1]
    extra = B["tx"] - A["tx"]
    print("\nEFECTO 1 — Unificar los hospitales (B vs A)")
    print(f"  Receptores adicionales trasplantados : {_signo(extra)}"
          + (f" ({extra / A['tx']:+.1%})" if A["tx"] else ""))
    print(f"  Hipersensibilizados adicionales      : {_signo(B['hip_tx'] - A['hip_tx'])} | "
          f"pediátricos adicionales: {_signo(B['ped_tx'] - A['ped_tx'])}")
    pH = columnas[0][1]["por_hospital"]
    print(f"  Trasplantes por hospital en A: min {min(pH)} / media {sum(pH) / len(pH):.1f} / máx {max(pH)} "
          f"(de {n // len(pH)} parejas cada uno)")

    if len(ms) >= 3:
        C, rc = ms[2], columnas[2][1]
        na = rc["n_altruistas"]
        print(f"\nEFECTO 2 — Añadir {na} donantes altruistas a la red nacional (C vs B)")
        print(f"  Receptores de parejas adicionales    : {_signo(C['tx'] - B['tx'])}   "
              f"<- efecto REAL sobre pacientes en espera de una pareja")
        print(f"  Trasplantes a lista de espera        : {_signo(C['lista'])}   "
              f"(1 por cadena: efecto por construcción del modelo)")
        print(f"  Trasplantes totales                  : {_signo(C['total'] - B['total'])} "
              f"({(C['total'] - B['total']) / na:+.2f} por altruista)")
        print(f"  Hipersensibilizados adicionales      : {_signo(C['hip_tx'] - B['hip_tx'])} "
              f"(quedan {C['hip_n'] - C['hip_tx']} sin trasplante, de {C['hip_n']})")
        print(f"  Pediátricos adicionales              : {_signo(C['ped_tx'] - B['ped_tx'])}")
        print(f"  Puntaje clínico                      : {_signo(C['clinico'] - B['clinico'])}")


def imprimir_barrido(filas):
    print("\n" + "=" * 100)
    print(" BARRIDO DE IMPACTO MARGINAL (red nacional + N altruistas)")
    print("=" * 100)
    print(f"{'N alt.':>7}{'Rec. parejas':>14}{'Lista espera':>14}{'Totales':>9}"
          f"{'Hipersens.':>12}{'Pediátr.':>10}{'Δ rec. vs 0':>13}{'Δ hiper vs 0':>14}{'t (s)':>8}")
    base = filas[0][1]
    for n_alt, m, r in filas:
        print(f"{n_alt:>7}{m['tx']:>14}{m['lista']:>14}{m['total']:>9}"
              f"{m['hip_tx']:>12}{m['ped_tx']:>10}{m['tx'] - base['tx']:>+13}"
              f"{m['hip_tx'] - base['hip_tx']:>+14}{r['t_total']:>8.1f}")


# ----------------------------------------------------------------------------
# 7. PROGRAMA PRINCIPAL
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Benchmark nacional KPD v2.0 (con donantes altruistas)")
    ap.add_argument("--n", type=int, default=N_PAREJAS, help="nº de parejas (def. 1000)")
    ap.add_argument("--hospitales", type=int, default=N_HOSPITALES)
    ap.add_argument("--altruistas", type=int, default=N_ALTRUISTAS,
                    help="nº de donantes altruistas del escenario C (0 = omitir C)")
    ap.add_argument("--abo-altruistas", choices=("O", "mx"), default="O",
                    help="O = todos grupo O; mx = distribución mexicana")
    ap.add_argument("--lmax", type=int, default=LMAX_CADENA, help="parejas por cadena abierta (L_max)")
    ap.add_argument("--kmax", type=int, choices=(2, 3), default=2, help="longitud máxima de ciclo cerrado")
    ap.add_argument("--max-ciclos", type=int, default=MAX_CICLOS)
    ap.add_argument("--max-cadenas", type=int, default=MAX_CADENAS)
    ap.add_argument("--top-k", type=int, default=TOP_K_PARES, help="poda: ciclos de 2 por nodo (0 = sin poda)")
    ap.add_argument("--top-k3", type=int, default=TOP_K_TRIADAS, help="poda: salientes por nodo en ciclos de 3 (0 = sin poda)")
    ap.add_argument("--top-k-inicio", type=int, default=TOP_K_INICIO, help="poda: receptores del primer eslabón de cada cadena")
    ap.add_argument("--top-k-cadena", type=int, default=TOP_K_CADENA, help="poda: sucesores por eslabón de cadena")
    ap.add_argument("--limite-solver", type=float, default=LIMITE_SOLVER_S)
    ap.add_argument("--barrido", type=str, default="", help="lista de nº de altruistas, p. ej. 0,1,2,5,10")
    ap.add_argument("--sin-memoria", action="store_true", help="omite la pasada con tracemalloc")
    cfg = ap.parse_args()
    cfg.top_k, cfg.top_k3 = cfg.top_k or None, cfg.top_k3 or None
    if cfg.n % cfg.hospitales:
        raise SystemExit("--n debe ser múltiplo de --hospitales")
    conteos = [int(x) for x in cfg.barrido.split(",") if x.strip()] if cfg.barrido else []
    n_alt_total = max([cfg.altruistas] + conteos)

    print("=" * 78)
    print(f" BENCHMARK NACIONAL KPD v2.0 | N={cfg.n} | semilla={SEMILLA} | k_max={cfg.kmax} | "
          f"L_max={cfg.lmax} | altruistas={cfg.altruistas}")
    print(" Solver: HiGHS (scipy.optimize.milp)")
    print("=" * 78)

    t0 = time.perf_counter()
    parejas, stats = generar_poblacion_nacional(cfg.n, SEMILLA)
    altruistas = generar_altruistas_nacionales(n_alt_total, SEMILLA, cfg.abo_altruistas)
    print(f"Población sintética generada en {time.perf_counter() - t0:.2f} s\n")
    resumen_calibracion(parejas, stats)
    print(f"  Altruistas generados: {n_alt_total} (ABO: "
          + ", ".join(f"{g} {sum(a.donante.abo == g for a in altruistas)}" for g in DIST_ABO
                      if any(a.donante.abo == g for a in altruistas)) + ")")

    # Asignación a hospitales: orden aleatorio reproducible (sin sesgo por ID)
    barajadas = parejas[:]
    random.Random(SEMILLA + 1).shuffle(barajadas)

    print()
    autoverificacion(parejas, altruistas, cfg)

    print("\nEjecutando Escenario A (hospitales aislados)...")
    A = escenario_a(barajadas, cfg)
    print(f"  listo en {A['t_total']:.2f} s")
    print("Ejecutando Escenario B (red nacional unificada)...")
    B = escenario_b(barajadas, cfg)
    print(f"  listo en {B['t_total']:.2f} s "
          f"(grafo {B['t_grafo']:.2f} s | ciclos {B['t_ciclos']:.2f} s | ILP {B['t_ilp']:.2f} s)")
    columnas = [("A: 20 hospitales", A), ("B: red nacional", B)]
    funciones = [(escenario_a, (barajadas, cfg)), (escenario_b, (barajadas, cfg))]

    if cfg.altruistas > 0:
        alt_c = altruistas[:cfg.altruistas]
        print(f"Ejecutando Escenario C (red nacional + {cfg.altruistas} altruistas, L_max={cfg.lmax})...")
        C = escenario_c(barajadas, alt_c, cfg)
        print(f"  listo en {C['t_total']:.2f} s "
              f"(grafo {C['t_grafo']:.2f} s | ciclos+cadenas {C['t_ciclos']:.2f} s | ILP {C['t_ilp']:.2f} s)")
        columnas.append((f"C: red + {cfg.altruistas} altruistas", C))
        funciones.append((escenario_c, (barajadas, alt_c, cfg)))

    memorias = None
    if not cfg.sin_memoria:
        print("Midiendo memoria (pasada aparte con tracemalloc; no afecta a los tiempos)...")
        memorias = [medir_memoria_mb(f, *args) for f, args in funciones]

    imprimir_tabla(parejas, columnas, memorias)

    if conteos:
        print("\nEjecutando barrido de altruistas...")
        filas = []
        for k in conteos:
            r = B if k == 0 else escenario_c(barajadas, altruistas[:k], cfg)
            filas.append((k, metricas(parejas, r), r))
        imprimir_barrido(filas)

    print("\nNota: simulación con datos sintéticos y modelo HLA simplificado; "
          "los números ilustran el efecto de escala, no son una predicción clínica.")


if __name__ == "__main__":
    main()
