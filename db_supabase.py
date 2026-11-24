"""Acceso opcional a Supabase para el registro KPD.

No abre conexión ni requiere secretos al importar. La aplicación conserva así
el modo sintético cuando Supabase no está configurado.
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

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
    if key.startswith("sb_secret_"):
        raise RuntimeError("La aplicación solo admite la clave anon/publishable; sb_secret/service_role omite RLS.")
    parts = key.split(".")
    if len(parts) == 3:
        try:
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            role = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii"))).get("role")
        except (ValueError, UnicodeError, json.JSONDecodeError):
            role = None
        if role == "service_role":
            raise RuntimeError("La aplicación solo admite la clave anon/publishable; service_role omite RLS.")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("Falta la dependencia supabase. Instala requirements.txt.") from exc
    return create_client(url, key)


def cliente_usuario():
    """Construye un cliente con la sesión del médico del Streamlit actual."""
    access = st.session_state.get("auth_access_token")
    refresh = st.session_state.get("auth_refresh_token")
    if not access or not refresh:
        raise PermissionError("Inicia sesión para realizar esta operación.")
    client = cliente_supabase()
    response = client.auth.set_session(access, refresh)
    session = getattr(response, "session", None) or response
    st.session_state["auth_access_token"] = session.access_token
    st.session_state["auth_refresh_token"] = session.refresh_token
    return client


def supabase_disponible() -> bool:
    try:
        _credenciales()
        return True
    except SupabaseNoConfigurado:
        return False


def _respuesta_data(respuesta) -> list[dict[str, Any]]:
    return list(getattr(respuesta, "data", None) or [])


def _meses_transcurridos(creado_en: Any, hoy: date | None = None) -> int:
    """Meses completos desde creado_en, usando la fecha local de México."""
    if not creado_en:
        return 0
    try:
        if isinstance(creado_en, datetime):
            fecha_inicio = (creado_en.astimezone(ZoneInfo("America/Mexico_City")).date()
                            if creado_en.tzinfo else creado_en.date())
        elif isinstance(creado_en, date):
            fecha_inicio = creado_en
        else:
            texto = str(creado_en).strip().replace("Z", "+00:00")
            try:
                fecha_inicio_dt = datetime.fromisoformat(texto)
                fecha_inicio = (fecha_inicio_dt.astimezone(ZoneInfo("America/Mexico_City")).date()
                                if fecha_inicio_dt.tzinfo else fecha_inicio_dt.date())
            except ValueError:
                fecha_inicio = date.fromisoformat(texto[:10])
        fecha_hoy = hoy or datetime.now(ZoneInfo("America/Mexico_City")).date()
        return max(0, (fecha_hoy.year - fecha_inicio.year) * 12 + fecha_hoy.month - fecha_inicio.month
                   - (fecha_hoy.day < fecha_inicio.day))
    except (TypeError, ValueError, OverflowError):
        return 0


def insertar_pareja(
    id_pareja: str, hospital_id: int, donante_abo: str, donante_hla,
    receptor_abo: str, receptor_hla, receptor_inaceptables, receptor_cpra: int,
    receptor_edad: int, *, meses_espera_previos: int = 0,
    advertir_compatibilidad: bool = True, client=None,
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
    if not 0 <= int(meses_espera_previos) <= 240:
        raise ValueError("Los meses previos de espera deben estar entre 0 y 240.")
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
        "receptor_cpra": int(receptor_cpra), "receptor_edad": int(receptor_edad),
        "meses_espera_previos": int(meses_espera_previos), "activa": True,
    }
    data = _respuesta_data((client or cliente_usuario()).table("parejas").insert(fila).execute())
    return {**fila, "compatible_origen": compatible, **(data[0] if data else {})}


def insertar_altruista(id_altruista: str, hospital_id: int, donante_abo: str, donante_hla, *, client=None) -> dict[str, Any]:
    id_altruista = id_altruista.strip()
    if not id_altruista:
        raise ValueError("El ID anónimo es obligatorio.")
    if donante_abo not in kpd.ABO_PUEDE_DONAR_A or not set(donante_hla).issubset(kpd.TODOS_HLA):
        raise ValueError("Grupo ABO o antígenos HLA inválidos.")
    fila = {"id_altruista": id_altruista, "hospital_id": hospital_id, "donante_abo": donante_abo,
            "donante_hla": sorted(set(donante_hla)), "activo": True}
    data = _respuesta_data((client or cliente_usuario()).table("altruistas").insert(fila).execute())
    return {**fila, **(data[0] if data else {})}


def obtener_pool_real(client=None) -> tuple[list[kpd.Pareja], list[kpd.DonanteAltruista], dict[str, str]]:
    """Descarga filas activas y devuelve parejas, altruistas y hospital_id por nodo."""
    client = client or cliente_usuario()
    rows_p = _respuesta_data(client.table("parejas").select("*").eq("estado", "activo").execute())
    rows_a = _respuesta_data(client.table("altruistas").select("*").eq("estado", "activo").execute())
    parejas: list[kpd.Pareja] = []
    altruistas: list[kpd.DonanteAltruista] = []
    ubicaciones: dict[str, str] = {}
    for row in rows_p:
        donante = kpd.Donante(str(row["donante_abo"]), frozenset(row.get("donante_hla") or []))
        receptor = kpd.Receptor(str(row["receptor_abo"]), frozenset(row.get("receptor_hla") or []),
                                frozenset(row.get("receptor_inaceptables") or []), int(row.get("receptor_cpra") or 0), int(row.get("receptor_edad") or 0))
        espera_previa = int(row.get("meses_espera_previos") or 0)
        espera_total = espera_previa + _meses_transcurridos(row.get("creado_en"))
        pareja = kpd.Pareja(str(row["id_pareja"]), donante, receptor, espera_total)
        parejas.append(pareja)
        ubicaciones[pareja.id] = str(row["hospital_id"])
    for row in rows_a:
        donante = kpd.Donante(str(row["donante_abo"]), frozenset(row.get("donante_hla") or []))
        altruista = kpd.DonanteAltruista(str(row["id_altruista"]), donante)
        altruistas.append(altruista)
        ubicaciones[altruista.id] = str(row["hospital_id"])
    return parejas, altruistas, ubicaciones


def obtener_historial_registros(client=None) -> list[dict[str, Any]]:
    """Devuelve parejas/altruistas trasplantados o dados de baja, visibles por RLS."""
    client = client or cliente_usuario()
    historial = []
    for tabla, columna_id, tipo in (("parejas", "id_pareja", "Pareja"),
                                    ("altruistas", "id_altruista", "Altruista")):
        filas = _respuesta_data(client.table(tabla).select("*")
            .in_("estado", ["trasplantado", "baja"]).order("actualizado_en", desc=True).execute())
        historial.extend({**fila, "tipo": tipo, "id": fila.get(columna_id)} for fila in filas)
    return sorted(historial, key=lambda fila: str(fila.get("actualizado_en") or ""), reverse=True)


def _dar_de_baja(tabla: str, columna_id: str, registro_id: str, motivo: str | None, *, client=None) -> None:
    motivo = (motivo or "").strip()[:500] or None
    data = _respuesta_data((client or cliente_usuario()).table(tabla)
        .update({"estado": "baja", "motivo_baja": motivo})
        .eq(columna_id, str(registro_id)).eq("estado", "activo").select(columna_id).execute())
    if not data:
        raise PermissionError("No se actualizó el registro; puede no estar activo o la política RLS lo impide.")


def dar_de_baja_pareja(pareja_id: str, motivo: str | None = None, *, client=None) -> None:
    _dar_de_baja("parejas", "id_pareja", pareja_id, motivo, client=client)


def dar_de_baja_altruista(altruista_id: str, motivo: str | None = None, *, client=None) -> None:
    _dar_de_baja("altruistas", "id_altruista", altruista_id, motivo, client=client)


def confirmar_trasplante_lote(pareja_ids, altruista_ids=(), *, client=None) -> dict[str, Any]:
    """Confirma de forma atómica parejas y donantes de una estructura (solo admin)."""
    parejas = list(dict.fromkeys(str(i) for i in pareja_ids))
    altruistas = list(dict.fromkeys(str(i) for i in altruista_ids))
    if not parejas and not altruistas:
        raise ValueError("No hay registros en la estructura seleccionada.")
    respuesta = (client or cliente_usuario()).rpc("confirmar_trasplante_lote", {
        "p_pareja_ids": parejas, "p_altruista_ids": altruistas,
    }).execute()
    datos = getattr(respuesta, "data", None)
    return datos if isinstance(datos, dict) else {"resultado": datos}


def obtener_hospitales_aprobados(client=None) -> list[dict[str, Any]]:
    """Lee el catálogo público aprobado; las solicitudes pendientes no se filtran al público."""
    client = client or cliente_supabase()
    return _respuesta_data(client.table("hospitales").select(
        "id,nombre,ciudad,latitud,longitud,hub_aereo,estado"
    ).eq("estado", "aprobado").order("id").execute())


def solicitar_alta_hospital(nombre: str, ciudad: str, latitud: float, longitud: float,
                            hub_aereo: bool, *, client=None) -> dict[str, Any]:
    """Inserta solicitud omitiendo estado; PostgreSQL fija pendiente por default/RLS."""
    nombre, ciudad = nombre.strip(), ciudad.strip()
    if not nombre or not ciudad:
        raise ValueError("El nombre y la ciudad son obligatorios.")
    latitud, longitud = float(latitud), float(longitud)
    if not -90 <= latitud <= 90 or not -180 <= longitud <= 180:
        raise ValueError("Latitud o longitud fuera de rango.")
    fila = {"nombre": nombre, "ciudad": ciudad, "latitud": latitud,
            "longitud": longitud, "hub_aereo": bool(hub_aereo)}
    data = _respuesta_data((client or cliente_usuario()).table("hospitales").insert(fila).execute())
    return data[0] if data else fila


def obtener_hospitales_pendientes(client=None) -> list[dict[str, Any]]:
    client = client or cliente_usuario()
    return _respuesta_data(client.table("hospitales").select(
        "id,nombre,ciudad,latitud,longitud,hub_aereo,estado,creado_en"
    ).eq("estado", "pendiente").order("creado_en").execute())


def actualizar_estado_hospital(hospital_id: int, nuevo_estado: str, *, client=None) -> None:
    if nuevo_estado not in {"aprobado", "rechazado", "inactivo"}:
        raise ValueError("Estado de hospital no permitido.")
    data = _respuesta_data((client or cliente_usuario()).table("hospitales")
        .update({"estado": nuevo_estado}).eq("id", int(hospital_id))
        .eq("estado", "pendiente").select("id").execute())
    if not data:
        raise PermissionError("No se actualizó el hospital; verifica su estado actual y que la cuenta sea admin.")


def cambiar_estado_hospital(hospital_id: int, nuevo_estado: str, *, client=None) -> None:
    """Activa o inactiva un hospital aprobado; la política RLS valida rol admin."""
    if nuevo_estado not in {"aprobado", "inactivo"}:
        raise ValueError("El hospital solo puede activarse o inactivarse desde esta acción.")
    origen = "inactivo" if nuevo_estado == "aprobado" else "aprobado"
    data = _respuesta_data((client or cliente_usuario()).table("hospitales")
        .update({"estado": nuevo_estado}).eq("id", int(hospital_id))
        .eq("estado", origen).select("id").execute())
    if not data:
        raise PermissionError("No se cambió el hospital; revisa su estado y los permisos administrativos.")
