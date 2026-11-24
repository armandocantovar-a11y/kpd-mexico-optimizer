"""Dashboard Streamlit del optimizador KPD, con simulación o pool Supabase."""
from __future__ import annotations

import time

import pandas as pd
import pydeck as pdk
import streamlit as st

import benchmark_nacional as bench
import auth_medica as auth_ui
import db_supabase as db
import kpd_motor_emparejamiento as kpd
import logistica_geografica as geo


st.set_page_config(page_title="KPD México | Optimización", layout="wide")
perfil_actual = auth_ui.render_access_gate()
if perfil_actual is None:
    st.stop()

st.title("Optimización KPD con logística geográfica")
st.caption("Prototipo de investigación. Las compatibilidades y estimaciones logísticas no sustituyen la evaluación clínica ni los protocolos hospitalarios.")
auth_ui.render_admin_approval_panel(perfil_actual)

with st.sidebar:
    st.caption(f"Sesión: {perfil_actual['nombre_completo']} · {perfil_actual['rol']}")
    if st.button("Cerrar sesión", key="logout_dashboard", width="stretch"):
        auth_ui.cerrar_sesion()
        st.rerun()
    st.header("Fuente de datos")
    fuente = st.radio("Fuente de datos", ["Simulación Sintética", "Base de Datos Real (Supabase)"], label_visibility="collapsed")
    if fuente == "Simulación Sintética":
        st.header("Escenario sintético")
        n = st.slider("Parejas en espera", 50, 1000, 250, 50)
        n_alt = st.slider("Donantes altruistas", 0, 15, 3)
    else:
        n, n_alt = None, None
    st.header("Optimización")
    kmax = st.selectbox("Ciclos kₘₐₓ", [2, 3], index=1)
    lmax = st.slider("Cadenas Lₘₐₓ (parejas)", 2, 5, 4)
    max_cit_horas = st.slider("Isquemia fría máxima (horas)", 1.0, 12.0, 4.0, 0.5)
    lam = st.slider("Penalización geográfica λ", 0.0, 2.0, 0.25, 0.05)
    vista = st.radio("Vista del mapa", ["República Mexicana", "Valle de México"])
    ejecutar = st.button("Ejecutar optimización", type="primary", width="stretch")

tabs = st.tabs(["Mapa y Optimización", "Registro Clínico (Supabase)", "Hospitales"], on_change="rerun")


def crear_resultado(parejas, altruistas, ubic, centros, max_cit_min, peso_geo, k_max, l_max, exacto=False):
    """Construye, filtra y resuelve; funciona también con pools de tamaño cero."""
    G = bench.construir_grafo_rapido(parejas, altruistas)
    if ubic:
        desconocidos = sorted({h for h in ubic.values() if h not in centros})
        if desconocidos:
            raise ValueError("Hay hospital_id sin hospital aprobado en el catálogo: " + ", ".join(desconocidos))
    geo.filtrar_y_ponderar_grafo(G, ubic, max_cit_min, peso_geo, centros)
    # Enumeración exacta para pools pequeños; poda acotada a escala nacional.
    pequeno = exacto and (len(parejas) + len(altruistas) <= 8)
    if pequeno:
        ciclos, trunc_c = kpd.enumerar_ciclos(G, k_max), False
        cadenas, trunc_h = (kpd.enumerar_cadenas(G, l_max), False) if altruistas else ([], False)
    else:
        ciclos, trunc_c = bench.enumerar_ciclos_acotado(G, k_max)
        cadenas, trunc_h = bench.enumerar_cadenas_acotado(G, l_max) if altruistas else ([], False)
    t0 = time.perf_counter()
    solucion = kpd.resolver_ilp(G, ciclos, cadenas, limite_s=bench.LIMITE_SOLVER_S, gap_rel=0.0 if pequeno else bench.GAP_RELATIVO)
    return {"parejas": parejas, "altruistas": altruistas, "grafo": G, "ubicaciones": ubic, "centros": centros,
            "solucion": solucion, "segundos": time.perf_counter()-t0,
            "truncado": trunc_c or trunc_h, "candidatos": (len(ciclos), len(cadenas))}


def ejecutar_optimizacion():
    if fuente == "Simulación Sintética":
        parejas, _ = bench.generar_poblacion_nacional(n, bench.SEMILLA)
        altruistas = bench.generar_altruistas_nacionales(n_alt, bench.SEMILLA)
        ubic = geo.asignar_centros(parejas, altruistas, bench.SEMILLA)
        centros = geo.CENTROS_POR_ID
        resultado = crear_resultado(parejas, altruistas, ubic, centros, round(max_cit_horas*60), lam, kmax, lmax)
        resultado["fuente"] = fuente
        resultado["config"] = (n, n_alt, kmax, lmax, max_cit_horas, lam)
    else:
        client = db.cliente_usuario()
        hospitales = db.obtener_hospitales_aprobados(client)
        centros = geo.catalogo_desde_hospitales(hospitales)
        parejas, altruistas, ubic = db.obtener_pool_real(client)
        resultado = crear_resultado(parejas, altruistas, ubic, centros, round(max_cit_horas*60), lam, kmax, lmax, exacto=True)
        resultado["fuente"] = fuente
        resultado["config"] = (kmax, lmax, max_cit_horas, lam)
    st.session_state["resultado_kpd"] = resultado


def selector_hospital_clinico(hospitales, perfil, key: str) -> int:
    """Admin elige centro; cada médico queda fijado a su hospital de perfil."""
    if perfil.get("rol") == "admin":
        opciones = [int(h["id"]) for h in hospitales]
        if not opciones:
            st.warning("No hay hospitales aprobados disponibles.")
            return 0
        por_id = {int(h["id"]): h for h in hospitales}
        return st.selectbox("Hospital", opciones,
                            format_func=lambda hid: f"{por_id[hid]['nombre']} — {por_id[hid]['ciudad']}", key=key)
    hid = int(perfil["hospital_id"])
    hospital = next((h for h in hospitales if int(h["id"]) == hid), None)
    nombre = f"{hospital['nombre']} — {hospital['ciudad']}" if hospital else f"Hospital ID {hid} (sin aprobación vigente)"
    st.text_input("Hospital asignado a tu perfil", value=nombre, disabled=True, key=key)
    return hid


if ejecutar:
    try:
        with st.spinner("Preparando el pool y resolviendo la asignación…"):
            ejecutar_optimizacion()
    except db.SupabaseNoConfigurado as exc:
        st.session_state.pop("resultado_kpd", None)
        st.sidebar.error(str(exc))
    except Exception as exc:
        st.sidebar.error(f"No se pudo ejecutar la optimización: {exc}")

firma_actual = ((n, n_alt, kmax, lmax, max_cit_horas, lam) if fuente == "Simulación Sintética"
                else (kmax, lmax, max_cit_horas, lam))
resultado = st.session_state.get("resultado_kpd")
if resultado and (resultado.get("fuente") != fuente or resultado.get("config") != firma_actual):
    resultado = None


with tabs[0]:
    st.subheader("Asignación óptima")
    if fuente == "Base de Datos Real (Supabase)" and not db.supabase_disponible():
        st.info("Supabase aún no está configurado. Añade SUPABASE_URL y SUPABASE_KEY a `.streamlit/secrets.toml`; la simulación sintética seguirá disponible.")
    if resultado is None:
        st.info("Ajusta los parámetros y pulsa «Ejecutar optimización» para ver el resultado.")
    else:
        parejas = resultado["parejas"]
        altruistas = resultado["altruistas"]
        G = resultado["grafo"]
        ubic = resultado["ubicaciones"]
        sol = resultado["solucion"]
        centros = resultado["centros"]
        arcos: list[dict] = []

        def agregar_arco(u, v, tipo):
            cu, cv = centros[ubic[u]], centros[ubic[v]]
            rec = G.nodes[v]["pareja"].receptor
            clinica = "; ".join(x for x, ok in (("hipersensibilizado", rec.cpra >= 80), ("pediátrico", rec.pediatrico)) if ok) or "sin etiqueta especial"
            color = [16,185,129] if tipo == "Cadena NEAD" else [6,182,212]
            arcos.append({"origen_lon":cu["lon"], "origen_lat":cu["lat"], "destino_lon":cv["lon"], "destino_lat":cv["lat"],
                          "color":color, "tooltip":f"{cu['nombre']} → {cv['nombre']} | {geo.calcular_tiempo_isquemia_min(ubic[u],ubic[v],centros)} min | {tipo} | cPRA {rec.cpra}% | {clinica}"})

        for ciclo in sol.ciclos:
            for i, u in enumerate(ciclo):
                agregar_arco(u, ciclo[(i+1) % len(ciclo)], "Ciclo cerrado")
        for cadena in sol.cadenas:
            for u, v in zip(cadena.nodos, cadena.nodos[1:]):
                agregar_arco(u, v, "Cadena NEAD")
            u = cadena.nodos[-1]
            centro = centros[ubic[u]]
            arcos.append({"origen_lon":centro["lon"], "origen_lat":centro["lat"], "destino_lon":centro["lon"]+.08, "destino_lat":centro["lat"]+.04,
                          "color":[245,158,11], "tooltip":f"{centro['nombre']} → Lista de espera | cierre de cadena"})

        ids_tx = sol.ids_parejas_trasplantadas()
        por_hospital = {hid: 0 for hid in centros}
        for pareja in parejas:
            if pareja.id in ids_tx and ubic[pareja.id] in por_hospital:
                por_hospital[ubic[pareja.id]] += 1
        # En Supabase los hospital_id deben corresponder al catálogo geográfico.
        hospitales_usados = sorted(set(ubic.values()))
        filas_nodos = [{**centros[h], "trasplantes": por_hospital.get(h,0),
                        "radio": max(3500, por_hospital.get(h,0)*1800),
                        "tooltip":f"{centros[h]['nombre']} · {centros[h]['ciudad']} · {por_hospital.get(h,0)} trasplantes"}
                       for h in hospitales_usados]
        nodes = pd.DataFrame(filas_nodos, columns=["id","nombre","ciudad","lat","lon","hub_aereo","trasplantes","radio","tooltip"])
        datos_arcos = pd.DataFrame(arcos, columns=["origen_lon","origen_lat","destino_lon","destino_lat","color","tooltip"])
        view = {"latitude":23.5,"longitude":-102.0,"zoom":4.2,"pitch":42} if vista == "República Mexicana" else {"latitude":19.39,"longitude":-99.14,"zoom":9.4,"pitch":35}
        layers = [pdk.Layer("ScatterplotLayer", data=nodes, get_position="[lon, lat]", get_radius="radio", get_fill_color=[30,90,180,180], pickable=True, auto_highlight=True),
                  pdk.Layer("ArcLayer", data=datos_arcos, get_source_position="[origen_lon, origen_lat]", get_target_position="[destino_lon, destino_lat]", get_source_color="color", get_target_color="color", get_width=5, pickable=True)]
        st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(**view), tooltip={"text":"{tooltip}"},
                                 map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"), width="stretch")

        hiper = [p for p in parejas if p.receptor.hipersensibilizado]
        hiper_tx = sum(p.id in ids_tx for p in hiper)
        edges = []
        for c in sol.ciclos:
            edges.extend((u,c[(i+1)%len(c)]) for i,u in enumerate(c))
        for ch in sol.cadenas:
            edges.extend(zip(ch.nodos,ch.nodos[1:]))
        cit = [G[u][v].get("cit_min",0) for u,v in edges]
        local = sum(centros[ubic[u]]["ciudad"] == centros[ubic[v]]["ciudad"] for u,v in edges)
        cols = st.columns(5)
        cols[0].metric("Cobertura de parejas", f"{len(ids_tx)}/{len(parejas)}", f"{len(ids_tx)/len(parejas):.1%}" if parejas else "Pool vacío")
        cols[1].metric("Cobertura cPRA ≥80%", f"{hiper_tx}/{len(hiper)}", f"{hiper_tx/len(hiper):.1%}" if hiper else "Sin casos")
        cols[2].metric("Traslados locales", f"{local}/{len(edges)}", f"{local/len(edges):.1%}" if edges else "—")
        cols[3].metric("CIT promedio", f"{sum(cit)/len(cit):.0f} min" if cit else "—")
        cols[4].metric("Solver HiGHS", f"{resultado['segundos']:.2f} s", "Óptimo" if sol.optimo else "Solución factible")
        if not parejas and not altruistas:
            st.info("El pool está vacío. Registra participantes en la pestaña clínica o activa registros existentes.")
        if resultado["truncado"]:
            st.warning("La enumeración acotada alcanzó su límite de candidatos; el resultado optimiza el conjunto enumerado.")

        filas = []
        def fila_tx(u, v, tipo):
            rec = G.nodes[v]["pareja"].receptor
            filas.append({"origen_id":u,"destino_id":v,"hospital_origen":centros[ubic[u]]["nombre"],"hospital_destino":centros[ubic[v]]["nombre"],
                          "cit_min":G[u][v].get("cit_min",0),"estructura":tipo,"cpra":rec.cpra,
                          "hipersensibilizado":rec.hipersensibilizado,"pediatrico":rec.pediatrico})
        for c in sol.ciclos:
            for i,u in enumerate(c): fila_tx(u,c[(i+1)%len(c)],"Ciclo cerrado")
        for ch in sol.cadenas:
            for u,v in zip(ch.nodos,ch.nodos[1:]): fila_tx(u,v,"Cadena NEAD")
        tabla = pd.DataFrame(filas, columns=["origen_id","destino_id","hospital_origen","hospital_destino","cit_min","estructura","cpra","hipersensibilizado","pediatrico"])
        st.dataframe(tabla, hide_index=True, width="stretch")
        st.download_button("Descargar trasplantes CSV", tabla.to_csv(index=False).encode("utf-8-sig"), "trasplantes_kpd.csv", "text/csv")


with tabs[1]:
    st.subheader("Registro Clínico")
    st.warning("Privacidad (LFPDPPP): registra únicamente IDs anónimos hospitalarios. No ingreses nombres, CURP, teléfonos, domicilios ni otros identificadores directos.")
    hospitales_db = st.session_state.get("catalogo_hospitales_aprobados", [])
    if tabs[1].open:
        try:
            hospitales_db = db.obtener_hospitales_aprobados()
            st.session_state["catalogo_hospitales_aprobados"] = hospitales_db
        except Exception as exc:
            st.error(f"No se pudo cargar el catálogo aprobado de hospitales: {exc}")

    st.markdown("#### Registrar pareja incompatible")
    with st.form("form_pareja", clear_on_submit=True):
        id_p = st.text_input("ID anónimo de pareja")
        hospital_p_id = selector_hospital_clinico(hospitales_db, perfil_actual, "hospital_p")
        a,b = st.columns(2)
        abo_d = a.selectbox("ABO del donante", list(kpd.ABO_PUEDE_DONAR_A), key="abo_d")
        hla_d = a.multiselect("HLA del donante", kpd.TODOS_HLA, key="hla_d")
        abo_r = b.selectbox("ABO del receptor", list(kpd.ABO_PUEDE_DONAR_A), key="abo_r")
        hla_r = b.multiselect("HLA propio del receptor", kpd.TODOS_HLA, key="hla_r")
        inac = st.multiselect("Antígenos inaceptables del receptor", kpd.TODOS_HLA, key="inac")
        a,b = st.columns(2)
        cpra = a.slider("cPRA (%)", 0, 100, 0)
        edad = b.number_input("Edad del receptor", min_value=0, max_value=120, value=40)
        enviar_p = st.form_submit_button("Guardar pareja")
    if enviar_p:
        try:
            db.insertar_pareja(id_p, hospital_p_id, abo_d, hla_d, abo_r, hla_r, inac, cpra, int(edad), client=db.cliente_usuario())
            st.success("Pareja guardada como activa.")
        except Exception as exc:
            st.error(f"No se pudo guardar la pareja: {exc}")

    st.markdown("#### Registrar donante altruista")
    with st.form("form_altruista", clear_on_submit=True):
        id_a = st.text_input("ID anónimo de donante altruista")
        hospital_a_id = selector_hospital_clinico(hospitales_db, perfil_actual, "hospital_a")
        abo_a = st.selectbox("ABO del donante", list(kpd.ABO_PUEDE_DONAR_A), key="abo_a")
        hla_a = st.multiselect("HLA del donante", kpd.TODOS_HLA, key="hla_a")
        enviar_a = st.form_submit_button("Guardar altruista")
    if enviar_a:
        try:
            db.insertar_altruista(id_a, hospital_a_id, abo_a, hla_a, client=db.cliente_usuario())
            st.success("Donante altruista guardado como activo.")
        except Exception as exc:
            st.error(f"No se pudo guardar el altruista: {exc}")

    st.markdown("#### Pool actualmente activo")
    recargar = st.button("Recargar pool activo", key="recargar_pool")
    if db.supabase_disponible() and tabs[1].open:
        try:
            parejas_db, altruistas_db, ubic_db = db.obtener_pool_real()
            st.session_state["pool_clinico_visible"] = (parejas_db, altruistas_db, ubic_db)
            filas_pool = [{"tipo":"Pareja", "id":p.id, "hospital_id":ubic_db[p.id], "ABO donante":p.donante.abo,
                           "ABO receptor":p.receptor.abo, "cPRA":p.receptor.cpra, "edad":p.receptor.edad, "activo":True} for p in parejas_db]
            filas_pool.extend({"tipo":"Altruista", "id":a.id, "hospital_id":ubic_db[a.id], "ABO donante":a.donante.abo,
                               "ABO receptor":"—", "cPRA":"—", "edad":"—", "activo":True} for a in altruistas_db)
            st.dataframe(pd.DataFrame(filas_pool, columns=["tipo","id","hospital_id","ABO donante","ABO receptor","cPRA","edad","activo"]), hide_index=True, width="stretch")
            if perfil_actual.get("rol") == "admin":
                filas_gestion = filas_pool
            else:
                hospital_medico = str(perfil_actual["hospital_id"])
                filas_gestion = [r for r in filas_pool if str(r["hospital_id"]) == hospital_medico]
            if filas_gestion:
                opcion = st.selectbox("Registro de tu hospital para dar de baja", filas_gestion, format_func=lambda r:f"{r['tipo']} · {r['id']}")
                if st.button("Marcar como inactivo", key="desactivar_registro"):
                    tabla = "parejas" if opcion["tipo"] == "Pareja" else "altruistas"
                    db.desactivar_registro(tabla, opcion["id"])
                    st.success("Registro marcado como inactivo.")
                    st.rerun()
            elif not filas_pool:
                st.info("No hay parejas ni donantes altruistas activos en Supabase.")
            else:
                st.caption("No hay registros de tu hospital disponibles para gestionar.")
        except Exception as exc:
            st.error(f"No se pudo leer el pool activo de Supabase: {exc}")
    elif db.supabase_disponible() and st.session_state.get("pool_clinico_visible"):
        st.caption("Vista del pool guardada de la última consulta. Abre esta pestaña para actualizarla.")
        parejas_db, altruistas_db, ubic_db = st.session_state["pool_clinico_visible"]
        filas_pool = [{"tipo":"Pareja", "id":p.id, "hospital_id":ubic_db[p.id], "ABO donante":p.donante.abo,
                       "ABO receptor":p.receptor.abo, "cPRA":p.receptor.cpra, "edad":p.receptor.edad, "activo":True} for p in parejas_db]
        filas_pool.extend({"tipo":"Altruista", "id":a.id, "hospital_id":ubic_db[a.id], "ABO donante":a.donante.abo,
                           "ABO receptor":"—", "cPRA":"—", "edad":"—", "activo":True} for a in altruistas_db)
        st.dataframe(pd.DataFrame(filas_pool, columns=["tipo","id","hospital_id","ABO donante","ABO receptor","cPRA","edad","activo"]), hide_index=True, width="stretch")
    else:
        st.caption("El pool se mostrará aquí cuando Supabase esté configurado.")

with tabs[2]:
    st.subheader("Catálogo de hospitales")
    hospitales_catalogo = st.session_state.get("catalogo_hospitales_aprobados", [])
    if tabs[2].open:
        try:
            hospitales_catalogo = db.obtener_hospitales_aprobados()
            st.session_state["catalogo_hospitales_aprobados"] = hospitales_catalogo
        except Exception as exc:
            st.error(f"No se pudo cargar el catálogo: {exc}")
    st.dataframe(pd.DataFrame(hospitales_catalogo, columns=["id","nombre","ciudad","latitud","longitud","hub_aereo"]),
                 hide_index=True, width="stretch")
    auth_ui.render_hospital_request_form()
