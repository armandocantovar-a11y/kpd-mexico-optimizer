#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
 PROTOTIPO EDUCATIVO v2.0: Motor de Donación Renal Pareada (KPD)
 con Donantes Altruistas (no dirigidos) y cadenas abiertas
 Maximum Weight Cycle & Chain Packing sobre un grafo dirigido ponderado
===============================================================================
AVISO: simulación con datos 100% sintéticos y un modelo HLA simplificado.
       NO es un dispositivo médico ni debe usarse para decisiones clínicas.

Novedades de la v2.0 respecto a la v1:
  * Donante altruista (`DonanteAltruista`): ofrece un riñón sin pedir otro a
    cambio. En el grafo es un nodo SOLO con aristas salientes.
  * Cadenas abiertas  ALT -> P1 -> P2 -> ... -> Pn -> [lista de espera].
    Cada pareja de la cadena recibe un riñón; el donante de la última pareja
    entrega el suyo a la lista de espera general (+1 trasplante, peso base).
    (Ver p. ej. Montgomery et al. 2006, "domino paired donation"; Rees et al.
    2009, cadenas NEAD no simultáneas.)
  * ILP conjunto de ciclos cerrados + cadenas abiertas con scipy.optimize.milp
    (HiGHS) usando matrices dispersas. Ya NO depende de PuLP.

Modelo matemático:
    max  sum_c w_c x_c  +  sum_h w_h y_h
    s.a. sum_{c ∋ v} x_c + sum_{h ∋ v} y_h <= 1     para toda pareja v
         sum_{h inicia en a} y_h            <= 1     para todo altruista a
         x_c, y_h ∈ {0,1}
  (la restricción de cada altruista queda cubierta al tratarlo como un nodo
   más: solo aparece como primer elemento de las cadenas.)

Dependencias:
    pip install networkx scipy        (scipy trae numpy; compatible con Python 3.14)

Ejecución:
    python kpd_motor_emparejamiento.py
===============================================================================
"""

import random
from dataclasses import dataclass
from itertools import combinations
from typing import NamedTuple

import networkx as nx
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix

# ----------------------------------------------------------------------------
# PARÁMETROS GLOBALES
# ----------------------------------------------------------------------------
SEMILLA = 323               # reproducibilidad de los datos sintéticos
N_PAREJAS = 12
N_ALTRUISTAS = 1            # donantes altruistas del ejemplo pequeño
LONGITUD_MAX_CICLO = 3      # 2 o 3 parejas (viabilidad quirúrgica simultánea)
LONGITUD_MAX_CADENA = 4     # nº máximo de PAREJAS por cadena abierta (L_max)

# Peso base por trasplante: mucho mayor que cualquier bonificación clínica por
# arista, de modo que se prioriza maximizar el nº de trasplantes y, a igualdad,
# el puntaje clínico (prioridad ponderada casi lexicográfica).
PESO_BASE = 1000
LIMITE_SOLVER_S = 120       # tiempo máximo del solver (s)

# Catálogo simplificado de antígenos HLA por locus
HLA_A = ["A1", "A2", "A3", "A11", "A24", "A26", "A29", "A30"]
HLA_B = ["B7", "B8", "B13", "B27", "B35", "B44", "B51", "B57"]
HLA_DR = ["DR1", "DR3", "DR4", "DR7", "DR11", "DR13", "DR15"]
TODOS_HLA = HLA_A + HLA_B + HLA_DR

# Compatibilidad ABO: grupo del donante -> grupos de receptor que puede recibir
ABO_PUEDE_DONAR_A = {
    "O": {"O", "A", "B", "AB"},
    "A": {"A", "AB"},
    "B": {"B", "AB"},
    "AB": {"AB"},
}


# ----------------------------------------------------------------------------
# MODELO DE DATOS
# ----------------------------------------------------------------------------
@dataclass
class Donante:
    abo: str
    hla: frozenset              # 6 antígenos (2 por locus A, B, DR)


@dataclass
class Receptor:
    abo: str
    hla: frozenset              # HLA propio del receptor
    inaceptables: frozenset     # antígenos contra los que tiene anticuerpos
    cpra: int                   # % sensibilización (0-100)
    edad: int

    @property
    def hipersensibilizado(self):
        return self.cpra >= 80

    @property
    def pediatrico(self):
        return self.edad < 18


@dataclass
class Pareja:
    id: str
    donante: Donante
    receptor: Receptor


@dataclass
class DonanteAltruista:
    """Donante no dirigido: tiene un Donante pero NO un Receptor asociado."""
    id: str
    donante: Donante


class Cadena(NamedTuple):
    """Cadena abierta: altruista -> parejas[0] -> ... -> parejas[-1] -> lista de espera."""
    altruista: str
    parejas: tuple

    @property
    def nodos(self) -> tuple:
        return (self.altruista,) + self.parejas


@dataclass
class Solucion:
    ciclos: list                # ciclos cerrados elegidos (tuplas de ids)
    cadenas: list               # cadenas abiertas elegidas (Cadena)
    objetivo: int               # valor de la función objetivo
    optimo: bool                # True si el solver probó optimalidad (dentro del gap)

    def ids_parejas_trasplantadas(self) -> set:
        """Parejas cuyo receptor recibe un riñón (ciclos + cadenas)."""
        ids = {v for c in self.ciclos for v in c}
        ids.update(v for ch in self.cadenas for v in ch.parejas)
        return ids

    @property
    def n_lista_espera(self) -> int:
        """Trasplantes a la lista de espera general (1 por cadena abierta)."""
        return len(self.cadenas)


# ----------------------------------------------------------------------------
# 1. REGLAS DE COMPATIBILIDAD
# ----------------------------------------------------------------------------
def abo_compatible(abo_donante: str, abo_receptor: str) -> bool:
    return abo_receptor in ABO_PUEDE_DONAR_A[abo_donante]


def choque_hla(donante: Donante, receptor: Receptor) -> set:
    """Antígenos del donante contra los que el receptor tiene anticuerpos
    (crossmatch virtual positivo). Conjunto vacío = sin choque."""
    return set(donante.hla) & set(receptor.inaceptables)


def es_compatible(donante: Donante, receptor: Receptor) -> bool:
    return abo_compatible(donante.abo, receptor.abo) and not choque_hla(donante, receptor)


def desajustes_hla(donante: Donante, receptor: Receptor) -> int:
    """Nº de antígenos del donante ausentes en el receptor (mismatches, 0-6)."""
    return len(set(donante.hla) - set(receptor.hla))


# ----------------------------------------------------------------------------
# 2. GENERACIÓN DE DATOS SINTÉTICOS
# ----------------------------------------------------------------------------
def _hla_aleatorio(rng: random.Random) -> frozenset:
    """2 antígenos por locus (A, B, DR); simplificación sin homocigotos."""
    return frozenset(rng.sample(HLA_A, 2) + rng.sample(HLA_B, 2) + rng.sample(HLA_DR, 2))


def generar_parejas(rng: random.Random) -> list:
    # cPRA preasignados para garantizar variedad (incluye hipersensibilizados)
    cpras = [0, 5, 20, 35, 50, 65, 80, 85, 92, 98, 99, 10]
    rng.shuffle(cpras)

    # Edades: dos casos pediátricos (<18); el resto adultos
    edades = [rng.randint(24, 68) for _ in range(N_PAREJAS)]
    for idx in rng.sample(range(N_PAREJAS), 2):
        edades[idx] = rng.choice([7, 11, 14, 16])

    grupos = ["O", "A", "B", "AB"]
    pesos_abo = [0.45, 0.35, 0.15, 0.05]  # frecuencias aproximadas

    parejas = []
    for i in range(N_PAREJAS):
        # Se regenera hasta que la pareja sea INCOMPATIBLE entre sí
        # (esa es la razón de entrar a un programa de donación pareada).
        while True:
            hla_rec = _hla_aleatorio(rng)
            # nº de antígenos inaceptables proporcional al cPRA (máx. 12)
            candidatos = [a for a in TODOS_HLA if a not in hla_rec]
            n_inac = round(cpras[i] / 100 * 12)
            inaceptables = frozenset(rng.sample(candidatos, min(n_inac, len(candidatos))))

            rec = Receptor(
                abo=rng.choices(grupos, pesos_abo)[0],
                hla=hla_rec,
                inaceptables=inaceptables,
                cpra=cpras[i],
                edad=edades[i],
            )
            don = Donante(
                abo=rng.choices(grupos, pesos_abo)[0],
                hla=_hla_aleatorio(rng),
            )
            if not es_compatible(don, rec):
                break
        parejas.append(Pareja(f"PAR-{i + 1:02d}", don, rec))
    return parejas


def generar_altruistas(rng: random.Random, n: int = N_ALTRUISTAS, abo: str = "O") -> list:
    """Donantes altruistas (ALT-01...). Grupo O por defecto (donante universal,
    el caso clínicamente más valioso). Se llama DESPUÉS de generar_parejas para
    no alterar la secuencia aleatoria de las parejas."""
    return [DonanteAltruista(f"ALT-{i + 1:02d}", Donante(abo, _hla_aleatorio(rng)))
            for i in range(n)]


# ----------------------------------------------------------------------------
# 3. CONSTRUCCIÓN DEL GRAFO DIRIGIDO PONDERADO
# ----------------------------------------------------------------------------
def prioridad_clinica(receptor: Receptor, donante: Donante) -> int:
    """Bonificación clínica de una arista (donante -> receptor)."""
    p = 0
    # Hipersensibilización (pocos donantes compatibles => prioridad alta)
    if receptor.cpra >= 98:
        p += 40
    elif receptor.cpra >= 80:
        p += 25
    elif receptor.cpra >= 50:
        p += 8
    # Prioridad pediátrica
    if receptor.pediatrico:
        p += 20
    # Mejor histocompatibilidad: +1 por cada antígeno que COINCIDE (0-6)
    p += 6 - desajustes_hla(donante, receptor)
    return p


def es_altruista(G: nx.DiGraph, nodo: str) -> bool:
    return G.nodes[nodo].get("tipo") == "altruista"


def construir_grafo(parejas: list, altruistas=()) -> nx.DiGraph:
    """Nodos: parejas (tipo='pareja') y altruistas (tipo='altruista').
    Arista X -> B: el donante de X es compatible con el receptor de la pareja B.
    Los altruistas solo emiten aristas: ninguna arista entra a un altruista."""
    G = nx.DiGraph()
    for p in parejas:
        G.add_node(p.id, tipo="pareja", pareja=p)
    for a in altruistas:
        G.add_node(a.id, tipo="altruista", altruista=a)

    donantes = [(p.id, p.donante) for p in parejas] + [(a.id, a.donante) for a in altruistas]
    for id_donante, donante in donantes:
        for b in parejas:
            if id_donante != b.id and es_compatible(donante, b.receptor):
                G.add_edge(
                    id_donante, b.id,
                    weight=PESO_BASE + prioridad_clinica(b.receptor, donante),
                    mismatches=desajustes_hla(donante, b.receptor),
                )
    return G


# ----------------------------------------------------------------------------
# 4. ENUMERACIÓN DE CICLOS CERRADOS (k <= 3) Y CADENAS ABIERTAS (<= L_max)
# ----------------------------------------------------------------------------
def enumerar_ciclos(G: nx.DiGraph, k_max: int = LONGITUD_MAX_CICLO) -> list:
    """Ciclos cerrados de 2 o 3 parejas, sin duplicados por rotación.
    Los altruistas no tienen aristas entrantes, por lo que nunca aparecen."""
    ciclos = []
    nodos = sorted(n for n in G.nodes if not es_altruista(G, n))
    for a, b in combinations(nodos, 2):                 # ciclos de 2
        if G.has_edge(a, b) and G.has_edge(b, a):
            ciclos.append((a, b))
    if k_max >= 3:                                      # ciclos de 3
        for a in nodos:
            for b in G.successors(a):
                if b <= a:
                    continue
                for c in G.successors(b):
                    if c <= a or c == b:
                        continue
                    if G.has_edge(c, a):
                        ciclos.append((a, b, c))
    return ciclos


def enumerar_cadenas(G: nx.DiGraph, l_max: int = LONGITUD_MAX_CADENA) -> list:
    """Cadenas abiertas simples que inician en un altruista y recorren de 1 a
    `l_max` parejas distintas. Cada prefijo es una cadena candidata distinta
    (el donante de su última pareja iría a la lista de espera).
    Búsqueda exhaustiva: pensada para redes pequeñas (ver el benchmark para
    la versión acotada/podada)."""
    cadenas = []

    def extender(altruista, camino):
        cadenas.append(Cadena(altruista, tuple(camino)))
        if len(camino) < l_max:
            for v in sorted(G.successors(camino[-1])):
                if v not in camino:
                    camino.append(v)
                    extender(altruista, camino)
                    camino.pop()

    for alt in sorted(n for n in G.nodes if es_altruista(G, n)):
        for v in sorted(G.successors(alt)):
            extender(alt, [v])
    return cadenas


def peso_ciclo(G: nx.DiGraph, ciclo: tuple) -> int:
    n = len(ciclo)
    return sum(G[ciclo[i]][ciclo[(i + 1) % n]]["weight"] for i in range(n))


def peso_cadena(G: nx.DiGraph, cadena: Cadena) -> int:
    """Suma de aristas (altruista->P1->...->Pn) + PESO_BASE por el trasplante
    final del donante de Pn a la lista de espera."""
    nodos = cadena.nodos
    return (sum(G[nodos[i]][nodos[i + 1]]["weight"] for i in range(len(nodos) - 1))
            + PESO_BASE)


# ----------------------------------------------------------------------------
# 5. OPTIMIZACIÓN CONJUNTA: ILP con scipy.optimize.milp (HiGHS), matriz dispersa
# ----------------------------------------------------------------------------
def resolver_ilp(G: nx.DiGraph, ciclos, cadenas=(), limite_s: float = LIMITE_SOLVER_S,
                 gap_rel: float = 0.0) -> Solucion:
    """Selecciona el conjunto de ciclos y cadenas disjuntos de peso máximo.
    gap_rel=0 exige el óptimo exacto; valores pequeños (1e-3) aceleran redes
    grandes con una pérdida acotada."""
    ciclos, cadenas = list(ciclos), list(cadenas)
    if not ciclos and not cadenas:
        return Solucion([], [], 0, True)

    pesos = [peso_ciclo(G, c) for c in ciclos] + [peso_cadena(G, h) for h in cadenas]
    miembros = ciclos + [h.nodos for h in cadenas]      # nodos de cada estructura

    indice = {v: i for i, v in enumerate(G.nodes)}
    filas, cols = [], []
    for j, nodos in enumerate(miembros):
        for v in nodos:
            filas.append(indice[v])
            cols.append(j)
    A = csr_matrix((np.ones(len(filas)), (filas, cols)), shape=(len(indice), len(miembros)))

    res = milp(
        c=-np.array(pesos, dtype=float),                 # milp minimiza
        constraints=LinearConstraint(A, ub=np.ones(len(indice))),
        integrality=np.ones(len(miembros)),
        bounds=Bounds(0, 1),
        options={"time_limit": limite_s, "mip_rel_gap": gap_rel},
    )
    if res.x is None:
        raise RuntimeError(f"El solver no devolvió solución: {res.message}")

    elegidos = [j for j in np.flatnonzero(res.x > 0.5)]
    n_c = len(ciclos)
    return Solucion(
        ciclos=[ciclos[j] for j in elegidos if j < n_c],
        cadenas=[cadenas[j - n_c] for j in elegidos if j >= n_c],
        objetivo=int(sum(pesos[j] for j in elegidos)),
        optimo=bool(res.success),
    )


# ----------------------------------------------------------------------------
# 6. SALIDA EN CONSOLA
# ----------------------------------------------------------------------------
def linea(titulo=""):
    print("\n" + "=" * 78)
    if titulo:
        print(f" {titulo}")
        print("=" * 78)


def imprimir_parejas(parejas):
    linea("1. PAREJAS DONANTE-RECEPTOR (todas incompatibles entre sí)")
    print(f"{'ID':<7}{'Don.':<6}{'Rec.':<6}{'cPRA':>5}{'Edad':>6}  Etiquetas clínicas")
    print("-" * 78)
    for p in parejas:
        r = p.receptor
        etiquetas = []
        if r.hipersensibilizado:
            etiquetas.append("HIPERSENSIBILIZADO")
        if r.pediatrico:
            etiquetas.append("PEDIÁTRICO")
        print(f"{p.id:<7}{p.donante.abo:<6}{r.abo:<6}{r.cpra:>4}%{r.edad:>6}  {', '.join(etiquetas)}")
    print("\nDetalle HLA (donante | receptor | anticuerpos inaceptables del receptor):")
    for p in parejas:
        print(f"  {p.id}: D[{' '.join(sorted(p.donante.hla))}]")
        print(f"          R[{' '.join(sorted(p.receptor.hla))}]")
        print(f"          Ac[{' '.join(sorted(p.receptor.inaceptables)) or '—'}]")


def imprimir_altruistas(altruistas):
    linea("2. DONANTES ALTRUISTAS (sin receptor asociado: solo aristas salientes)")
    for a in altruistas:
        print(f"  {a.id}: grupo {a.donante.abo} | HLA [{' '.join(sorted(a.donante.hla))}]")


def imprimir_compatibilidades(G):
    linea("3. COMPATIBILIDADES (arista X -> B: donante de X sirve al receptor de B; ★ = altruista)")
    print(f"{'Arista':<18}{'ABO':<8}{'Mismatch HLA':>13}{'Peso':>8}")
    print("-" * 78)
    for u, v, d in sorted(G.edges(data=True)):
        du = G.nodes[u]["altruista"].donante if es_altruista(G, u) else G.nodes[u]["pareja"].donante
        pv = G.nodes[v]["pareja"]
        marca = "★" if es_altruista(G, u) else " "
        abo = f"{du.abo}->{pv.receptor.abo}"
        print(f"{marca}{u} -> {v:<8}{abo:<8}{d['mismatches']:>13}{d['weight']:>8}")
    n_p = sum(1 for n in G.nodes if not es_altruista(G, n))
    print(f"\nTotal: {G.number_of_edges()} aristas válidas "
          f"(densidad entre parejas {sum(1 for u, v in G.edges if not es_altruista(G, u)) / (n_p * (n_p - 1)):.0%}).")
    assert all(G.in_degree(n) == 0 for n in G.nodes if es_altruista(G, n)), \
        "un altruista no puede tener aristas entrantes"


def _linea_cadena_detalle(G, ch):
    nodos = ch.nodos
    for i in range(len(nodos) - 1):
        a, b = nodos[i], nodos[i + 1]
        if es_altruista(G, a):
            origen = f"Altruista {a} ({G.nodes[a]['altruista'].donante.abo})"
        else:
            origen = f"Donante {a} ({G.nodes[a]['pareja'].donante.abo})"
        r = G.nodes[b]["pareja"].receptor
        nota = " [HIPERSENS.]" if r.hipersensibilizado else ""
        nota += " [PEDIÁTRICO]" if r.pediatrico else ""
        print(f"    {origen} -> Receptor {b} ({r.abo}, cPRA {r.cpra}%, {r.edad} a){nota}")
    ultimo = nodos[-1]
    print(f"    Donante {ultimo} ({G.nodes[ultimo]['pareja'].donante.abo}) "
          f"-> LISTA DE ESPERA general (+1 trasplante)")


def imprimir_solucion(G, ciclos_todos, cadenas_todas, sol):
    linea("4. OPTIMIZACIÓN (ciclos de 2-3 parejas + cadenas abiertas, ILP conjunto / HiGHS)")
    print(f"Candidatos: {len(ciclos_todos)} ciclos cerrados "
          f"({sum(len(c) == 2 for c in ciclos_todos)} de 2, {sum(len(c) == 3 for c in ciclos_todos)} de 3) "
          f"+ {len(cadenas_todas)} cadenas abiertas (<= {LONGITUD_MAX_CADENA} parejas)")
    print(f"Estado del solver: {'óptimo' if sol.optimo else 'NO probado óptimo'}\n")
    if not sol.ciclos and not sol.cadenas:
        print("No se encontró ninguna estructura: no hay intercambio posible.")
        return
    k = 0
    for ciclo in sol.ciclos:
        k += 1
        print(f"Ciclo cerrado #{k}  [{len(ciclo)} trasplantes, peso {peso_ciclo(G, ciclo)}]")
        print("  " + " -> ".join(ciclo) + f" -> ({ciclo[0]})")
        n = len(ciclo)
        for i in range(n):
            a, b = ciclo[i], ciclo[(i + 1) % n]
            r = G.nodes[b]["pareja"].receptor
            nota = " [HIPERSENS.]" if r.hipersensibilizado else ""
            nota += " [PEDIÁTRICO]" if r.pediatrico else ""
            print(f"    Donante {a} ({G.nodes[a]['pareja'].donante.abo}) -> Receptor {b} "
                  f"({r.abo}, cPRA {r.cpra}%, {r.edad} a){nota}")
        print()
    for ch in sol.cadenas:
        k += 1
        print(f"Cadena abierta #{k}  [{len(ch.parejas)} parejas + 1 lista de espera = "
              f"{len(ch.parejas) + 1} trasplantes, peso {peso_cadena(G, ch)}]")
        print("  " + " -> ".join(ch.nodos) + " -> [LISTA DE ESPERA]")
        _linea_cadena_detalle(G, ch)
        print()


def imprimir_metricas(G, sol, referencia_sin_altruistas=None):
    linea("5. MÉTRICAS FINALES")
    parejas = [G.nodes[n]["pareja"] for n in sorted(G.nodes) if not es_altruista(G, n)]
    ids_tx = sol.ids_parejas_trasplantadas()
    total_p, n_tx, n_lista = len(parejas), len(ids_tx), sol.n_lista_espera

    hiper = [p for p in parejas if p.receptor.hipersensibilizado]
    ped = [p for p in parejas if p.receptor.pediatrico]
    hiper_tx = [p for p in hiper if p.id in ids_tx]
    ped_tx = [p for p in ped if p.id in ids_tx]
    puntaje_clinico = sol.objetivo - PESO_BASE * (n_tx + n_lista)

    print(f"Receptores de parejas trasplantados : {n_tx} de {total_p} ({n_tx / total_p:.1%})")
    print(f"Trasplantes a lista de espera       : {n_lista} (cierre de cadenas abiertas)")
    print(f"TRASPLANTES TOTALES                 : {n_tx + n_lista}")
    print(f"Ciclos cerrados / cadenas abiertas  : {len(sol.ciclos)} / {len(sol.cadenas)}")
    print(f"Hipersensibilizados (>=80%)         : {len(hiper_tx)} de {len(hiper)} lograron match"
          + (f" ({len(hiper_tx) / len(hiper):.0%})" if hiper else ""))
    print(f"Pediátricos (<18 años)              : {len(ped_tx)} de {len(ped)} lograron match")
    print(f"Puntaje clínico acumulado           : {puntaje_clinico}  "
          f"(objetivo total: {sol.objetivo})")

    if referencia_sin_altruistas is not None:
        ref_tx = len(referencia_sin_altruistas.ids_parejas_trasplantadas())
        print(f"\nReferencia SIN altruistas           : {ref_tx} receptores trasplantados "
              f"-> el altruista aporta {n_tx - ref_tx:+d} receptores de parejas "
              f"y {n_lista} trasplante(s) a lista de espera")

    sin_match = [p for p in parejas if p.id not in ids_tx]
    if sin_match:
        print("\nParejas SIN match en esta ronda (pasan a la siguiente corrida):")
        for p in sin_match:
            ent = G.in_degree(p.id)
            sal = G.out_degree(p.id)
            if ent == 0:
                motivo = "ningún donante del pool (altruistas incluidos) es compatible con su receptor"
            elif sal == 0:
                motivo = "su donante no es compatible con ningún receptor del pool"
            else:
                motivo = "tiene compatibilidades, pero ninguna estructura disjunta las incluye"
            etq = " [HIPERSENS.]" if p.receptor.hipersensibilizado else ""
            print(f"  {p.id}{etq}: {motivo} (entradas={ent}, salidas={sal})")


def main():
    rng = random.Random(SEMILLA)
    parejas = generar_parejas(rng)
    altruistas = generar_altruistas(rng)
    imprimir_parejas(parejas)
    imprimir_altruistas(altruistas)

    G = construir_grafo(parejas, altruistas)
    imprimir_compatibilidades(G)

    ciclos = enumerar_ciclos(G)
    cadenas = enumerar_cadenas(G)
    sol = resolver_ilp(G, ciclos, cadenas)
    imprimir_solucion(G, ciclos, cadenas, sol)

    # Línea base: el mismo pool SIN altruistas (para ver el aporte marginal)
    G0 = construir_grafo(parejas)
    sol0 = resolver_ilp(G0, enumerar_ciclos(G0))
    imprimir_metricas(G, sol, referencia_sin_altruistas=sol0)

    linea("NOTA")
    print("Prototipo educativo con datos sintéticos. En un programa real se añade:\n"
          "crossmatch físico, donantes puente en cadenas no simultáneas, tiempo en\n"
          "lista, logística de traslado, y aprobación del comité de trasplantes.")


if __name__ == "__main__":
    main()
