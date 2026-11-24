-- Ciclo de vida lógico para hospitales y registros clínicos. No elimina filas.
ALTER TYPE public.estado_hospital ADD VALUE IF NOT EXISTS 'inactivo';

-- Un médico cuyo centro quedó inactivo pierde acceso operativo al pool. Los
-- admins siguen autorizados por la política independiente medico_actual_es_admin.
CREATE OR REPLACE FUNCTION public.medico_actual_hospital_id()
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ''
AS $$
    SELECT p.hospital_id
    FROM public.perfiles_medicos p
    JOIN public.hospitales h ON h.id = p.hospital_id
    WHERE p.id = (SELECT auth.uid())
      AND p.estado = 'aprobado'::public.estado_medico
      AND p.rol IN ('medico'::public.rol_medico, 'admin'::public.rol_medico)
      AND h.estado = 'aprobado'::public.estado_hospital
    LIMIT 1;
$$;
REVOKE ALL ON FUNCTION public.medico_actual_hospital_id() FROM public;
GRANT EXECUTE ON FUNCTION public.medico_actual_hospital_id() TO authenticated;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typname = 'estado_registro'
    ) THEN
        CREATE TYPE public.estado_registro AS ENUM ('activo', 'trasplantado', 'baja');
    END IF;
END
$$;

ALTER TABLE public.parejas
    ADD COLUMN IF NOT EXISTS estado public.estado_registro NOT NULL DEFAULT 'activo',
    ADD COLUMN IF NOT EXISTS motivo_baja text,
    ADD COLUMN IF NOT EXISTS actualizado_en timestamptz NOT NULL DEFAULT now();

ALTER TABLE public.altruistas
    ADD COLUMN IF NOT EXISTS estado public.estado_registro NOT NULL DEFAULT 'activo',
    ADD COLUMN IF NOT EXISTS motivo_baja text,
    ADD COLUMN IF NOT EXISTS actualizado_en timestamptz NOT NULL DEFAULT now();

-- Asegura NOT NULL/default incluso si alguna columna existía previamente.
UPDATE public.parejas SET estado = 'activo' WHERE estado IS NULL;
UPDATE public.altruistas SET estado = 'activo' WHERE estado IS NULL;
UPDATE public.parejas SET actualizado_en = now() WHERE actualizado_en IS NULL;
UPDATE public.altruistas SET actualizado_en = now() WHERE actualizado_en IS NULL;
ALTER TABLE public.parejas ALTER COLUMN estado SET DEFAULT 'activo',
    ALTER COLUMN estado SET NOT NULL, ALTER COLUMN actualizado_en SET DEFAULT now(),
    ALTER COLUMN actualizado_en SET NOT NULL;
ALTER TABLE public.altruistas ALTER COLUMN estado SET DEFAULT 'activo',
    ALTER COLUMN estado SET NOT NULL, ALTER COLUMN actualizado_en SET DEFAULT now(),
    ALTER COLUMN actualizado_en SET NOT NULL;

-- Las políticas UPDATE limitan médicos a su hospital y permiten al admin operar
-- a nivel nacional. Se recrean explícitamente para conservar esa autorización.
DROP POLICY IF EXISTS "actualizacion de parejas del hospital" ON public.parejas;
CREATE POLICY "actualizacion de parejas del hospital" ON public.parejas
    FOR UPDATE TO authenticated
    USING ((hospital_id = public.medico_actual_hospital_id()) OR (SELECT public.medico_actual_es_admin()))
    WITH CHECK ((hospital_id = public.medico_actual_hospital_id()) OR (SELECT public.medico_actual_es_admin()));

DROP POLICY IF EXISTS "actualizacion de altruistas del hospital" ON public.altruistas;
CREATE POLICY "actualizacion de altruistas del hospital" ON public.altruistas
    FOR UPDATE TO authenticated
    USING ((hospital_id = public.medico_actual_hospital_id()) OR (SELECT public.medico_actual_es_admin()))
    WITH CHECK ((hospital_id = public.medico_actual_hospital_id()) OR (SELECT public.medico_actual_es_admin()));

-- Marca la última modificación tanto en bajas individuales como por lote.
CREATE OR REPLACE FUNCTION public.fijar_actualizado_en()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.actualizado_en := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS parejas_fijar_actualizado_en ON public.parejas;
CREATE TRIGGER parejas_fijar_actualizado_en
    BEFORE UPDATE ON public.parejas
    FOR EACH ROW EXECUTE FUNCTION public.fijar_actualizado_en();
DROP TRIGGER IF EXISTS altruistas_fijar_actualizado_en ON public.altruistas;
CREATE TRIGGER altruistas_fijar_actualizado_en
    BEFORE UPDATE ON public.altruistas
    FOR EACH ROW EXECUTE FUNCTION public.fijar_actualizado_en();

-- Confirmación nacional atómica: solo un admin aprobado puede marcar todos los
-- nodos de un ciclo/cadena. Si falta o ya no está activo algún ID, revierte todo.
CREATE OR REPLACE FUNCTION public.confirmar_trasplante_lote(
    p_pareja_ids text[], p_altruista_ids text[] DEFAULT ARRAY[]::text[]
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    n_parejas integer := 0;
    n_altruistas integer := 0;
BEGIN
    IF NOT public.medico_actual_es_admin() THEN
        RAISE EXCEPTION 'Solo un administrador aprobado puede confirmar trasplantes nacionales'
            USING ERRCODE = '42501';
    END IF;
    IF coalesce(cardinality(p_pareja_ids), 0) + coalesce(cardinality(p_altruista_ids), 0) = 0 THEN
        RAISE EXCEPTION 'La selección de trasplante está vacía';
    END IF;

    UPDATE public.parejas SET estado = 'trasplantado'
    WHERE id_pareja = ANY(coalesce(p_pareja_ids, ARRAY[]::text[])) AND estado = 'activo';
    GET DIAGNOSTICS n_parejas = ROW_COUNT;
    UPDATE public.altruistas SET estado = 'trasplantado'
    WHERE id_altruista = ANY(coalesce(p_altruista_ids, ARRAY[]::text[])) AND estado = 'activo';
    GET DIAGNOSTICS n_altruistas = ROW_COUNT;

    IF n_parejas <> coalesce(cardinality(p_pareja_ids), 0)
       OR n_altruistas <> coalesce(cardinality(p_altruista_ids), 0) THEN
        RAISE EXCEPTION 'Uno o más registros ya no están activos o no existen; no se confirmó ningún trasplante';
    END IF;
    RETURN jsonb_build_object('parejas', n_parejas, 'altruistas', n_altruistas,
                              'estado', 'trasplantado');
END;
$$;

REVOKE ALL ON FUNCTION public.confirmar_trasplante_lote(text[], text[]) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.confirmar_trasplante_lote(text[], text[]) TO authenticated;
