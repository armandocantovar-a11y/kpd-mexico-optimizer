-- KPD México v3.1: catálogo de hospitales y aislamiento por hospital.
-- Ejecutar una vez en Supabase SQL Editor sobre el esquema existente.

do $$ begin
    create type public.estado_hospital as enum ('pendiente', 'aprobado', 'rechazado');
exception when duplicate_object then null;
end $$;

create table if not exists public.hospitales (
    id serial primary key,
    nombre text unique not null,
    ciudad text not null,
    latitud double precision not null,
    longitud double precision not null,
    hub_aereo boolean not null default false,
    estado public.estado_hospital not null default 'pendiente',
    creado_en timestamptz not null default now(),
    constraint hospitales_latitud_rango check (latitud between -90 and 90),
    constraint hospitales_longitud_rango check (longitud between -180 and 180)
);

-- En una tabla nueva estos IDs seriales quedan del 1 al 12, conservando el
-- mapeo hospital_id 1-12 usado por los perfiles médicos existentes.
insert into public.hospitales (id,nombre,ciudad,latitud,longitud,hub_aereo,estado) values
 (1,'CMN Siglo XXI', 'Ciudad de México', 19.4067, -99.1558, true, 'aprobado'),
 (2,'CMN La Raza', 'Ciudad de México', 19.4668, -99.1365, true, 'aprobado'),
 (3,'INCMNSZ Nutrición', 'Ciudad de México', 19.2850, -99.1630, true, 'aprobado'),
 (4,'CMNO (UMAE)', 'Guadalajara', 20.6736, -103.3440, true, 'aprobado'),
 (5,'UMAE Hospital de Especialidades 25', 'Monterrey', 25.6866, -100.3161, true, 'aprobado'),
 (6,'UMAE Hospital de Especialidades Manuel Ávila Camacho', 'Puebla', 19.0414, -98.2063, false, 'aprobado'),
 (7,'Centro Médico Nacional del Bajío', 'León', 21.1220, -101.6820, false, 'aprobado'),
 (8,'Hospital Regional de Alta Especialidad de la Península de Yucatán', 'Mérida', 20.9674, -89.5926, true, 'aprobado'),
 (9,'Hospital General de Tijuana', 'Tijuana', 32.5149, -117.0382, true, 'aprobado'),
 (10,'Hospital Privado San José de Hermosillo', 'Hermosillo', 29.0729, -110.9559, true, 'aprobado'),
 (11,'CMN Adolfo Ruiz Cortines', 'Veracruz', 19.1738, -96.1342, false, 'aprobado'),
 (12,'Hospital de Especialidades No. 50', 'San Luis Potosí', 22.1511, -100.9760, false, 'aprobado')
on conflict (id) do nothing;

-- Alinea la secuencia serial para los nuevos hospitales solicitados.
select setval(pg_get_serial_sequence('public.hospitales','id'),
              (select coalesce(max(id),1) from public.hospitales), true);

-- Si una ejecución parcial previa dejó otra tabla de hospitales, no se reasignan
-- los IDs existentes. Revisa los IDs en Supabase antes de migrar perfiles/datos.
alter table public.perfiles_medicos
    drop constraint if exists perfiles_medicos_hospital_id_check;

-- La v3.0 guardaba slugs de geo.CENTROS en las tablas clínicas. Migra dichos
-- slugs al ID serial correspondiente; valores desconocidos producen error en vez
-- de asignar silenciosamente el registro a un hospital incorrecto.
alter table public.parejas add column if not exists hospital_id integer;
alter table public.altruistas add column if not exists hospital_id integer;

do $$
declare
    v_tipo text;
    v_fk record;
begin
    -- Quitar FK anteriores de hospital_id antes de cambiar su tipo.
    for v_fk in
        select c.conname, c.conrelid::regclass as tabla
        from pg_constraint c
        join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
        where c.contype = 'f'
          and c.conrelid in ('public.parejas'::regclass, 'public.altruistas'::regclass)
          and a.attname = 'hospital_id'
    loop
        execute format('alter table %s drop constraint %I', v_fk.tabla, v_fk.conname);
    end loop;

    alter table public.parejas alter column hospital_id drop default;
    alter table public.altruistas alter column hospital_id drop default;

    select data_type into v_tipo from information_schema.columns
      where table_schema='public' and table_name='parejas' and column_name='hospital_id';
    if v_tipo is distinct from 'integer' then
        alter table public.parejas alter column hospital_id type integer using
            case trim(hospital_id::text)
                when 'cmn_siglo_xxi' then 1 when 'cmn_la_raza' then 2
                when 'incmnsz' then 3 when 'cmno' then 4 when 'um_a_nl' then 5
                when 'umae_puebla' then 6 when 'h Puebla' then 6 when 'cmn_bajio' then 7
                when 'h_juarez_merida' then 8 when 'h_general_tijuana' then 9
                when 'h_privado_sonora' then 10 when 'h_general_hermosillo' then 10
                when 'cmn_veracruz' then 11 when 'h_veracruz' then 11
                when 'he_slp' then 12 when 'h_central_slp' then 12 when '' then null
                else trim(hospital_id::text)::integer
            end;
    end if;

    select data_type into v_tipo from information_schema.columns
      where table_schema='public' and table_name='altruistas' and column_name='hospital_id';
    if v_tipo is distinct from 'integer' then
        alter table public.altruistas alter column hospital_id type integer using
            case trim(hospital_id::text)
                when 'cmn_siglo_xxi' then 1 when 'cmn_la_raza' then 2
                when 'incmnsz' then 3 when 'cmno' then 4 when 'um_a_nl' then 5
                when 'umae_puebla' then 6 when 'h Puebla' then 6 when 'cmn_bajio' then 7
                when 'h_juarez_merida' then 8 when 'h_general_tijuana' then 9
                when 'h_privado_sonora' then 10 when 'h_general_hermosillo' then 10
                when 'cmn_veracruz' then 11 when 'h_veracruz' then 11
                when 'he_slp' then 12 when 'h_central_slp' then 12 when '' then null
                else trim(hospital_id::text)::integer
            end;
    end if;
end $$;

-- Perfiles ya usaban IDs enteros 1-12. La FK se agrega después del seed.
do $$ begin
    if not exists (select 1 from pg_constraint where conname='perfiles_medicos_hospital_id_fkey'
                   and conrelid='public.perfiles_medicos'::regclass) then
        alter table public.perfiles_medicos add constraint perfiles_medicos_hospital_id_fkey
            foreign key (hospital_id) references public.hospitales(id);
    end if;
    if not exists (select 1 from pg_constraint where conname='parejas_hospital_id_fkey'
                   and conrelid='public.parejas'::regclass) then
        alter table public.parejas add constraint parejas_hospital_id_fkey
            foreign key (hospital_id) references public.hospitales(id);
    end if;
    if not exists (select 1 from pg_constraint where conname='altruistas_hospital_id_fkey'
                   and conrelid='public.altruistas'::regclass) then
        alter table public.altruistas add constraint altruistas_hospital_id_fkey
            foreign key (hospital_id) references public.hospitales(id);
    end if;
end $$;

-- Un hospital solicitado o no aprobado no se puede usar para crear el perfil.
create or replace function public.crear_perfil_medico_desde_auth()
returns trigger language plpgsql security definer set search_path = ''
as $$
declare
    v_nombre text;
    v_cedula text;
    v_cedula_especialidad text;
    v_hospital_id integer;
begin
    v_nombre := nullif(trim(new.raw_user_meta_data ->> 'nombre_completo'), '');
    v_cedula := nullif(trim(new.raw_user_meta_data ->> 'cedula_profesional'), '');
    v_cedula_especialidad := nullif(trim(new.raw_user_meta_data ->> 'cedula_especialidad'), '');
    begin
        v_hospital_id := nullif(new.raw_user_meta_data ->> 'hospital_id', '')::integer;
    exception when invalid_text_representation then
        raise exception 'hospital_id debe ser un ID entero del catálogo aprobado';
    end;
    if v_nombre is null or v_cedula is null or v_cedula_especialidad is null or new.email is null then
        raise exception 'Faltan datos requeridos del perfil médico';
    end if;
    if not exists (select 1 from public.hospitales h where h.id=v_hospital_id and h.estado='aprobado') then
        raise exception 'El hospital seleccionado no está aprobado';
    end if;
    insert into public.perfiles_medicos
        (id,nombre_completo,correo,cedula_profesional,cedula_especialidad,hospital_id)
    values (new.id,v_nombre,lower(new.email),v_cedula,v_cedula_especialidad,v_hospital_id);
    return new;
end;
$$;
revoke all on function public.crear_perfil_medico_desde_auth() from public, anon, authenticated;

create or replace function public.medico_actual_hospital_id()
returns integer
language sql stable security definer set search_path = ''
as $$
    select p.hospital_id
    from public.perfiles_medicos p
    where p.id=(select auth.uid())
      and p.estado='aprobado'::public.estado_medico
      and p.rol in ('medico'::public.rol_medico,'admin'::public.rol_medico)
    limit 1;
$$;
revoke all on function public.medico_actual_hospital_id() from public;
grant execute on function public.medico_actual_hospital_id() to authenticated;

-- Eliminar políticas anteriores que pudieran permitir escritura global. Las
-- reglas nuevas se vuelven la única fuente de autorización para estas tablas.
do $$
declare p record;
begin
    for p in select schemaname, tablename, policyname from pg_policies
             where schemaname='public' and tablename in ('parejas','altruistas')
    loop
        execute format('drop policy %I on %I.%I',p.policyname,p.schemaname,p.tablename);
    end loop;
end $$;

alter table public.hospitales enable row level security;
alter table public.parejas enable row level security;
alter table public.altruistas enable row level security;

revoke all on public.hospitales from public, anon, authenticated;
grant select on public.hospitales to anon, authenticated;
grant insert (nombre,ciudad,latitud,longitud,hub_aereo) on public.hospitales to authenticated;
grant update on public.hospitales to authenticated;
revoke all on sequence public.hospitales_id_seq from public, anon, authenticated;
grant usage on sequence public.hospitales_id_seq to authenticated;

drop policy if exists "todos leen hospitales aprobados" on public.hospitales;
create policy "todos leen hospitales aprobados" on public.hospitales
    for select to anon, authenticated using (estado='aprobado');
drop policy if exists "admin lee todos los hospitales" on public.hospitales;
create policy "admin lee todos los hospitales" on public.hospitales
    for select to authenticated using ((select public.medico_actual_es_admin()));
drop policy if exists "usuario autenticado solicita hospital" on public.hospitales;
create policy "usuario autenticado solicita hospital" on public.hospitales
    for insert to authenticated with check (estado='pendiente');
drop policy if exists "admin actualiza hospitales" on public.hospitales;
create policy "admin actualiza hospitales" on public.hospitales
    for update to authenticated
    using ((select public.medico_actual_es_admin()))
    with check ((select public.medico_actual_es_admin()));

revoke all on public.parejas, public.altruistas from public, anon, authenticated;
grant select, insert, update on public.parejas, public.altruistas to authenticated;

create policy "medicos aprobados leen parejas nacionales" on public.parejas
    for select to authenticated
    using (public.medico_actual_hospital_id() is not null or (select public.medico_actual_es_admin()));
create policy "escritura de parejas del hospital" on public.parejas
    for insert to authenticated
    with check ((hospital_id=public.medico_actual_hospital_id()) or (select public.medico_actual_es_admin()));
create policy "actualizacion de parejas del hospital" on public.parejas
    for update to authenticated
    using ((hospital_id=public.medico_actual_hospital_id()) or (select public.medico_actual_es_admin()))
    with check ((hospital_id=public.medico_actual_hospital_id()) or (select public.medico_actual_es_admin()));

create policy "medicos aprobados leen altruistas nacionales" on public.altruistas
    for select to authenticated
    using (public.medico_actual_hospital_id() is not null or (select public.medico_actual_es_admin()));
create policy "escritura de altruistas del hospital" on public.altruistas
    for insert to authenticated
    with check ((hospital_id=public.medico_actual_hospital_id()) or (select public.medico_actual_es_admin()));
create policy "actualizacion de altruistas del hospital" on public.altruistas
    for update to authenticated
    using ((hospital_id=public.medico_actual_hospital_id()) or (select public.medico_actual_es_admin()))
    with check ((hospital_id=public.medico_actual_hospital_id()) or (select public.medico_actual_es_admin()));

-- El trigger existente ya está instalado; CREATE OR REPLACE conserva su enlace.
