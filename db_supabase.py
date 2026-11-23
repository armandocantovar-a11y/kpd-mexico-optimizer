"""Acceso opcional a Supabase para el registro KPD.

No abre conexión ni requiere secretos al importar. La aplicación conserva así
el modo sintético cuando Supabase no está configurado.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

import kpd_motor_emparejamiento as kpd


class SupabaseNoConfigurado(RuntimeError):
    """Las credenciales no están disponibles en st.secrets."""


def _credenciales() -> tuple[str, str]:
    try:
        url = str(st.secrets["SUPABASE_URL"]).strip()
        key = str(st.secrets["SUPABASE_KEY"]).strip()
    except Exception as exc:
        raise SupabaseNoConfigurado(
            "Configura SUPABASE_URL y SUPABASE_KEY en .streamlit/secrets.toml para habilitar Supabase."
        ) from exc
    if not url or not key:
        raise SupabaseNoConfigurado("SUPABASE_URL y SUPABASE_KEY no pueden estar vacíos.")
    return url, key


def cliente_supabase():
    """Crea cliente bajo demanda; nunca expone las credenciales al usuario."""
    url, key = _credenciales()
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("Falta la dependencia supabase. Instala requirements.txt.") from exc
    return create_client(url, key)


def supabase_disponible() -> bool:
    try:
        _credenciales()
        return True
    except SupabaseNoConfigurado:
        return False


def _respuesta_data(respuesta) -> list[dict[str, Any]]:
    return list(getattr(respuesta, "data", None) or [])


def insertar_pareja(
    id_pareja: str, hospital_id: str, donante_abo: str, donante_hla,
    receptor_abo: str, receptor_hla, receptor_inaceptables, receptor_cpra: int,
    receptor_edad: int, *, advertir_compatibilidad: bool = True,
) -> dict[str, Any]:
    """Registra pareja activa; retorna la fila y si era compatible de origen."""
    id_pareja = id_pareja.strip()
    if not id_pareja:
        raise ValueError("El ID anónimo es obligatorio.")
    if donante_abo not in kpd.ABO_PUEDE_DONAR_A or receptor_abo not in kpd.ABO_PUEDE_DONAR_A:
        raise ValueError("Grupo ABO inválido.")
    listas = [set(donante_hla), set(receptor_hla), set(receptor_inaceptables)]
    if any(not valores.issubset(kpd.TODOS_HLA) for valores in listas):
        raise ValueError("Se incluyeron antígenos HLA fuera del catálogo.")
    if not 0 <= int(receptor_cpra) <= 100 or not 0 <= int(receptor_edad) <= 120:
        raise ValueError("cPRA o edad fuera de rango.")
    donante = kpd.Donante(donante_abo, frozenset(donante_hla))
    receptor = kpd.Receptor(receptor_abo, frozenset(receptor_hla), frozenset(receptor_inaceptables), int(receptor_cpra), int(receptor_edad))
    compatible = kpd.es_compatible(donante, receptor)
    if compatible and advertir_compatibilidad:
        st.warning("Esta pareja resulta compatible según el modelo ABO/HLA actual. Puedes registrarla de todos modos.")
    fila = {
        "id_pareja": id_pareja, "hospital_id": hospital_id,
        "donante_abo": donante_abo, "donante_hla": sorted(donante.hla),
        "receptor_abo": receptor_abo, "receptor_hla": sorted(receptor.hla),
        "receptor_inaceptables": sorted(receptor.inaceptables),
        "receptor_cpra": int(receptor_cpra), "receptor_edad": int(receptor_edad), "activa": True,
    }
    data = _respuesta_data(cliente_supabase().table("parejas").insert(fila).execute())
    return {**fila, "compatible_origen": compatible, **(data[0] if data else {})}


def insertar_altruista(id_altruista: str, hospital_id: str, donante_abo: str, donante_hla) -> dict[str, Any]:
    id_altruista = id_altruista.strip()
    if not id_altruista:
        raise ValueError("El ID anónimo es obligatorio.")
    if donante_abo not in kpd.ABO_PUEDE_DONAR_A or not set(donante_hla).issubset(kpd.TODOS_HLA):
        raise ValueError("Grupo ABO o antígenos HLA inválidos.")
    fila = {"id_altruista": id_altruista, "hospital_id": hospital_id, "donante_abo": donante_abo,
            "donante_hla": sorted(set(donante_hla)), "activo": True}
    data = _respuesta_data(cliente_supabase().table("altruistas").insert(fila).execute())
    return {**fila, **(data[0] if data else {})}


def obtener_pool_real() -> tuple[list[kpd.Pareja], list[kpd.DonanteAltruista], dict[str, str]]:
    """Descarga filas activas y devuelve parejas, altruistas y hospital_id por nodo."""
    client = cliente_supabase()
    rows_p = _respuesta_data(client.table("parejas").select("*").eq("activa", True).execute())
    rows_a = _respuesta_data(client.table("altruistas").select("*").eq("activo", True).execute())
    parejas: list[kpd.Pareja] = []
    altruistas: list[kpd.DonanteAltruista] = []
    ubicaciones: dict[str, str] = {}
    for row in rows_p:
        donante = kpd.Donante(str(row["donante_abo"]), frozenset(row.get("donante_hla") or []))
        receptor = kpd.Receptor(str(row["receptor_abo"]), frozenset(row.get("receptor_hla") or []),
                                frozenset(row.get("receptor_inaceptables") or []), int(row.get("receptor_cpra") or 0), int(row.get("receptor_edad") or 0))
        pareja = kpd.Pareja(str(row["id_pareja"]), donante, receptor)
        parejas.append(pareja)
        ubicaciones[pareja.id] = str(row["hospital_id"])
    for row in rows_a:
        donante = kpd.Donante(str(row["donante_abo"]), frozenset(row.get("donante_hla") or []))
        altruista = kpd.DonanteAltruista(str(row["id_altruista"]), donante)
        altruistas.append(altruista)
        ubicaciones[altruista.id] = str(row["hospital_id"])
    return parejas, altruistas, ubicaciones


def desactivar_registro(tabla: str, id_registro: str) -> None:
    """Da de baja lógica una fila; las tablas y columnas están en allowlist."""
    config = {"parejas": ("id_pareja", "activa"), "altruistas": ("id_altruista", "activo")}
    if tabla not in config:
        raise ValueError("Tabla no permitida; usa parejas o altruistas.")
    columna_id, columna_estado = config[tabla]
    resultado = cliente_supabase().table(tabla).update({columna_estado: False}).eq(columna_id, id_registro).execute()
    if not _respuesta_data(resultado):
        # PostgREST puede ocultar la fila cuando retorna representación mínima;
        # la operación se considera ejecutada si no lanzó excepción.
        return
