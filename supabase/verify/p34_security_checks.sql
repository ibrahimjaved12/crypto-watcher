-- P34 / #275: run each check in the new project's Supabase SQL Editor.
-- Project: xshzjhocpcenuumdrhdh. These queries are read-only.
-- Verification is pending; migration text alone does not prove live privileges.

-- a. RLS: expect zero rows.
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND NOT c.relrowsecurity;

-- b. Write grants: expect zero rows for protected_results_table = true.
-- Return other write grants too: each must be justified in the PR or fixed.
SELECT grantee, table_name, privilege_type,
       table_name ~ '(forward|paper|signal|outcome|setup)' AS protected_results_table
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND grantee IN ('anon', 'authenticated')
  AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
ORDER BY table_name, grantee, privilege_type;

-- c. List every public SECURITY DEFINER function and every commit function.
-- Commit RPCs also use SECURITY INVOKER, so include them explicitly.
-- Every commit function must have anon_exec = false, auth_exec = false,
-- and service_role_exec = true. Return these flags along with the row count.
SELECT p.proname, p.oid::regprocedure AS function_signature,
       p.prosecdef AS security_definer,
       has_function_privilege('anon', p.oid, 'EXECUTE') AS anon_exec,
       has_function_privilege('authenticated', p.oid, 'EXECUTE') AS auth_exec,
       has_function_privilege('service_role', p.oid, 'EXECUTE') AS service_role_exec
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND (p.prosecdef OR p.proname LIKE 'commit%')
ORDER BY p.proname, function_signature;

-- d. Extensions: expect two rows, named pg_cron and pg_net.
SELECT extname
FROM pg_extension
WHERE extname IN ('pg_cron', 'pg_net')
ORDER BY extname;
