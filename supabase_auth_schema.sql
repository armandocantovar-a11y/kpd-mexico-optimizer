-- KPD México v3.1: perfiles, alta automática y políticas RLS.
-- Ejecutar en Supabase SQL Editor antes de desplegar app_web.py.

do $$ begin
    create type public.estado_medico as enum ('pendiente', 'aprobado', 'rechazado');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type public.rol_medico as enum ('medico', 'admin');
exception when duplicate_object then null;
end $$;

create table if not exists public.perfiles_medicos (
    id uuid primary key references auth.users(id) on delete cascade,
    nombre_completo text not null,
    correo text not null unique,
    cedula_profesional text not null unique,
    cedula_especialidad text not null,
    hospital_id integer not null check (hospital_id between 1 and 12),
    estado public.estado_medico not null default 'pendiente',
    rol public.rol_medico not null default 'medico',
    creado_en timestamptz not null default now()
);

comment on table public.perfiles_medicos is
    'Perfil y estado de validación manual de profesionales KPD; nunca almacenar contraseñas aquí.';
comment on column public.perfiles_medicos.hospital_id is
    'Índice entero 1-12 del catálogo nacional de hospitales usado por la aplicación.';

alter table public.perfiles_medicos enable row level security;
grant select, update on public.perfiles_medicos to authenticated;

-- SECURITY DEFINER evita recursión de RLS al consultar el rol del administrador.
create or replace function public.medico_actual_es_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.perfiles_medicos p
        where p.id = (select auth.uid())
          and p.rol = 'admin'::public.rol_medico
          and p.estado = 'aprobado'::public.estado_medico
    );
$$;
revoke all on function public.medico_actual_es_admin() from public;
grant execute on function public.medico_actual_es_admin() to authenticated;

drop policy if exists "medico lee su perfil" on public.perfiles_medicos;
create policy "medico lee su perfil"
on public.perfiles_medicos for select to authenticated
using (id = (select auth.uid()));

drop policy if exists "admin lee todos los perfiles" on public.perfiles_medicos;
create policy "admin lee todos los perfiles"
on public.perfiles_medicos for select to authenticated
using ((select public.medico_actual_es_admin()));

drop policy if exists "admin actualiza perfiles" on public.perfiles_medicos;
create policy "admin actualiza perfiles"
on public.perfiles_medicos for update to authenticated
using ((select public.medico_actual_es_admin()))
with check ((select public.medico_actual_es_admin()));

-- El trigger es el único camino de inserción para usuarios públicos.
create or replace function public.crear_perfil_medico_desde_auth()
returns trigger
language plpgsql
security definer
set search_path = ''
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
        raise exception 'hospital_id debe ser un entero entre 1 y 12';
    end;

    if v_nombre is null or v_cedula is null or v_cedula_especialidad is null then
        raise exception 'Faltan datos requeridos del perfil médico';
    end if;
    if v_hospital_id is null or v_hospital_id not between 1 and 12 then
        raise exception 'hospital_id debe ser un entero entre 1 y 12';
    end if;
    if new.email is null then
        raise exception 'El registro requiere un correo electrónico';
    end if;

    insert into public.perfiles_medicos (
        id, nombre_completo, correo, cedula_profesional,
        cedula_especialidad, hospital_id
    ) values (
        new.id, v_nombre, lower(new.email), v_cedula,
        v_cedula_especialidad, v_hospital_id
    );
    return new;
end;
$$;
revoke all on function public.crear_perfil_medico_desde_auth() from public, anon, authenticated;

drop trigger if exists al_crear_usuario_crear_perfil_medico on auth.users;
create trigger al_crear_usuario_crear_perfil_medico
after insert on auth.users
for each row execute function public.crear_perfil_medico_desde_auth();

-- Bootstrap único del primer administrador:
-- 1. Registra su cuenta normalmente desde la app y confirma su correo.
-- 2. Ejecuta manualmente en SQL Editor, sustituyendo el correo:
-- update public.perfiles_medicos
--    set rol = 'admin', estado = 'aprobado'
--  where correo = lower('admin@hospital.mx');
-- Después, la cuenta puede aprobar/rechazar solicitudes desde la aplicación.
