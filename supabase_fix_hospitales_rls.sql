-- Corrige RLS para el formulario autenticado "Solicitar alta de nuevo hospital".
-- db_supabase.solicitar_alta_hospital envía nombre, ciudad, latitud, longitud y
-- hub_aereo; omite estado para que PostgreSQL lo establezca como 'pendiente'.
-- La llamada se hace con la sesión Supabase autenticada del usuario actual.

ALTER TABLE public.hospitales ENABLE ROW LEVEL SECURITY;

-- El acceso por columnas limita también los datos que el cliente puede proponer.
REVOKE INSERT ON public.hospitales FROM public, anon, authenticated;
GRANT INSERT (nombre, ciudad, latitud, longitud, hub_aereo, estado)
    ON public.hospitales TO authenticated, anon;

-- Quita políticas INSERT/ALL previas que, por ser permisivas, podrían OR
-- combinarse con esta y aceptar estados distintos de pendiente. SELECT y UPDATE
-- quedan intactas; en particular se conserva la lectura y gestión administrativa.
DO $$
DECLARE
    politica record;
BEGIN
    FOR politica IN
        SELECT polname
        FROM pg_policy
        WHERE polrelid = 'public.hospitales'::regclass
          AND polcmd IN ('a', '*') -- INSERT o ALL
    LOOP
        EXECUTE format('DROP POLICY %I ON public.hospitales', politica.polname);
    END LOOP;
END
$$;

CREATE POLICY "usuario autenticado solicita hospital"
    ON public.hospitales
    FOR INSERT
    TO authenticated, anon
    WITH CHECK (estado = 'pendiente'::public.estado_hospital);
