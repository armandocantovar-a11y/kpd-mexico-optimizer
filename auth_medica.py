"""Registro, autenticación y aprobación de médicos para el dashboard KPD.

Las sesiones se guardan por conexión Streamlit y se revalidan contra Supabase;
la autorización final se obtiene del perfil protegido por RLS, no de metadata
editable ni de un rol enviado desde el navegador.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import pandas as pd
import streamlit as st

import db_supabase as db
import logistica_geografica as geo


def _validar_clave_publica() -> None:
    """Impide usar claves privilegiadas que omiten RLS en el servidor."""
    try:
        key = str(st.secrets["SUPABASE_KEY"]).strip()
    except Exception as exc:
        raise RuntimeError("Falta SUPABASE_KEY en .streamlit/secrets.toml.") from exc
    if key.startswith("sb_secret_"):
        raise RuntimeError("Configura la clave publishable/anon de Supabase; nunca uses una clave secret/service_role en la app.")
    parts = key.split(".")
    if len(parts) == 3:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            claims = {}
        if claims.get("role") == "service_role":
            raise RuntimeError("La clave service_role omite RLS. Usa la clave anon/publishable en SUPABASE_KEY.")


def _estado_auth() -> None:
    st.session_state.setdefault("auth_access_token", None)
    st.session_state.setdefault("auth_refresh_token", None)
    st.session_state.setdefault("auth_user_id", None)
    st.session_state.setdefault("auth_correo", None)


def _limpiar_sesion() -> None:
    for key in ("auth_access_token", "auth_refresh_token", "auth_user_id", "auth_correo", "auth_perfil"):
        st.session_state.pop(key, None)


def _guardar_sesion(session: Any, user: Any) -> None:
    if session is None or user is None:
        raise RuntimeError("Supabase no devolvió una sesión autenticada.")
    st.session_state["auth_access_token"] = session.access_token
    st.session_state["auth_refresh_token"] = session.refresh_token
    st.session_state["auth_user_id"] = str(user.id)
    st.session_state["auth_correo"] = user.email


def _cliente_sesion():
    """Crea cliente por sesión Streamlit y renueva tokens sin compartirlos."""
    _estado_auth()
    access = st.session_state.get("auth_access_token")
    refresh = st.session_state.get("auth_refresh_token")
    if not access or not refresh:
        return None, None
    _validar_clave_publica()
    client = db.cliente_supabase()
    response = client.auth.set_session(access, refresh)
    session = getattr(response, "session", None) or response
    user_response = client.auth.get_user()
    user = getattr(user_response, "user", None)
    if user is None:
        raise RuntimeError("La sesión no pudo validarse con Supabase.")
    _guardar_sesion(session, user)
    return client, user


def _obtener_perfil(client, user_id: str) -> dict[str, Any]:
    response = (client.table("perfiles_medicos").select(
        "id,nombre_completo,correo,cedula_profesional,cedula_especialidad,hospital_id,estado,rol"
    ).eq("id", user_id).single().execute())
    if not response.data:
        raise RuntimeError("No se encontró el perfil médico asociado a esta cuenta.")
    return dict(response.data)


def registrar_medico(
    client, *, nombre_completo: str, correo: str, password: str,
    cedula_profesional: str, cedula_especialidad: str, hospital_id: int,
) -> tuple[Any, bool]:
    """Crea usuario de Auth; retorna usuario y si requiere confirmación email."""
    nombre_completo = nombre_completo.strip()
    correo = correo.strip().lower()
    cedula_profesional = cedula_profesional.strip()
    cedula_especialidad = cedula_especialidad.strip()
    if not all((nombre_completo, correo, cedula_profesional, cedula_especialidad)):
        raise ValueError("Completa todos los campos del registro.")
    if "@" not in correo:
        raise ValueError("Ingresa un correo válido.")
    if len(password) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres.")
    if int(hospital_id) < 1:
        raise ValueError("Selecciona un hospital aprobado del catálogo nacional.")
    response = client.auth.sign_up({
        "email": correo,
        "password": password,
        "options": {"data": {
            "nombre_completo": nombre_completo,
            "cedula_profesional": cedula_profesional,
            "cedula_especialidad": cedula_especialidad,
            "hospital_id": int(hospital_id),
        }},
    })
    user = getattr(response, "user", None)
    if user is None:
        raise RuntimeError("Supabase no confirmó la creación de la cuenta.")
    return user, getattr(response, "session", None) is None


def _vista_login_registro() -> None:
    st.title("Acceso para profesionales de salud")
    st.caption("El acceso al optimizador requiere validación manual de la cédula profesional.")
    modo = st.radio("Acceso", ["Iniciar sesión", "Solicitar acceso"], key="auth_modo")
    try:
        _validar_clave_publica()
        client = db.cliente_supabase()
    except Exception as exc:
        st.error(f"No se pudo conectar con Supabase: {exc}")
        return

    if modo == "Solicitar acceso":
        try:
            hospitales = db.obtener_hospitales_aprobados(client)
        except Exception:
            st.error("No se pudo cargar el catálogo de hospitales aprobados. Aplica primero supabase_hospitales_rls.sql.")
            return
        if not hospitales:
            st.error("No hay hospitales aprobados disponibles para el registro.")
            return
        with st.form("registro_medico", clear_on_submit=True):
            nombre = st.text_input("Nombre completo")
            correo = st.text_input("Correo institucional", type="email")
            cedula = st.text_input("Cédula profesional")
            cedula_esp = st.text_input("Cédula de especialidad")
            hospital_id = st.selectbox(
                "Hospital de adscripción", [int(h["id"]) for h in hospitales],
                format_func=lambda hid: next(f"{h['nombre']} — {h['ciudad']}" for h in hospitales if int(h["id"]) == hid),
            )
            password = st.text_input("Contraseña", type="password", help="Usa al menos 10 caracteres.")
            confirmacion = st.text_input("Confirmar contraseña", type="password")
            submitted = st.form_submit_button("Crear cuenta y solicitar validación", type="primary", width="stretch")
        if submitted:
            if password != confirmacion:
                st.error("Las contraseñas no coinciden.")
            else:
                try:
                    _, requiere_email = registrar_medico(
                        client, nombre_completo=nombre, correo=correo, password=password,
                        cedula_profesional=cedula, cedula_especialidad=cedula_esp,
                        hospital_id=hospital_id,
                    )
                    if requiere_email:
                        st.success("Solicitud recibida. Revisa tu correo para confirmar la cuenta. Después, el administrador validará tus cédulas.", title="Cuenta en revisión")
                    else:
                        st.success("Solicitud recibida. Tu cuenta permanecerá bloqueada hasta que un administrador valide tus cédulas.", title="Cuenta en revisión")
                except Exception as exc:
                    st.error(_mensaje_error_auth(exc, "No se pudo completar el registro."))
        return

    with st.form("login_medico"):
        correo = st.text_input("Correo", type="email")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Iniciar sesión", type="primary", width="stretch")
    if submitted:
        try:
            response = client.auth.sign_in_with_password({"email": correo.strip().lower(), "password": password})
            user = getattr(response, "user", None)
            session = getattr(response, "session", None)
            _guardar_sesion(session, user)
            st.rerun()
        except Exception as exc:
            _limpiar_sesion()
            st.error(_mensaje_error_auth(exc, "No se pudo iniciar sesión. Verifica tus datos y la confirmación del correo."))


def _mensaje_error_auth(exc: Exception, fallback: str) -> str:
    """Muestra errores comunes sin divulgar trazas ni credenciales."""
    message = str(exc).lower()
    if "already registered" in message or "duplicate key" in message or "cedula_profesional" in message:
        return "Ya existe una cuenta con ese correo o cédula profesional. Contacta al administrador si necesitas recuperar el acceso."
    if "invalid login credentials" in message or "invalid email or password" in message:
        return "Correo o contraseña incorrectos."
    return fallback


def cerrar_sesion() -> None:
    try:
        client, _ = _cliente_sesion()
        if client is not None:
            client.auth.sign_out()
    except Exception:
        # Limpieza local incluso si el endpoint de logout no está disponible.
        pass
    _limpiar_sesion()


def render_access_gate() -> dict[str, Any] | None:
    """Autentica y autoriza por perfil; solo retorna perfiles aprobados."""
    _estado_auth()
    if not st.session_state.get("auth_access_token"):
        _vista_login_registro()
        return None
    try:
        client, user = _cliente_sesion()
        perfil = _obtener_perfil(client, str(user.id))
        st.session_state["auth_perfil"] = perfil
    except Exception:
        _limpiar_sesion()
        st.warning("Tu sesión expiró o no se pudo validar. Inicia sesión de nuevo.")
        _vista_login_registro()
        return None

    estado = perfil.get("estado")
    if estado == "pendiente":
        st.title("Sala de espera")
        st.info("Tu cédula profesional está en proceso de validación. El dashboard se habilitará cuando el administrador apruebe tu solicitud.", title="Cédula en proceso de validación")
        render_hospital_request_form()
        if st.button("Cerrar sesión", key="logout_pendiente"):
            cerrar_sesion()
            st.rerun()
        return None
    if estado != "aprobado":
        st.title("Acceso no autorizado")
        st.error("La solicitud fue rechazada o el perfil no está habilitado. Contacta al administrador.")
        if st.button("Cerrar sesión", key="logout_rechazado"):
            cerrar_sesion()
            st.rerun()
        return None
    return perfil


def actualizar_estado_medico(client, medico_id: str, nuevo_estado: str) -> None:
    if nuevo_estado not in {"aprobado", "rechazado"}:
        raise ValueError("Estado de aprobación no permitido.")
    data = (client.table("perfiles_medicos").update({"estado": nuevo_estado})
            .eq("id", medico_id).eq("estado", "pendiente").select("id").execute().data)
    if not data:
        raise PermissionError("No se actualizó el perfil; verifica el rol admin y las políticas RLS.")


def render_hospital_request_form() -> None:
    """Permite a cualquier usuario autenticado solicitar un centro nuevo."""
    with st.expander("Solicitar alta de nuevo hospital"):
        with st.form("solicitar_hospital", clear_on_submit=True):
            nombre = st.text_input("Nombre del hospital")
            ciudad = st.text_input("Ciudad")
            col_lat, col_lon = st.columns(2)
            latitud = col_lat.number_input("Latitud", min_value=-90.0, max_value=90.0, value=19.4326, format="%.6f")
            longitud = col_lon.number_input("Longitud", min_value=-180.0, max_value=180.0, value=-99.1332, format="%.6f")
            hub = st.checkbox("Cuenta con hub aéreo")
            enviar = st.form_submit_button("Enviar solicitud", width="stretch")
        if enviar:
            try:
                db.solicitar_alta_hospital(nombre, ciudad, latitud, longitud, hub, client=db.cliente_usuario())
                st.success("La solicitud quedó pendiente de aprobación administrativa.")
            except Exception as exc:
                st.error(f"No se pudo solicitar el hospital: {exc}")


def _render_admin_hospitales(client) -> None:
    st.markdown("#### Hospitales pendientes")
    try:
        pendientes = db.obtener_hospitales_pendientes(client)
    except Exception:
        st.error("No fue posible cargar las solicitudes de hospitales.")
        return
    if not pendientes:
        st.caption("No hay hospitales pendientes.")
    else:
        st.dataframe(pd.DataFrame(pendientes), hide_index=True, width="stretch")
        for hospital in pendientes:
            with st.container(border=True):
                st.write(f"**{hospital['nombre']}** · {hospital['ciudad']} · ID {hospital['id']}")
                aprobar, rechazar = st.columns(2)
                if aprobar.button("Aprobar hospital", key=f"aprobar_hospital_{hospital['id']}", type="primary", width="stretch"):
                    try:
                        db.actualizar_estado_hospital(hospital["id"], "aprobado", client=client)
                        st.success("Hospital aprobado y visible en los catálogos.")
                        st.rerun()
                    except Exception:
                        st.error("No se pudo aprobar el hospital. Comprueba la política RLS.")
                if rechazar.button("Rechazar hospital", key=f"rechazar_hospital_{hospital['id']}", width="stretch"):
                    try:
                        db.actualizar_estado_hospital(hospital["id"], "rechazado", client=client)
                        st.success("Solicitud de hospital rechazada.")
                        st.rerun()
                    except Exception:
                        st.error("No se pudo rechazar el hospital. Comprueba la política RLS.")

    st.markdown("#### Gestión de hospitales aprobados / inactivos")
    try:
        centros = list(client.table("hospitales").select(
            "id,nombre,ciudad,estado,hub_aereo"
        ).in_("estado", ["aprobado", "inactivo"]).order("nombre").execute().data or [])
    except Exception:
        st.error("No fue posible cargar los hospitales para su gestión.")
        return
    if not centros:
        st.caption("No hay hospitales aprobados o inactivos.")
        return
    st.dataframe(pd.DataFrame(centros), hide_index=True, width="stretch")
    for hospital in centros:
        with st.container(border=True):
            estado_actual = hospital["estado"]
            accion = "Inactivar hospital" if estado_actual == "aprobado" else "Reactivar hospital"
            nuevo_estado = "inactivo" if estado_actual == "aprobado" else "aprobado"
            st.write(f"**{hospital['nombre']}** · {hospital['ciudad']} · {estado_actual}")
            if st.button(accion, key=f"cambiar_hospital_{hospital['id']}",
                         type="primary" if nuevo_estado == "inactivo" else "secondary", width="stretch"):
                try:
                    db.cambiar_estado_hospital(hospital["id"], nuevo_estado, client=client)
                    st.success("Estado del hospital actualizado.")
                    st.rerun()
                except Exception:
                    st.error("No se pudo cambiar el estado del hospital. Comprueba la política RLS y la migración.")


def render_admin_approval_panel(perfil: dict[str, Any]) -> None:
    """Lista solicitudes pendientes y ofrece acciones solo a administradores aprobados."""
    if perfil.get("rol") != "admin" or perfil.get("estado") != "aprobado":
        return
    st.subheader("Aprobación de cédulas")
    try:
        client, user = _cliente_sesion()
        if client is None or str(user.id) != str(perfil["id"]):
            st.error("No fue posible validar la sesión administrativa.")
            return
        response = (client.table("perfiles_medicos").select(
            "id,nombre_completo,correo,cedula_profesional,cedula_especialidad,hospital_id,estado"
        ).eq("estado", "pendiente").order("nombre_completo").execute())
        pendientes = list(response.data or [])
    except Exception:
        st.error("No fue posible cargar las solicitudes pendientes. Comprueba la conexión y las políticas RLS.")
        return
    _render_admin_hospitales(client)
    if not pendientes:
        st.success("No hay solicitudes pendientes.")
        return
    seguros = [{"Nombre":p["nombre_completo"], "Correo":p["correo"], "Cédula profesional":p["cedula_profesional"],
                "Cédula especialidad":p["cedula_especialidad"], "Hospital ID":p["hospital_id"]} for p in pendientes]
    st.dataframe(pd.DataFrame(seguros), hide_index=True, width="stretch")
    for solicitud in pendientes:
        with st.container(border=True):
            st.write(f"**{solicitud['nombre_completo']}** · {solicitud['correo']} · Cédula: {solicitud['cedula_profesional']}")
            col_aprobar, col_rechazar = st.columns(2)
            if col_aprobar.button("Aprobar", key=f"aprobar_{solicitud['id']}", type="primary", width="stretch"):
                try:
                    actualizar_estado_medico(client, solicitud["id"], "aprobado")
                    st.success("Cuenta aprobada.")
                    st.rerun()
                except Exception:
                    st.error("No se pudo aprobar la cuenta. Comprueba las políticas RLS.")
            if col_rechazar.button("Rechazar", key=f"rechazar_{solicitud['id']}", width="stretch"):
                try:
                    actualizar_estado_medico(client, solicitud["id"], "rechazado")
                    st.success("Solicitud rechazada.")
                    st.rerun()
                except Exception:
                    st.error("No se pudo rechazar la cuenta. Comprueba las políticas RLS.")
