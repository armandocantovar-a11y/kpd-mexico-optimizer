-- Migración idempotente para persistir espera previa y fecha de alta.
ALTER TABLE public.parejas
    ADD COLUMN IF NOT EXISTS meses_espera_previos integer NOT NULL DEFAULT 0;

ALTER TABLE public.parejas
    ADD COLUMN IF NOT EXISTS creado_en timestamptz NOT NULL DEFAULT now();

-- Normaliza instalaciones donde las columnas ya existían pero permitían NULL.
UPDATE public.parejas
SET meses_espera_previos = 0
WHERE meses_espera_previos IS NULL;

UPDATE public.parejas
SET creado_en = now()
WHERE creado_en IS NULL;

ALTER TABLE public.parejas
    ALTER COLUMN meses_espera_previos SET DEFAULT 0,
    ALTER COLUMN meses_espera_previos SET NOT NULL,
    ALTER COLUMN creado_en SET DEFAULT now(),
    ALTER COLUMN creado_en SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.parejas'::regclass
          AND conname = 'parejas_meses_espera_previos_check'
    ) THEN
        ALTER TABLE public.parejas
            ADD CONSTRAINT parejas_meses_espera_previos_check
            CHECK (meses_espera_previos >= 0);
    END IF;
END
$$;
